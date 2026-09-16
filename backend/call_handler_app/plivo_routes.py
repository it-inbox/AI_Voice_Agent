"""
plivo_routes.py — Plivo webhook signature verification, hangup/stream
webhooks, number listing/linking/unlinking, call-status polling, and
phone-number <-> agent routing lookups.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import plivo
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .campaigns import NON_TERMINAL
from .config import (
    PLIVO_ANSWER_URL,
    PLIVO_APP_NAME,
    PLIVO_AUTH_TOKEN,
    _PHONE_MAP_CACHE,
    _get_supabase,
    _with_retry,
    business_status,
    logger,
    plivo_client,
    resolve_agent_id_for_number,
)

router = APIRouter()


async def _verify_plivo_signature(request: Request) -> Dict[str, str]:
    form   = await request.form()
    params = dict(form)
    signature = request.headers.get("X-Plivo-Signature-V3", "")
    nonce     = request.headers.get("X-Plivo-Signature-V3-Nonce", "")
    if not signature or not nonce:
        raise HTTPException(status_code=403, detail="Missing Plivo signature headers")
    # FIX (silent signature failures behind a tunnel/proxy): request.url
    # reflects the raw connection uvicorn sees internally — scheme is
    # always "http" and the host may be the internal one, never what
    # Plivo actually called (e.g. "https://xyz.trycloudflare.com"). That
    # mismatch fails validate_v3_signature every time, with no log line
    # anywhere on this path (the `if not valid` branch below had none at
    # all) — completely silent 403s. Rebuild the URL from the standard
    # forwarded headers, same pattern already used in server_app's
    # plivo_answer() for the WS host, so the URL used for verification
    # matches what Plivo actually signed.
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host   = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", request.url.netloc)
    url    = f"{scheme}://{host}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    try:
        valid = plivo.utils.validate_v3_signature("POST", url, nonce, PLIVO_AUTH_TOKEN, signature, params)
    except Exception as exc:
        logger.warning("Plivo signature verification error: %s", exc)
        raise HTTPException(status_code=403, detail="Signature verification error")
    if not valid:
        # NEW — this branch previously had NO log line at all, meaning a
        # rejected webhook here produced zero terminal output, making it
        # indistinguishable from the request never arriving in the first
        # place.
        logger.warning("Plivo signature INVALID — url=%s (check CALL_HANDLER_URL / tunnel scheme+host match)", url)
        raise HTTPException(status_code=403, detail="Invalid Plivo signature")
    return params


# ═══════════════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════════════

@router.post("/plivo/hangup")
async def plivo_hangup(request: Request):
    """Plivo's real hangup webhook — fires once, the instant the CDR is
    final. Updates BOTH the plain `calls` table (existing dashboard) and,
    if one exists, the campaign `call_attempts` row for this call_uuid
    (see campaigns.py) — so batch-campaign rows get the same
    event-driven update as everything else."""
    try:
        params = await _verify_plivo_signature(request)
    except HTTPException:
        raise

    call_uuid    = (params.get("CallUUID") or "").strip()
    hangup_cause = params.get("HangupCauseName") or params.get("HangupCause") or ""
    to_number    = (params.get("To") or "").strip()

    if not call_uuid:
        logger.warning("Hangup webhook fired with no CallUUID — params=%s", params)
        return JSONResponse({"status": "ignored", "detail": "no CallUUID in payload"})

    def _update():
        return (
            _get_supabase().table("calls").update({"hangup_cause": hangup_cause})
            .eq("call_sid", call_uuid).execute()
        )
    try:
        result = await asyncio.to_thread(_with_retry, _update)
        if not result.data:
            logger.warning("Hangup webhook: no calls row matched call_sid=%s", call_uuid)
        else:
            # NEW — explicit success line. --no-access-log on uvicorn hides
            # the normal per-request log, and this handler previously had
            # no log statement at all on the success path, making it
            # impossible to tell "webhook never arrived" apart from
            # "webhook arrived and worked fine" just by watching the
            # terminal.
            logger.info("Hangup webhook received — call_uuid=%s hangup_cause=%s", call_uuid, hangup_cause)
    except Exception as e:
        logger.error("Hangup webhook Supabase update failed — call_uuid=%s: %s", call_uuid, e)

    # NEW — campaign attempt update, same event, no separate webhook needed.
    # FIX (root cause of "stuck DIALING forever" for rejected/busy/no-
    # answer calls): call_attempts.call_uuid only ever gets written once
    # the ANSWER webhook fires and the dashboard resolves it — but Plivo
    # never fires answer_url for a call that was never actually answered.
    # This hangup webhook DOES still fire for those (with the real cause,
    # e.g. "Busy Line"), but matching strictly on call_uuid found zero
    # rows, so the update silently no-opped and the row sat on DIALING
    # forever. Fallback: if no row matches call_uuid, match the most
    # recent still-in-flight row for this phone number instead, and
    # backfill call_uuid onto it at the same time. Safe because
    # duplicate numbers are already excluded pre-flight and only
    # CONCURRENCY numbers are ever dialing at once — at most one
    # non-terminal row per number can exist at a time.
    def _update_attempt():
        result = (
            _get_supabase().table("call_attempts")
            .update({
                "hangup_cause":    hangup_cause,
                "business_status": business_status(hangup_cause),
                "ended_at":        datetime.utcnow().isoformat(),
            })
            .eq("call_uuid", call_uuid).execute()
        )
        if result.data or not to_number:
            return result
        # Fallback match: PostgREST doesn't support order+limit on an
        # UPDATE, so find the target row's id with a SELECT first, then
        # update that exact row — avoids any risk of touching more than
        # the one intended row if this ever matches more than one.
        candidate = (
            _get_supabase().table("call_attempts")
            .select("id")
            .eq("to_number", to_number)
            .is_("call_uuid", "null")
            .in_("business_status", list(NON_TERMINAL))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not candidate.data:
            return result
        return (
            _get_supabase().table("call_attempts")
            .update({
                "call_uuid":       call_uuid,
                "hangup_cause":    hangup_cause,
                "business_status": business_status(hangup_cause),
                "ended_at":        datetime.utcnow().isoformat(),
            })
            .eq("id", candidate.data[0]["id"]).execute()
        )
    try:
        result2 = await asyncio.to_thread(_with_retry, _update_attempt)
        if result2.data:
            lead_id = result2.data[0]["lead_id"]
            status  = result2.data[0]["business_status"]
            logger.info("Hangup webhook: call_attempts updated — call_uuid=%s status=%s", call_uuid, status)
            await asyncio.to_thread(
                _with_retry,
                lambda: _get_supabase().table("campaign_leads").update({"status": status}).eq("lead_id", lead_id).execute()
            )
        else:
            logger.debug("Hangup webhook: no campaign attempt row for call_uuid=%s", call_uuid)
    except Exception as e:
        logger.debug("Hangup webhook: no campaign attempt row for call_uuid=%s (%s)", call_uuid, e)

    return JSONResponse({"status": "ok"})


@router.post("/plivo/stream-status")
async def plivo_stream_status(request: Request):
    try:
        params = await _verify_plivo_signature(request)
        logger.debug("Stream status: %s", params.get("StreamEvent", params))
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Stream status parse error: %s", e)
    return JSONResponse({"status": "ok"})


# ═══════════════════════════════════════════════════════════════
# NUMBER LINKING / ROUTING
# ═══════════════════════════════════════════════════════════════

_plivo_app_id_cache: Optional[str] = None


async def _ensure_shared_application() -> str:
    global _plivo_app_id_cache
    if _plivo_app_id_cache:
        return _plivo_app_id_cache
    if not PLIVO_ANSWER_URL:
        raise HTTPException(status_code=500, detail="PLIVO_ANSWER_URL not configured")
    def _find_or_create() -> str:
        existing = plivo_client.applications.list()
        for app_obj in existing:
            if getattr(app_obj, "app_name", None) == PLIVO_APP_NAME:
                if getattr(app_obj, "answer_url", None) != PLIVO_ANSWER_URL:
                    plivo_client.applications.update(app_id=app_obj.app_id, answer_url=PLIVO_ANSWER_URL, answer_method="POST")
                return app_obj.app_id
        created = plivo_client.applications.create(app_name=PLIVO_APP_NAME, answer_url=PLIVO_ANSWER_URL, answer_method="POST")
        return created["app_id"]
    app_id = await asyncio.to_thread(_find_or_create)
    _plivo_app_id_cache = app_id
    return app_id


@router.get("/api/agent-for-number")
async def agent_for_number(to: str):
    await resolve_agent_id_for_number(to)
    agent_id = (
        _PHONE_MAP_CACHE.get(to) or _PHONE_MAP_CACHE.get(f"+{to}") or _PHONE_MAP_CACHE.get(to.lstrip("+"))
    )
    return JSONResponse({"to": to, "agent_id": agent_id})


@router.get("/api/number-for-agent")
async def number_for_agent(agent_id: str):
    def _lookup():
        rows = (
            _get_supabase().table("agent_numbers").select("number").eq("agent_id", agent_id).limit(1).execute().data or []
        )
        return rows[0]["number"] if rows else None
    number = await asyncio.to_thread(_with_retry, _lookup)
    return JSONResponse({"agent_id": agent_id, "number": number})


@router.get("/api/plivo/numbers")
async def list_plivo_numbers():
    def _list_all() -> List[Dict[str, Any]]:
        out, offset = [], 0
        while True:
            page = plivo_client.numbers.list(limit=20, offset=offset)
            if not page:
                break
            out.extend(page)
            if len(page) < 20:
                break
            offset += 20
        return out
    try:
        numbers = await asyncio.to_thread(_with_retry, _list_all)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Plivo numbers.list failed: {e}")
    def _load_ownership():
        agent_numbers_rows = _get_supabase().table("agent_numbers").select("number, agent_id").execute().data or []
        agents_rows = _get_supabase().table("agents").select("agent_id, name").execute().data or []
        return agent_numbers_rows, agents_rows
    agent_numbers_rows, agents_rows = await asyncio.to_thread(_with_retry, _load_ownership)
    agent_name_by_id = {a["agent_id"]: a["name"] for a in agents_rows}
    owner_agent_id_by_number = {r["number"]: r["agent_id"] for r in agent_numbers_rows}
    result = []
    for n in numbers:
        number = getattr(n, "number", None) or n.get("number") if isinstance(n, dict) else getattr(n, "number", "")
        owner_agent_id = owner_agent_id_by_number.get(number) or owner_agent_id_by_number.get(f"+{number}")
        result.append({
            "number": number,
            "region": getattr(n, "region", None) or (n.get("region") if isinstance(n, dict) else None),
            "voice_enabled": getattr(n, "voice_enabled", None) if not isinstance(n, dict) else n.get("voice_enabled"),
            "monthly_rental_rate": getattr(n, "monthly_rental_rate", None) if not isinstance(n, dict) else n.get("monthly_rental_rate"),
            "assigned_agent_id": owner_agent_id,
            "assigned_agent_name": agent_name_by_id.get(owner_agent_id) if owner_agent_id else None,
        })
    return JSONResponse({"numbers": result})


@router.post("/api/plivo/link-number")
async def link_plivo_number(request: Request):
    body     = await request.json()
    agent_id = (body.get("agent_id") or "").strip()
    number   = (body.get("number") or "").strip()
    region   = (body.get("region") or "").strip() or None
    if not agent_id or not number:
        raise HTTPException(status_code=400, detail="agent_id and number are required")
    app_id = await _ensure_shared_application()
    def _bind():
        plivo_client.numbers.update(number=number, app_id=app_id)
    try:
        await asyncio.to_thread(_bind)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Plivo number.update failed: {e}")
    def _save():
        _get_supabase().table("agent_numbers").upsert({"number": number, "agent_id": agent_id, "region": region, "assigned_at": datetime.utcnow().isoformat()}, on_conflict="number").execute()
    await asyncio.to_thread(_save)
    _PHONE_MAP_CACHE.clear()
    return JSONResponse({"status": "ok", "agent_id": agent_id, "number": number})


@router.post("/api/plivo/unlink-number")
async def unlink_plivo_number(request: Request):
    body   = await request.json()
    number = (body.get("number") or "").strip()
    if not number:
        raise HTTPException(status_code=400, detail="number is required")
    def _delete():
        _get_supabase().table("agent_numbers").delete().eq("number", number).execute()
    await asyncio.to_thread(_delete)
    _PHONE_MAP_CACHE.clear()
    return JSONResponse({"status": "ok", "number": number})


@router.get("/api/plivo/call-status")
async def plivo_call_status(call_uuid: str):
    call_uuid = (call_uuid or "").strip()
    if not call_uuid:
        raise HTTPException(status_code=400, detail="call_uuid is required")
    def _get():
        return plivo_client.calls.get(call_uuid)
    try:
        call = await asyncio.wait_for(asyncio.to_thread(_with_retry, _get), timeout=12)
    except asyncio.TimeoutError:
        return JSONResponse({"status": "timeout", "call_uuid": call_uuid})
    except Exception as e:
        return JSONResponse({"status": "not_found", "call_uuid": call_uuid, "detail": str(e)})
    call_status  = getattr(call, "call_status", None) or (call.get("call_status") if isinstance(call, dict) else None)
    end_time     = getattr(call, "end_time", None) or (call.get("end_time") if isinstance(call, dict) else None)
    hangup_cause = (
        getattr(call, "hangup_cause_name", None) or getattr(call, "hangup_cause", None)
        or (call.get("hangup_cause_name") if isinstance(call, dict) else None)
        or (call.get("hangup_cause") if isinstance(call, dict) else None)
    )
    return JSONResponse({"status": "ok", "call_uuid": call_uuid, "call_status": call_status, "end_time": end_time, "hangup_cause": hangup_cause})