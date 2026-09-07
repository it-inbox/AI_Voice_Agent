"""
System prompt composition (addon guardrails + dashboard/default base) and
raw Deepgram Listen (STT) settings builder.

MIGRATION NOTE: build_dg_settings() / build_recovery_settings() (Deepgram
Voice Agent settings + reconnect-recovery prompt injection) are REMOVED.
There is no more single "agent session" object to rebuild — STT, LLM, and
TTS are three independent connections now. STT reconnects with a plain
settings dict (build_stt_settings, no prompt/history involved). The LLM
side already rebuilds its message list from s.history + s.system_prompt
on every turn, so no special recovery path is needed there either.
"""

from typing import Optional

from .config import HISTORY_REPLAY_TURNS, STT_ENDPOINTING_MS, UTTERANCE_END_MS  # noqa: F401  (HISTORY_REPLAY_TURNS kept for future recap use)
from .session import Session  # noqa: F401  (kept for type hints elsewhere)

# NEW — explicit tool-use policy. `update_call_state` is gone from
# FUNCTIONS (config.py) entirely, but the model was also reaching for
# update_lead_facts / end_conversation reflexively on plain conversational
# turns (greetings, acknowledgements, clarifying questions) instead of
# just answering — each one still burns a tool-call hop out of
# _MAX_TOOL_HOPS before the model ever speaks. Stated up front, ahead of
# the cost-control rules below, so it governs every tool call, not just
# the state-tracking one that got removed.
_TOOL_USE_POLICY = """
TOOL USE POLICY:
Only call a tool for a real action/data update. For greetings, acks,
clarifications, and plain conversational replies, just speak — no tool.
If you need more than one tool this turn (e.g. update_lead_facts +
end_conversation), call them together in the SAME reply, not spread
across turns — every extra hop costs a full LLM call.
""".strip()

# ── ADDON PROMPT ──────────────────────────────────────────────
# Always prepended to whatever system prompt is active — whether
# that's a custom prompt saved from the dashboard (PageAgentProfiles
# → agent_config.system_prompt) or the _DEFAULT_PROMPT fallback below.
# This is where call-quality / cost-control guardrails live, so they
# apply no matter what prompt an agent owner writes in the dashboard.
#
# FIX (LLM rate-limiting / TPM pressure): this whole block gets resent
# as the system message on EVERY hop of EVERY turn (see _build_messages
# in llm_bridge.py) — it was ~1000 tokens, so a 2-hop turn alone burned
# 2000 tokens on guardrail text before a word of the actual call.
# Condensed to the same rules, denser wording — same behavior, roughly
# half the tokens.
_ADDON_PROMPT = f"""
CALL GUARDRAILS (apply on top of everything below — never skip these):

HARD STOP — CALLER WANTS OFF THE CALL (highest priority — overrides every other rule here, TOOL USE POLICY included):
- The instant the caller unambiguously asks to end the call ("end the call", "hang up", "I have to go", "stop calling me", "not interested, bye") — stop pitching, don't ask another question, don't try to recover it, don't just apologize and keep going.
- FIRST time they ask: confirm, don't end yet. Say ONE short line asking them to confirm ("Sure thing — should I go ahead and end the call now?") and STOP there — no tool call yet.
- Once they confirm on their next reply (a "yes", "bye", "go ahead", or anything else affirming) — OR if they ask a second time — say ONE short warm line ("Of course, take care!") and call end_conversation in that SAME reply — DO_NOT_CALL if told not to call again, NOT_INTERESTED if declining, else CALL_DROPPED.
- Act on the confirmed end immediately even if the audio's been glitchy or unclear — never argue with a clear end request.

{_TOOL_USE_POLICY}

STAY ON TOPIC: a brief pleasantry is fine. If the caller derails into an unrelated topic and keeps at it: 1st time, one warm line then steer back ("Coming back to why I called though — ..."). 2nd time, don't warn again — end_conversation(reason="stayed off-topic after a warning", outcome="OFF_TOPIC") after a brief warm goodbye. Never sound annoyed or scolding.

NO FLUFFING: 1-2 sentence replies, no restating yourself. After 2-3 vague/stalling non-answers, move the call forward or end it (NOT_INTERESTED/NO_RESPONSE). Two genuinely unresponsive turns in a row → end_conversation(NO_RESPONSE).

TIME BUDGET: on a "time almost up" system note, don't just announce you're wrapping up — ASK permission first, one short line, then end on their reply. Warm lead already qualified (budget/timeline/pain-point captured, or in QUALIFICATION/CLOSING) → tell them you're low on time and ask if it's okay to call them back, THEN end_conversation(outcome="CALLBACK_REQUESTED") on their reply (or right away if they answer in the same breath). Otherwise ask if it's alright to wrap up here, then end with whatever outcome fits on their reply. If a "time's completely up, end the call now" note ever arrives, that's a hard stop — end immediately, no asking, whatever's been said stands.

Call update_lead_facts immediately whenever company/budget/timeline/pain points/services come up.

QUALIFIED HOT LEAD: once budget + timeline + at least one pain point/interested service are captured, stop digging. Say it's a great fit and ASK permission to wrap up ("Sounds like a great fit — is it alright if I have the team follow up with you?"). Only call end_conversation(reason="qualified lead - budget/timeline/pain points captured", outcome="INTERESTED") once they've agreed — on their next reply, or right away if they clearly say yes/sure/sounds good in that same breath. Never call the tool before permission is given.
""".strip()

