"""Speech-to-text with OpenAI Whisper (runs locally; model is loaded lazily, once)."""

import logging
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


class TranscriptionError(Exception):
    pass


def friendly_transcription_error(exc: Exception) -> str:
    """Turn Whisper/ffmpeg failures (which embed ffmpeg's whole banner) into a short, readable message."""
    raw = str(exc)
    lowered = raw.lower()
    if "does not contain any stream" in lowered or "output file #0 does not contain" in lowered:
        return "Transcription failed: this file has no audio track (is it a silent video?)."
    if "invalid data found" in lowered or "moov atom not found" in lowered:
        return "Transcription failed: the file is corrupted or not a valid audio/video recording."
    if "failed to load audio" in lowered:
        lines = [ln.strip() for ln in raw.replace("\r", "\n").splitlines() if ln.strip()]
        detail = lines[-1] if lines else "unknown error"
        return f"Transcription failed: the audio could not be decoded ({detail[:200]})."
    return f"Transcription failed: {raw[:300]}"


@dataclass
class TranscriptionResult:
    text: str
    language: str | None
    segments: list[dict] = field(default_factory=list)
    duration: float | None = None


class _ProgressBar:
    """Stands in for the tqdm bar Whisper updates after each 30-second window."""

    def __init__(
        self, callback: Callable[[float], None], *args: Any, total: int | None = None, **kwargs: Any
    ) -> None:
        self._callback, self.total, self.n = callback, total or 0, 0

    def __enter__(self) -> "_ProgressBar":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def update(self, n: int = 1) -> None:
        self.n += n
        if self.total:
            try:
                self._callback(min(self.n / self.total, 1.0))
            except Exception:  # progress reporting must never break transcription
                logger.debug("progress callback failed", exc_info=True)

    def __getattr__(self, name: str) -> Callable[..., None]:  # set_description, close, ...
        return lambda *a, **k: None


@contextmanager
def _whisper_progress(callback: Callable[[float], None] | None) -> Iterator[None]:
    """Temporarily route Whisper's progress bar to `callback`. No-op if Whisper's internals differ."""
    module = sys.modules.get("whisper.transcribe")  # note: `whisper.transcribe` the attribute is a function
    if callback is None or module is None or not hasattr(getattr(module, "tqdm", None), "tqdm"):
        yield
        return
    original = module.tqdm
    module.tqdm = SimpleNamespace(tqdm=lambda *a, **k: _ProgressBar(callback, *a, **k))
    try:
        yield
    finally:
        module.tqdm = original


class WhisperTranscriber:
    def __init__(self, model_size: str = "base") -> None:
        self.model_size = model_size
        self._model = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()  # Whisper models are not safe for concurrent use

    def _get_model(self) -> Any:
        with self._load_lock:
            if self._model is None:
                try:
                    import torch
                    import whisper
                except ImportError as exc:
                    raise TranscriptionError("openai-whisper / torch are not installed.") from exc
                from app.services.ffmpeg import ensure_ffmpeg

                ensure_ffmpeg()
                device = "cuda" if torch.cuda.is_available() else "cpu"
                logger.info("Loading Whisper '%s' model on %s", self.model_size, device)
                self._model = whisper.load_model(self.model_size, device=device)
            return self._model

    def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> TranscriptionResult:
        model = self._get_model()
        if on_progress:
            on_progress(0.0)  # model is loaded; real transcription starts now
        logger.info("Transcribing %s (language=%s)", audio_path.name, language or "auto")
        try:
            with self._run_lock, _whisper_progress(on_progress):
                result = model.transcribe(
                    str(audio_path),
                    language=language or None,
                    verbose=False,
                    fp16=model.device.type == "cuda",
                )
        except Exception as exc:
            logger.exception("Whisper failed on %s", audio_path.name)
            raise TranscriptionError(friendly_transcription_error(exc)) from exc

        return self._build_result(result)

    def transcribe_audio(
        self, audio: Any, language: str | None = None, initial_prompt: str | None = None
    ) -> TranscriptionResult:
        """Transcribe a float32 16 kHz mono numpy array (used for live transcription)."""
        model = self._get_model()
        try:
            with self._run_lock:
                result = model.transcribe(
                    audio,
                    language=language or None,
                    initial_prompt=initial_prompt,
                    condition_on_previous_text=False,
                    verbose=None,
                    fp16=model.device.type == "cuda",
                )
        except Exception as exc:
            logger.exception("Whisper failed on a live audio window")
            raise TranscriptionError(friendly_transcription_error(exc)) from exc
        return self._build_result(result)

    @staticmethod
    def _build_result(result: dict[str, Any]) -> TranscriptionResult:
        segments = [
            {"id": s["id"], "start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
            for s in result.get("segments", [])
        ]
        text = (result.get("text") or "").strip()
        duration = segments[-1]["end"] if segments else None
        return TranscriptionResult(text=text, language=result.get("language"), segments=segments, duration=duration)


@lru_cache
def get_transcriber() -> WhisperTranscriber:
    return WhisperTranscriber(get_settings().whisper_model)
