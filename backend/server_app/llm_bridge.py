"""
MIGRATION: NEW FILE — Phase 2.

Owns the LLM call directly. Runs on Groq (AsyncGroq, model=GROQ_MODEL /
openai/gpt-oss-120b by default — same client already used for lead
extraction in call_handler_app, now also driving the live conversation).
This is the leg that reads the STT transcript and decides what to say —
previously proxied through Deepgram's ThinkSettingsV1Provider passthrough,
now called straight against our own GROQ_API_KEY.

Streams sentence-by-sentence to TTS so latency stays acceptable — waiting
for the full completion before speaking would add dead air per turn that
the Voice Agent API used to hide via internal pipelining. This is the
single riskiest quality regression in the whole migration; if replies
start feeling sluggish, look here first.
"""

import json
from typing import Any, Dict, List

from .config import FUNCTIONS, GROQ_MODEL, groq_client, log
from .session import Session, clog
from .stt_bridge import handle_fn
from .tts_bridge import send_to_tts

_TOOLS = [{"type": "function", "function": f} for f in FUNCTIONS]
_MAX_TOOL_HOPS = 4   # guard against a pathological tool-call loop

# NEW — two paths used to leave the caller in dead silence with zero
# recovery: a Groq API error (`except Exception: return`, nothing spoken),
# and exhausting all _MAX_TOOL_HOPS without the model ever producing a
# non-tool-call reply (final_text stays "", loop just ends). On a live
# cold-outbound call that reads as a frozen/dropped call to the person on
# the other end — worse than an honest "having trouble" line. Both now
# fall through to this instead of silently returning.
_FALLBACK_LINE = "Sorry, I'm having a little trouble right now — could you say that one more time?"


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


def _build_messages(s: Session) -> List[Dict[str, Any]]:
    with s.lock:
        system_prompt = s.system_prompt
        history       = list(s.history)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for turn in history:
        messages.append({"role": turn["role"], "content": turn["text"]})
    return messages


async def on_turn_complete(s: Session, user_text: str) -> None:
    """Called by stt_bridge.stt_listener_thread once a user turn finishes
    (STT has handed off a finalized transcript). user_text has already
    been appended to s.history by the caller — this is where the LLM
    actually reads/understands it and produces a reply.

    NEW (item 1): this coroutine now runs as a tracked, cancellable task
    (see stt_bridge._launch_llm_turn / s.active_llm_task) — a barge-in
    mid-turn calls task.cancel(), which raises asyncio.CancelledError at
    whichever await is currently in flight (the Groq stream call or the
    `async for chunk in stream` loop below). CancelledError is a
    BaseException, not an Exception, so the `except Exception` block
    right below can't accidentally swallow it — cancellation always
    propagates and stops this turn cleanly, without appending a partial
    reply to history."""
    call_sid = s.call_sid
    messages = _build_messages(s)

    final_text = ""

    for hop in range(_MAX_TOOL_HOPS):
        try:
            stream = await groq_client.chat.completions.create(
                model=GROQ_MODEL,   # MIGRATION: openai/gpt-oss-120b by default, called directly on Groq
                messages=messages,
                tools=_TOOLS,
                stream=True,
            )
        except Exception as e:
            log.error("[%s] llm call error: %s", call_sid, e)
            await send_to_tts(s, _FALLBACK_LINE)   # NEW — was a silent `return`, dead air on the call
            with s.lock:
                s.history.append({"role": "assistant", "text": _FALLBACK_LINE})
            return

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
        final_text = turn_text
        break

    if final_text.strip():
        with s.lock:
            s.history.append({"role": "assistant", "text": final_text.strip()})
    else:
        # NEW — loop exhausted all _MAX_TOOL_HOPS attempts without the
        # model ever giving a spoken reply (kept calling tools every hop).
        # Previously silent; now log it as the anomaly it is (a healthy
        # conversation shouldn't hit this) and still say something.
        log.error("[%s] tool-call hop limit (%d) reached with no spoken reply", call_sid, _MAX_TOOL_HOPS)
        await send_to_tts(s, _FALLBACK_LINE)
        with s.lock:
            s.history.append({"role": "assistant", "text": _FALLBACK_LINE})
