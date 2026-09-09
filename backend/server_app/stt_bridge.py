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
import time
from typing import Callable, Dict

from aiohttp import web

# MIGRATION: NEW — raw Listen API event types (verify import path against
# your pinned deepgram-sdk version if it differs).
from deepgram.listen.v1.types import (
    ListenV1Results,
    ListenV1SpeechStarted,
    ListenV1UtteranceEnd,
)

from .audio import _perform_hangup, _start_media_sender, drain_audio_queue, hangup_call, plivo_clear_audio
from . import metrics
from .config import (
    AMD_MAX_WAIT_S,
    CallOutcome,
    CallState,
    CallType,
    HOT_LEAD_GRACE_S,
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
from .session import Session, _sessions, clog, log_outcome, run_async, ws_open
from .tts_bridge import cancel_inflight_tts, send_to_tts


# NEW — how long to wait after a barge-in, with no real user transcript
# following, before deciding it was a false trigger (echo/click/noise)
# and replaying the interrupted reply. FIX: this used to be a single
# fixed sleep (was 1.5s, then 1.1s, then 2.2s — none of which fit every
# interruption length). Replaced with a poll in _handle_barge_in_stop
# that extends as long as last_stt_activity_at keeps advancing (i.e.
# real ongoing speech), using these three instead of one number:
_FALSE_BARGE_IN_MIN_WAIT_S   = 1.2   # never decide before this, even if silent immediately
_FALSE_BARGE_IN_IDLE_GRACE_S = 1.8   # give up once genuinely nothing heard for this long (> UTTERANCE_END_MS's 1.4s gap)
_FALSE_BARGE_IN_MAX_WAIT_S   = 8.0   # hard ceiling regardless of activity, so dead air can't stretch forever

# NEW — debounce a barge-in BEFORE committing to the disruptive cut,
# instead of only checking after the fact (that's what the
# _FALSE_BARGE_IN_* wait above already did — but by then TTS has
# already been chopped mid-sentence and the caller heard it happen).
# Deepgram's ListenV1SpeechStarted fires on ANY detected energy — a
# click, line noise, or the agent's own voice leaking back into the mic
# (no acoustic echo cancellation in front of it) — not just real speech.
# On a noisy line this was firing repeatedly and cutting the agent off
# mid-reply every time, which is exactly what produced replies that cut
# off after a few words and forced the caller to re-ask the same
# question. Give the local RMS onset tracker (_check_barge_in_metrics
# in audio.py, already running per-frame while the agent is speaking) a
# brief window to confirm the energy is actually sustained before
# treating a SpeechStarted event as a real interruption. Real barge-ins
# are effectively unaffected — human reaction time is far longer than
# this.
_BARGE_IN_DEBOUNCE_S = 0.22


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
                # FIX: newer deepgram-sdk (v6+, the "V1SocketClient" seen
                # in the old error) removed the raw .send(json string)
                # method in favor of named control methods. requirements.txt
                # pins deepgram-sdk>=3.0.0 with no upper bound, so this
                # broke silently the moment the environment resolved a
                # newer SDK version — every keepalive attempt failed and
                # the whole loop died after the very first one, every call.
                await asyncio.to_thread(s.stt_conn.send_keep_alive)
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


async def _speak_goodbye_and_hangup(
    ws: web.WebSocketResponse, s: Session, goodbye: str, outcome: str, reason: str,
) -> None:
    """Shared tail for every code-driven (non-LLM) forced call ending:
    the HOT_LEAD_GRACE_S graceful close and the MAX_CALL_DURATION_S hard
    backstop both do the exact same thing — speak a fixed line, wait it
    out, hang up for real. Factored out so both stages stay identical
    instead of duplicating the flush-wait/estimate/hangup dance.

    FIX (race condition): this used to act FIRST — cancel_inflight_tts()
    + send_to_tts(goodbye) — and only set s.pending_hangup=True
    afterward. Nothing stopped this from racing the model's own
    end_conversation() (handle_fn, this file) landing at nearly the same
    moment: e.g. the model decides to wrap up right around the 3:30 grace
    point. Both paths could fire — cancel_inflight_tts() here would chop
    off whatever goodbye the model had already queued to TTS, speak a
    SECOND, different goodbye over it, and both paths would then run
    their own independent hangup sequence concurrently. Now this claims
    pending_hangup atomically under the lock FIRST, and backs off
    entirely — no TTS, no hangup — if someone else (the LLM path, or an
    earlier duration_guard stage) already claimed it. Whoever gets there
    first owns the ending; there is only ever one goodbye."""
    with s.lock:
        if s.pending_hangup:
            clog(s.call_sid, f"skip forced goodbye ({reason}) — hangup already in progress elsewhere")
            return
        s.pending_hangup = True
        if not s.outcome:   # don't clobber an outcome the model already set itself
            s.outcome = outcome

    log_outcome(s.call_sid, outcome, reason)

    if ws_open(ws):
        await cancel_inflight_tts(s)          # replaces implicit InjectAgentMessage(behavior="interrupt")
        await send_to_tts(s, goodbye)

    # Deepgram's SpeakV1Flushed tells us when it's actually done generating
    # audio for the goodbye — not when Plivo has finished playing it out
    # over the phone line (generation is faster than real-time). Keep the
    # old estimate as a timeout floor in case the event never arrives, AND
    # as the actual post-flush wait — otherwise this hangs up on the
    # caller mid-goodbye. Same fix as llm_bridge.py's on_turn_complete /
    # _hangup_if_pending.
    if ws_open(ws):
        est_speak_s = max(1.5, len(goodbye.split()) / 2.5)
        if s.tts_flushed_event:
            try:
                await asyncio.wait_for(s.tts_flushed_event.wait(), timeout=est_speak_s + 8.0)
            except asyncio.TimeoutError:
                clog(s.call_sid, "goodbye flush event timed out — hanging up on estimate instead")
            await asyncio.sleep(est_speak_s + 0.4)
        else:
            await asyncio.sleep(est_speak_s)
    await _perform_hangup(ws, s.call_sid)   # always run — real Plivo hangup, ws-agnostic now

    await asyncio.sleep(5.0)
    if ws_open(ws):
        await ws.close()


async def duration_guard(ws: web.WebSocketResponse, s: Session) -> None:
    """Three-stage duration control.

    MIGRATION: rewritten for the raw-API world — there's no more
    UpdatePrompt/InjectUserMessage/InjectAgentMessage socket protocol.

    WARNING_DURATION_S (3:00) — the model is made duration-aware by
      appending a time-budget note directly onto s.system_prompt. The
      NEXT on_turn_complete() call (llm_bridge.py) picks up the updated
      prompt automatically since it's rebuilt from s.system_prompt on
      every turn — no socket call needed, and unlike the old
      UpdatePrompt/InjectUserMessage pair this can't be silently refused.
      Prompt-level only: asks the model to ask the caller's permission
      before wrapping up. Not deterministic — that's what the next two
      stages are for.

    HOT_LEAD_GRACE_S (3:30) — NEW. Deterministic, code-driven close for
      HOT/WARM leads specifically: if the model hasn't already ended the
      call itself by now, speak a fixed "we've notified the team, you'll
      be reached out to again soon" line and hang up — same mechanism as
      the hard backstop below, just earlier and only for leads that
      actually showed interest. Leads that AREN'T hot/warm are left
      alone here and fall through to the generic MAX_CALL_DURATION_S
      backstop instead.

    MAX_CALL_DURATION_S (4:00) — hard backstop regardless of what the
      model (or the 3:30 stage) did: cancel whatever TTS is in flight,
      force-speak a goodbye line, wait out its rough speaking duration,
      then hang up. This is the one path with no "ask permission" —
      by definition there's no time left to wait for a reply.
    """
    try:
        await asyncio.sleep(WARNING_DURATION_S)

        with s.lock:
            already_warned = s.warning_sent
            s.warning_sent = True

        if not already_warned and ws_open(ws):
            time_note = (
                "\n\n--- TIME BUDGET ALERT ---\n"
                "You are almost out of time on this call. Follow the "
                "TIME BUDGET rule above right now."
            )
            with s.lock:
                s.system_prompt += time_note
            clog(s.call_sid, "duration warning — system_prompt updated for next turn")

        # ── Stage 2: 3:30 — deterministic graceful close, HOT/WARM leads only ──
        await asyncio.sleep(HOT_LEAD_GRACE_S - WARNING_DURATION_S)

        with s.lock:
            already_decided = s.outcome is not None

        if not already_decided and _is_hot_or_warm_lead(s):
            clog(s.call_sid, "hot-lead grace point (3:30) reached — closing gracefully")
            hot_goodbye = (
                "I've let our team know everything we discussed — they'll be "
                "reaching back out to you again soon. Thank you so much for "
                "your time today!"
            )
            await _speak_goodbye_and_hangup(
                ws, s, hot_goodbye, CallOutcome.CALLBACK_REQUESTED, "hot lead — 3:30 grace close",
            )
            return

        # ── Stage 3: 4:00 — hard backstop for everyone else ──
        await asyncio.sleep(MAX_CALL_DURATION_S - HOT_LEAD_GRACE_S)

        clog(s.call_sid, "hard duration limit reached")

        with s.lock:
            already_decided = s.outcome is not None
        # Only decide CALLBACK_REQUESTED vs CALL_DROPPED here if the model
        # hasn't already ended the call itself (e.g. via the warning-stage
        # TIME BUDGET rule, or the 3:30 stage above) — this is a backstop,
        # not a second opinion.
        is_warm = _is_hot_or_warm_lead(s) if not already_decided else False

        if already_decided or not is_warm:
            goodbye = "I've got to wrap up now — thank you so much for your time today. Have a great day!"
            outcome = CallOutcome.CALL_DROPPED
        else:
            goodbye = "I'm so sorry, I've got to run — I don't want to lose touch though, I'll give you a call back to finish this up. Take care!"
            outcome = CallOutcome.CALLBACK_REQUESTED

        await _speak_goodbye_and_hangup(ws, s, goodbye, outcome, "hard duration limit")

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
            already_pending = s.pending_hangup   # FIX — same claim pattern as _speak_goodbye_and_hangup
            s.pending_hangup = True
            if not already_pending:
                s.outcome = outcome
        if already_pending:
            # Model called end_conversation a second time this call (rare,
            # but seen when it double-reacts across hops). The first call
            # already owns the ending — don't re-log, don't spawn a
            # second watchdog on top of the first.
            clog(call_sid, "end_conversation called again — already ending, ignoring duplicate")
            return {"success": True, "message": "", "reason": reason, "outcome": outcome}
        log_outcome(call_sid, outcome, reason)
        # FIX (bug: end_conversation sometimes not hanging up): the normal
        # path is llm_bridge._hangup_if_pending(), read at the tail of
        # on_turn_complete() once the goodbye line's been queued to TTS.
        # That tail is SKIPPED if this turn gets barge-in-cancelled before
        # reaching it (CancelledError propagates straight out, by design)
        # and no further real transcript ever follows to launch a fresh
        # turn (e.g. caller says something too garbled for speech_final to
        # fire). In that gap s.pending_hangup stays True with nothing left
        # to act on it — call just sits connected until the 4-min hard
        # duration_guard backstop. This watchdog is a pure safety net: if
        # the normal path already finished the call (session cleaned up),
        # it's a no-op; otherwise it forces the real hangup itself.
        if s.loop:
            run_async(_end_conversation_watchdog(s), s.loop)
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


_END_CONVERSATION_WATCHDOG_S = 20.0   # generous — normal path finishes in ~5-8s


async def _end_conversation_watchdog(s: Session) -> None:
    """See handle_fn()'s end_conversation branch for why this exists.
    Gives the normal hangup path (llm_bridge._hangup_if_pending) plenty
    of time to finish on its own, then force-hangs-up regardless if the
    call is somehow still connected with pending_hangup still set."""
    try:
        await asyncio.sleep(_END_CONVERSATION_WATCHDOG_S)
    except asyncio.CancelledError:
        return
    with s.lock:
        still_pending = s.pending_hangup
    call_ended = s.call_sid not in _sessions   # _cleanup() already popped it — normal path won
    if still_pending and not call_ended:
        clog(s.call_sid, "end_conversation watchdog fired — forcing hangup")
        await hangup_call(s.call_sid)


def _check_amd_ivr_voicemail(call_sid: str, s: Session, user_text: str, end_call: Callable) -> None:
    """MIGRATION: previously ran inline off every ConversationText event
    (which no longer exists). Now runs off finalized user transcripts
    only — that's the audio actually coming from the far end of the
    call, which is what voicemail/IVR phrasing would appear in.

    FIX (bug: voicemail/IVR "hangup" didn't actually end the call): this
    used to just close our own media-stream websocket. That does NOT end
    the underlying phone call — Plivo's <Stream keepCallAlive="true">
    (set so a dropped WS mid-setup doesn't kill the call) keeps the PSTN
    leg up in silence regardless, exactly the same failure mode
    _perform_hangup() (audio.py) was already fixed for on every OTHER
    hangup path. A detected voicemail/IVR call was sitting connected and
    silent, burning minutes, until something unrelated eventually ended
    it. Now routes through _perform_hangup() — the one path that issues
    the real Plivo REST hangup — same as every other ending in this
    file. Also claims s.pending_hangup atomically first, same pattern as
    every other hangup path (see _speak_goodbye_and_hangup /
    handle_fn's end_conversation branch), so this can't double-fire
    alongside a concurrent duration_guard/end_conversation close."""
    with s.lock:
        amd_done = s.amd_done
    if amd_done:
        return

    lower = user_text.lower()
    if any(p in lower for p in VOICEMAIL_PHRASES):
        outcome, reason, call_type = CallOutcome.VOICEMAIL, "voicemail phrase in transcript", CallType.VOICEMAIL
    elif "press" in lower and ("for" in lower or "to" in lower):
        outcome, reason, call_type = CallOutcome.IVR, "IVR pattern in transcript", CallType.IVR
    else:
        return

    with s.lock:
        s.call_type = call_type
        s.amd_done  = True
        already_pending  = s.pending_hangup
        s.pending_hangup = True
    if already_pending:
        clog(call_sid, f"{call_type} detected, but a hangup is already in progress elsewhere — skipping")
        return

    log_outcome(call_sid, outcome, reason)
    end_call()


def _launch_llm_turn(ws: web.WebSocketResponse, s: Session, on_turn_complete: Callable, text: str) -> None:
    """NEW (item 1) — launches the LLM turn as a TRACKED, cancellable
    future instead of true fire-and-forget. Barge-in reads s.active_llm_task
    and cancels it immediately, so an interrupted turn's LLM stream
    actually stops consuming tokens and feeding TTS instead of finishing
    in the background after the caller's already moved on.

    CHANGED: now passes ws through to on_turn_complete so it can actually
    hang up the call when end_conversation() sets s.pending_hangup — see
    llm_bridge._hangup_if_pending()."""
    fut = run_async(on_turn_complete(s, ws, text), s.loop)

    def _clear_if_current(done_fut) -> None:
        with s.lock:
            if s.active_llm_task is done_fut:
                s.active_llm_task = None
        # FIX (bug 5): done_fut.exception() was never checked, so any
        # error mid-turn (AttributeError/TypeError/etc.) silently killed
        # the coroutine — TTS just stopped mid-sentence with no log line
        # and no fallback spoken to the caller. Surface it now.
        if done_fut.cancelled():
            return
        exc = done_fut.exception()
        if exc is not None:
            log.error("[%s] on_turn_complete crashed: %r", s.call_sid, exc)

    fut.add_done_callback(_clear_if_current)
    with s.lock:
        s.active_llm_task = fut


async def _handle_barge_in_stop(
    ws: web.WebSocketResponse,
    s: Session,
    on_turn_complete: Callable = None,
    retry_text: str = None,
) -> None:
    """Bundles the three barge-in-stop actions into one awaited sequence
    so audio-stop latency (item 3) measures real completion, not just
    fire-and-forget dispatch — then schedules the false-positive check.

    FIX: Deepgram's ListenV1SpeechStarted fires on ANY detected speech
    energy, including the agent's own voice leaking back into the mic
    path (no acoustic echo cancellation in front of it) — a click, or
    line noise. Every one of those was treated as a real barge-in: the
    in-flight LLM turn got cancelled and TTS cut, unconditionally. If no
    real user speech then follows, nothing ever triggers a new turn —
    the caller just sits in dead air until the ghost-call silence timer
    eventually hangs up on them. This is exactly what the "agent says
    half a sentence then goes silent, call drops after ~N sec" report
    was: a false barge-in with nothing to recover it.

    retry_text (the user utterance the cut-off reply was answering) is
    passed in by the caller only when an actual in-flight LLM turn was
    interrupted. If no real transcript shows up in the recovery window
    and nothing has re-interrupted this generation since, replay that
    turn instead of leaving the line dead."""
    await drain_audio_queue(s.audio_queue)
    if s.stream_sid:
        await plivo_clear_audio(ws, s.stream_sid)
    await cancel_inflight_tts(s)
    metrics.record_audio_stopped(s)

    with s.lock:
        history_len_at_barge_in = len(s.history)
        gen_at_barge_in         = s.generation_id
    # FIX: a single fixed sleep before checking can't fit every
    # interruption length — a short "no" finalizes fast, but "which
    # company are you from" needs real speech time PLUS Deepgram's own
    # UTTERANCE_END_MS silence gap on top of that before it ever lands in
    # s.history. A fixed wait either cuts off longer replies too early
    # (marking them false) or makes short ones wait needlessly. Poll
    # instead: keep waiting as long as last_stt_activity_at (any interim
    # or final transcript event, see stt_bridge.py's ListenV1Results
    # handler) keeps advancing — that's real, ongoing speech, not
    # silence. Only give up once genuinely nothing has been heard for
    # _FALSE_BARGE_IN_IDLE_GRACE_S, bounded by a hard ceiling so a caller
    # is never left in dead air indefinitely on some edge case.
    barge_in_ts = time.time()
    deadline    = barge_in_ts + _FALSE_BARGE_IN_MAX_WAIT_S
    had_transcript = False
    still_same_gen = True
    while True:
        await asyncio.sleep(0.25)
        with s.lock:
            had_transcript = len(s.history) > history_len_at_barge_in
            still_same_gen = s.generation_id == gen_at_barge_in
            last_activity  = s.last_stt_activity_at
        if had_transcript or not still_same_gen:
            return   # real speech landed (or something else already superseded this) — nothing to retry
        now = time.time()
        heard_recently = last_activity is not None and last_activity >= barge_in_ts and (now - last_activity) < _FALSE_BARGE_IN_IDLE_GRACE_S
        if heard_recently and now < deadline:
            continue   # still actively talking — keep waiting
        if now - barge_in_ts >= _FALSE_BARGE_IN_MIN_WAIT_S:
            break      # genuinely quiet for a while, or hit the ceiling — safe to decide now
    metrics.record_barge_in_outcome(s, had_transcript)

    if not had_transcript and still_same_gen and retry_text and on_turn_complete is not None:
        clog(s.call_sid, "false barge-in (no speech followed) — retrying interrupted reply")
        _launch_llm_turn(ws, s, on_turn_complete, retry_text)


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

    def end_call():
        # NEW — used by _check_amd_ivr_voicemail. Real Plivo REST hangup
        # + ws.close() (via _perform_hangup), not just a bare ws.close() —
        # see _check_amd_ivr_voicemail's docstring for why a bare
        # ws.close() alone was never enough to actually end the call.
        run(_perform_hangup(ws, call_sid))

    interim_buf = ""
    try:
        for msg in socket:
            if isinstance(msg, ListenV1SpeechStarted):
                with s.lock:
                    currently_speaking = s.agent_speaking
                    call_ending        = s.pending_hangup
                if call_ending:
                    # FIX (bug: end_conversation goodbye barge-in-cancelled,
                    # call never actually hangs up — the core live-call
                    # report behind this fix): once end_conversation() has
                    # set s.pending_hangup=True, the caller saying literally
                    # anything back — "ok", "bye", "thanks" — while the
                    # goodbye line is still playing used to register as a
                    # real barge-in below: task.cancel() on the in-flight
                    # on_turn_complete, which raises CancelledError INSIDE
                    # llm_bridge._hangup_if_pending()'s flush-wait — i.e.
                    # BEFORE _perform_hangup() is ever reached. The goodbye
                    # gets cut off mid-sentence AND the call never hangs up
                    # on this turn — pending_hangup is left True with
                    # nothing left to act on it until the 20s
                    # _end_conversation_watchdog backstop fires. The call
                    # is ending either way — ignore any further caller
                    # speech and let the goodbye + hangup proceed
                    # undisturbed instead of treating it as something to
                    # recover from.
                    clog(call_sid, "speech detected during pending hangup — ignoring, call is ending")
                    continue
                if currently_speaking:
                    # NEW — debounce before committing to a disruptive
                    # cut. See _BARGE_IN_DEBOUNCE_S above for why.
                    time.sleep(_BARGE_IN_DEBOUNCE_S)
                    with s.lock:
                        sustained = s._local_speech_onset_ts is not None
                    if not sustained:
                        clog(call_sid, "barge-in debounce: energy didn't sustain — ignoring, no cut")
                        continue

                # REPLACES "UserStartedSpeaking" — same barge-in logic, new trigger
                with s.lock:
                    s.generation_id += 1
                    gen_id            = s.generation_id
                    s.agent_speaking  = False
                    s.silence_seconds = 0.0
                    task              = s.active_llm_task     # NEW (item 1)
                    s.active_llm_task = None
                    # FIX — capture what the interrupted reply was
                    # answering, only when a real in-flight turn existed,
                    # so a false barge-in (echo/noise, see
                    # _handle_barge_in_stop) can retry it below instead
                    # of leaving the call in dead air.
                    retry_text = None
                    if task is not None:
                        for turn in reversed(s.history):
                            if turn["role"] == "user":
                                retry_text = turn["text"]
                                break
                    if s.call_type == CallType.UNKNOWN:
                        s.call_type = CallType.HUMAN
                        s.amd_done  = True

                if task is not None and not task.done():
                    # NEW (item 1) — the biggest improvement: stop the
                    # in-flight LLM stream right now instead of letting
                    # it keep running (and keep feeding sentences to TTS
                    # for a turn the caller just talked over).
                    task.cancel()

                metrics.record_barge_in_detected(s)   # NEW (item 3)
                run(_handle_barge_in_stop(ws, s, on_turn_complete, retry_text))   # NEW (item 3) — was 3 separate fire-and-forget calls
                clog(call_sid, f"barge-in gen={gen_id}")

            elif isinstance(msg, ListenV1Results):
                alt = msg.channel.alternatives[0] if msg.channel.alternatives else None
                if not alt or not alt.transcript:
                    continue
                with s.lock:
                    s.last_stt_activity_at = time.time()
                if msg.is_final:
                    interim_buf += (" " + alt.transcript).strip()
                if msg.speech_final:
                    # REPLACES ConversationText(role=user) + Deepgram's own turn-end
                    text = interim_buf.strip()
                    interim_buf = ""
                    if not text:
                        continue
                    with s.lock:
                        # FIX (same live-call end_conversation bug as the
                        # SpeechStarted guard above): don't launch a fresh
                        # LLM turn once the call is already ending —
                        # nothing cancels the ORIGINAL turn that's mid
                        # goodbye/hangup here (only a real barge-in does,
                        # and that path is now guarded off too), so
                        # without this a second on_turn_complete would run
                        # concurrently with it: overlapping TTS audio,
                        # possibly its own competing hangup. The call is
                        # ending — nothing the caller says now changes that.
                        call_ending = s.pending_hangup
                    if call_ending:
                        clog(call_sid, "transcript finalized during pending hangup — ignoring, call is ending")
                        continue
                    with s.lock:
                        s.history.append({"role": "user", "text": text})
                    _check_amd_ivr_voicemail(call_sid, s, text, end_call)
                    _launch_llm_turn(ws, s, on_turn_complete, text)     # NEW (item 1) — tracked, cancellable

            elif isinstance(msg, ListenV1UtteranceEnd):
                # backstop if speech_final never fired (noisy line, etc.)
                text = interim_buf.strip()
                interim_buf = ""
                if text:
                    with s.lock:
                        call_ending = s.pending_hangup   # FIX — same guard as speech_final above
                    if call_ending:
                        clog(call_sid, "utterance-end during pending hangup — ignoring, call is ending")
                        continue
                    with s.lock:
                        s.history.append({"role": "user", "text": text})
                    _check_amd_ivr_voicemail(call_sid, s, text, end_call)
                    _launch_llm_turn(ws, s, on_turn_complete, text)

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