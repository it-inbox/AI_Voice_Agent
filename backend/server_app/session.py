"""
Session class, per-call registries, and small cross-cutting helpers
(clog/log_outcome/ws_open/run_async) used by nearly every other module.
"""

import asyncio
import collections
import json
import queue as sync_queue
import threading
import time
from typing import Any, Dict, Optional

from aiohttp import web

from .config import CallState, CallType, log


class Session:
    """
    All mutable state for one call, keyed by call_sid.
    Lock discipline: acquire `lock` before reading/writing any field
    that is touched from both the asyncio loop and listener thread.
    Fields only ever accessed from a single context need no lock.
    """

    __slots__ = (
        "call_sid", "stream_sid", "agent_id", "agent_id_hint",
        "to_number", "from_number", "lead_name_hint",
        # MIGRATION: REMOVED "_cm", "agent_conn" (single Voice Agent socket).
        # ADDED — STT and TTS are now two independent connections.
        "_stt_cm", "stt_conn", "stt_lock",
        "_tts_cm", "tts_conn", "tts_lock",
        "send_q", "audio_queue",
        "audio_sender_task", "keepalive_task", "listener",
        "duration_task", "amd_task", "reconnect_lock", "tts_reconnect_lock",
        "agent_speaking", "generation_id",
        # NEW — barge-in improvements:
        # active_llm_task: handle to the in-flight Groq stream so barge-in
        #   can cancel it immediately instead of letting it keep running
        #   (and keep feeding a now-interrupted turn to TTS) in the background.
        # tts_send_gen: generation stamped on a TTS request AT SEND TIME —
        #   the listener tags audio with this, not with whatever
        #   generation_id happens to be current when audio arrives. Fixes
        #   a race where a barge-in landing between send and audio-arrival
        #   would mistag stale audio as belonging to the new generation.
        # _local_speech_onset_ts / _missed_flagged: local RMS-based ground
        #   truth for when the caller actually started talking over the
        #   agent, used to measure detection latency and catch cases where
        #   Deepgram never fired SpeechStarted at all.
        "active_llm_task", "tts_send_gen", "tts_flushed_event",
        "_local_speech_onset_ts", "_barge_in_detect_ts", "_missed_flagged",
        "silence_seconds", "ghost_fired", "ghost_prompted",
        "call_type", "amd_speech_start", "amd_done",
        "call_state",
        "pending_hangup",
        "outcome",
        "call_start_time", "warning_sent",
        "history", "facts",
        "stt_settings", "system_prompt",   # MIGRATION: "dg_settings" → "stt_settings" (plain dict, no recovery object)
        "loop",
        "lock",
    )

    def __init__(self) -> None:
        self.call_sid        = ""
        self.stream_sid      = ""
        self.agent_id        = "default"
        self.agent_id_hint   = ""
        self.to_number       = ""
        self.from_number     = ""
        self.lead_name_hint  = ""  # NEW — per-call customer name, from outbound-call request → answer_url → ws query
        self._stt_cm         = None
        self.stt_conn        = None
        self.stt_lock        = threading.Lock()
        self._tts_cm         = None
        self.tts_conn        = None
        self.tts_lock        = threading.Lock()
        self.send_q: Optional[sync_queue.Queue] = None
        self.audio_queue: Optional[asyncio.Queue] = None
        self.audio_sender_task = None
        self.keepalive_task    = None
        self.listener          = None
        self.duration_task     = None
        self.amd_task          = None
        self.reconnect_lock: Optional[asyncio.Lock] = None
        self.tts_reconnect_lock: Optional[asyncio.Lock] = None
        self.agent_speaking  = False
        self.generation_id   = 0
        self.active_llm_task = None
        self.tts_send_gen    = 0
        self.tts_flushed_event: Optional[asyncio.Event] = None
        self._local_speech_onset_ts: Optional[float] = None
        self._barge_in_detect_ts: Optional[float] = None
        self._missed_flagged = False
        self.silence_seconds = 0.0
        self.ghost_fired     = False
        self.ghost_prompted  = False   # NEW — "are you there?" already spoken this silence window
        self.call_type       = CallType.UNKNOWN
        self.amd_speech_start: Optional[float] = None
        self.amd_done        = False
        self.call_state      = CallState.VERIFY_IDENTITY
        self.pending_hangup  = False
        self.outcome: Optional[str] = None
        self.call_start_time: Optional[float] = None
        self.warning_sent    = False
        self.history: collections.deque = collections.deque()
        self.facts: Dict[str, Any] = {
            "lead_name": None, "company": None,
            "budget": None,    "timeline": None,
            "pain_points": [], "interested_services": [],
        }
        self.stt_settings: Dict = {}
        self.system_prompt: str = ""
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.lock = threading.Lock()


_sessions: Dict[str, Session] = {}
_answered_call_uuids: set = set()

# NEW — bridges our own dash_id (embedded on answer_url when we place the
# call) to Plivo's REAL CallUUID (only known once plivo_answer() fires).
# Plivo's outbound calls.create() only ever returns request_uuid
# synchronously — a DIFFERENT identifier that calls.get() and the
# CallUUID webhook param do not accept. Without this bridge, every
# call_uuid handed to the dashboard was actually a request_uuid, so
# every downstream poll (Plivo call-status, Supabase live_outcome) was
# querying an id Plivo/Supabase never had a record of — guaranteed
# stuck at Unresolved/Ringing regardless of what really happened on
# the call.
_dash_call_uuid_map: Dict[str, str] = {}
_DASH_MAP_MAX = 1000


def log_outcome(call_sid: str, outcome: str, reason: str) -> None:
    log.info(json.dumps({
        "event": "call_outcome", "call_sid": call_sid,
        "outcome": outcome, "reason": reason, "ts": time.time(),
    }))


def clog(call_sid: str, msg: str) -> None:
    log.info("[%s] %s", call_sid, msg)


def run_async(coro, loop: asyncio.AbstractEventLoop):
    """Fire-and-forget coroutine from any thread. Returns the
    concurrent.futures.Future so callers that need to track/cancel it
    (see stt_bridge._launch_llm_turn) can — existing call sites that
    don't care simply discard the return value, unchanged behavior."""
    return asyncio.run_coroutine_threadsafe(coro, loop)


def ws_open(ws: web.WebSocketResponse) -> bool:
    try:
        return not ws.closed
    except Exception:
        return False