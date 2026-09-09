"""
HTTP/WS route handlers: Plivo signature verification, the answer and
outbound-call webhooks, the Plivo Audio Streaming WS handler, per-call
session setup/cleanup, health/ping, and the aiohttp app entrypoint.
"""

import asyncio
import base64
import json
import queue as sync_queue
import time
from typing import Any, Dict
from urllib.parse import quote

import aiohttp
import plivo
from aiohttp import web
from aiohttp_cors import ResourceOptions, setup as cors_setup

from .audio import _process_media, _start_media_sender, audio_sender_task
from . import metrics
from .call_handler_client import (
    fetch_agent_config,
    place_outbound_call,
    resolve_agent_for_call,
    resolve_agent_id,
    save_call_result,
    save_call_transcript,
)
from .config import CALL_HANDLER_URL, CallOutcome, PLIVO_ANSWER_URL, PLIVO_AUTH_TOKEN, PORT, SEND_QUEUE_MAXSIZE, log
from .llm_bridge import on_turn_complete
from .prompts import build_greeting, build_stt_settings, build_system_prompt
from .stt_bridge import _stt_close, _stt_connect, amd_max_wait_guard, duration_guard, keepalive_loop, stt_listener_thread
from .tts_bridge import close_tts_connection, open_tts_connection, send_to_tts
from .session import (
    Session,
    _answered_call_uuids,
    _dash_call_uuid_map,
    _sessions,
    _DASH_MAP_MAX,
    clog,
    log_outcome,
    ws_open,
)


async def _verify_plivo_signature(request: web.Request) -> Dict[str, str]:
    """
    Returns the parsed form params if verification passes (or is skipped
    because credentials aren't configured for local testing). Raises
    web.HTTPForbidden if verification is enabled and fails.
    """
    form   = await request.post()
    params = dict(form)

    signature = request.headers.get("X-Plivo-Signature-V3", "")
    nonce     = request.headers.get("X-Plivo-Signature-V3-Nonce", "")

    if not signature or not nonce:
        log.warning("plivo webhook missing signature headers — rejecting")
        raise web.HTTPForbidden(reason="Missing Plivo signature headers")

    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    url = f"{scheme}://{host}{request.path_qs}"
    try:
        valid = plivo.utils.validate_v3_signature(
            "POST", url, nonce, PLIVO_AUTH_TOKEN, signature, params
        )
    except Exception as exc:
        log.warning("plivo signature verify error: %s", exc)
        raise web.HTTPForbidden(reason="Signature verification error")

    if not valid:
        raise web.HTTPForbidden(reason="Invalid Plivo signature")

    return params


