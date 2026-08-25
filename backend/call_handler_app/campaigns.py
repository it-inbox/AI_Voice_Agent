"""
campaigns.py — durable batch-calling state (DB is source of truth).
Fixes: browser-only batch state, duplicate-call risk on resume,
no attempt history, no campaign id, weak reconciliation.
Run schema_campaigns.sql once before using these.

CLAIM SAFETY (2024 hardening pass) — dial_lead() used to be a
read-then-write: check for a non-terminal attempt, and if none, upsert
a new one. Two concurrent callers (two tabs/workers) could both pass
the read before either had written, and the endpoint would tell BOTH
of them reused=False — the caller only invokes Plivo when it sees
reused=False, so both would dial. The upsert's own on_conflict
protected the DB *row* (both writes collapsed onto one attempt row via
the idempotency_key unique constraint) but never protected the
*caller* — nothing told the second caller it had lost the race.

Fixed by making the actual claim atomic at the Postgres level:
upsert(..., ignore_duplicates=True) performs INSERT ... ON CONFLICT DO
NOTHING. Only the request whose INSERT actually lands gets a row back
in `data`; a request that lost the race gets `data == []` and is told
reused=True with the winner's row. The browser is never trusted to
enforce this — trying twice from two tabs converges on one claim
because the unique constraint on idempotency_key is enforced by
Postgres itself, not by application code.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import _get_supabase, _with_retry, business_status, logger, plivo_client

router = APIRouter()

NON_TERMINAL = {"QUEUED", "DIALING", "RINGING", "IN_PROGRESS"}

# How long a QUEUED attempt with no call_uuid is allowed to sit before
# it's considered abandoned (browser crashed / network died between
# reserving the attempt and actually reaching Plivo). Inspected against
# the target workload (125 calls/day, ~1.5min avg call, wave concurrency
# 1-3) — the gap between "attempt reserved" and "call_uuid attached" is
# normally low single-digit seconds, so 75s gives generous margin for
# a slow Plivo API round trip without leaving a lead un-callable for long.
STALE_QUEUED_TIMEOUT_S = 75

# How long a DIALING/RINGING/IN_PROGRESS attempt that DOES have a
# call_uuid is allowed to go without a terminal hangup_cause before the
# reconciler asks Plivo directly. Kept distinct from the QUEUED timeout
# since a real in-progress call can legitimately run long.
STUCK_WITH_UUID_TIMEOUT_S = 90


def _parse_ts(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempt_age_s(attempt: Dict[str, Any]) -> Optional[float]:
    ts = attempt.get("started_at") or attempt.get("created_at")
    parsed = _parse_ts(ts)
    if parsed is None:
        return None
    return datetime.now(timezone.utc).timestamp() - parsed


def _try_recover_stale(attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Atomic compare-and-swap recovery for a stale non-terminal attempt.

    Race-safety: the UPDATE carries a WHERE business_status=<the status
    we last saw> guard. Postgres serializes concurrent UPDATEs against
    the same row — only the first one to land actually matches that
    WHERE clause and flips the row; every later one (from another
    worker racing to recover the same stale attempt) matches zero rows
    and gets [] back. So at most one caller ever "wins" the recovery,
    which is what prevents two workers from both recovering and then
    both redialing the same lead.
    """
    status = attempt["business_status"]
    if attempt.get("call_uuid"):
        return None  # has a call_uuid — Plivo was reached; that's the with-uuid reconcile path, not this one
    age = _attempt_age_s(attempt)
    if age is None or age < STALE_QUEUED_TIMEOUT_S:
        return None

    def _cas():
        return (
            _get_supabase().table("call_attempts")
            .update({
                "business_status": "ABANDONED",
                "failure_reason": f"stale {status} attempt recovered after {STALE_QUEUED_TIMEOUT_S}s with no call_uuid",
                "ended_at": _now_iso(),
            })
            .eq("attempt_id", attempt["attempt_id"])
            .eq("business_status", status)   # <- the CAS guard
            .is_("call_uuid", "null")
            .execute().data
        )
    recovered = _with_retry(_cas)
    if not recovered:
        return None  # someone else already recovered/updated this row

    def _lead_update():
        return _get_supabase().table("campaign_leads").update({"status": "ABANDONED"}).eq("lead_id", attempt["lead_id"]).execute()
    try:
        _with_retry(_lead_update)
    except Exception as e:
        logger.warning("stale-recovery: campaign_leads status sync failed for lead_id=%s: %s", attempt["lead_id"], e)
    logger.info("stale-recovery: attempt_id=%s lead_id=%s recovered from %s (age=%.0fs)", attempt["attempt_id"], attempt["lead_id"], status, age)
    return recovered[0]


