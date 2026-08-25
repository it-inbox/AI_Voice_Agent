"""
Deepgram Voice Agent connection lifecycle (connect/close/reconnect), the
agent event listener thread, LLM function-call handling, and the
duration/AMD guard tasks that ride alongside a live call.
"""

import asyncio
import json
import queue as sync_queue
import threading
from typing import Callable, Dict

from aiohttp import web
from deepgram.agent.v1.types import AgentV1SendFunctionCallResponse

from .audio import _perform_hangup, _start_media_sender, drain_audio_queue, plivo_clear_audio
from .config import (
    AMD_MAX_WAIT_S,
    CallOutcome,
    CallState,
    CallType,
    KEEPALIVE_INTERVAL_S,
    MAX_CALL_DURATION_S,
    RECONNECT_DELAYS,
    SEND_QUEUE_MAXSIZE,
    VOICEMAIL_PHRASES,
    WARNING_DURATION_S,
    deepgram_client,
    log,
)
from .prompts import build_recovery_settings
from .session import Session, clog, log_outcome, run_async, ws_open


def _dg_connect(lock: threading.Lock):
    with lock:
        cm     = deepgram_client.agent.v1.connect()
        socket = cm.__enter__()
        return cm, socket


def _dg_close(cm, lock: threading.Lock) -> None:
    with lock:
        try:
            cm.__exit__(None, None, None)
        except Exception as e:
            log.warning("dg_close: %s", e)


async def keepalive_loop(s: Session) -> None:
    try:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            try:
                await asyncio.to_thread(
                    s.agent_conn.send, json.dumps({"type": "KeepAlive"})
                )
            except Exception as e:
                clog(s.call_sid, f"keepalive failed: {e}")
                break
    except asyncio.CancelledError:
        pass


def _is_hot_or_warm_lead(s: Session) -> bool:
    """Server-side backstop for whether this lead has shown real interest —
    same signal the TIME BUDGET addon rule tells the model to use, but
    checked here directly so the hard-limit fallback doesn't just trust
    the model to have judged (and acted on) it correctly in time."""
    with s.lock:
        facts      = s.facts
        call_state = s.call_state
    if call_state in (CallState.QUALIFICATION, CallState.CLOSING):
        return True
    if facts.get("budget") or facts.get("timeline") or facts.get("company"):
        return True
    if facts.get("pain_points") or facts.get("interested_services"):
        return True
    return False


async def amd_max_wait_guard(ws: web.WebSocketResponse, s: Session) -> None:
    try:
        await asyncio.sleep(AMD_MAX_WAIT_S)
        with s.lock:
            if not s.amd_done:
                s.call_type = CallType.HUMAN
                s.amd_done  = True
        clog(s.call_sid, "[GUARD-CKPT] AMD max-wait fired — defaulting HUMAN")
    except asyncio.CancelledError:
        clog(s.call_sid, "[GUARD-CKPT] AMD guard cancelled (call ended before wait elapsed)")


