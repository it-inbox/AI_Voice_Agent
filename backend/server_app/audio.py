"""
Mulaw RMS computation, Plivo audio send/clear I/O, the media-sender
thread/task pair, ghost-call and AMD (answering-machine detection)
silence analysis, and the hangup paths they trigger.
"""

import asyncio
import base64
import json
import math
import queue as sync_queue
import threading
import time
from typing import List

from aiohttp import web

from .config import (
    AMD_SPEECH_TIMEOUT_S,
    CallOutcome,
    CallType,
    FRAME_DURATION_S,
    GHOST_CALL_TIMEOUT_S,
    GHOST_CALL_WARNING_S,
    SILENCE_THRESHOLD,
    log,
    plivo_client,
)
from . import metrics
from .session import Session, clog, log_outcome, run_async, ws_open
from .tts_bridge import send_to_tts   # NEW — "are you there?" ghost-call prompt

# NEW — how long the caller must talk over the agent, with no barge-in
# registered, before we count it as a missed interruption. Short enough
# to catch real problems, long enough that a stray cough/echo doesn't
# trip it.
MISSED_INTERRUPTION_WINDOW_S = 0.6


def _build_ulaw_table() -> List[int]:
    table = []
    for u in range(256):
        u    = ~u & 0xFF
        sign = u & 0x80
        exp  = (u >> 4) & 0x07
        mant = u & 0x0F
        s    = ((mant << 3) + 0x84) << exp
        s   -= 0x84
        table.append(-s if sign else s)
    return table


_ULAW_TABLE = _build_ulaw_table()


def ulaw_rms(data: bytes) -> float:
    if not data:
        return 0.0
    return math.sqrt(sum(_ULAW_TABLE[b] ** 2 for b in data) / len(data))


async def plivo_clear_audio(ws: web.WebSocketResponse, stream_id: str) -> None:
    """Plivo's barge-in event — no stream_id needed in the payload since
    each call gets its own dedicated WS connection, but Plivo does expect
    this exact event name (CHANGED from Telnyx's {"event":"clear"})."""
    if ws_open(ws):
        try:
            await ws.send_str(json.dumps({"event": "clearAudio"}))
        except Exception:
            pass


async def send_audio_to_plivo(ws: web.WebSocketResponse, audio: bytes, stream_id: str) -> None:
    """CHANGED: Plivo's outbound audio event is "playAudio" (not "media"
    like Telnyx/Twilio), and the media object needs explicit contentType +
    sampleRate fields — Plivo doesn't infer format from the inbound stream."""
    if not ws_open(ws):
        return
    try:
        await ws.send_str(json.dumps({
            "event": "playAudio",
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate":  8000,
                "payload":     base64.b64encode(audio).decode(),
            },
        }))
    except Exception as e:
        log.warning("audio→plivo: %s", e)


def enqueue_audio(q: sync_queue.Queue, raw: bytes) -> None:
    """Drop-oldest-frame strategy under backpressure."""
    try:
        q.put_nowait(raw)
    except sync_queue.Full:
        try:
            q.get_nowait()
        except sync_queue.Empty:
            pass
        try:
            q.put_nowait(raw)
        except sync_queue.Full:
            pass


async def drain_audio_queue(q: asyncio.Queue) -> None:
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break


def media_sender_thread(s: Session) -> None:
    clog(s.call_sid, "media-sender started")
    try:
        while True:
            chunk = s.send_q.get()
            if chunk is None:
                break
            try:
                s.stt_conn.send_media(chunk)   # MIGRATION: was s.agent_conn (Voice Agent) → now raw Listen socket
            except Exception as e:
                clog(s.call_sid, f"media-sender error: {e}")
    finally:
        clog(s.call_sid, "media-sender stopped")


def _start_media_sender(s: Session) -> None:
    threading.Thread(
        target=media_sender_thread, args=(s,),
        daemon=True, name=f"media-sender-{s.call_sid}",
    ).start()


async def audio_sender_task(ws: web.WebSocketResponse, s: Session) -> None:
    try:
        while True:
            item = await s.audio_queue.get()
            if item is None:
                break
            gen_id, audio = item
            is_current = gen_id == s.generation_id
            metrics.record_tts_chunk(s.call_sid, is_stale=not is_current)   # NEW — item 3: stale-response rate
            if is_current:
                await send_audio_to_plivo(ws, audio, s.stream_sid)
    except asyncio.CancelledError:
        pass


async def hangup_call(call_sid: str) -> None:
    """
    CHANGED: this is new for Plivo. Telnyx's original code only closed the
    WS to end a call — that's NOT enough for Plivo when keepCallAlive="true"
    on the <Stream> (which we set so a dropped WS doesn't kill the call
    mid-setup). Closing the WS only stops the audio stream; the underlying
    PSTN call stays connected in silence until this REST hangup runs.
    """
    if not call_sid:
        return
    try:
        await asyncio.to_thread(plivo_client.calls.delete, call_uuid=call_sid)
        clog(call_sid, "plivo REST hangup sent")
    except Exception as e:
        clog(call_sid, f"plivo REST hangup failed (call may already be over): {e}")


