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
from fastapi import Request
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


def check_internal_key(request) -> None:
    """Shared by any route only ever meant to be called server-to-server
    (server_app -> call_handler_app), never directly by a browser.
    FIX (fail-open -> fail-closed): an unset/empty INTERNAL_API_KEY used
    to mean "skip this check" in the old per-file version of this
    function — a misconfigured deployment silently ran with no internal
    auth at all instead of failing loudly. Now always rejects when the
    key isn't configured."""
    from fastapi import HTTPException
    if not INTERNAL_API_KEY:
        raise HTTPException(status_code=500, detail="Server misconfigured: INTERNAL_API_KEY not set")
    if request.headers.get("X-Internal-Key", "") != INTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid internal API key")
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


async def require_user(request: Request):
    """FastAPI dependency: verifies the caller sent a real, currently
    valid Supabase Auth session (the same login the dashboard already
    requires), not just a raw HTTP request to the Railway URL. Add
    `Depends(require_user)` to any route the browser calls directly on
    behalf of a logged-in dashboard user. Do NOT use this on: Plivo
    webhooks (verified by Plivo signature instead — Plivo can't send a
    Supabase session), or server-to-server calls between server_app and
    call_handler_app (use check_internal_key instead — there's no
    end-user session in that hop at all)."""
    # FIX (422 on every protected route): `request` had no type
    # annotation, so FastAPI couldn't tell this was meant to be the
    # special injected Request object — it silently treated it as a
    # required QUERY PARAMETER named "request" instead, and rejected
    # every call that didn't have ?request=... in the URL with a 422.
    from fastapi import HTTPException
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    try:
        result = await asyncio.to_thread(_get_supabase().auth.get_user, token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not result or not result.user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return result.user


# NEW — rate limiting for paid/costly operations (outbound calls, batch
# dial, Plivo number mgmt, form email). In-memory only: fine for a
# single Railway instance (your current setup); would need a shared
# store (Redis) if this ever runs multiple instances, since each
# instance would count independently otherwise.
_rate_limit_hits: Dict[str, list] = {}


def rate_limit(key: str, max_calls: int, window_s: float) -> None:
    """Call at the top of a route body (after require_user/
    check_internal_key). Raises 429 if `key` (usually the user's id,
    scoped per-endpoint) has exceeded max_calls within the last
    window_s seconds."""
    from fastapi import HTTPException
    now = time.time()
    hits = [t for t in _rate_limit_hits.get(key, []) if now - t < window_s]
    if len(hits) >= max_calls:
        raise HTTPException(status_code=429, detail="Too many requests — slow down and try again shortly.")
    hits.append(now)
    _rate_limit_hits[key] = hits


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