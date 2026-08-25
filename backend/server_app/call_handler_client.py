"""
All outbound HTTP calls this service makes to call_handler.py: agent
config fetch (with cache), number↔agent routing lookups, saving call
results/transcripts, and placing outbound calls via Plivo.
"""

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from .config import (
    CALL_HANDLER_URL,
    CONFIG_FETCH_BACKOFF_S,
    CONFIG_FETCH_RETRIES,
    INTERNAL_API_KEY,
    log,
    plivo_client,
)
from .session import clog

DEFAULT_AGENT_ID = "default"

_config_cache: Dict[str, Dict[str, Any]] = {}       # {agent_id: {...}}
_config_cache_ts: Dict[str, float] = {}              # {agent_id: monotonic_ts}
_config_cache_lock = asyncio.Lock()  # NEW: prevents thundering-herd concurrent fetches
_CONFIG_CACHE_TTL_S = 60.0


async def fetch_agent_config(agent_id: str = DEFAULT_AGENT_ID, force: bool = False) -> Dict[str, Any]:
    global _config_cache, _config_cache_ts

    async with _config_cache_lock:
        now       = time.monotonic()
        cached    = _config_cache.get(agent_id)
        cached_ts = _config_cache_ts.get(agent_id, 0.0)
        if not force and (now - cached_ts) < _CONFIG_CACHE_TTL_S and cached:
            return cached

        for attempt in range(CONFIG_FETCH_RETRIES):
            if attempt:
                await asyncio.sleep(CONFIG_FETCH_BACKOFF_S * attempt)
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(
                        f"{CALL_HANDLER_URL}/api/config",
                        params={"agent_id": agent_id},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            cfg = await resp.json()
                            _config_cache[agent_id]    = cfg
                            _config_cache_ts[agent_id] = now
                            return cfg
                        log.warning("config HTTP %s attempt %d (agent_id=%s)", resp.status, attempt + 1, agent_id)
            except Exception as e:
                log.warning("config attempt %d (agent_id=%s): %s", attempt + 1, agent_id, e)

        log.warning("config fetch failed for agent_id=%s — using cache or defaults", agent_id)
        return _config_cache.get(agent_id, {})


_phone_map_cache: Dict[str, str] = {}
_phone_map_cache_ts: float = 0.0
_PHONE_MAP_TTL_S = 300.0


async def resolve_agent_id(to_number: Optional[str]) -> str:
    global _phone_map_cache, _phone_map_cache_ts
    if not to_number:
        return DEFAULT_AGENT_ID

    now = time.monotonic()
    if to_number in _phone_map_cache and (now - _phone_map_cache_ts) < _PHONE_MAP_TTL_S:
        return _phone_map_cache[to_number]

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{CALL_HANDLER_URL}/api/agent-for-number",
                params={"to": to_number},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    agent_id = data.get("agent_id", DEFAULT_AGENT_ID)
                    _phone_map_cache[to_number] = agent_id
                    _phone_map_cache_ts = now
                    return agent_id
    except Exception as e:
        log.warning("agent-for-number lookup failed for %s: %s", to_number, e)

    return DEFAULT_AGENT_ID


async def resolve_agent_for_call(to_number: str) -> Optional[str]:
    """STRICT number-to-agent binding: this number must be explicitly
    linked to an agent (dashboard's Agent Profiles -> Assign). No pool
    round-robin fallback — an unmapped number returns None and the
    caller must reject the call rather than silently routing it to a
    default/random agent. This applies to both inbound and outbound,
    since Plivo's `To` param is the same field either way."""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{CALL_HANDLER_URL}/api/agent-for-number",
                params={"to": to_number},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    mapped = data.get("agent_id")
                    if mapped:
                        return mapped
    except Exception as e:
        log.error("agent-for-number lookup failed for to=%s: %s", to_number, e)

    log.error("NO AGENT MAPPED for to=%s — call cannot be routed", to_number)
    return None


async def save_call_result(
    call_sid: str,
    facts: Dict[str, Any],
    outcome: Optional[str],
    history: List[Dict[str, str]],
) -> None:
    headers = {"X-Internal-Key": INTERNAL_API_KEY} if INTERNAL_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{CALL_HANDLER_URL}/api/call-live-facts",
                json={
                    "call_sid": call_sid,
                    "facts":    facts,
                    "outcome":  outcome,
                    "history":  history,
                },
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    clog(call_sid, f"save_call_result HTTP {resp.status}")
    except Exception as e:
        clog(call_sid, f"save_call_result failed: {e}")


async def save_call_transcript(
    call_sid:      str,
    history:       List[Dict[str, str]],
    to_number:     str,
    from_number:   str,
    duration_sec:  float,
    agent_id:      str,
) -> None:
    """NEW: replaces the old record-download-transcribe pipeline. The
    transcript already exists as text (s.history, built live from
    Deepgram's ConversationText events during the call), so this just
    ships that text straight to call_handler.py's /api/call-transcript,
    which runs it through Groq for lead scoring — no audio recording,
    no download, no re-transcription needed."""
    if not history:
        clog(call_sid, "save_call_transcript skipped — empty history")
        return
    headers = {"X-Internal-Key": INTERNAL_API_KEY} if INTERNAL_API_KEY else {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{CALL_HANDLER_URL}/api/call-transcript",
                json={
                    "call_sid":     call_sid,
                    "history":      history,
                    "to_number":    to_number,
                    "from_number":  from_number,
                    "duration_sec": duration_sec,
                    "agent_id":     agent_id,
                    "source":       "Plivo",
                },
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    clog(call_sid, f"save_call_transcript HTTP {resp.status}")
                else:
                    clog(call_sid, "save_call_transcript OK — Groq scoring queued")
    except Exception as e:
        clog(call_sid, f"save_call_transcript failed: {e}")


async def place_outbound_call(to_number: str, agent_id: str, host: str, lead_name: str = "") -> Dict[str, Any]:
    from . import session as _session  # local import: avoids a session<->call_handler_client cycle

    to_number = (to_number or "").strip()
    agent_id  = (agent_id or "").strip()
    lead_name = (lead_name or "").strip()  # NEW — per-call customer name
    if not to_number or not agent_id:
        return {"error": "'to' and 'agent_id' are required"}

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                f"{CALL_HANDLER_URL}/api/number-for-agent",
                params={"agent_id": agent_id},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json() if resp.status == 200 else {}
    except Exception as e:
        import traceback
        log.error("outbound call create failed — to=%s agent_id=%s\nanswer_url=%r\n%s",
                   to_number, agent_id, answer_url, traceback.format_exc())
        return {"error": f"Plivo call create failed: {e}"}

    from_number = data.get("number")
    if not from_number:
        return {"error": f"agent_id={agent_id} has no Plivo number assigned — assign one in Agent Profiles first"}

    # NEW — dash_id is OUR correlation token (Plivo doesn't echo request_uuid
    # back to us anywhere), embedded on the answer_url so plivo_answer() can
    # bridge it to the real CallUUID the instant it fires.
    dash_id = uuid.uuid4().hex

    # lead_name travels on the answer_url query string (Plivo calls this URL
    # back once the callee picks up), so plivo_answer() can pick it up and
    # forward it to the WS session, same call every time.
    answer_url = f"https://{host}/plivo/answer?agent_id={agent_id}&lead_name={quote(lead_name)}&dash_id={dash_id}"

    try:
        result = await asyncio.to_thread(
            plivo_client.calls.create,
            from_=from_number,
            to_=to_number,
            answer_url=answer_url,
            answer_method="POST",
        )
    except Exception as e:
        log.error("outbound call create failed — to=%s agent_id=%s: %s", to_number, agent_id, e)
        return {"error": f"Plivo call create failed: {e}"}

    request_uuid = getattr(result, "request_uuid", None) or (result.get("request_uuid") if isinstance(result, dict) else None)

    # NEW — wait for plivo_answer() to resolve the REAL CallUUID via
    # _dash_call_uuid_map. In testing this has taken 15-25s in practice
    # (Plivo → tunnel → local dev server round trip), not the 1-3s a
    # production deployment would usually see — the previous 6s budget
    # was giving up and reporting "Failed" on calls that went on to
    # connect and hold a full conversation seconds later. 25s covers the
    # observed worst case with margin. If the callee's carrier rejects
    # before Plivo ever dials out (bad number, blocked, etc.), plivo_answer
    # never fires and this stays empty regardless of how long we wait —
    # that's a real, distinct outcome (see /api/resolve-call-uuid and the
    # dashboard's handling of a missing call_uuid), not something to fake.
    call_uuid = None
    for _ in range(50):  # ~25s max
        await asyncio.sleep(0.5)
        call_uuid = _session._dash_call_uuid_map.get(dash_id)
        if call_uuid:
            break

    log.info(
        "outbound call placed — to=%s from=%s agent_id=%s lead_name=%s request_uuid=%s call_uuid=%s",
        to_number, from_number, agent_id, lead_name or "—", request_uuid, call_uuid or "not yet resolved",
    )
    return {
        "status": "ok", "to": to_number, "from": from_number, "agent_id": agent_id, "lead_name": lead_name or None,
        # NEW — call_uuid is now the REAL Plivo CallUUID (or null if the
        # answer webhook hasn't fired yet). This is what the dashboard
        # should poll /api/plivo/call-status and Supabase live_outcome
        # with — request_uuid works for neither.
        "call_uuid": call_uuid,
        "request_uuid": request_uuid,  # kept for logs/debugging only — NOT valid for status lookups
        "dash_id": dash_id,            # if call_uuid is null, the dashboard can keep resolving via /api/resolve-call-uuid
    }
