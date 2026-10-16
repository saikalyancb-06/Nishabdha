"""UNCLONE backend entrypoint.  Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import agents, calls, copilot, customer_app, judge_demo, knowledge, passkeys, staff, ws
from app.config import get_settings
from app.core.session import SESSIONS
from app.db import engine, init_db
from app.knowledge.seed_data import seed_knowledge_base
from app.providers.llm import get_llm
from app.providers.stt import get_stt
from app.providers.voice_engine import get_voice_engine
from app.seed import seed

s = get_settings()
logging.basicConfig(
    level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("unclone")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed()
    seed_knowledge_base()
    if s.app_env != "test":
        # warm local models in the background so startup stays fast
        async def _warm():
            res = await get_voice_engine().warmup()
            log.info("voice engine warmup: %s", res)
            try:
                log.info("local STT warmup: %s", await get_stt().warmup())
            except Exception as e:
                log.warning("local STT warmup failed: %s", e)

        asyncio.create_task(_warm())
    if s.staff_api_key == "change-me-staff-key":
        log.warning("STAFF_API_KEY is the default - set it in .env before the demo")
    log.info(
        "voice chain=%s  stt=%s  llm=%s",
        get_voice_engine().chain,
        s.stt_provider,
        s.llm_provider,
    )

    yield
    for sess in list(SESSIONS.values()):
        try:
            await sess.end("server_shutdown")
        except Exception:
            pass
    await get_voice_engine().close()
    await get_stt().close()
    await get_llm().close()
    await engine.dispose()


app = FastAPI(
    title=s.app_name,
    version="1.0.0",
    description="Voice banking agent that gates sensitive actions behind speaker verification, deepfake detection, liveness and step-up approval.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=s.cors_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal error", "type": type(exc).__name__},
    )


app.include_router(calls.router)
app.include_router(customer_app.router)
app.include_router(knowledge.router)
app.include_router(staff.router)
app.include_router(ws.router)
app.include_router(passkeys.router)
app.include_router(agents.router)
app.include_router(copilot.router)
app.include_router(judge_demo.router)


@app.get("/health", tags=["system"])
async def health_check():
    """System health check for cloud deployments (Hugging Face Spaces, Render, etc.)."""
    from app.knowledge import store

    return {
        "status": "healthy",
        "app": s.app_name,
        "env": s.app_env,
        "voice_engine": get_voice_engine().chain,
        "knowledge_store": store.get_stats(),
        "demo_staff_key": s.staff_api_key if s.app_env != "prod" else "",
    }


STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def root():
    """The demo frontend (caller, customer phone app, fraud desk) - a single static page."""
    return FileResponse(STATIC / "ui" / "index.html")
