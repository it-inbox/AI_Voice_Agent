"""
agent_routes.py — agent-facing config endpoint, active/inactive toggle,
active-agent count.
"""

import asyncio
import os
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import DEFAULT_AGENT_ID, _get_supabase, _with_retry, get_agent_config

router = APIRouter()


def _fetch_agent_profile_sync(agent_id: str) -> Dict[str, Optional[str]]:
    row = (
        _get_supabase().table("agents").select("name, phone_number")
        .eq("agent_id", agent_id).maybe_single().execute().data
    )
    return row or {}


@router.get("/api/config")
async def get_config(agent_id: str = DEFAULT_AGENT_ID):
    cfg     = await get_agent_config(agent_id)
    profile = await asyncio.to_thread(_fetch_agent_profile_sync, agent_id)
    safe_keys = {"system_prompt", "company_name", "calendly_link", "followup_delay", "notification_email", "lead_name"}
    filtered = {k: v for k, v in cfg.items() if k in safe_keys}
    filtered["agent_id"]     = agent_id
    filtered["agent_name"]   = (profile.get("name") or "").strip() or "Assistant"
    filtered["phone_number"] = profile.get("phone_number")
    if "lead_name" not in filtered:
        env_lead = os.getenv("LEAD_NAME", "").strip()
        if env_lead:
            filtered["lead_name"] = env_lead
    return JSONResponse(filtered)


@router.patch("/api/agents/{agent_id}/toggle")
async def toggle_agent(agent_id: str, request: Request):
    body      = await request.json()
    is_active = bool(body.get("is_active"))
    def _update():
        _get_supabase().table("agents").update({"is_active": is_active, "updated_at": datetime.utcnow().isoformat()}).eq("agent_id", agent_id).execute()
    await asyncio.to_thread(_update)
    return JSONResponse({"status": "ok", "agent_id": agent_id, "is_active": is_active})


@router.get("/api/agents/active-count")
async def active_agent_count():
    def _counts():
        total  = _get_supabase().table("agents").select("agent_id", count="exact").execute()
        active = _get_supabase().table("agents").select("agent_id", count="exact").eq("is_active", True).execute()
        return (active.count or 0), (total.count or 0)
    active_n, total_n = await asyncio.to_thread(_with_retry, _counts)
    return JSONResponse({"count": active_n, "total": total_n})
