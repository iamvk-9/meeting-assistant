"""WebSocket endpoint for live transcription.

Protocol (client -> server):
  1. text  {"title": "...", "language": "en" | null, "participants": "a@x.com, Bob"}   (once, first)
  2. binary frames of raw little-endian PCM16, mono, 16 kHz (any frame size up to 1 MB)
  3. text  {"type": "stop"}  (or just close the socket; the recording so far is still saved)
Server -> client: {"type": "ready"} | {"type": "segments", "segments": [...]} |
  {"type": "warning", "detail": "..."} | {"type": "done", "meeting_id": n} | {"type": "error", "detail": "..."}
When a session ends, the recording becomes a normal meeting (status "transcribed") ready to be summarized.
Browsers cannot set headers on WebSockets, so the optional API key is passed as ?api_key=.
"""

import asyncio
import json
import logging
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.services import pipeline
from app.services.live import LiveLimitError, LiveSession
from app.services.transcription import get_transcriber
from app.utils import parse_participants

logger = logging.getLogger(__name__)
router = APIRouter(tags=["live"])
MAX_FRAME_BYTES = 1024 * 1024


async def _send(ws: WebSocket, payload: dict[str, Any]) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect):  # client already gone
        return False


async def _close(ws: WebSocket) -> None:
    await _close(ws)


@router.websocket("/ws/live")
async def live_transcription(ws: WebSocket, api_key: str | None = Query(default=None)) -> None:
    settings = get_settings()
    if settings.api_key and not (api_key and secrets.compare_digest(api_key.encode(), settings.api_key.encode())):
        await ws.close(code=4401)
        return
    await ws.accept()

    try:
        first = await ws.receive()
        config = json.loads(first.get("text") or "{}") if first["type"] == "websocket.receive" else None
    except (json.JSONDecodeError, WebSocketDisconnect):
        config = None
    if not isinstance(config, dict):
        await _send(ws, {"type": "error", "detail": "Send a JSON settings message first."})
        await _close(ws)
        return

    title = config.get("title") if isinstance(config.get("title"), str) else None
    participants = config.get("participants") if isinstance(config.get("participants"), str) else None
    language = config.get("language") if isinstance(config.get("language"), str) else None
    wav_name = f"{uuid.uuid4().hex}.wav"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    session = LiveSession(
        get_transcriber(),
        settings.upload_dir / wav_name,
        language=(language or "").strip() or None,
        window_seconds=settings.live_window_seconds,
        max_seconds=settings.live_max_minutes * 60,
    )
    wake, closing = asyncio.Event(), False

    async def worker() -> None:
        warned = False
        while True:
            await wake.wait()
            wake.clear()
            try:
                segments = await asyncio.to_thread(session.process_ready)
                if segments:
                    await _send(ws, {"type": "segments", "segments": segments})
            except Exception:
                logger.exception("Live transcription window failed")
                if not warned:
                    warned = True
                    await _send(ws, {"type": "warning", "detail": "Part of the audio could not be transcribed."})
            if closing:
                return

    task = asyncio.create_task(worker())
    await _send(ws, {"type": "ready"})
    logger.info("Live session started: %s", session.wav_path.name)

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if len(message["bytes"]) > MAX_FRAME_BYTES:
                    await _send(ws, {"type": "error", "detail": "Audio frame too large."})
                    break
                session.write(message["bytes"])
                if session.ready():
                    wake.set()
            elif message.get("text"):
                try:
                    if json.loads(message["text"]).get("type") == "stop":
                        break
                except (json.JSONDecodeError, AttributeError):
                    continue
    except LiveLimitError as exc:
        await _send(ws, {"type": "warning", "detail": str(exc)})
    finally:
        closing = True
        wake.set()
        await task
        try:
            final = await asyncio.to_thread(session.finish)
        except Exception:
            logger.exception("Live transcription flush failed")
            final = []
            session.close()
        if final:
            await _send(ws, {"type": "segments", "segments": final})

    if not session.text:
        session.wav_path.unlink(missing_ok=True)
        await _send(ws, {"type": "error", "detail": "No speech was detected, so nothing was saved."})
    else:
        meeting_id = await asyncio.to_thread(
            pipeline.save_live_meeting,
            title=title,
            participants=parse_participants(participants),
            stored_filename=wav_name,
            file_size=session.file_size,
            language=session.language,
            segments=session.segments,
            duration=session.duration,
        )
        await _send(ws, {"type": "done", "meeting_id": meeting_id})
    await _close(ws)