async def plivo_answer(request: web.Request) -> web.Response:
    params    = await _verify_plivo_signature(request)
    call_uuid = params.get("CallUUID", "")
    to_number = params.get("To", "")
    from_number = params.get("From", "")

    if call_uuid and call_uuid in _answered_call_uuids:
        clog(call_uuid, f"DUPLICATE answer webhook — to={to_number} — ignoring, call already answered")
        return web.Response(
            text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            content_type="application/xml",
        )
    if call_uuid:
        _answered_call_uuids.add(call_uuid)
        if len(_answered_call_uuids) > 500:
            _answered_call_uuids.pop()

    # NEW — this is the FIRST point the real Plivo CallUUID exists. Record
    # it against our own dash_id (set on the answer_url by
    # place_outbound_call) so /api/outbound-call and /api/resolve-call-uuid
    # can hand the dashboard the id that actually works for polling.
    dash_id = request.query.get("dash_id", "")
    if dash_id and call_uuid:
        _dash_call_uuid_map[dash_id] = call_uuid
        if len(_dash_call_uuid_map) > _DASH_MAP_MAX:
            _dash_call_uuid_map.pop(next(iter(_dash_call_uuid_map)))

    explicit_agent_id = request.query.get("agent_id", "")
    if explicit_agent_id:
        agent_id = explicit_agent_id
        clog(call_uuid, f"answer webhook (outbound) — to={to_number} → agent_id={agent_id}")
    else:
        # Inbound call (no agent_id on the query string — that's only ever
        # set by place_outbound_call). We deliberately do NOT run the AI
        # pipeline for these: a caller dialing our agent's number back
        # would otherwise burn STT/LLM/TTS minutes for free. Play a fixed
        # message and hang up — no Stream, no WebSocket, no agent cost.
        clog(call_uuid, f"REJECTED (inbound) — to={to_number} — static message, no AI pipeline")
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Response>'
            '<Speak voice="WOMAN" language="en-US">'
            "Thank you for calling. We'll shortly connect you with our sales team."
            '</Speak>'
            '<Hangup/>'
            '</Response>'
        )
        return web.Response(text=xml, content_type="application/xml")

    asyncio.ensure_future(fetch_agent_config(agent_id))

    # NEW — per-call customer name, set by place_outbound_call() on the
    # answer_url query string; forwarded straight through to the WS URL
    # so _start_session can read it as s.lead_name_hint.
    lead_name_q = request.query.get("lead_name", "")

    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", f"localhost:{PORT}")
    ws_url = (
        f"wss://{host}/ws/plivo?agent_id={agent_id}&amp;call_uuid={call_uuid}"
        f"&amp;to={to_number}&amp;from={from_number}"
        f"&amp;lead_name={quote(lead_name_q)}"
    )
    stream_status_url = f"{CALL_HANDLER_URL}/plivo/stream-status"

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>'
        f'<Stream bidirectional="true" keepCallAlive="true" '
        f'contentType="audio/x-mulaw;rate=8000" '
        f'statusCallbackUrl="{stream_status_url}">'
        f'{ws_url}'
        f'</Stream>'
        '</Response>'
    )
    return web.Response(text=xml, content_type="application/xml")


async def resolve_call_uuid(request: web.Request) -> web.Response:
    """Fallback resolver — used when the outbound-call response came back
    with call_uuid=null (the answer webhook hadn't fired within the ~6s
    place_outbound_call() waits). The dashboard can keep polling this with
    the dash_id it got back until it resolves, or until it gives up and
    marks the row as never-connected."""
    dash_id = request.query.get("dash_id", "")
    if not dash_id:
        return web.json_response({"error": "dash_id is required"}, status=400)
    call_uuid = _dash_call_uuid_map.get(dash_id)
    if call_uuid:
        return web.json_response({"status": "ok", "call_uuid": call_uuid})
    return web.json_response({"status": "pending", "call_uuid": None})


