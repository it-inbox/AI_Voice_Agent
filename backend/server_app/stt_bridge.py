"""
MIGRATION: renamed from deepgram_bridge.py — Phase 1 + Phase 4.

Raw Deepgram Listen (STT) connection lifecycle (connect/close/reconnect),
the STT event listener thread (turn detection + barge-in), LLM
function-call execution (handle_fn — now socket-agnostic, no more
FunctionCallRequest/response protocol), and the duration/AMD guard tasks
that ride alongside a live call.
"""

import asyncio
import json
import queue as sync_queue
import threading
from typing import Callable, Dict

from aiohttp import web

# MIGRATION: NEW — raw Listen API event types (verify import path against
# your pinned deepgram-sdk version if it differs).
from deepgram.listen.v1.types import (
    ListenV1Results,
    ListenV1SpeechStarted,
    ListenV1UtteranceEnd,
)

from .audio import _perform_hangup, _start_media_sender, drain_audio_queue, plivo_clear_audio
from . import metrics
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
from .prompts import build_stt_settings
from .session import Session, clog, log_outcome, run_async, ws_open
from .tts_bridge import cancel_inflight_tts, send_to_tts


def _stt_connect(lock: threading.Lock, settings: dict):
    with lock:
        cm     = deepgram_client.listen.v1.connect(**settings)
        socket = cm.__enter__()
        return cm, socket


def _stt_close(cm, lock: threading.Lock) -> None:
    with lock:
        try:
            cm.__exit__(None, None, None)
        except Exception as e:
            log.warning("stt_close: %s", e)