@router.post("/api/campaigns")
async def create_campaign(request: Request):
    body = await request.json()
    agent_id = (body.get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")

    def _create():
        return _get_supabase().table("campaigns").insert({
            "agent_id": agent_id, "file_name": body.get("file_name", ""),
        }).execute().data[0]
    campaign = await asyncio.to_thread(_with_retry, _create)

    leads = body.get("leads", [])  # [{row_index, phone, name, raw_row}]
    lead_rows = []
    if leads:
        def _insert_leads():
            payload = [{
                "campaign_id": campaign["campaign_id"], "row_index": l["row_index"],
                "phone": l["phone"], "name": l.get("name"), "raw_row": l.get("raw_row", {}),
            } for l in leads]
            return _get_supabase().table("campaign_leads").insert(payload).execute().data
        lead_rows = await asyncio.to_thread(_with_retry, _insert_leads)
        await asyncio.to_thread(
            _with_retry,
            lambda: _get_supabase().table("campaigns").update({"total_leads": len(lead_rows)}).eq("campaign_id", campaign["campaign_id"]).execute()
        )

    return JSONResponse({"campaign_id": campaign["campaign_id"], "leads": lead_rows})


@router.get("/api/campaigns")
async def list_campaigns(agent_id: Optional[str] = None, limit: int = 20):
    """Recent campaigns, newest first — powers the dashboard's 'Resume a
    past campaign' picker (resumeCampaign() in batchCallStore.js). Without
    this, a campaign_id was only known by whoever created it in that
    browser session — nothing could rediscover it after a full reload."""
    def _list():
        q = _get_supabase().table("campaigns").select("*").order("created_at", desc=True).limit(limit)
        if agent_id:
            q = q.eq("agent_id", agent_id)
        return q.execute().data or []
    rows = await asyncio.to_thread(_with_retry, _list)
    return JSONResponse({"campaigns": rows})


@router.delete("/api/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str):
    """Frees space once a batch is done and you only care about HOT leads
    (those already live independently in `calls`, written by
    process_transcript_pipeline — deleting a campaign never touches that).
    campaign_leads and call_attempts cascade-delete via their FKs in
    final_schema.sql — one delete here is enough, no manual cleanup of
    the other two tables needed."""
    def _delete():
        return _get_supabase().table("campaigns").delete().eq("campaign_id", campaign_id).execute()
    result = await asyncio.to_thread(_with_retry, _delete)
    if not result.data:
        raise HTTPException(status_code=404, detail="campaign not found")
    return JSONResponse({"status": "ok", "deleted": campaign_id})


@router.get("/api/campaigns/{campaign_id}/leads")
async def get_campaign_leads(campaign_id: str):
    """Single source of truth for the UI — status per lead comes from
    the DB, not browser memory. Includes each lead's latest attempt."""
    def _load():
        leads = _get_supabase().table("campaign_leads").select("*").eq("campaign_id", campaign_id).order("row_index").execute().data or []
        attempts = _get_supabase().table("call_attempts").select("*").eq("campaign_id", campaign_id).order("attempt_number", desc=True).execute().data or []
        return leads, attempts
    leads, attempts = await asyncio.to_thread(_with_retry, _load)

    latest_by_lead: Dict[str, Dict[str, Any]] = {}
    for a in attempts:
        latest_by_lead.setdefault(a["lead_id"], a)  # desc order -> first hit is latest

    for l in leads:
        a = latest_by_lead.get(l["lead_id"])
        l["hangup_cause"]    = a["hangup_cause"] if a else None
        l["business_status"] = a["business_status"] if a else "PENDING"
        l["call_uuid"]       = a["call_uuid"] if a else None
        l["attempt_number"]  = a["attempt_number"] if a else 0

    return JSONResponse({"campaign_id": campaign_id, "leads": leads})


@router.post("/api/campaigns/{campaign_id}/dial/{lead_id}")
async def dial_lead(campaign_id: str, lead_id: str):
    """ATOMIC claim — the fix for duplicate-call risk (see module
    docstring). Only the caller that wins the DB-level claim is told
    reused=False; that's the ONLY caller allowed to proceed to
    server.py's /api/outbound-call and actually invoke Plivo. Every
    other caller — a second tab, a retried request, a resume after
    refresh — gets reused=True and the winner's row, and must NOT dial."""
    def _get_lead():
        return _get_supabase().table("campaign_leads").select("*").eq("lead_id", lead_id).single().execute().data
    lead = await asyncio.to_thread(_with_retry, _get_lead)
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")

    def _existing_attempts():
        return _get_supabase().table("call_attempts").select("*").eq("lead_id", lead_id).order("attempt_number", desc=True).execute().data or []
    attempts = await asyncio.to_thread(_with_retry, _existing_attempts)

    latest = attempts[0] if attempts else None
    if latest and latest["business_status"] in NON_TERMINAL:
        recovered = await asyncio.to_thread(_try_recover_stale, latest)
        if recovered:
            # This attempt is now terminal (ABANDONED) — the lead is
            # callable again. Fall through to claim a fresh attempt.
            latest = recovered
        else:
            # Still genuinely active (or another worker just recovered/
            # claimed it a moment ago) — re-read to get the current
            # truth instead of trusting our possibly-stale local copy.
            def _refetch():
                return _get_supabase().table("call_attempts").select("*").eq("attempt_id", latest["attempt_id"]).single().execute().data
            latest = await asyncio.to_thread(_with_retry, _refetch) or latest
            if latest["business_status"] in NON_TERMINAL:
                return JSONResponse({"attempt_id": latest["attempt_id"], "reused": True, **latest})

    next_n   = (attempts[0]["attempt_number"] + 1) if attempts else 1
    idem_key = f"{campaign_id}:{lead_id}:{next_n}"

    def _claim():
        # ignore_duplicates=True -> INSERT ... ON CONFLICT DO NOTHING.
        # Postgres's unique constraint on idempotency_key is the actual
        # arbiter: exactly one concurrent caller's row lands in `data`.
        return _get_supabase().table("call_attempts").upsert({
            "lead_id": lead_id, "campaign_id": campaign_id, "attempt_number": next_n,
            "idempotency_key": idem_key, "business_status": "QUEUED",
        }, on_conflict="idempotency_key", ignore_duplicates=True).execute().data
    claimed = await asyncio.to_thread(_with_retry, _claim)

    if not claimed:
        # Lost the race — someone else's claim for this idem_key landed
        # first. Fetch THEIR row and report reused=True; do not dial.
        def _fetch_winner():
            return _get_supabase().table("call_attempts").select("*").eq("idempotency_key", idem_key).single().execute().data
        winner = await asyncio.to_thread(_with_retry, _fetch_winner)
        if not winner:
            # Extremely unlikely (row deleted between conflict and
            # fetch) — surface as a clean error rather than silently
            # letting the caller think it's safe to dial.
            raise HTTPException(status_code=409, detail="dial claim conflict — retry")
        return JSONResponse({"attempt_id": winner["attempt_id"], "reused": True, **winner})

    attempt = claimed[0]
    await asyncio.to_thread(
        _with_retry,
        lambda: _get_supabase().table("campaign_leads").update({"status": "QUEUED"}).eq("lead_id", lead_id).execute()
    )
    return JSONResponse({"attempt_id": attempt["attempt_id"], "reused": False, **attempt})


@router.patch("/api/campaigns/attempts/{attempt_id}")
async def update_attempt(attempt_id: str, request: Request):
    """Called right after Plivo returns call_uuid (business_status ->
    DIALING), and by the reconciliation pass for stuck rows."""
    body  = await request.json()
    patch: Dict[str, Any] = {}
    for k in ("call_uuid", "provider_status", "hangup_cause", "business_status", "failure_reason"):
        if k in body:
            patch[k] = body[k]
    if "hangup_cause" in patch and "business_status" not in patch:
        patch["business_status"] = business_status(patch["hangup_cause"])
    new_status = patch.get("business_status")
    if new_status not in (None, *NON_TERMINAL):
        patch["ended_at"] = datetime.utcnow().isoformat()

    def _update():
        q = _get_supabase().table("call_attempts").update(patch).eq("attempt_id", attempt_id)
        if new_status in NON_TERMINAL:
            # STATE SAFETY — a terminal call must never move back to an
            # active state. This patch is only ever meant to move a row
            # forward (QUEUED -> DIALING, etc). Guarding the WHERE
            # clause with "current status is still non-terminal" means
            # a late/racing DIALING-patch can never overwrite a
            # business_status a hangup webhook already finalized.
            q = q.in_("business_status", list(NON_TERMINAL))
        return q.execute().data
    updated = await asyncio.to_thread(_with_retry, _update)
    if updated and "business_status" in patch:
        lead_id = updated[0]["lead_id"]
        await asyncio.to_thread(
            _with_retry,
            lambda: _get_supabase().table("campaign_leads").update({"status": patch["business_status"]}).eq("lead_id", lead_id).execute()
        )
    return JSONResponse({"status": "ok", "attempt": (updated or [{}])[0]})


@router.post("/api/campaigns/{campaign_id}/reconcile")
async def reconcile_campaign(campaign_id: str, stuck_after_s: int = STUCK_WITH_UUID_TIMEOUT_S):
    """FALLBACK — repairs two distinct kinds of stuck rows so DB state
    stays authoritative and every lead eventually converges to a
    terminal status, without ever double-dialing:

    1. Non-terminal attempts WITH a call_uuid but no hangup_cause after
       stuck_after_s (missed webhook, dropped Realtime event, backend
       restart) — ask Plivo directly for the real CDR outcome.
    2. Non-terminal attempts with NO call_uuid at all (QUEUED that never
       reached Plivo, or DIALING/RINGING that lost the race before the
       call_uuid patch landed) older than STALE_QUEUED_TIMEOUT_S — these
       can never be resolved by asking Plivo (there's no CallUUID to
       ask about), so they're recovered via the same atomic CAS used by
       dial_lead(), making the lead callable again on the next wave.

    Call periodically from the UI while a campaign is running (e.g.
    every 20-30s) instead of one blocking per-row poll."""
    cutoff = (datetime.utcnow().timestamp() - stuck_after_s)

    def _all_non_terminal():
        return (
            _get_supabase().table("call_attempts").select("*").eq("campaign_id", campaign_id)
            .in_("business_status", list(NON_TERMINAL)).execute().data or []
        )
    rows = await asyncio.to_thread(_with_retry, _all_non_terminal)

    with_uuid_stuck = [
        r for r in rows if r.get("call_uuid") and r.get("started_at")
        and _parse_ts(r["started_at"]) is not None and _parse_ts(r["started_at"]) < cutoff
    ]
    no_uuid_stale = [r for r in rows if not r.get("call_uuid")]

    repaired, recovered_ids = [], []

    for row in with_uuid_stuck:
        try:
            call = await asyncio.to_thread(_with_retry, lambda cu=row["call_uuid"]: plivo_client.calls.get(cu))
        except Exception:
            continue
        hangup_cause = getattr(call, "hangup_cause_name", None) or getattr(call, "hangup_cause", None)
        if not hangup_cause:
            continue
        patch = {"hangup_cause": hangup_cause, "business_status": business_status(hangup_cause), "ended_at": _now_iso()}
        # Same CAS guard as _try_recover_stale — only apply if the row
        # hasn't already moved on (e.g. the real webhook landed a beat
        # ago) so a slow reconcile pass never stomps a fresher update
        # or flips a terminal row back to non-terminal.
        def _apply(p=patch, aid=row["attempt_id"], prev_status=row["business_status"]):
            return _get_supabase().table("call_attempts").update(p).eq("attempt_id", aid).eq("business_status", prev_status).execute().data
        updated = await asyncio.to_thread(_with_retry, _apply)
        if not updated:
            continue
        await asyncio.to_thread(_with_retry, lambda ls=patch["business_status"], lid=row["lead_id"]: _get_supabase().table("campaign_leads").update({"status": ls}).eq("lead_id", lid).execute())
        repaired.append(row["attempt_id"])

    for row in no_uuid_stale:
        recovered = await asyncio.to_thread(_try_recover_stale, row)
        if recovered:
            recovered_ids.append(row["attempt_id"])

    return JSONResponse({"checked": len(rows), "repaired": repaired, "recovered_stale": recovered_ids})
