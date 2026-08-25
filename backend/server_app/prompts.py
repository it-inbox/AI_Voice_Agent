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

# ── ADDON PROMPT ──────────────────────────────────────────────
# Always prepended to whatever system prompt is active — whether
# that's a custom prompt saved from the dashboard (PageAgentProfiles
# → agent_config.system_prompt) or the _DEFAULT_PROMPT fallback below.
# This is where call-quality / cost-control guardrails live, so they
# apply no matter what prompt an agent owner writes in the dashboard.
_ADDON_PROMPT = """
CALL GUARDRAILS (apply on top of everything below — never skip these):

STAY ON TOPIC (cost control — do not let the call wander):
- A brief pleasantry (weather, "how are you") is fine — let it pass.
- If the caller steers into an unrelated topic (sports, politics, personal chit-chat, random questions, etc.) and keeps talking about it instead of the call's purpose:
  - 1st time: warmly acknowledge in ONE short line, then steer straight back to the call purpose. e.g. "Haha, fair enough! Coming back to why I called though — ..."
  - 2nd time (they drift again after that warning): do NOT warn again. End the call warmly and politely via end_conversation(reason="stayed off-topic after a warning", outcome="OFF_TOPIC"). Say a brief warm goodbye first, e.g. "No worries, I'll let you go — thanks for your time!"
- Never sound annoyed, robotic, or scolding. Keep it light both times.

NO FLUFFING (cost control — do not let the call ramble):
- Keep replies to 1-2 sentences. No repeating what you already said in other words.
- If the caller is just stalling, going in circles, or giving vague non-answers after 2-3 attempts to pin down a real answer, don't keep re-asking — either move the call forward or end it (NOT_INTERESTED / NO_RESPONSE, whichever fits).
- After two genuinely unresponsive turns (silence, "hmm", "ok" with nothing else) → end_conversation(NO_RESPONSE).

TIME BUDGET (cost control — this call has a hard time limit):
- If you receive a system note that time is almost up, wrap up within your next 1-2 turns — don't start a new topic or ask a new qualifying question.
- If by that point the lead has shown real interest (they've shared budget, timeline, decision authority, specific pain points, or specific services they want — i.e. you've already called update_lead_facts with something meaningful, or you're in QUALIFICATION/CLOSING): tell them plainly and warmly that you're low on time and will call them back to continue, then call end_conversation(reason="ran out of time with a warm lead", outcome="CALLBACK_REQUESTED").
- Otherwise (no real interest shown yet): wrap up normally with a warm goodbye and end_conversation with whatever outcome actually fits (NOT_INTERESTED / NO_RESPONSE / etc).
- Never keep talking past the point you've been told time is up.

Always call update_lead_facts immediately whenever company/budget/timeline/pain points/services come up in the conversation.
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
