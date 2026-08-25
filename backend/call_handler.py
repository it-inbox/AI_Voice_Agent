"""
call_handler.py  —  FastAPI + Supabase
Plivo webhook receiver + transcription + lead extraction

Thin entrypoint: wires up the app and mounts each route module from
call_handler_app/. Business logic lives in the submodules.

Run:
  pip install fastapi uvicorn httpx groq python-dotenv supabase plivo resend
  uvicorn call_handler:app --host 0.0.0.0 --port 8000 --reload --log-level warning --no-access-log
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from call_handler_app import agent_routes, campaigns, forms, lead_extraction, plivo_routes
from call_handler_app.config import (
    ALLOWED_ORIGINS,
    DEFAULT_AGENT_ID,
    _CONFIG_CACHE,
    _get_supabase,
    get_agent_config,
    init_supabase,
    logger,
    resolve_agent_id_for_number,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_supabase()
    try:
        await get_agent_config(DEFAULT_AGENT_ID, force=True)
        await resolve_agent_id_for_number(None)
    except Exception as e:
        logger.error("Startup config pre-warm failed: %s", e)
    logger.warning("call_handler started")
    yield
    logger.warning("call_handler stopped")


app = FastAPI(title="Inbox Infotech — Call Handler", version="3.9.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])

app.include_router(plivo_routes.router)
app.include_router(agent_routes.router)
app.include_router(lead_extraction.router)
app.include_router(forms.router)
app.include_router(campaigns.router)


@app.get("/health")
async def health():
    try:
        res   = await asyncio.to_thread(lambda: _get_supabase().table("calls").select("id", count="exact").execute())
        count = res.count or 0
    except Exception:
        count = -1
    cfg = await get_agent_config(DEFAULT_AGENT_ID)
    return JSONResponse({
        "status": "ok", "calls_stored": count, "config_keys_loaded": list(cfg.keys()),
        "agents_cached": list(_CONFIG_CACHE.keys()), "timestamp": datetime.utcnow().isoformat(),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("call_handler:app", host="0.0.0.0", port=8000, reload=False, log_level="warning", access_log=False)