# ── DEFAULT PROMPT ───────────────────────────────────────────
# Fallback ONLY — used when the dashboard has no system_prompt saved
# for this agent (cfg.system_prompt is empty). Normal operation is
# expected to always supply a prompt from the dashboard now.
_DEFAULT_PROMPT = """
You are a friendly outbound sales caller from Inbox Infotech. Agent name: {agent_name}.
Speak naturally on a real-time phone call. Warm, not pushy.

SERVICES: AI/ML | IoT | CRM/ERP | Mobile & Web | Cloud & DevOps | API Integration | Automation

GOAL:
Greet {lead_name}, check it's a good time to talk, understand their biggest tech challenge,
qualify budget/timeline/decision authority, and if it's a good fit propose a short discovery
call or demo. If not a fit, or they're busy/DNC/wrong number, end the call with the matching
outcome via end_conversation.
""".strip()


def build_system_prompt(dashboard_prompt: str, agent_name: str, lead_name: Optional[str]) -> str:
    """Compose the final system prompt: ADDON guardrails + the active
    base (dashboard-supplied custom prompt, or _DEFAULT_PROMPT as
    fallback when the dashboard hasn't set one for this agent)."""
    base = (dashboard_prompt or "").strip() or _DEFAULT_PROMPT.format(
        agent_name=agent_name,
        lead_name=lead_name or "the decision maker",
    )
    return f"{_ADDON_PROMPT}\n\n---\n\n{base}"


def build_greeting(lead_name: Optional[str]) -> str:
    # No hardcoded fallback name. When no real customer name is available
    # for this call, use the same generic unknown-lead line as
    # VERIFY_IDENTITY in _DEFAULT_PROMPT, instead of a fake name.
    if lead_name:
        return f"Hello, may I please speak with {lead_name}?"
    return "Hello, is this a good time to speak with you?"


def build_stt_settings() -> dict:
    """MIGRATION: NEW — replaces build_dg_settings(). Raw Deepgram Listen
    (STT-only) connection settings. Turn-detection (utterance_end_ms /
    endpointing) is now tuned explicitly here — the Voice Agent API did
    this internally and it was never exposed as a knob before."""
    return dict(
        model="nova-3",
        encoding="mulaw",
        sample_rate=8000,
        interim_results=True,
        utterance_end_ms=UTTERANCE_END_MS,
        endpointing=STT_ENDPOINTING_MS,
        vad_events=True,          # replaces Voice Agent's UserStartedSpeaking
        smart_format=True,
    )