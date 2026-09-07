"""
Owns the LLM call directly. Provider-agnostic: primary/fallback pair
configured via LLM_PROVIDER_PRIMARY / LLM_PROVIDER_FALLBACK (env vars,
see config.py) — default is OpenAI (gpt-4.1-mini) primary, Cerebras
(llama-3.3-70b) fallback. Both providers talk the OpenAI chat-completions
wire format (Cerebras is OpenAI-API-compatible), so the same call code
below works for either — only the client object + model string change.

This is the leg that reads the STT transcript and decides what to say.
If the primary provider errors when opening the stream (auth issue, rate
limit, model deprecated out from under us — this happened repeatedly on
Groq), we transparently retry on the fallback provider before giving up
and speaking the dead-air recovery line. Once a stream has already
started emitting tokens we don't attempt a mid-stream provider swap
(the caller would have already heard part of a reply) — only the
before-first-token failure path retries on fallback.

Streams sentence-by-sentence to TTS so latency stays acceptable — waiting
for the full completion before speaking would add dead air per turn.
"""

import asyncio
import json
import re
from typing import Any, Dict, List

from aiohttp import web

from .audio import _perform_hangup
from .config import (
    FUNCTIONS,
    HISTORY_REPLAY_TURNS,
    LLM_CLIENTS,
    LLM_MODELS,
    LLM_PROVIDER_FALLBACK,
    LLM_PROVIDER_PRIMARY,
    log,
)
from .session import Session, clog
from .stt_bridge import handle_fn
from .tts_bridge import send_to_tts

_TOOLS = [{"type": "function", "function": f} for f in FUNCTIONS]
_MAX_TOOL_HOPS = 4   # guard against a pathological tool-call loop

# NEW — two paths used to leave the caller in dead silence with zero
# recovery: an LLM API error (`except Exception: return`, nothing spoken),
# and exhausting all _MAX_TOOL_HOPS without the model ever producing a
# non-tool-call reply (final_text stays "", loop just ends). On a live
# cold-outbound call that reads as a frozen/dropped call to the person on
# the other end — worse than an honest "having trouble" line. Both now
# fall through to this instead of silently returning.
_FALLBACK_LINE = "Sorry, I'm having a little trouble right now — could you say that one more time?"

# NEW — the retry-after backoff on a 429 (either provider) can be several
# seconds under load. That's several seconds of total dead air per hit —
# which is exactly what a caller on a glitchy line reads as "the call
# dropped" / "your voice is breaking", not as "the agent is thinking". A
# short spoken filler bridges the gap so the line stays audibly alive
# during the wait.
_RATE_LIMIT_FILLER = "One moment, please."
_RATE_LIMIT_FILLER_THRESHOLD_S = 1.5   # don't bother for a short/negligible wait

# NEW (LLM rate-limiting / TPM pressure) — _build_messages used to send
# the CALLER'S ENTIRE s.history on every single hop of every turn. Token
# count for that grows the whole call: a 15-turn call sends ~15x the
# per-turn tokens on turn 15 that it did on turn 1, on top of the system
# prompt resent every hop too. update_lead_facts already persists the
# durable stuff (company/budget/timeline/pain points) into s.facts
# independent of raw history, and NO FLUFFING keeps replies short, so a
# sliding window of the most recent turns is enough context for this
# kind of qualification call — it just stops token usage from growing
# unbounded as the call goes on. *2 because HISTORY_REPLAY_TURNS counts
# user+assistant turn PAIRS, and s.history stores one entry per message.
_HISTORY_WINDOW_MESSAGES = HISTORY_REPLAY_TURNS * 2


# NEW — the naive `endswith((".", "!", "?"))` check flushed early on
# "Rs. 50,000", "Mr. Patel", "e.g. Power BI", "3.5 lakhs" — exactly the
# kind of content cold-outbound sales calls (prices, titles, abbreviated
# service names) are full of, chopping the agent's own sentences mid-
# thought. This checks the token right before the trailing punctuation.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st",
    "rs", "vs", "etc", "eg", "ie", "no", "approx",
    "ltd", "pvt", "inc", "co", "corp", "govt", "dept",
    "min", "max", "avg", "hr", "hrs", "kg", "km",
}
_MAX_SENTENCE_BUF_CHARS = 220  # safety cap — force a flush rather than
                               # let one pathological run of "abbreviations"
                               # (or the model just not using terminal
                               # punctuation) hold the whole reply hostage


