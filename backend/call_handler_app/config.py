"""
config.py — env vars, external clients, Supabase access helpers,
agent-config + phone-number-map caches, hangup-cause -> business-status
classifier.

Everything else (route modules) imports from here instead of touching
env/clients directly.
"""

import asyncio
import logging
import os
import time
from typing import Dict, Optional

import httpx
import plivo
import resend
from dotenv import load_dotenv
from groq import AsyncGroq
from supabase import create_client, Client

# ── env ───────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("call_handler")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)

DEEPGRAM_API_KEY          = os.getenv("DEEPGRAM_API_KEY", "")
GROQ_API_KEY              = os.getenv("GROQ_API_KEY", "")
SUPABASE_URL              = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
PLIVO_AUTH_ID             = os.getenv("PLIVO_AUTH_ID", "")
PLIVO_AUTH_TOKEN          = os.getenv("PLIVO_AUTH_TOKEN", "")
PLIVO_ANSWER_URL          = os.getenv("PLIVO_ANSWER_URL", "")
PLIVO_APP_NAME            = os.getenv("PLIVO_APP_NAME", "ai-voice-agent")
INTERNAL_API_KEY          = os.getenv("INTERNAL_API_KEY", "")
RESEND_API_KEY            = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL         = os.getenv("RESEND_FROM_EMAIL", "Inbox Infotech <onboarding@resend.dev>")
ALLOWED_ORIGINS           = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

for name, val in [
    ("DEEPGRAM_API_KEY",          DEEPGRAM_API_KEY),
    ("GROQ_API_KEY",              GROQ_API_KEY),
    ("SUPABASE_URL",              SUPABASE_URL),
    ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY),
    ("PLIVO_AUTH_ID",             PLIVO_AUTH_ID),
    ("PLIVO_AUTH_TOKEN",          PLIVO_AUTH_TOKEN),
]:
    if not val:
        raise ValueError(f"{name} missing from .env")

# ── clients ───────────────────────────────────────────────────

groq_client:  AsyncGroq        = AsyncGroq(api_key=GROQ_API_KEY)
plivo_client: plivo.RestClient = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)
supabase:     Optional[Client] = None  # set in init_supabase(), called from lifespan

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

DEFAULT_AGENT_ID = "default"


def init_supabase() -> Client:
    """Called once from the app lifespan on startup."""
    global supabase
    supabase = create_client(supabase_url=SUPABASE_URL, supabase_key=SUPABASE_SERVICE_ROLE_KEY)
    logger.warning("Supabase client initialised")
    return supabase


def _get_supabase() -> Client:
    if supabase is None:
        raise RuntimeError("Supabase client not initialised yet")
    return supabase


def _with_retry(fn, *args, retries: int = 2, delay: float = 0.15, **kwargs):
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except (httpx.ReadError, httpx.RemoteProtocolError, ConnectionError) as e:
            last_exc = e
            if attempt < retries:
                logger.debug("Transient Supabase read error, retry %d/%d: %s", attempt + 1, retries, e)
                time.sleep(delay)
            else:
                logger.warning("Supabase call failed after %d retries: %s", retries, e)
    raise last_exc


# ── agent config cache ───────────────────────────────────────

_CONFIG_CACHE:    Dict[str, Dict[str, str]] = {}
_CONFIG_CACHE_TS: Dict[str, float] = {}
_CONFIG_TTL:      float = 300.0


def _fetch_agent_config_sync(agent_id: str) -> Dict[str, str]:
    rows = (
        _get_supabase().table("agent_config").select("key, value")
        .eq("agent_id", agent_id).execute().data or []
    )
    return {r["key"]: r["value"] for r in rows}


async def get_agent_config(agent_id: str = DEFAULT_AGENT_ID, force: bool = False) -> Dict[str, str]:
    now = time.monotonic()
    cached    = _CONFIG_CACHE.get(agent_id)
    cached_ts = _CONFIG_CACHE_TS.get(agent_id, 0.0)
    if force or not cached or (now - cached_ts) > _CONFIG_TTL:
        try:
            cfg = await asyncio.to_thread(_fetch_agent_config_sync, agent_id)
            _CONFIG_CACHE[agent_id]    = cfg
            _CONFIG_CACHE_TS[agent_id] = now
        except Exception as e:
            logger.warning("Config fetch failed — agent_id=%s using cache/defaults: %s", agent_id, e)
    return _CONFIG_CACHE.get(agent_id, {})


# ── phone -> agent map cache ─────────────────────────────────
#
# BUGFIX (found during the file split): resolve_agent_id_for_number()
# used to do `_PHONE_MAP_CACHE = await ...` — a rebind of the module
# global. That's harmless as long as everything lives in one file and
# always reads the name `_PHONE_MAP_CACHE` fresh off the module. But
# once routes live in other files, they'd do
# `from .config import _PHONE_MAP_CACHE` and bind their own local name
# to whatever dict object existed at import time. A later rebind here
# repoints THIS module's name to a new dict — the other module's name
# still points at the old, stale (and never-updated-again) dict, so
# agent_routes.agent_for_number() and campaigns' link/unlink cache
# invalidation would silently stop seeing updates. Fixed by mutating
# the same dict object in place (clear + update) instead of rebinding.

_PHONE_MAP_CACHE: Dict[str, str] = {}
_PHONE_MAP_CACHE_TS: float = 0.0
_PHONE_MAP_TTL = 300.0


def _fetch_phone_map_sync() -> Dict[str, str]:
    rows = (
        _get_supabase().table("agent_numbers").select("number, agent_id").execute().data or []
    )
    return {r["number"]: r["agent_id"] for r in rows if r.get("number")}


async def resolve_agent_id_for_number(to_number: Optional[str]) -> str:
    global _PHONE_MAP_CACHE_TS
    if not to_number:
        return DEFAULT_AGENT_ID
    now = time.monotonic()
    if not _PHONE_MAP_CACHE or (now - _PHONE_MAP_CACHE_TS) > _PHONE_MAP_TTL:
        try:
            fresh = await asyncio.to_thread(_fetch_phone_map_sync)
            _PHONE_MAP_CACHE.clear()
            _PHONE_MAP_CACHE.update(fresh)   # mutate in place, don't rebind
            _PHONE_MAP_CACHE_TS = now
        except Exception as e:
            logger.warning("Phone map fetch failed, using stale/empty cache: %s", e)
    return _PHONE_MAP_CACHE.get(to_number, DEFAULT_AGENT_ID)


# ── hangup-cause -> business-status classifier ───────────────
# Used only for queue/concurrency/reconciliation logic — the raw
# hangup_cause is still stored/exported verbatim, unchanged.

BUSINESS_STATUS_MAP = {
    "Normal Hangup":        "COMPLETED",
    "No Answer":            "TIMEOUT",
    "Ring Timeout Reached": "TIMEOUT",
    "Busy Line":            "FAILED",
    "Busy everywhere":      "FAILED",
    "Rejected":             "FAILED",
    "Declined":             "FAILED",
}


def business_status(hangup_cause: Optional[str]) -> str:
    if not hangup_cause:
        return "IN_PROGRESS"
    return BUSINESS_STATUS_MAP.get(hangup_cause, "COMPLETED")
