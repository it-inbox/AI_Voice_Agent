"""
MIGRATION: NEW FILE — Phase 3.

Owns the raw Deepgram Speak (TTS) connection. Replaces the Voice Agent
API's built-in "speak" leg. Connects ONCE per call (not lazily after a
SettingsApplied event — that event doesn't exist on the raw APIs), and
feeds decoded audio straight into the existing audio.py queue/generation_id
plumbing, unchanged.
"""

import asyncio
import threading
from typing import Optional

# FIX: the old import (`SpeakWebSocketEvents`, `SpeakWSOptions`) is the
# deepgram-sdk v3-era callback API. The pinned version in requirements.txt
# resolves to v7.7.0, which uses the same typed "v1 client" shape as the
# listen/agent connections already in this codebase: a sync context
# manager (`speak.v1.connect(...)`) yielding a V1SocketClient with
# send_text/send_flush/send_clear/send_close, and audio delivered as raw
# `bytes` from iterating/receiving on that socket — not an event callback.
from deepgram.speak.v1 import SpeakV1Flushed, SpeakV1Text

from .config import CallOutcome, RECONNECT_DELAYS, deepgram_client, log
from .session import Session, clog, log_outcome, run_async

TTS_MODEL       = "aura-2-helena-en"
TTS_ENCODING    = "mulaw"
TTS_SAMPLE_RATE = 8000


def _tts_connect(lock: threading.Lock):
    """Mirrors _stt_connect()'s pattern exactly — sync context manager
    entered manually so the connection stays open across the whole call
    instead of a single `with` block."""
    with lock:
        cm     = deepgram_client.speak.v1.connect(
            model=TTS_MODEL, encoding=TTS_ENCODING, sample_rate=TTS_SAMPLE_RATE,
        )
        socket = cm.__enter__()
        return cm, socket


def _tts_close(cm, lock: threading.Lock) -> None:
    with lock:
        try:
            cm.__exit__(None, None, None)
        except Exception as e:
            log.warning("tts_close: %s", e)


def _tts_listener_thread(socket, s: Session, ws) -> None:
    """Raw Speak socket yields `bytes` for audio chunks and typed messages
    (SpeakV1Metadata/Flushed/Cleared/Warning) for control events — there's
    no separate AudioData callback to subscribe to, you just read whatever
    comes off the socket. Runs for the life of the call, same shape as
    stt_listener_thread.

    FIX (item 2): previously tagged each incoming chunk with whatever
    s.generation_id happened to be AT RECEIVE TIME. That's a race: if a
    barge-in landed between send_to_tts() and this audio actually
    arriving, stale audio for the OLD (interrupted) turn would get
    mistagged with the NEW generation and play anyway. Now tags with
    s.tts_send_gen — stamped once, at the moment the text was sent — so
    each chunk carries the generation it actually belongs to, not
    whatever's current when it happens to show up.

    FIX (reconnect): this used to just log-and-die on error, same as
    stt_bridge before it got _reconnect_stt — one TTS-side network blip
    left the call listening-only (STT still worked, agent could never
    speak again) for the rest of its duration. Now triggers the same
    kind of backoff-reconnect stt_bridge already has, and — if every
    attempt fails — hangs up rather than leaving a live, silent Plivo
    channel burning minutes for an agent that can no longer speak."""
    try:
        for msg in socket:
            if isinstance(msg, (bytes, bytearray)):
                with s.lock:
                    gen_id = s.tts_send_gen
                run_async(s.audio_queue.put((gen_id, bytes(msg))), s.loop)
            elif isinstance(msg, SpeakV1Flushed):
                # NEW — Deepgram confirming it's done generating audio for
                # everything sent so far. duration_guard's hangup path
                # waits on this instead of guessing words-per-second.
                # .set() from a background thread needs call_soon_threadsafe
                # — asyncio.Event isn't itself thread-safe to touch directly.
                if s.loop and s.tts_flushed_event:
                    s.loop.call_soon_threadsafe(s.tts_flushed_event.set)
            # SpeakV1Metadata / SpeakV1Cleared / SpeakV1Warning carry no
            # audio and need no action here — clear is already driven from
            # cancel_inflight_tts() on our side.
    except Exception as e:
        clog(s.call_sid, f"tts listener error: {e}")
        run_async(_reconnect_tts(s, ws), s.loop)
    finally:
        clog(s.call_sid, "tts listener exited")