async def outbound_call(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    to_number = (body.get("to") or "").strip()
    agent_id  = (body.get("agent_id") or "").strip()
    # NEW — optional per-call customer name from the dashboard (single dial
    # "Customer Name" field, or batch sheet's detected name column). Goes
    # straight to the LLM as lead_name for this call only — accepts either
    # key so existing callers using "lead_name" keep working.
    lead_name = (body.get("name") or body.get("lead_name") or "").strip()
    if not PLIVO_ANSWER_URL:
        return web.json_response({"error": "PLIVO_ANSWER_URL not set — required for outbound calls"}, status=500)
    host = PLIVO_ANSWER_URL.split("://", 1)[-1].split("/", 1)[0]

    result = await place_outbound_call(to_number, agent_id, host, lead_name)
    if "error" not in result:
        return web.json_response(result, status=200)
    if "required" in result["error"]:
        return web.json_response(result, status=400)
    if "assigned" in result["error"]:
        return web.json_response(result, status=400)
    return web.json_response(result, status=502)


async def _start_session(ws: web.WebSocketResponse, s: Session, data: Dict) -> bool:
    start      = data.get("start", {})
    stream_sid = start.get("streamId") or data.get("streamId", "")
    call_sid   = start.get("callId") or stream_sid

    agent_id_hint = s.agent_id_hint

    s.stream_sid      = stream_sid
    s.call_sid        = call_sid
    s.call_start_time = time.monotonic()
    s.reconnect_lock  = asyncio.Lock()
    s.tts_reconnect_lock = asyncio.Lock()
    s.tts_flushed_event  = asyncio.Event()
    s.loop            = asyncio.get_running_loop()
    clog(call_sid, "call started")

    try:
        agent_id      = agent_id_hint or await resolve_agent_id(None)
        s.agent_id    = agent_id

        # MIGRATION: was a single Deepgram Voice Agent connect. Now STT
        # settings are just a plain dict (build_stt_settings — no prompt,
        # no functions, no greeting baked in) and the raw Listen socket
        # connects independently of the LLM/TTS legs.
        stt_settings = build_stt_settings()
        cfg, (cm, socket) = await asyncio.gather(
            fetch_agent_config(agent_id),
            asyncio.to_thread(_stt_connect, s.stt_lock, stt_settings),
        )

        agent_name    = cfg.get("agent_name", "").strip() or "Assistant"
        # NEW — per-call customer name (from dashboard "Place Call" / batch
        # sheet, threaded through answer_url → ws query → s.lead_name_hint)
        # takes priority over the static per-agent agent_config.lead_name.
        lead_name     = s.lead_name_hint.strip() or cfg.get("lead_name", "").strip() or None
        system_prompt = build_system_prompt(cfg.get("system_prompt", ""), agent_name, lead_name)
        s.system_prompt = system_prompt

        s.stt_settings = stt_settings
        if lead_name:
            s.facts["lead_name"] = lead_name

        s._stt_cm  = cm
        s.stt_conn = socket

        s.send_q      = sync_queue.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        s.audio_queue = asyncio.Queue()

        # MIGRATION: TTS connects once per call, right here — not lazily
        # after a SettingsApplied event (that event doesn't exist on the
        # raw APIs).
        open_tts_connection(s, ws)

        s.audio_sender_task = asyncio.ensure_future(audio_sender_task(ws, s))
        s.listener          = asyncio.ensure_future(
            asyncio.to_thread(stt_listener_thread, socket, ws, s, on_turn_complete)
        )
        s.keepalive_task = asyncio.ensure_future(keepalive_loop(s))
        s.duration_task  = asyncio.ensure_future(duration_guard(ws, s))
        s.amd_task       = asyncio.ensure_future(amd_max_wait_guard(ws, s))

        # MIGRATION: raw Listen API is ready to receive audio as soon as
        # the connection is open — no SettingsApplied handshake to wait
        # for, so the media sender starts immediately instead of being
        # kicked off from inside the listener thread.
        _start_media_sender(s)

        # NEW — greeting: the Voice Agent API used to speak this on
        # connect via agent.greeting. We own that now — speak it directly
        # once TTS is up.
        greeting = build_greeting(lead_name)
        with s.lock:
            s.history.append({"role": "assistant", "text": greeting})
        asyncio.ensure_future(send_to_tts(s, greeting))

        _sessions[call_sid] = s
        return True

    except Exception as e:
        clog(call_sid, f"setup failed: {type(e).__name__}: {e}")
        log_outcome(call_sid, CallOutcome.CALL_DROPPED, f"setup error: {e}")
        s.outcome = CallOutcome.CALL_DROPPED
        return False


async def _cleanup(s: Session) -> None:
    from .audio import drain_audio_queue  # local import: avoids an audio<->routes cycle

    call_sid = s.call_sid
    _sessions.pop(call_sid, None)

    if not s.outcome:
        # BUGFIX (Lead page "Call Status" stuck) — this branch used to only
        # LOG a CALL_DROPPED outcome without ever assigning it to s.outcome.
        # save_call_result() below always fires regardless, so it shipped
        # outcome=None to /api/call-live-facts, which upserts `live_outcome`
        # as an explicit NULL — the Leads/Dashboard "Call Status" column
        # then had nothing to show (or kept whatever stale value was written
        # by an earlier failed attempt for the same lead). Any call that
        # ends without the agent explicitly calling end_conversation
        # (network drop, caller hangs up mid-call, exception, etc.) must
        # still resolve to a real terminal status.
        s.outcome = CallOutcome.CALL_DROPPED
        log_outcome(call_sid, CallOutcome.CALL_DROPPED, "session ended without outcome")

    for task in filter(None, [
        s.keepalive_task, s.audio_sender_task, s.listener,
        s.duration_task, s.amd_task,
    ]):
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # NEW (item 1) — a turn can still be mid-flight when the call ends
    # (hangup, duration limit, etc.), not just on barge-in. Cancel it too
    # instead of leaving a dangling LLM stream running past call end.
    if llm_task := s.active_llm_task:
        llm_task.cancel()

    if q := s.send_q:
        q.put(None)

    if aq := s.audio_queue:
        await drain_audio_queue(aq)
        try:
            aq.put_nowait(None)
        except Exception:
            pass

    if cm := s._stt_cm:
        await asyncio.to_thread(_stt_close, cm, s.stt_lock)
    await close_tts_connection(s)

    try:
        await save_call_result(call_sid, dict(s.facts), s.outcome, list(s.history))
    except Exception as e:
        clog(call_sid, f"save_call_result error: {e}")

    try:
        duration = time.monotonic() - s.call_start_time if s.call_start_time else 0.0
        await save_call_transcript(
            call_sid, list(s.history), s.to_number, s.from_number,
            duration, s.agent_id,
        )
    except Exception as e:
        clog(call_sid, f"save_call_transcript error: {e}")

    clog(call_sid, f"cleanup done — outcome={s.outcome}")


async def plivo_ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)

    s               = Session()
    s.agent_id_hint = request.query.get("agent_id", "")   # from the WS URL, see plivo_answer()
    s.to_number     = request.query.get("to", "")
    s.from_number   = request.query.get("from", "")
    s.lead_name_hint = request.query.get("lead_name", "") # NEW — per-call customer name, see plivo_answer()
    loop            = asyncio.get_running_loop()

    try:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
                continue

            try:
                data  = json.loads(message.data)
                event = data.get("event")
            except json.JSONDecodeError:
                log.warning("non-JSON WS message, skipping: %.200s", message.data)
                continue

            if event == "connected":
                continue

            elif event == "start":
                ok = await _start_session(ws, s, data)
                if not ok:
                    await ws.close()
                    break

            elif event == "stop":
                break

            elif event == "media":
                if not s.stt_conn or not s.send_q:
                    continue
                payload = data.get("media", {}).get("payload")
                if payload:
                    _process_media(ws, s, base64.b64decode(payload), loop)

    except Exception as e:
        log.error("WS loop exception: %s: %s", type(e).__name__, e)
    finally:
        await _cleanup(s)

    return ws


