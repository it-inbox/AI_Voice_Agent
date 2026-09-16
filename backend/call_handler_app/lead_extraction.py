"""
lead_extraction.py — Groq-based lead extraction pipeline, call-record
storage, and the live-facts / transcript ingestion routes that feed it.
"""

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import (
    INTERNAL_API_KEY,
    _get_supabase,
    groq_client,
    logger,
    resolve_agent_id_for_number,
)

router = APIRouter()

# ═══════════════════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════════════════

EXTRACTION_PROMPT = """
You are an expert sales analyst at Inbox Infotech.
Analyze this call transcript and extract structured lead information.

Inbox Infotech services:
- AI / ML Development | IoT Solutions | CRM / ERP
- Mobile & Web Development | Cloud & DevOps
- API Integration | Automation Solutions

Transcript:
{transcript}

Return ONLY valid JSON with this exact structure:
{{
    "name": "", "company": "", "pain_points": [], "budget": "",
    "requirements": [], "timeline": "", "decision_maker": true,
    "industry": "", "intent_level": "high", "lead_score": 8,
    "lead_category": "HOT", "summary": "", "next_action": "",
    "interested_services": []
}}

Scoring rules — apply strictly:
HOT  (score 8-10): clear interest + budget indicator + decision maker + urgency
WARM (score 4-7) : interested but vague on budget/timeline, or not decision maker
COLD (score 1-3) : no interest, declined, hung up, wrong number, voicemail
"""

_COLD_FALLBACK: Dict[str, Any] = {
    "name": "", "company": "", "pain_points": [], "budget": "not mentioned",
    "requirements": [], "timeline": "not mentioned", "decision_maker": False,
    "industry": "unknown", "intent_level": "low", "lead_score": 1,
    "lead_category": "COLD", "summary": "No transcript available.",
    "next_action": "Retry call later.", "interested_services": [],
}


async def extract_insights(call_sid: str, transcript: str) -> Dict[str, Any]:
    if not transcript.strip():
        return _COLD_FALLBACK.copy()
    try:
        response = await groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "You are a sales analyst. Return only valid JSON."},
                {"role": "user",   "content": EXTRACTION_PROMPT.format(transcript=transcript)},
            ],
            response_format={"type": "json_object"}, temperature=0.1, max_tokens=1500,
            # FIX (json_validate_failed / "max completion tokens reached"):
            # gpt-oss-120b is a reasoning model — it spends part of the
            # token budget on internal chain-of-thought BEFORE writing the
            # actual JSON. At max_tokens=800, longer/messier transcripts
            # could burn the whole budget on reasoning and get cut off
            # mid-JSON. This task is plain structured extraction, not
            # multi-step reasoning, so turn reasoning down rather than up —
            # cheaper, faster, and leaves the full token budget for output.
            reasoning_effort="low",
        )
        extracted = json.loads(response.choices[0].message.content)
        extracted.setdefault("name", ""); extracted.setdefault("company", "")
        extracted.setdefault("pain_points", []); extracted.setdefault("requirements", [])
        extracted.setdefault("interested_services", []); extracted.setdefault("lead_score", 1)
        extracted.setdefault("lead_category", "COLD")
        extracted["lead_score"] = max(1, min(10, int(extracted["lead_score"])))
        cat = extracted["lead_category"].upper()
        extracted["lead_category"] = cat if cat in ("HOT", "WARM", "COLD") else "COLD"
        name = str(extracted.get("name", "")).strip()
        extracted["name"] = name if name.lower() not in ("", "unknown", "n/a", "none") else ""
        company = str(extracted.get("company", "")).strip()
        extracted["company"] = company if company.lower() not in ("", "unknown", "n/a", "none") else ""
        logger.info("Lead extracted — call_sid=%s category=%s score=%s", call_sid, extracted["lead_category"], extracted["lead_score"])
        return extracted
    except Exception as e:
        logger.error("Lead extraction failed: %s", e)
        return {**_COLD_FALLBACK, "summary": "Extraction failed.", "next_action": "Manual review required."}


# ═══════════════════════════════════════════════════════════════
# SUPABASE STORAGE
# ═══════════════════════════════════════════════════════════════

def _save_record_sync(row: Dict[str, Any]) -> None:
    _get_supabase().table("calls").upsert(row, on_conflict="call_sid").execute()


