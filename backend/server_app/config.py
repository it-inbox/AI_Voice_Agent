"""
Deepgram Voice Agent — Plivo Audio Streaming
Python 3.10+ | Deepgram SDK v5+ | aiohttp v3+ | plivo SDK v4+

Endpoints:
  POST /plivo/answer  — Plivo Answer URL webhook (returns Record+Stream XML)
  GET  /ws/plivo      — Plivo Audio Streaming WebSocket
  GET  /health        — Health check
  GET  /ping          — Liveness probe

Audio format stays audio/x-mulaw;rate=8000 end-to-end (same as the old
Telnyx setup) so the Deepgram Voice Agent settings, AMD/ghost-call RMS
detection, and audio queue plumbing below are UNCHANGED from the Telnyx
version — only the WS transport envelope, webhook/XML, and hangup
mechanics are Plivo-specific (each change point is commented CHANGED/NEW
inline where it happens).

This module: env vars, constants, enums, and third-party client setup.
Everything else in server_app/ imports config for these.
"""

import logging
import os
from enum import Enum
from typing import Dict, List

import plivo
from deepgram import DeepgramClient
from dotenv import load_dotenv
from groq import AsyncGroq   # kept for lead-scoring leg (call_handler_app) — NOT used for live-call LLM anymore
from openai import AsyncOpenAI   # NEW — used for both OpenAI and Cerebras (Cerebras is OpenAI-API-compatible via base_url)

# FIX: load_dotenv() used to run AFTER logging.basicConfig() below, but
# basicConfig reads os.getenv("LOG_LEVEL", ...) — .env hadn't been loaded
# into the environment yet at that point, so LOG_LEVEL always silently
# fell back to the "WARNING" default no matter what .env actually said.
# Every clog()/log.info() call in the whole voice pipeline (barge-in,
# ghost-warning, outbound-call-placed, hangup handling, etc.) was
# invisible as a result. Moved above basicConfig so the env var is
# actually available when read.
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("voice_agent")
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

DEEPGRAM_API_KEY  = os.getenv("DEEPGRAM_API_KEY") or ""

# --- Live-call LLM leg: provider-agnostic, swappable via env var only ---
# Primary/fallback pattern so a single provider outage/deprecation (see:
# Groq's repeated model shutdowns) never takes the live call down.
# To switch providers in production: change LLM_PROVIDER_PRIMARY /
# LLM_PROVIDER_FALLBACK on Railway and restart — no code change needed.
# Cerebras was evaluated and dropped — free tier hard-stops on billing
# ($0 quota) rather than just rate-limiting, not usable without a paid
# card attached. Groq kept only as fallback (rarely fires), using its
# still-supported reasoning model gpt-oss-20b — acceptable here since
# fallback isn't on the hot path every turn like primary is.
LLM_PROVIDER_PRIMARY  = os.getenv("LLM_PROVIDER_PRIMARY", "openai").lower()
LLM_PROVIDER_FALLBACK = os.getenv("LLM_PROVIDER_FALLBACK", "groq").lower()

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY") or ""
OPENAI_MODEL      = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

GROQ_API_KEY      = os.getenv("GROQ_API_KEY") or ""
GROQ_MODEL        = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

CALL_HANDLER_URL  = os.getenv("CALL_HANDLER_URL", "http://localhost:8000").rstrip("/")
PORT              = int(os.getenv("PORT", "5002"))
PLIVO_AUTH_ID     = os.getenv("PLIVO_AUTH_ID", "")
PLIVO_AUTH_TOKEN  = os.getenv("PLIVO_AUTH_TOKEN", "")
PLIVO_ANSWER_URL  = os.getenv("PLIVO_ANSWER_URL", "")
INTERNAL_API_KEY  = os.getenv("INTERNAL_API_KEY", "")  # shared secret for calling call_handler.py

if not DEEPGRAM_API_KEY:
    raise ValueError("DEEPGRAM_API_KEY missing")
if not PLIVO_AUTH_ID or not PLIVO_AUTH_TOKEN:
    raise ValueError("PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN missing — needed for signature verification and call hangup")

# Validate whichever providers are actually selected for the live-call leg
# have their key set.
_LLM_KEY_MAP = {
    "openai": OPENAI_API_KEY,
    "groq":   GROQ_API_KEY,
}
for _provider in {LLM_PROVIDER_PRIMARY, LLM_PROVIDER_FALLBACK}:
    if _provider not in _LLM_KEY_MAP:
        raise ValueError(f"Unknown LLM provider '{_provider}' — expected one of {list(_LLM_KEY_MAP)}")
    if not _LLM_KEY_MAP[_provider]:
        raise ValueError(f"{_provider.upper()}_API_KEY missing — required because LLM_PROVIDER_PRIMARY/FALLBACK references '{_provider}'")

plivo_client    = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)
deepgram_client = DeepgramClient(api_key=DEEPGRAM_API_KEY)   # now used for raw Listen + Speak, not Agent

# One AsyncOpenAI-compatible client for OpenAI, one AsyncGroq client for Groq.
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
groq_client   = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