def _is_real_sentence_end(buf: str) -> bool:
    stripped = buf.rstrip()
    if not stripped or stripped[-1] not in ".!?":
        return len(stripped) >= _MAX_SENTENCE_BUF_CHARS
    if stripped[-1] != ".":
        return True   # '!' / '?' are unambiguous, no abbreviation/decimal case for them
    before = stripped[:-1]
    if not before:
        return False
    if before[-1].isdigit():
        return False   # "3.5", "Rs. 2.5" — decimal in progress, not a sentence end
    words = before.split()
    last_word = words[-1].lower().replace(".", "").replace(",", "") if words else ""
    if last_word in _ABBREVIATIONS:
        return False
    return True


def _parse_retry_after(e: Exception) -> float:
    """Either provider's SDK error (RateLimitError etc.) carries the real
    httpx Response on `.response` with a `Retry-After` header when the
    server sends one — check that first. Groq's error body used to spell
    it out as text ('Please try again in 8.1525s'); keep that regex as a
    fallback in case a proxy/compatible endpoint still does the same.
    Falls back to a flat 2s if neither is present."""
    response = getattr(e, "response", None)
    if response is not None:
        header = getattr(response, "headers", {}).get("retry-after") if hasattr(response, "headers") else None
        if header:
            try:
                return min(float(header) + 0.3, 10.0)
            except ValueError:
                pass
    m = re.search(r"try again in ([\d.]+)s", str(e))
    if m:
        try:
            return min(float(m.group(1)) + 0.3, 10.0)
        except ValueError:
            pass
    return 2.0


async def _open_completion_stream(call_sid: str, messages: List[Dict[str, Any]]):
    """Try LLM_PROVIDER_PRIMARY first; on any error opening the stream,
    log it and retry once on LLM_PROVIDER_FALLBACK. Raises the fallback's
    exception (or the primary's, if primary==fallback) if both fail —
    caller turns that into the spoken _FALLBACK_LINE (or, for a 429,
    retries the whole primary/fallback pair once more first — see
    on_turn_complete)."""
    providers = [LLM_PROVIDER_PRIMARY]
    if LLM_PROVIDER_FALLBACK != LLM_PROVIDER_PRIMARY:
        providers.append(LLM_PROVIDER_FALLBACK)

    last_err: Exception | None = None
    for i, provider in enumerate(providers):
        client = LLM_CLIENTS[provider]
        model = LLM_MODELS[provider]
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=_TOOLS,
                stream=True,
            )
            if i > 0:
                log.warning("[%s] LLM primary (%s) failed, using fallback (%s)", call_sid, LLM_PROVIDER_PRIMARY, provider)
            return stream
        except Exception as e:
            last_err = e
            log.error("[%s] llm call error on provider=%s: %s", call_sid, provider, e)
    raise last_err


def _build_messages(s: Session) -> List[Dict[str, Any]]:
    with s.lock:
        system_prompt = s.system_prompt
        history       = list(s.history)
    # NEW — bound token growth over a long call, see comment above.
    if len(history) > _HISTORY_WINDOW_MESSAGES:
        history = history[-_HISTORY_WINDOW_MESSAGES:]
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for turn in history:
        messages.append({"role": turn["role"], "content": turn["text"]})
    return messages


async def _hangup_if_pending(ws: web.WebSocketResponse, s: Session, spoken_text: str = "") -> None:
    """NEW — the missing other half of end_conversation(). handle_fn()
    (stt_bridge.py) only ever set s.pending_hangup = True; nothing in the
    live call path (this file, stt_bridge.py, routes.py) ever read that
    flag back out, so the flag did nothing and the call just kept
    running until the caller hung up or the hard duration_guard backstop
    (4 min) eventually fired. This is called once per turn — from EVERY
    exit path of on_turn_complete now, not just the happy path (see FIX
    note there) — right after whatever the model had to say has been
    queued to TTS, and actually ends the call when the flag is set.

    FIX (12s to hang up): est_speak_s used to be a flat 2.0s guess no
    matter how long the actual line was, stacked on top of the flush-wait
    + grace below. Estimate from the real spoken text length instead
    (same formula duration_guard already uses) so a short "Bye!" doesn't
    wait as long as a full goodbye sentence."""
    with s.lock:
        pending = s.pending_hangup
    if not pending:
        return

    clog(s.call_sid, "pending_hangup set — ending call")
    est_speak_s = max(1.2, len(spoken_text.split()) / 2.5) if spoken_text.strip() else 2.0
    if s.tts_flushed_event:
        try:
            await asyncio.wait_for(s.tts_flushed_event.wait(), timeout=est_speak_s + 3.0)
        except asyncio.TimeoutError:
            clog(s.call_sid, "end_conversation flush wait timed out — hanging up anyway")
        await asyncio.sleep(0.4)   # grace for last audio chunk(s) to reach Plivo
    else:
        await asyncio.sleep(est_speak_s)
    await _perform_hangup(ws, s.call_sid)


