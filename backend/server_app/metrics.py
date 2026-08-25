"""
NEW — barge-in production metrics (item 3 of the barge-in improvement
request).

Every event is logged as a structured JSON line (same shape as
session.log_outcome) so an external log pipeline can aggregate across
processes/instances — that's the durable source of truth in production.
This module ALSO keeps a small in-process rolling window so summary()
can answer "what does p95 look like right now" without a log pipeline,
e.g. from a debug endpoint — that number is process-lifetime only and
resets on deploy, it is not a substitute for the logged events.

Six things tracked, matching the request exactly:
  1. interrupt detection latency  — local RMS onset → Deepgram SpeechStarted
  2. audio-stop latency           — SpeechStarted → clearAudio+TTS-clear done
  3. false barge-in rate          — SpeechStarted with no transcript after it
  4. missed interruption rate     — sustained user RMS over agent audio with
                                     no SpeechStarted at all
  5. stale-response rate          — TTS audio chunks discarded by generation
  6. p95 interruption latency     — computed from (1)'s rolling window
"""

import collections
import json
import time
from typing import Deque, Dict, Optional

from .config import log

_ROLLING_WINDOW = 500  # samples kept per metric, per process

_detection_latencies_ms: Deque[float] = collections.deque(maxlen=_ROLLING_WINDOW)
_audio_stop_latencies_ms: Deque[float] = collections.deque(maxlen=_ROLLING_WINDOW)
_barge_in_total  = 0
_barge_in_false  = 0
_missed_total    = 0
_tts_chunks_total = 0
_tts_chunks_stale = 0


def _log_metric(name: str, call_sid: str, **fields) -> None:
    log.info(json.dumps({
        "event": "barge_in_metric", "metric": name,
        "call_sid": call_sid, "ts": time.time(), **fields,
    }))


def record_local_speech_onset(s) -> None:
    """audio.py calls this on the first above-threshold frame after
    silence WHILE the agent is speaking — our own ground-truth timestamp
    for 'caller actually started talking', independent of (and earlier
    than) Deepgram's own detection."""
    with s.lock:
        if s._local_speech_onset_ts is None:
            s._local_speech_onset_ts = time.monotonic()


def record_barge_in_detected(s) -> Optional[float]:
    """stt_bridge.py calls this the instant ListenV1SpeechStarted arrives.
    Returns detection latency in ms if a local onset was captured."""
    global _barge_in_total
    now = time.monotonic()
    with s.lock:
        onset = s._local_speech_onset_ts
        s._local_speech_onset_ts = None
        s._missed_flagged        = False
        s._barge_in_detect_ts    = now
    _barge_in_total += 1
    if onset is None:
        return None
    latency_ms = (now - onset) * 1000
    _detection_latencies_ms.append(latency_ms)
    _log_metric("interrupt_detection_latency_ms", s.call_sid, value=round(latency_ms, 1))
    return latency_ms


def record_audio_stopped(s) -> Optional[float]:
    """stt_bridge.py calls this right after drain+clearAudio+TTS-clear
    have all actually completed — measures detection-to-silenced time."""
    with s.lock:
        detect_ts = s._barge_in_detect_ts
    if detect_ts is None:
        return None
    latency_ms = (time.monotonic() - detect_ts) * 1000
    _audio_stop_latencies_ms.append(latency_ms)
    _log_metric("audio_stop_latency_ms", s.call_sid, value=round(latency_ms, 1))
    return latency_ms


def record_barge_in_outcome(s, had_transcript: bool) -> None:
    """Called ~1.5s after a barge-in — if no real user transcript followed
    it, this was noise/echo, not a genuine interruption."""
    global _barge_in_false
    if not had_transcript:
        _barge_in_false += 1
        _log_metric("false_barge_in", s.call_sid)


def record_missed_interruption(s) -> None:
    """audio.py calls this when local RMS shows sustained caller speech
    over agent audio but Deepgram never fired SpeechStarted at all — a
    real interruption we failed to react to."""
    global _missed_total
    _missed_total += 1
    _log_metric("missed_interruption", s.call_sid)


def record_tts_chunk(call_sid: str, is_stale: bool) -> None:
    """audio.py's audio_sender_task calls this per chunk popped off
    audio_queue — is_stale = its generation didn't match current, i.e.
    Deepgram already synthesized audio for a turn the caller interrupted."""
    global _tts_chunks_total, _tts_chunks_stale
    _tts_chunks_total += 1
    if is_stale:
        _tts_chunks_stale += 1
        _log_metric("stale_tts_chunk", call_sid)


def _p95(values: Deque[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def _avg(values: Deque[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summary() -> Dict:
    """Process-lifetime rolling snapshot — wire to a /metrics debug route
    or a periodic log line (see routes.py's metrics_summary_loop)."""
    return {
        "interrupt_detection_latency_ms_p95": round(_p95(_detection_latencies_ms), 1),
        "interrupt_detection_latency_ms_avg": round(_avg(_detection_latencies_ms), 1),
        "audio_stop_latency_ms_p95":          round(_p95(_audio_stop_latencies_ms), 1),
        "audio_stop_latency_ms_avg":          round(_avg(_audio_stop_latencies_ms), 1),
        "false_barge_in_rate":       round(_barge_in_false / _barge_in_total, 4) if _barge_in_total else 0.0,
        "missed_interruption_count": _missed_total,
        "stale_response_rate":       round(_tts_chunks_stale / _tts_chunks_total, 4) if _tts_chunks_total else 0.0,
        "sample_size_barge_ins":     _barge_in_total,
        "sample_size_tts_chunks":    _tts_chunks_total,
    }