async def duration_guard(ws: web.WebSocketResponse, s: Session) -> None:
    """Two-stage duration control.
    WARNING_DURATION_S — the model is actually made duration-aware:
      1. UpdatePrompt appends a time-budget note to its live system
         prompt (previously the model was never told anything at all —
         the old code just force-spoke a canned line via
         InjectAgentMessage with no `behavior` set, which Deepgram
         silently refuses whenever the user or agent is mid-turn, and
         which never touched the model's own reasoning either way).
      2. InjectUserMessage nudges it to actually take a turn using that
         new awareness — per the TIME BUDGET rule in _ADDON_PROMPT, it
         decides itself whether to promise a callback (CALLBACK_REQUESTED,
         for a lead that's shown real interest) or wrap up normally.
    MAX_CALL_DURATION_S — hard backstop regardless of what the model did:
      force a goodbye line with behavior="interrupt" (guarantees it's
      said even mid-turn) and hang up.
    """
    try:
        await asyncio.sleep(WARNING_DURATION_S)
        if not ws_open(ws):
            return

        with s.lock:
            already_warned = s.warning_sent
            s.warning_sent = True

        if not already_warned:
            socket = s.agent_conn
            informed = False
            if socket:
                try:
                    time_note = (
                        "\n\n--- TIME BUDGET ALERT ---\n"
                        "You are almost out of time on this call. Follow the "
                        "TIME BUDGET rule above right now."
                    )
                    await asyncio.to_thread(
                        socket.send,
                        json.dumps({
                            "type":   "UpdatePrompt",
                            "prompt": s.system_prompt + time_note,
                        })
                    )
                    await asyncio.to_thread(
                        socket.send,
                        json.dumps({
                            "type":    "InjectUserMessage",
                            "content": "(system: we're almost at the call time limit)",
                        })
                    )
                    informed = True
                except Exception as e:
                    clog(s.call_sid, f"duration warning error: {e}")

            if not informed:
                log_outcome(s.call_sid, CallOutcome.CALL_DROPPED, "max duration (warning unavailable)")
                with s.lock:
                    s.pending_hangup = True
                    if not s.outcome:
                        s.outcome = CallOutcome.CALL_DROPPED
                if not s.agent_speaking:
                    await _perform_hangup(ws, s.call_sid)
                return

        await asyncio.sleep(MAX_CALL_DURATION_S - WARNING_DURATION_S)
        if not ws_open(ws):
            return

        clog(s.call_sid, "hard duration limit reached")

        with s.lock:
            already_decided = s.outcome is not None
        # Only decide CALLBACK_REQUESTED vs CALL_DROPPED here if the model
        # hasn't already ended the call itself (e.g. via the warning-stage
        # TIME BUDGET rule) — this is a backstop, not a second opinion.
        is_warm = _is_hot_or_warm_lead(s) if not already_decided else False

        if already_decided:
            goodbye = "I've got to wrap up now — thank you so much for your time today. Have a great day!"
        elif is_warm:
            goodbye = "I'm so sorry, I've got to run — I don't want to lose touch though, I'll give you a call back to finish this up. Take care!"
        else:
            goodbye = "I've got to wrap up now — thank you so much for your time today. Have a great day!"

        log_outcome(
            s.call_sid,
            s.outcome if already_decided else (CallOutcome.CALLBACK_REQUESTED if is_warm else CallOutcome.CALL_DROPPED),
            "hard duration limit",
        )

        socket = s.agent_conn
        injected = False
        if socket:
            try:
                await asyncio.to_thread(
                    socket.send,
                    json.dumps({
                        "type":     "InjectAgentMessage",
                        "message":  goodbye,
                        "behavior": "interrupt",  # CHANGED — was unset (defaults to "default", which Deepgram silently refuses if a turn is in progress). This is the actual reason the hard cutoff often didn't fire.
                    })
                )
                injected = True
            except Exception as e:
                clog(s.call_sid, f"hard-limit inject error: {e}")

        with s.lock:
            s.pending_hangup = True
            # CHANGED — don't clobber an outcome the model already set
            # itself (e.g. CALLBACK_REQUESTED from the warning stage).
            # If it didn't decide in time, fall back to the server-side
            # hot/warm check above — CALLBACK_REQUESTED only for a lead
            # that actually showed interest, CALL_DROPPED otherwise.
            if not s.outcome:
                s.outcome = CallOutcome.CALLBACK_REQUESTED if is_warm else CallOutcome.CALL_DROPPED

        if not injected and not s.agent_speaking:
            await _perform_hangup(ws, s.call_sid)

        await asyncio.sleep(5.0)
        if ws_open(ws):
            await ws.close()

    except asyncio.CancelledError:
        pass


def _send_fn_response(socket, fn_id: str, fn_name: str, content: Dict) -> None:
    try:
        socket.send_function_call_response(
            AgentV1SendFunctionCallResponse(id=fn_id, name=fn_name, content=json.dumps(content))
        )
    except Exception as e:
        log.warning("fn_response[%s]: %s", fn_name, e)


def handle_fn(socket, fn, s: Session, call_sid: str, close_ws: Callable) -> None:
    try:
        args = json.loads(fn.arguments)
    except Exception:
        args = {}

    if fn.name == "end_conversation":
        outcome = args.get("outcome", CallOutcome.CALL_DROPPED)
        reason  = args.get("reason", "")
        with s.lock:
            s.outcome        = outcome
            s.pending_hangup = True
        log_outcome(call_sid, outcome, reason)
        _send_fn_response(socket, fn.id, fn.name, {
            "success": True,
            "message": "Thank you for your time. Have a great day!",
            "reason":  reason,
            "outcome": outcome,
        })

    elif fn.name == "update_call_state":
        new_state = args.get("state", "")
        if new_state in CallState._value2member_map_:
            with s.lock:
                s.call_state = new_state
        _send_fn_response(socket, fn.id, fn.name, {"success": True, "state": new_state})

    elif fn.name == "update_lead_facts":
        with s.lock:
            for key in ("company", "budget", "timeline"):
                if args.get(key):
                    s.facts[key] = args[key]
            for key in ("pain_points", "interested_services"):
                if args.get(key):
                    existing = set(s.facts.get(key) or [])
                    existing.update(args[key])
                    s.facts[key] = list(existing)
            snapshot = dict(s.facts)
        _send_fn_response(socket, fn.id, fn.name, {"success": True, "facts": snapshot})

    else:
        _send_fn_response(socket, fn.id, fn.name, {"success": False, "error": f"unknown fn: {fn.name}"})


