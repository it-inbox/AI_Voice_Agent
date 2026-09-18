"""
lead_extraction.py — Groq-based lead extraction pipeline, call-record
storage, and the live-facts / transcript ingestion routes that feed it.
"""

import asyncio
import json
import re
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


def _has_value(v: Any) -> bool:
    """A field counts as 'actually provided' only if it's non-empty and
    isn't one of the LLM's own placeholder-for-nothing strings."""
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("", "not mentioned", "not provided", "unknown", "n/a", "none", "unclear")


# NEW — root cause (live report): a joke/tease budget ("my budget is 1rs
# or 100rs") counted as "budget provided" the same as a real number, since
# _has_value only checks presence, not amount — pushing an obviously fake
# answer toward HOT. Fix: parse the number out and judge it against our
# own package price. Currency isn't distinguished (rs/$/plain number) —
# a deliberate simplification, see report — the point is catching
# orders-of-magnitude-off joke numbers, not doing FX conversion.
_BUDGET_NUM_RE        = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k)?", re.IGNORECASE)
BUDGET_JOKE_MAX        = 100   # at/under this = mockery, not a real budget → force COLD
BUDGET_NEAR_RANGE_MIN  = 500   # half our $999 package price — "near range" per the ask


def _parse_budget_amount(budget_str: Any) -> Optional[float]:
    if not budget_str:
        return None
    m = _BUDGET_NUM_RE.search(str(budget_str).replace(",", ""))
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2):   # "1k" -> 1000
        amount *= 1000
    return amount


def _extract_budget_amount_from_transcript(transcript: str) -> Optional[float]:
    """Fallback when extracted['budget'] has no parseable number — the
    extraction LLM can write "too low"/"not viable" in the structured
    field while still saying "1 rupee" in the prose summary. Scan the raw
    transcript directly instead of trusting the field."""
    for m in re.finditer(r"budget[^.\d]{0,30}?(\d[\d,]*(?:\.\d+)?)\s*(k)?", transcript, re.IGNORECASE):
        try:
            amt = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if m.group(2):
            amt *= 1000
        return amt   # first mention wins
    return None


def _reconcile_lead_category(extracted: Dict[str, Any], transcript: str = "") -> Dict[str, Any]:
    """FIX (root cause — live report: a ~1:45 call with no budget, no
    timeline, and only a couple of one-word answers ("yes", "let's go")
    got filed as a HOT lead). EXTRACTION_PROMPT above already STATES the
    HOT/WARM/COLD rule, but nothing ever checked the model's own
    lead_category/lead_score against the very fields it extracted in the
    same response — it can call something HOT on general enthusiasm in
    the summary while leaving budget/timeline blank in the structured
    output, and nothing catches the contradiction. This is a deterministic
    pass over the ALREADY-EXTRACTED fields (not a second LLM call) that
    enforces the stated rule instead of trusting the model to have
    applied it: HOT requires budget + timeline + decision_maker all
    actually present, not just enthusiasm; a near-empty call (no pain
    points, no requirements, no interested services, on top of missing
    budget/timeline) is capped at COLD regardless of tone. Downgrades
    only — never invents a category more confident than the LLM gave."""
    budget_ok    = _has_value(extracted.get("budget"))
    timeline_ok  = _has_value(extracted.get("timeline"))
    decision_ok  = bool(extracted.get("decision_maker") is True)
    substance    = (
        bool(extracted.get("pain_points")) or bool(extracted.get("requirements"))
        or bool(extracted.get("interested_services")) or budget_ok or timeline_ok
    )
    cat = extracted["lead_category"]

    # NEW (item 4 — see _parse_budget_amount docstring) — a real number
    # overrides everything else: a joke amount means the "interest" shown
    # elsewhere in the call isn't trustworthy either, so this forces COLD
    # outright rather than just un-counting budget_ok. A too-low-but-real
    # amount still counts as "they engaged", so it's capped at WARM
    # instead of wiped to COLD.
    budget_amount = _parse_budget_amount(extracted.get("budget"))
    if budget_amount is None and transcript:
        budget_amount = _extract_budget_amount_from_transcript(transcript)   # NEW — fallback, see docstring
    if budget_amount is not None:
        if budget_amount <= BUDGET_JOKE_MAX:
            cat = "COLD"
            logger.info("Lead category forced COLD — joke/mockery budget amount=%s", budget_amount)
        elif budget_amount < BUDGET_NEAR_RANGE_MIN:
            budget_ok = False   # too low to count toward HOT eligibility
            if cat == "HOT":
                cat = "WARM"

    if cat == "HOT" and not (budget_ok and timeline_ok and decision_ok):
        cat = "WARM" if substance else "COLD"
    if cat == "WARM" and not substance:
        cat = "COLD"
    if cat != extracted["lead_category"]:
        logger.info(
            "Lead category downgraded %s -> %s (budget_ok=%s timeline_ok=%s decision_ok=%s substance=%s)",
            extracted["lead_category"], cat, budget_ok, timeline_ok, decision_ok, substance,
        )
        extracted["lead_category"] = cat
        # Keep the numeric score inside the bucket its category implies —
        # a downgraded category with an 8-10 score would just recreate
        # the same inconsistency one layer down (e.g. dashboards that
        # sort/filter on lead_score directly instead of lead_category).
        bucket = {"HOT": (8, 10), "WARM": (4, 7), "COLD": (1, 3)}[cat]
        extracted["lead_score"] = max(bucket[0], min(bucket[1], extracted["lead_score"]))
    return extracted


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
        extracted = _reconcile_lead_category(extracted, transcript)   # NEW — see docstring above
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