async def ping(request: web.Request) -> web.Response:
    return web.Response(text='{"status":"ok"}', content_type="application/json")


async def health(request: web.Request) -> web.Response:
    cfg = await fetch_agent_config()
    body = json.dumps({
        "status":          "ok",
        "active_sessions": len(_sessions),
        "call_handler":    CALL_HANDLER_URL,
        "prompt_loaded":   bool(cfg.get("system_prompt")),
        "agent_name":      cfg.get("agent_name", "Assistant"),
    })
    return web.Response(text=body, content_type="application/json")


async def metrics_endpoint(request: web.Request) -> web.Response:
    """NEW (item 3) — process-lifetime rolling snapshot of barge-in
    metrics. Point a dashboard/curl at this; the durable per-event data
    is in the logs (event=barge_in_metric), this is just a quick summary."""
    return web.json_response(metrics.summary())


async def main() -> None:
    app = web.Application()
    app.router.add_post("/plivo/answer", plivo_answer)
    app.router.add_post("/api/outbound-call", outbound_call)
    app.router.add_get("/api/resolve-call-uuid", resolve_call_uuid)
    app.router.add_get("/ws/plivo",      plivo_ws_handler)
    app.router.add_get("/health",        health)
    app.router.add_get("/ping",          ping)
    app.router.add_get("/metrics",       metrics_endpoint)

    cors = cors_setup(app, defaults={
        "*": ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*")
    })
    for route in list(app.router.routes()):
        cors.add(route)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.warning("server started on port %d", PORT)

    await asyncio.Future()