"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.logging_config import setup_logging
from app.routers import exports, live, meetings
from app.services.pipeline import recover_interrupted_meetings

logger = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_dir)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    recovered = recover_interrupted_meetings()
    logger.info(
        "Started: whisper=%s llm=%s auth=%s recovered=%d",
        settings.whisper_model, settings.llm_model, "on" if settings.api_key else "off", recovered,
    )
    yield


app = FastAPI(
    title="AI Meeting Assistant",
    version="1.1.0",
    description="Upload a meeting recording, get a transcript, summary, decisions, risks and action items.",
    lifespan=lifespan,
)

if get_settings().cors_list:
    app.add_middleware(
        CORSMiddleware, allow_origins=get_settings().cors_list, allow_methods=["*"], allow_headers=["*"]
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(meetings.router)
app.include_router(exports.router)
app.include_router(live.router)

if FRONTEND_DIR.is_dir():  # must be mounted last so API routes win
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