async def _reconnect_tts(s: Session, ws) -> bool:
    """Mirrors stt_bridge._reconnect_stt's backoff pattern exactly.

    Known limitation, stated plainly rather than glossed over: whatever
    sentence chunk was in flight at the moment the connection dropped is
    lost — there's no buffering/replay of unsent text here. The caller
    may hear the agent cut off mid-sentence once, then continue normally
    on the next sentence/turn. That's a real but far smaller problem than
    the call going silent for its entire remaining duration, which is
    what happened before this existed."""
    async with s.tts_reconnect_lock:
        for attempt, delay in enumerate(RECONNECT_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            clog(s.call_sid, f"tts reconnect attempt {attempt + 1}")

            try:
                if old_cm := s._tts_cm:
                    await asyncio.to_thread(_tts_close, old_cm, s.tts_lock)

                cm, socket = await asyncio.to_thread(_tts_connect, s.tts_lock)
                with s.lock:
                    s._tts_cm  = cm
                    s.tts_conn = socket

                threading.Thread(
                    target=_tts_listener_thread, args=(socket, s, ws), daemon=True,
                ).start()

                clog(s.call_sid, f"tts reconnect success attempt {attempt + 1}")
                return True

            except Exception as e:
                clog(s.call_sid, f"tts reconnect attempt {attempt + 1} failed: {e}")

    clog(s.call_sid, "all tts reconnect attempts failed — hanging up")
    with s.lock:
        s.tts_conn = None
    log_outcome(s.call_sid, CallOutcome.CALL_DROPPED, "Deepgram TTS reconnect failed")
    if ws is not None:
        await ws.close()
    return False


def open_tts_connection(s: Session, ws) -> None:
    """Open the Speak websocket for this call and start its listener
    thread. Connects once per call, same lifecycle as stt_conn."""
    cm, socket = _tts_connect(s.tts_lock)
    with s.lock:
        s._tts_cm  = cm
        s.tts_conn = socket
    threading.Thread(
        target=_tts_listener_thread, args=(socket, s, ws), daemon=True,
    ).start()


async def send_to_tts(s: Session, text: str) -> None:
    if not text or not text.strip():
        return
    with s.lock:
        conn   = s.tts_conn
        gen_id = s.generation_id
        s.tts_send_gen   = gen_id   # NEW (item 2) — stamp the request's own generation
        s.agent_speaking = True
    if s.tts_flushed_event:
        s.tts_flushed_event.clear()   # NEW — reset so duration_guard's wait() below picks up THIS send's completion, not a stale one
    if not conn:
        clog(s.call_sid, "send_to_tts: no tts_conn — dropping")
        return
    try:
        await asyncio.to_thread(conn.send_text, SpeakV1Text(text=text))
        await asyncio.to_thread(conn.send_flush)
    except Exception as e:
        clog(s.call_sid, f"send_to_tts error (gen={gen_id}): {e}")


async def cancel_inflight_tts(s: Session) -> None:
    """Barge-in / hard-cutoff support — clears whatever Speak is currently
    synthesizing/queued. Real method name on the installed SDK is
    `send_clear()` (control-frame message), not `.clear()`."""
    with s.lock:
        conn = s.tts_conn
    if not conn:
        return
    try:
        await asyncio.to_thread(conn.send_clear)
    except Exception as e:
        clog(s.call_sid, f"cancel_inflight_tts error: {e}")


async def close_tts_connection(s: Session) -> None:
    with s.lock:
        conn = s.tts_conn
        cm   = s._tts_cm
        s.tts_conn = None
        s._tts_cm  = None
    if conn:
        try:
            await asyncio.to_thread(conn.send_close)
        except Exception as e:
            log.warning("close_tts_connection send_close: %s", e)
    if cm:
        await asyncio.to_thread(_tts_close, cm, s.tts_lock)