async def on_turn_complete(s: Session, ws: web.WebSocketResponse, user_text: str) -> None:
    """Called by stt_bridge.stt_listener_thread once a user turn finishes
    (STT has handed off a finalized transcript). user_text has already
    been appended to s.history by the caller — this is where the LLM
    actually reads/understands it and produces a reply.

    This coroutine runs as a tracked, cancellable task (see
    stt_bridge._launch_llm_turn / s.active_llm_task) — a barge-in
    mid-turn calls task.cancel(), which raises asyncio.CancelledError at
    whichever await is currently in flight (the LLM stream call or the
    `async for chunk in stream` loop below). CancelledError is a
    BaseException, not an Exception, so nothing in this function can
    accidentally swallow it — cancellation always propagates straight out
    and stops this turn cleanly, without appending a partial reply to
    history and WITHOUT running the tail below (agent_speaking reset /
    hangup check). That's intentional: a barge-in means the caller is
    mid-sentence, not that the call should end.

    FIX (end_conversation production-readiness): the hard-failure path
    below used to `return` directly out of the coroutine. If the model
    had already called end_conversation() on an earlier hop this turn
    (s.pending_hangup = True) and THEN the LLM call errored out while
    trying to produce the goodbye line, the function exited before ever
    reaching the hangup check at the bottom — pending_hangup stayed True
    forever with nothing left to read it, and the call just sat connected
    until the 4-minute hard duration_guard backstop eventually killed it.
    Now a hard failure sets `hard_fail` and breaks out of the hop loop
    instead of returning, so the common tail — agent_speaking reset AND
    the hangup check — always runs on every non-cancelled path."""
    call_sid = s.call_sid
    messages = _build_messages(s)

    # FIX (ghost-call hanging up right after the agent finishes talking):
    # `_should_analyze()` in audio.py gates ALL ghost-call/AMD silence
    # accumulation behind `not s.agent_speaking` — but this flag was only
    # ever set True once the first sentence actually reached TTS
    # (tts_bridge.send_to_tts). Every second spent HERE, waiting on the
    # LLM call (including 429 backoff — can be several seconds under
    # load), was being counted as CALLER silence, because agent_speaking
    # was still False. So a slow/rate-limited reply could push
    # s.silence_seconds right up near the ghost-call threshold before the
    # agent had said a word — and then the instant the turn finished and
    # agent_speaking flipped back False at the bottom of this function,
    # the very next quiet frame from the caller (who'd just been asked a
    # question and hadn't answered yet) tipped it over the line and fired
    # ghost_call_hangup — reading exactly like "the call ended the moment
    # the agent went silent." Setting this True here, at the start of
    # processing rather than at the start of speaking, means the whole
    # thinking+speaking window is correctly excluded from silence
    # accounting — that time is the agent's own latency, not the caller
    # failing to respond.
    with s.lock:
        s.agent_speaking = True

    final_text = ""
    hard_fail  = False
    # FIX (root cause of "are you there?" firing right after the agent's
    # own sentence, and of the agent getting cut off mid-word): every
    # send_to_tts() call below returns as soon as the TEXT is handed to
    # Deepgram's Speak socket — NOT once the audio has actually been
    # generated and played out to the caller over the phone line. That
    # real playback takes real wall-clock time (several seconds for a
    # normal sentence), happening asynchronously via
    # tts_bridge._tts_listener_thread -> s.audio_queue -> Plivo. Track
    # everything actually sent to TTS this turn so we can wait for that
    # real playback to finish (see the flush-wait block below, right
    # before agent_speaking is cleared) instead of clearing agent_speaking
    # — and re-arming ghost-call silence detection + barge-in debounce —
    # while the agent can still be audibly mid-sentence.
    total_spoken_text = ""
    looped_out = True   # NEW — becomes False the instant we hit a normal
                         # (non-tool-call) hop exit, whether or not that
                         # hop actually had anything to say. See the FIX
                         # below for why this distinction matters.

    for hop in range(_MAX_TOOL_HOPS):
        stream = None
        for retry in range(2):   # NEW — one retry for transient 429s only; not a fix for the TPM ceiling itself
            try:
                stream = await _open_completion_stream(call_sid, messages)
                break
            except Exception as e:
                status = getattr(e, "status_code", None) or getattr(e, "status", None)
                # FIX — OpenAI (and Groq) both return HTTP 429 for TWO very
                # different situations: a transient per-minute rate limit
                # (worth a short wait + retry — that's what the filler +
                # sleep below is for) vs a permanently exhausted quota /
                # no billing on the account (`code == "insufficient_quota"`
                # in the error body) — retrying that is pointless, it will
                # fail identically every time until billing is fixed. Left
                # undistinguished, a dead-quota key meant EVERY turn on
                # EVERY call spoke "One moment, please." and waited out a
                # pointless backoff before falling back to Groq anyway —
                # extra dead-feeling latency on every single turn, for a
                # condition retrying can never fix.
                error_code = getattr(e, "code", None) or getattr(getattr(e, "body", None), "get", lambda *_: None)("code")
                is_quota_dead = status == 429 and error_code == "insufficient_quota"
                if is_quota_dead:
                    log.error("[%s] LLM provider quota exhausted (insufficient_quota) — skipping retry, add billing/upgrade tier", call_sid)
                if status == 429 and not is_quota_dead and retry == 0:
                    wait_s = _parse_retry_after(e)
                    log.warning("[%s] llm rate-limited, retrying in %.1fs", call_sid, wait_s)
                    if wait_s >= _RATE_LIMIT_FILLER_THRESHOLD_S:
                        await send_to_tts(s, _RATE_LIMIT_FILLER)   # NEW — bridge the dead air, see comment above
                        total_spoken_text += " " + _RATE_LIMIT_FILLER
                    await asyncio.sleep(wait_s)
                    continue
                # both primary and fallback provider failed to open a stream
                await send_to_tts(s, _FALLBACK_LINE)   # was a silent `return`, dead air on the call
                total_spoken_text += " " + _FALLBACK_LINE
                with s.lock:
                    s.history.append({"role": "assistant", "text": _FALLBACK_LINE})
                hard_fail = True
                break
        if hard_fail:
            break
        if stream is None:
            hard_fail = True
            break

        sentence_buf   = ""
        turn_text      = ""
        tool_calls_acc: Dict[int, Dict[str, Any]] = {}

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    entry = tool_calls_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["name"] += tc.function.name
                        if tc.function.arguments:
                            entry["arguments"] += tc.function.arguments

            if delta.content:
                sentence_buf += delta.content
                turn_text    += delta.content
                if _is_real_sentence_end(sentence_buf):
                    await send_to_tts(s, sentence_buf)     # stream sentence-by-sentence, don't wait for full completion
                    total_spoken_text += sentence_buf
                    sentence_buf = ""

        if tool_calls_acc:
            # Any partial trailing sentence before the tool call isn't
            # spoken — the model will produce its real reply after seeing
            # the tool result on the next hop.
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": turn_text or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in tool_calls_acc.values()
                ],
            }
            messages.append(assistant_msg)

            for tc in tool_calls_acc.values():
                clog(call_sid, f"fn: {tc['name']}")
                result = handle_fn(tc["name"], tc["arguments"], s, call_sid)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result),
                })
            # loop again — model needs another completion call to react to the tool result(s)
            continue

        # No tool calls this hop — flush any trailing partial sentence and stop.
        if sentence_buf.strip():
            await send_to_tts(s, sentence_buf)
            total_spoken_text += sentence_buf
        final_text = turn_text
        looped_out = False
        break

    if not hard_fail:
        if final_text.strip():
            with s.lock:
                s.history.append({"role": "assistant", "text": final_text.strip()})
        else:
            with s.lock:
                pending = s.pending_hangup
            if pending:
                # FIX (bug: confusing "could you repeat that?" line spoken
                # right after a clean end_conversation goodbye, on live
                # calls): the model routinely returns a genuinely EMPTY
                # completion on the hop immediately after a successful
                # end_conversation() tool call — there's nothing left to
                # say, the goodbye was already spoken as content earlier
                # in the SAME hop the tool was called in (content deltas
                # are flushed to TTS as they stream, before the tool-call
                # branch above is ever reached). This used to be treated
                # identically to genuinely exhausting all _MAX_TOOL_HOPS
                # without a reply — logged as an error and spoke
                # _FALLBACK_LINE right after the goodbye. That's not just
                # a confusing thing to hear; it invites the caller to
                # reply, which barge-in-cancels THIS turn before the
                # hangup check at the tail below ever runs (barge-in
                # during a call that's already ending is now ignored
                # separately in stt_bridge.py's SpeechStarted handler,
                # but this is the actual root cause: don't invite a
                # reply the call has no intention of waiting for).
                # Nothing more to say — let the tail below hang up.
                clog(call_sid, "empty final reply after end_conversation — call is ending, nothing more to say")
            elif looped_out:
                # Genuine hop-limit exhaustion: every single hop called a
                # tool, the model never got a hop to just speak plainly.
                log.error("[%s] tool-call hop limit (%d) reached with no spoken reply", call_sid, _MAX_TOOL_HOPS)
                await send_to_tts(s, _FALLBACK_LINE)
                total_spoken_text += " " + _FALLBACK_LINE
                with s.lock:
                    s.history.append({"role": "assistant", "text": _FALLBACK_LINE})
                final_text = _FALLBACK_LINE
            else:
                # Model naturally finished this turn with an empty reply
                # and the call ISN'T ending — genuinely unusual, still
                # worth a recovery line so the caller isn't left in dead air.
                log.warning("[%s] model returned an empty reply outside a tool-call loop", call_sid)
                await send_to_tts(s, _FALLBACK_LINE)
                total_spoken_text += " " + _FALLBACK_LINE
                with s.lock:
                    s.history.append({"role": "assistant", "text": _FALLBACK_LINE})
                final_text = _FALLBACK_LINE

    # FIX (root cause — this is the actual fix, the docstring further up
    # was only the setup for it): don't clear agent_speaking the instant
    # the last send_to_tts() call returns — that's text-handoff time, not
    # playback-finished time. Wait for Deepgram to confirm it's done
    # GENERATING all of it (tts_flushed_event), then add a short estimate
    # for the remaining real-world PLAYBACK time over the phone line
    # (Plivo has no "audio finished playing" ack to wait on directly —
    # same word-count/2.5-wps estimate already used for the goodbye lines
    # in duration_guard/_hangup_if_pending). Skipped entirely when
    # pending_hangup is set: _should_analyze() (audio.py) already
    # excludes that case on its own, and _hangup_if_pending() below does
    # its own equivalent flush-wait right after this — no need to wait
    # twice.
    with s.lock:
        pending_hangup_now = s.pending_hangup
    if total_spoken_text.strip() and not pending_hangup_now:
        est_speak_s = max(1.0, len(total_spoken_text.split()) / 2.5)
        if s.tts_flushed_event:
            try:
                await asyncio.wait_for(s.tts_flushed_event.wait(), timeout=est_speak_s + 3.0)
            except asyncio.TimeoutError:
                clog(call_sid, "tts flush wait timed out — clearing agent_speaking on estimate instead")
            await asyncio.sleep(0.4)   # grace for the last audio chunk(s) to actually reach Plivo
        else:
            await asyncio.sleep(est_speak_s)

    # FIX (bug: ghost-call silence detection permanently disabled after
    # the agent's first line): s.agent_speaking was set True by every
    # send_to_tts() call but was ONLY ever set back False on a barge-in
    # (stt_bridge.py ListenV1SpeechStarted handler). A turn that finished
    # normally — no interruption — never cleared it. audio.py's
    # _should_analyze() and _process_media() both gate ghost-call/AMD
    # silence checks behind `not s.agent_speaking`, so once the agent
    # spoke its first line, those checks were dead for the rest of the
    # call. Reset it here, on every non-cancelled exit from this turn —
    # normal completion or hard failure alike — so silence detection
    # resumes for the caller's next turn, only once the agent is actually
    # done being audible (see the wait immediately above).
    with s.lock:
        s.agent_speaking = False

    # NEW — actually act on end_conversation(). See _hangup_if_pending().
    # Runs on the hard-failure path too now (see docstring above), and
    # passes what was actually spoken so the hangup wait is sized to it
    # instead of a flat guess.
    await _hangup_if_pending(ws, s, final_text)