LLM_CLIENTS = {"openai": openai_client, "groq": groq_client}
LLM_MODELS  = {"openai": OPENAI_MODEL, "groq": GROQ_MODEL}


# MIGRATION: NEW — raw STT turn-detection tuning (Voice Agent API did this internally)
UTTERANCE_END_MS   = 1400   # CHANGED (was 1000) — real callers pause mid-
                             # thought ("Yes... I want to..."); 1000ms was
                             # firing speech_final mid-sentence, agent
                             # jumping in and chopping the caller's own
                             # reply short (see prod call transcripts).
                             # Test/tune further from real call logs.
STT_ENDPOINTING_MS = 500    # CHANGED (was 300) — same reasoning; 300ms is
                             # Deepgram's own aggressive default, tuned for
                             # quick back-and-forth, not for callers who
                             # think out loud mid-sentence on a cold call.

KEEPALIVE_INTERVAL_S      = 8.0
FRAME_DURATION_S          = 0.020
SILENCE_THRESHOLD         = 80.0
GHOST_CALL_WARNING_S      = 7.5    # NEW — silence before the "are you there?" prompt fires
GHOST_CALL_TIMEOUT_S      = 8.0    # further silence AFTER that prompt before the real hangup
                                    # (total ~15.5s of dead air before we actually drop the call —
                                    # was a flat 18.0s straight to hangup, no prompt in between)
AMD_SPEECH_TIMEOUT_S      = 4.0
AMD_MAX_WAIT_S            = 8.0
WARNING_DURATION_S        = 3 * 60          # 3:00 — model gets a "time almost up" prompt-level nudge to ask permission
HOT_LEAD_GRACE_S          = 3 * 60 + 30     # NEW — 3:30 — deterministic, code-driven graceful close for HOT/WARM leads only
MAX_CALL_DURATION_S       = 4 * 60          # 4:00 — hard backstop for everyone else (or if the 3:30 stage somehow missed)
RECONNECT_DELAYS: List[float] = [0.0, 2.0, 5.0]
# HISTORY_MAX_TURNS         = 30
HISTORY_REPLAY_TURNS      = 8
SEND_QUEUE_MAXSIZE        = 200
CONFIG_FETCH_RETRIES      = 3
CONFIG_FETCH_BACKOFF_S    = 1.0

VOICEMAIL_PHRASES = frozenset([
    "leave a message", "leave your message", "after the tone", "after the beep",
    "not available", "please record", "voicemail", "voice mail",
])


class CallState(str, Enum):
    VERIFY_IDENTITY  = "VERIFY_IDENTITY"
    PERMISSION_CHECK = "PERMISSION_CHECK"
    DISCOVERY        = "DISCOVERY"
    QUALIFICATION    = "QUALIFICATION"
    CLOSING          = "CLOSING"


class CallType(str, Enum):
    UNKNOWN   = "UNKNOWN"
    HUMAN     = "HUMAN"
    VOICEMAIL = "VOICEMAIL"
    IVR       = "IVR"


class CallOutcome(str, Enum):
    INTERESTED         = "INTERESTED"
    NOT_INTERESTED     = "NOT_INTERESTED"
    CALLBACK_REQUESTED = "CALLBACK_REQUESTED"
    VOICEMAIL          = "VOICEMAIL"
    WRONG_NUMBER       = "WRONG_NUMBER"
    DO_NOT_CALL        = "DO_NOT_CALL"
    EXISTING_CUSTOMER  = "EXISTING_CUSTOMER"
    NO_RESPONSE        = "NO_RESPONSE"
    IVR                = "IVR"
    CALL_DROPPED       = "CALL_DROPPED"
    OFF_TOPIC          = "OFF_TOPIC"  # NEW — caller kept derailing after a warning


# CHANGED — `update_call_state` REMOVED from the tool list. The model was
# burning tool-call hops (and _MAX_TOOL_HOPS budget) advancing internal
# call-stage bookkeeping instead of speaking, on ordinary turns. Call
# staging isn't something the caller needs the model to announce via a
# tool; drop it as an LLM-facing tool entirely. handle_fn() in
# stt_bridge.py still has an `update_call_state` branch — left in place,
# just dead code now (harmless, in case something else calls it later).
FUNCTIONS: List[Dict] = [
    {
        "name": "end_conversation",
        "description": (
            "End the call when user says goodbye, is unresponsive, "
            "requests no contact, is wrong number, or conversation is complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason":  {"type": "string"},
                "outcome": {"type": "string", "enum": [o.value for o in CallOutcome]},
            },
            "required": ["reason", "outcome"],
        },
    },
    {
        "name": "update_lead_facts",
        "description": (
            "Call whenever new info is learned: company, budget, timeline, "
            "pain points, interested services. Partial updates OK."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "company":             {"type": ["string", "null"]},
                "budget":              {"type": ["string", "null"]},
                "timeline":            {"type": ["string", "null"]},
                "pain_points":         {"type": ["array", "null"], "items": {"type": "string"}},
                "interested_services": {"type": ["array", "null"], "items": {"type": "string"}},
            },
            "required": [],
        },
    },
]