def _safe_duration(raw: Any) -> float:
    try:
        return round(float(raw), 1)
    except (TypeError, ValueError):
        return 0.0


async def save_record(record: Dict[str, Any]) -> None:
    meta      = record.get("meta", {})
    extracted = record.get("extracted", {})
    agent_id = meta.get("agent_id_hint") or await resolve_agent_id_for_number(meta.get("to_number"))
    row = {
        "call_sid":      record["call_sid"],
        "from_number":   meta.get("from_number"),
        "to_number":     meta.get("to_number"),
        "duration_sec":  _safe_duration(meta.get("duration_sec", 0)),
        "transcript":    record.get("transcript", ""),
        "lead_category": extracted.get("lead_category", "COLD"),
        "lead_score":    extracted.get("lead_score", 1),
        "extracted":     extracted,
        "recording_url": meta.get("recording_url", ""),
        "source":        meta.get("source", "Unknown"),
        "name":          extracted.get("name", ""),
        "company":       extracted.get("company", ""),
        "agent_id":      agent_id,
    }
    try:
        await asyncio.to_thread(_save_record_sync, row)
        logger.debug("Call record saved — call_sid=%s", record["call_sid"])
    except Exception as e:
        logger.error("Supabase write failed: %s", e)


def _save_live_facts_sync(row: Dict[str, Any]) -> None:
    _get_supabase().table("calls").upsert(row, on_conflict="call_sid").execute()


async def save_live_facts(call_sid: str, facts: Dict[str, Any], outcome: Optional[str], history: List[Dict[str, str]]) -> None:
    row = {
        "call_sid": call_sid, "live_facts": facts, "live_outcome": outcome,
        "live_history": history, "live_updated_at": datetime.utcnow().isoformat(),
    }
    try:
        await asyncio.to_thread(_save_live_facts_sync, row)
    except Exception as e:
        logger.error("Live facts save failed — call_sid=%s: %s", call_sid, e)


# ═══════════════════════════════════════════════════════════════
# INGESTION ROUTES
# ═══════════════════════════════════════════════════════════════

def _check_internal_key(request: Request) -> None:
    if INTERNAL_API_KEY:
        provided = request.headers.get("X-Internal-Key", "")
        if provided != INTERNAL_API_KEY:
            raise HTTPException(status_code=403, detail="Invalid internal API key")


@router.post("/api/call-live-facts")
async def post_call_live_facts(request: Request):
    _check_internal_key(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "bad_request"}, status_code=400)
    call_sid = body.get("call_sid", "")
    if not call_sid:
        return JSONResponse({"status": "bad_request", "reason": "call_sid required"}, status_code=400)
    facts   = body.get("facts", {}) or {}
    outcome = body.get("outcome")
    history = body.get("history", []) or []
    await save_live_facts(call_sid, facts, outcome, history)
    return JSONResponse({"status": "ok", "call_sid": call_sid})


def format_transcript_from_history(history: List[Dict[str, str]]) -> str:
    lines: List[str] = []
    for turn in history:
        role = (turn.get("role") or "").lower()
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        label = "Agent" if role == "assistant" else "Customer" if role == "user" else role.title()
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


@router.post("/api/call-transcript")
async def post_call_transcript(request: Request, background_tasks: BackgroundTasks):
    _check_internal_key(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "bad_request"}, status_code=400)
    call_sid = body.get("call_sid", "")
    if not call_sid:
        return JSONResponse({"status": "bad_request", "reason": "call_sid required"}, status_code=400)
    history = body.get("history", []) or []
    call_meta = {
        "to_number": body.get("to_number", ""), "from_number": body.get("from_number", ""),
        "duration_sec": body.get("duration_sec", 0), "source": body.get("source", "Plivo"),
        "agent_id_hint": body.get("agent_id", ""),
    }
    background_tasks.add_task(process_transcript_pipeline, call_sid, history, call_meta)
    return JSONResponse({"status": "ok", "call_sid": call_sid})


async def process_transcript_pipeline(call_sid: str, history: List[Dict[str, str]], call_meta: Dict[str, Any]) -> None:
    transcript = format_transcript_from_history(history)
    if not transcript.strip():
        logger.warning("Transcript pipeline skipped — empty transcript, call_sid=%s", call_sid)
        return
    extracted = await extract_insights(call_sid, transcript)
    await save_record({"call_sid": call_sid, "meta": call_meta, "transcript": transcript, "extracted": extracted})