async def keepalive_loop(s: Session) -> None:
    try:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            try:
                await asyncio.to_thread(
                    s.stt_conn.send, json.dumps({"type": "KeepAlive"})
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
        clog(s.call_sid, "AMD max-wait fired — defaulting HUMAN")
    except asyncio.CancelledError:
        pass  # normal path: call ended before the guard's wait elapsed


async def duration_guard(ws: web.WebSocketResponse, s: Session) -> None:
    """Two-stage duration control.

    MIGRATION: rewritten for the raw-API world — there's no more
    UpdatePrompt/InjectUserMessage/InjectAgentMessage socket protocol.

    WARNING_DURATION_S — the model is made duration-aware by appending a
      time-budget note directly onto s.system_prompt. The NEXT
      on_turn_complete() call (llm_bridge.py) picks up the updated prompt
      automatically since it's rebuilt from s.system_prompt on every turn
      — no socket call needed, and unlike the old UpdatePrompt/
      InjectUserMessage pair this can't be silently refused.
    MAX_CALL_DURATION_S — hard backstop regardless of what the model did:
      cancel whatever TTS is in flight, force-speak a goodbye line, wait
      out its rough speaking duration (no more AgentAudioDone to await),
      then hang up.
    """
    try:
        await asyncio.sleep(WARNING_DURATION_S)
        if not ws_open(ws):
            return

        with s.lock:
            already_warned = s.warning_sent
            s.warning_sent = True

        if not already_warned:
            time_note = (
                "\n\n--- TIME BUDGET ALERT ---\n"
                "You are almost out of time on this call. Follow the "
                "TIME BUDGET rule above right now."
            )
            with s.lock:
                s.system_prompt += time_note
            clog(s.call_sid, "duration warning — system_prompt updated for next turn")

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

        if already_decided or not is_warm:
            goodbye = "I've got to wrap up now — thank you so much for your time today. Have a great day!"
        else:
            goodbye = "I'm so sorry, I've got to run — I don't want to lose touch though, I'll give you a call back to finish this up. Take care!"

        log_outcome(
            s.call_sid,
            s.outcome if already_decided else (CallOutcome.CALLBACK_REQUESTED if is_warm else CallOutcome.CALL_DROPPED),
            "hard duration limit",
        )

        await cancel_inflight_tts(s)          # NEW — replaces implicit InjectAgentMessage(behavior="interrupt")
        await send_to_tts(s, goodbye)

        with s.lock:
            s.pending_hangup = True
            # Don't clobber an outcome the model already set itself (e.g.
            # CALLBACK_REQUESTED from the warning stage). If it didn't
            # decide in time, fall back to the server-side hot/warm check
            # above — CALLBACK_REQUESTED only for a lead that actually
            # showed interest, CALL_DROPPED otherwise.
            if not s.outcome:
                s.outcome = CallOutcome.CALLBACK_REQUESTED if is_warm else CallOutcome.CALL_DROPPED

        # NEW — Deepgram's SpeakV1Flushed tells us when it's actually done
        # generating audio for the goodbye, rather than guessing
        # words-per-second. Still keep the old estimate as a timeout floor
        # in case the event never arrives (e.g. TTS connection dropped
        # mid-goodbye) — better to hang up a beat late than hang forever.
        est_speak_s = max(1.5, len(goodbye.split()) / 2.5)
        if s.tts_flushed_event:
            try:
                await asyncio.wait_for(s.tts_flushed_event.wait(), timeout=est_speak_s + 2.0)
            except asyncio.TimeoutError:
                clog(s.call_sid, "goodbye flush event timed out — hanging up on estimate instead")
            # small grace for the last audio chunk(s) to actually reach
            # Plivo and play out after Deepgram confirms generation is done
            await asyncio.sleep(0.4)
        else:
            await asyncio.sleep(est_speak_s)
        await _perform_hangup(ws, s.call_sid)

        await asyncio.sleep(5.0)
        if ws_open(ws):
            await ws.close()

    except asyncio.CancelledError:
        pass


def handle_fn(fn_name: str, arguments: str, s: Session, call_sid: str) -> Dict:
    """MIGRATION: reused almost verbatim from the old FunctionCallRequest
    handler — it was already socket-agnostic except for
    _send_fn_response(), which is GONE entirely. There's no
    FunctionCallRequest/response protocol on the raw APIs; the return
    value here just becomes a normal `role: tool` message that
    llm_bridge.py appends to the conversation for the next completion
    call."""
    try:
        args = json.loads(arguments) if arguments else {}
    except Exception:
        args = {}

    if fn_name == "end_conversation":
        outcome = args.get("outcome", CallOutcome.CALL_DROPPED)
        reason  = args.get("reason", "")
        with s.lock:
            s.outcome        = outcome
            s.pending_hangup = True
        log_outcome(call_sid, outcome, reason)
        return {
            "success": True,
            "message": "Thank you for your time. Have a great day!",
            "reason":  reason,
            "outcome": outcome,
        }

    elif fn_name == "update_call_state":
        new_state = args.get("state", "")
        if new_state in CallState._value2member_map_:
            with s.lock:
                s.call_state = new_state
        return {"success": True, "state": new_state}

    elif fn_name == "update_lead_facts":
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
        return {"success": True, "facts": snapshot}

    else:
        return {"success": False, "error": f"unknown fn: {fn_name}"}


def _check_amd_ivr_voicemail(call_sid: str, s: Session, user_text: str, close_ws: Callable) -> None:
    """MIGRATION: previously ran inline off every ConversationText event
    (which no longer exists). Now runs off finalized user transcripts
    only — that's the audio actually coming from the far end of the
    call, which is what voicemail/IVR phrasing would appear in."""
    with s.lock:
        amd_done = s.amd_done
    if amd_done:
        return
    lower = user_text.lower()
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


def _launch_llm_turn(s: Session, on_turn_complete: Callable, text: str) -> None:
    """NEW (item 1) — launches the LLM turn as a TRACKED, cancellable
    future instead of true fire-and-forget. Barge-in reads s.active_llm_task
    and cancels it immediately, so an interrupted turn's Groq stream
    actually stops consuming tokens and feeding TTS instead of finishing
    in the background after the caller's already moved on."""
    fut = run_async(on_turn_complete(s, text), s.loop)

    def _clear_if_current(done_fut) -> None:
        with s.lock:
            if s.active_llm_task is done_fut:
                s.active_llm_task = None

    fut.add_done_callback(_clear_if_current)
    with s.lock:
        s.active_llm_task = fut


async def _handle_barge_in_stop(ws: web.WebSocketResponse, s: Session) -> None:
    """Bundles the three barge-in-stop actions into one awaited sequence
    so audio-stop latency (item 3) measures real completion, not just
    fire-and-forget dispatch — then schedules the false-positive check."""
    await drain_audio_queue(s.audio_queue)
    if s.stream_sid:
        await plivo_clear_audio(ws, s.stream_sid)
    await cancel_inflight_tts(s)
    metrics.record_audio_stopped(s)

    with s.lock:
        history_len_at_barge_in = len(s.history)
    await asyncio.sleep(1.5)
    with s.lock:
        had_transcript = len(s.history) > history_len_at_barge_in
    metrics.record_barge_in_outcome(s, had_transcript)


def stt_listener_thread(
    socket,
    ws: web.WebSocketResponse,
    s: Session,
    on_turn_complete: Callable,
) -> None:
    """MIGRATION: replaces agent_listener_thread(). Biggest single change
    in the whole migration — the entire Voice Agent event-type chain
    (Welcome/SettingsApplied/UserStartedSpeaking/AgentStartedSpeaking/
    AgentAudioDone/FunctionCallRequest/ConversationText) is gone. What's
    left is pure STT: speech-start (barge-in) and finalized transcripts
    (turn boundaries), handed off to on_turn_complete() (llm_bridge.py)."""
    loop     = s.loop
    call_sid = s.call_sid

    def run(coro):
        run_async(coro, loop)

    def close_ws():
        run(ws.close())

    interim_buf = ""
    try:
        for msg in socket:
            if isinstance(msg, ListenV1SpeechStarted):
                # REPLACES "UserStartedSpeaking" — same barge-in logic, new trigger
                with s.lock:
                    s.generation_id += 1
                    gen_id            = s.generation_id
                    s.agent_speaking  = False
                    s.silence_seconds = 0.0
                    task              = s.active_llm_task     # NEW (item 1)
                    s.active_llm_task = None
                    if s.call_type == CallType.UNKNOWN:
                        s.call_type = CallType.HUMAN
                        s.amd_done  = True

                if task is not None and not task.done():
                    # NEW (item 1) — the biggest improvement: stop the
                    # in-flight Groq stream right now instead of letting
                    # it keep running (and keep feeding sentences to TTS
                    # for a turn the caller just talked over).
                    task.cancel()

                metrics.record_barge_in_detected(s)   # NEW (item 3)
                run(_handle_barge_in_stop(ws, s))      # NEW (item 3) — was 3 separate fire-and-forget calls
                clog(call_sid, f"barge-in gen={gen_id}")

            elif isinstance(msg, ListenV1Results):
                alt = msg.channel.alternatives[0] if msg.channel.alternatives else None
                if not alt or not alt.transcript:
                    continue
                if msg.is_final:
                    interim_buf += (" " + alt.transcript).strip()
                if msg.speech_final:
                    # REPLACES ConversationText(role=user) + Deepgram's own turn-end
                    text = interim_buf.strip()
                    interim_buf = ""
                    if not text:
                        continue
                    with s.lock:
                        s.history.append({"role": "user", "text": text})
                    _check_amd_ivr_voicemail(call_sid, s, text, close_ws)
                    _launch_llm_turn(s, on_turn_complete, text)     # NEW (item 1) — tracked, cancellable

            elif isinstance(msg, ListenV1UtteranceEnd):
                # backstop if speech_final never fired (noisy line, etc.)
                text = interim_buf.strip()
                interim_buf = ""
                if text:
                    with s.lock:
                        s.history.append({"role": "user", "text": text})
                    _check_amd_ivr_voicemail(call_sid, s, text, close_ws)
                    _launch_llm_turn(s, on_turn_complete, text)

    except Exception as e:
        log.error("[%s] stt listener error: %s", call_sid, e)
        if ws_open(ws):
            run(_reconnect_stt(ws, s))
    finally:
        clog(call_sid, "stt listener exited")
        run(s.audio_queue.put(None))


async def _reconnect_stt(ws: web.WebSocketResponse, s: Session) -> bool:
    # Lazy import — avoids a stt_bridge <-> llm_bridge circular import
    # (llm_bridge imports handle_fn from this module at top level).
    from .llm_bridge import on_turn_complete

    async with s.reconnect_lock:
        for attempt, delay in enumerate(RECONNECT_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            clog(s.call_sid, f"reconnect attempt {attempt + 1}")

            try:
                if old_cm := s._stt_cm:
                    await asyncio.to_thread(_stt_close, old_cm, s.stt_lock)

                settings = s.stt_settings or build_stt_settings()
                cm, socket = await asyncio.to_thread(_stt_connect, s.stt_lock, settings)
                with s.lock:
                    s._stt_cm  = cm
                    s.stt_conn = socket

                if old_sq := s.send_q:
                    old_sq.put(None)
                s.send_q = sync_queue.Queue(maxsize=SEND_QUEUE_MAXSIZE)

                if old_ka := s.keepalive_task:
                    old_ka.cancel()
                s.keepalive_task = asyncio.ensure_future(keepalive_loop(s))

                if old_l := s.listener:
                    old_l.cancel()
                s.listener = asyncio.ensure_future(
                    asyncio.to_thread(stt_listener_thread, socket, ws, s, on_turn_complete)
                )

                _start_media_sender(s)   # raw Listen API is ready as soon as connected — no SettingsApplied to wait for

                clog(s.call_sid, f"reconnect success attempt {attempt + 1}")
                return True

            except Exception as e:
                clog(s.call_sid, f"reconnect attempt {attempt + 1} failed: {e}")

    clog(s.call_sid, "all reconnect attempts failed")
    log_outcome(s.call_sid, CallOutcome.CALL_DROPPED, "Deepgram STT reconnect failed")
    await ws.close()
    return False
