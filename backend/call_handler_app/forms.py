"""
forms.py — sends the "please fill out this form" email to a lead via
Resend, and logs the send attempt.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import resend
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import RESEND_API_KEY, RESEND_FROM_EMAIL, _get_supabase, logger

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Minimum gap before the same (lead_email, form_url) can be emailed again.
# Prevents double-click / rapid-retry spam; does not block deliberate
# resends made after this window (e.g. the "Resend" button a day later).
RESEND_COOLDOWN_MINUTES = 10


def _build_form_email_html(name: str, form_url: str) -> str:
    safe_name = name or "there"
    return f"""
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px">
      <h2 style="color:#111">Hi {safe_name},</h2>
      <p style="color:#555;font-size:15px;line-height:1.6">Please fill out this quick form so we can understand your requirements better.</p>
      <a href="{form_url}" style="display:inline-block;margin-top:8px;padding:12px 28px;background:#6366f1;color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Open Form →</a>
      <p style="color:#aaa;font-size:12px;margin-top:24px">Or copy: <a href="{form_url}">{form_url}</a></p>
    </div>
    """


def _send_form_email_sync(to_email: str, name: str, form_url: str) -> Dict[str, Any]:
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY not set on server")
    return resend.Emails.send({"from": RESEND_FROM_EMAIL, "to": to_email, "subject": "Quick Form – Help Us Understand Your Requirements", "html": _build_form_email_html(name, form_url)})


def _log_form_send(name: str, to_email: str, form_url: str, status: str, provider_id: Optional[str] = None, error: Optional[str] = None, lead_id: Optional[str] = None) -> None:
    _get_supabase().table("form_send_log").insert({
        "lead_name": name, "lead_email": to_email, "form_url": form_url,
        "sent_by": "system", "status": status, "provider_id": provider_id,
        "error": error, "lead_id": lead_id,
    }).execute()


async def _safe_log_send(*args, **kwargs) -> None:
    # BUG FIX: this used to be an unguarded asyncio.to_thread(_log_form_send, ...)
    # call sitting directly in the route's try/except. If the log write
    # itself raised (Supabase hiccup) it would either (a) escape the
    # `except` block and mask the real send error with an unrelated
    # unhandled 500, or (b) on the SUCCESS path, fail the whole request
    # even though the email had already gone out — which would make a
    # user retry and double-email the lead. Logging is best-effort and
    # must never affect the response.
    try:
        await asyncio.to_thread(_log_form_send, *args, **kwargs)
    except Exception as log_err:
        logger.error("form_send_log write failed (non-fatal): %s", log_err)


def _recent_duplicate_send(to_email: str, form_url: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=RESEND_COOLDOWN_MINUTES)).isoformat()
    try:
        res = (
            _get_supabase().table("form_send_log")
            .select("id")
            .eq("lead_email", to_email)
            .eq("form_url", form_url)
            .eq("status", "sent")
            .gte("sent_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        # If the dedupe check itself fails, don't block sending on it —
        # fail open, just log it.
        logger.error("dedupe check failed (non-fatal): %s", e)
        return False


@router.post("/api/send-form-email")
async def send_form_email_route(request: Request):
    body     = await request.json()
    to_email = (body.get("lead_email") or "").strip().lower()
    name     = (body.get("lead_name") or "").strip()
    form_url = (body.get("form_url") or "").strip()
    lead_id  = body.get("lead_id")  # optional: form_submissions.id, when send was triggered from a known row
    force    = bool(body.get("force"))  # explicit "Resend" click bypasses the cooldown

    if not to_email or not form_url:
        raise HTTPException(status_code=400, detail="lead_email and form_url are required")
    # BUG FIX: server previously trusted the frontend's regex entirely —
    # the API is directly callable, so a bad address used to burn a
    # Resend API call and only fail (expensively) downstream.
    if not _EMAIL_RE.match(to_email):
        raise HTTPException(status_code=400, detail="lead_email is not a valid email address")

    # BUG FIX: nothing previously stopped the same lead being emailed
    # repeatedly (double-click before the button's `busy` state paints,
    # or someone mashing the row Send icon). A deliberate "Resend"
    # click still goes through via force=True.
    if not force and await asyncio.to_thread(_recent_duplicate_send, to_email, form_url):
        raise HTTPException(
            status_code=429,
            detail=f"Already sent to {to_email} in the last {RESEND_COOLDOWN_MINUTES} minutes. Use Resend to override.",
        )

    try:
        result      = await asyncio.to_thread(_send_form_email_sync, to_email, name, form_url)
        provider_id = result.get("id") if isinstance(result, dict) else None
        await _safe_log_send(name, to_email, form_url, "sent", provider_id, None, lead_id)
        return JSONResponse({"status": "ok", "provider_id": provider_id})
    except Exception as e:
        await _safe_log_send(name, to_email, form_url, "failed", None, str(e), lead_id)
        raise HTTPException(status_code=502, detail=f"Email send failed: {e}")