async def _perform_hangup(ws: web.WebSocketResponse, call_sid: str) -> None:
    """FIX (bug: end_conversation not working): this used to bail out
    entirely — skipping hangup_call() — whenever ws_open(ws) was already
    False. Plivo can close the media-stream WS on its own (network blip,
    Plivo-side timing) while keepCallAlive="true" keeps the PSTN call up
    in silence; when that happened, the REST hangup that actually ends
    the phone call never fired, so the call just sat connected. The REST
    hangup must run regardless of ws state — only the ws.close() call
    itself needs the ws_open() guard."""
    clog(call_sid, "graceful hangup")
    # FIX (12s to end call): this was a flat 1.0s sleep on EVERY call
    # site — llm_bridge._hangup_if_pending() and duration_guard already
    # wait out the real TTS flush event (+0.4s grace) before ever
    # reaching here, so that 1.0s was pure extra latency stacked on top
    # of a wait that already covered it. _check_amd's voicemail path
    # hangs up with nothing spoken at all, so it never needed a full
    # second either. Trimmed to a small safety buffer for the last audio
    # chunk(s) to clear the socket, not a second full grace period.
    await asyncio.sleep(0.2)
    await hangup_call(call_sid)   # always end the PSTN call via REST, ws state doesn't matter here
    if ws_open(ws):
        try:
            await ws.close()
        except Exception:
            pass


async def _ghost_are_you_there(ws: web.WebSocketResponse, s: Session, silence_at_fire: float) -> None:
    """NEW — fired once, at GHOST_CALL_WARNING_S of caller silence, instead
    of going straight to a hangup. Gives the caller a real chance to
    respond before the line drops on them mid-listen. Uses the lead's
    name when we have one, otherwise a plain "are you there?".

    Only a further GHOST_CALL_TIMEOUT_S of silence AFTER this actually
    ends the call (see _check_ghost_call) — this function does not hang
    up by itself.

    FIX (bug: LLM's next reply reads disconnected from what the customer
    actually said — "Thanks for confirming!" out of nowhere, or the
    original question getting re-asked oddly): this used to speak the
    prompt via send_to_tts() WITHOUT ever appending it to s.history. The
    customer's reply ("yes, I'm here") DOES land in history as a user
    turn (normal STT path, stt_bridge.py) — but the question it's
    answering never did. The model then sees an orphaned "Yes I'm here"
    with no idea what it's confirming, and its next reply has to guess.
    Appended to history now, same as any other agent line, so the
    customer's reply reads in context on the model's next turn."""
    if s.ghost_fired:
        return
    with s.lock:
        name = s.facts.get("lead_name") or s.lead_name_hint or None
    prompt = f"Sorry, are you still there, {name}?" if name else "Sorry, are you still there?"
    # NEW — real elapsed silence at fire time, logged explicitly. If this
    # ever reads meaningfully less than GHOST_CALL_WARNING_S on a live
    # call, that's a real bug worth chasing further — this is the number
    # to check first instead of guessing from how it felt on a live call.
    clog(s.call_sid, f"ghost warning fired — silence_seconds={silence_at_fire:.2f}s (threshold={GHOST_CALL_WARNING_S}s), prompting: {prompt!r}")
    with s.lock:
        s.history.append({"role": "assistant", "text": prompt})
    await send_to_tts(s, prompt)
    # send_to_tts() sets s.agent_speaking = True. On a normal LLM turn
    # that's cleared at the tail of llm_bridge.on_turn_complete() — but
    # this isn't a turn, so nothing else will ever clear it. Left set,
    # _should_analyze() would gate silence detection off for the rest of
    # the call and the ghost timer would never be able to fire again
    # (call sits connected forever if the caller really is gone). Clear
    # it ourselves once the prompt's actually finished playing.
    if s.tts_flushed_event:
        try:
            await asyncio.wait_for(s.tts_flushed_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.2)   # small grace for the last audio chunk(s) to clear the socket
    with s.lock:
        s.agent_speaking = False


async def ghost_call_hangup(ws: web.WebSocketResponse, s: Session) -> None:
    with s.lock:
        if s.ghost_fired:
            return
        s.ghost_fired = True
    total_s = GHOST_CALL_WARNING_S + GHOST_CALL_TIMEOUT_S
    clog(s.call_sid, f"ghost call fired — {total_s}s silence (prompted, no response)")
    log_outcome(s.call_sid, CallOutcome.NO_RESPONSE, "ghost call silence timeout — no response after 'are you there?' prompt")
    await plivo_clear_audio(ws, s.stream_sid)
    await hangup_call(s.call_sid)
    await ws.close()