def agent_listener_thread(
    socket,
    ws: web.WebSocketResponse,
    s: Session,
    settings: Dict,
) -> None:
    loop     = s.loop
    call_sid = s.call_sid

    def run(coro):
        run_async(coro, loop)

    def close_ws():
        run(ws.close())

    try:
        for msg in socket:
            if isinstance(msg, bytes):
                with s.lock:
                    s.agent_speaking = True
                    gen_id = s.generation_id
                run(s.audio_queue.put((gen_id, msg)))
                continue

            t = getattr(msg, "type", None)

            if t == "Welcome":
                clog(call_sid, "DG connected → sending settings")
                try:
                    socket.send_settings(settings)
                except Exception as e:
                    clog(call_sid, f"send_settings error: {e}")

            elif t == "SettingsApplied":
                clog(call_sid, "DG ready — starting media sender")
                _start_media_sender(s)

            elif t == "Error":
                desc = getattr(msg, "description", "")
                log.error("[%s] DG error %s: %s", call_sid, getattr(msg, "code", "?"), desc)
                if "connection" in str(desc).lower() or "websocket" in str(desc).lower():
                    run(_reconnect_deepgram(ws, s))

            elif t == "InjectionRefused":
                clog(call_sid, f"[GUARD-CKPT] injection refused: {getattr(msg, 'message', '')}")

            elif t == "UserStartedSpeaking":
                with s.lock:
                    s.generation_id += 1
                    gen_id           = s.generation_id
                    s.agent_speaking  = False
                    s.silence_seconds = 0.0
                    if s.call_type == CallType.UNKNOWN:
                        s.call_type = CallType.HUMAN
                        s.amd_done  = True
                run(drain_audio_queue(s.audio_queue))
                if s.stream_sid:
                    run(plivo_clear_audio(ws, s.stream_sid))
                clog(call_sid, f"barge-in gen={gen_id}")

            elif t == "AgentStartedSpeaking":
                with s.lock:
                    s.agent_speaking  = True
                    s.silence_seconds = 0.0

            elif t == "AgentAudioDone":
                with s.lock:
                    s.agent_speaking = False
                    pending = s.pending_hangup
                if pending:
                    run(_perform_hangup(ws, call_sid))

            elif t == "FunctionCallRequest":
                for fn in msg.functions:
                    clog(call_sid, f"fn: {fn.name}")
                    handle_fn(socket, fn, s, call_sid, close_ws)

            elif t == "ConversationText":
                role = getattr(msg, "role", "?")
                text = getattr(msg, "content", getattr(msg, "text", ""))
                with s.lock:
                    s.history.append({"role": role, "text": text})
                    amd_done = s.amd_done

                if not amd_done and role in ("user", "assistant"):
                    lower = text.lower()
                    if any(p in lower for p in VOICEMAIL_PHRASES):
                        with s.lock:
                            s.call_type = CallType.VOICEMAIL
                            s.amd_done  = True
                        log_outcome(call_sid, CallOutcome.VOICEMAIL, "voicemail phrase in transcript")
                        close_ws()
                    elif "press" in lower and ("for" in lower or "to" in lower):
                        with s.lock:
                            s.call_type = CallType.IVR
                            s.amd_done  = True
                        log_outcome(call_sid, CallOutcome.IVR, "IVR pattern in transcript")
                        close_ws()

    except Exception as e:
        log.error("[%s] listener error: %s", call_sid, e)
        if ws_open(ws):
            run(_reconnect_deepgram(ws, s))
    finally:
        clog(call_sid, "listener exited")
        run(s.audio_queue.put(None))
        run(ws.close())


async def _reconnect_deepgram(ws: web.WebSocketResponse, s: Session) -> bool:
    async with s.reconnect_lock:
        for attempt, delay in enumerate(RECONNECT_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            clog(s.call_sid, f"reconnect attempt {attempt + 1}")

            try:
                if old_cm := s._cm:
                    await asyncio.to_thread(_dg_close, old_cm, s.dg_lock)

                cm, socket = await asyncio.to_thread(_dg_connect, s.dg_lock)
                with s.lock:
                    s._cm        = cm
                    s.agent_conn = socket

                if old_sq := s.send_q:
                    old_sq.put(None)
                s.send_q = sync_queue.Queue(maxsize=SEND_QUEUE_MAXSIZE)

                if old_ka := s.keepalive_task:
                    old_ka.cancel()
                s.keepalive_task = asyncio.ensure_future(keepalive_loop(s))

                recovery = build_recovery_settings(s, s.dg_settings)
                if old_l := s.listener:
                    old_l.cancel()
                s.listener = asyncio.ensure_future(
                    asyncio.to_thread(agent_listener_thread, socket, ws, s, recovery)
                )

                clog(s.call_sid, f"reconnect success attempt {attempt + 1}")
                return True

            except Exception as e:
                clog(s.call_sid, f"reconnect attempt {attempt + 1} failed: {e}")

    clog(s.call_sid, "all reconnect attempts failed")
    log_outcome(s.call_sid, CallOutcome.CALL_DROPPED, "Deepgram reconnect failed")
    await ws.close()
    return False