def _should_analyze(s: Session) -> bool:
    """Skip rms calc entirely once both detectors are done and agent isn't speaking.

    FIX (race): on_turn_complete() resets s.agent_speaking=False BEFORE
    calling _hangup_if_pending() (llm_bridge.py) — which then still has
    to wait out the TTS flush + a real Plivo REST hangup, easily a
    second or two. Same gap exists in duration_guard's
    _speak_goodbye_and_hangup(). During that window agent_speaking is
    False but the call is already ending — without this check,
    _check_ghost_call() could start counting that wait as caller
    silence and, on a slow hangup, even fire its own "are you there?"
    prompt or hangup attempt layered on top of the one already in
    flight. Once s.pending_hangup is set, there's nothing left to detect."""
    return not s.agent_speaking and not s.pending_hangup and (not s.ghost_fired or not s.amd_done)


def _check_ghost_call(ws: web.WebSocketResponse, s: Session, rms: float, loop: asyncio.AbstractEventLoop) -> None:
    if s.ghost_fired:
        return
    if rms < SILENCE_THRESHOLD:
        should_prompt = False
        should_hangup = False
        with s.lock:
            s.silence_seconds += FRAME_DURATION_S
            silence = s.silence_seconds
            if not s.ghost_prompted and silence >= GHOST_CALL_WARNING_S:
                s.ghost_prompted = True   # claim it now, under the lock — never fire twice
                should_prompt = True
            elif s.ghost_prompted and silence >= GHOST_CALL_WARNING_S + GHOST_CALL_TIMEOUT_S:
                should_hangup = True
        if should_prompt:
            run_async(_ghost_are_you_there(ws, s, silence), loop)
        elif should_hangup:
            run_async(ghost_call_hangup(ws, s), loop)
    else:
        # Real caller audio — they're there. Reset both the timer and the
        # "already asked" flag so a LATER silence gap gets its own fresh
        # "are you there?" prompt instead of going straight to hangup.
        with s.lock:
            s.silence_seconds = 0.0
            s.ghost_prompted  = False


def _check_amd(ws: web.WebSocketResponse, s: Session, rms: float, loop: asyncio.AbstractEventLoop) -> None:
    if s.amd_done:
        return
    if rms >= SILENCE_THRESHOLD:
        if s.amd_speech_start is None:
            with s.lock:
                s.amd_speech_start = time.monotonic()
        else:
            elapsed = time.monotonic() - s.amd_speech_start
            if elapsed >= AMD_SPEECH_TIMEOUT_S:
                with s.lock:
                    s.call_type = CallType.VOICEMAIL
                    s.amd_done  = True
                log_outcome(s.call_sid, CallOutcome.VOICEMAIL, f"uninterrupted speech {elapsed:.1f}s")
                run_async(_perform_hangup(ws, s.call_sid), loop)
    else:
        with s.lock:
            s.amd_speech_start = None


def _check_barge_in_metrics(s: Session, rms: float) -> None:
    """NEW — item 3: runs only while the agent is talking. Provides the
    local RMS ground truth that interrupt-detection-latency is measured
    against, and separately catches the case Deepgram's own barge-in
    never fires at all (missed_interruption)."""
    if rms >= SILENCE_THRESHOLD:
        with s.lock:
            if s._local_speech_onset_ts is None:
                s._local_speech_onset_ts = time.monotonic()
            onset          = s._local_speech_onset_ts
            gen_at_onset   = s.generation_id
            already_flagged = s._missed_flagged
        elapsed = time.monotonic() - onset
        # Sustained caller speech, same generation the whole time (i.e.
        # no barge-in got registered in the meantime) → Deepgram missed it.
        if elapsed >= MISSED_INTERRUPTION_WINDOW_S and not already_flagged:
            with s.lock:
                still_same_gen = s.generation_id == gen_at_onset
                if still_same_gen:
                    s._missed_flagged = True
            if still_same_gen:
                metrics.record_missed_interruption(s)
    else:
        with s.lock:
            s._local_speech_onset_ts = None
            s._missed_flagged        = False


def _process_media(ws: web.WebSocketResponse, s: Session, raw: bytes, loop: asyncio.AbstractEventLoop) -> None:
    enqueue_audio(s.send_q, raw)

    if s.agent_speaking:
        # Ghost-call/AMD analysis doesn't apply mid-agent-speech, but the
        # barge-in ground-truth check needs RMS computed here specifically
        # — this used to be skipped entirely by _should_analyze() below.
        _check_barge_in_metrics(s, ulaw_rms(raw))
        return

    if not _should_analyze(s):
        return

    rms = ulaw_rms(raw)  # computed once, shared by both checks
    _check_ghost_call(ws, s, rms, loop)
    _check_amd(ws, s, rms, loop)