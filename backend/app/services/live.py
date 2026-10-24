"""Live transcription: browser microphone audio (16 kHz mono PCM16) -> rolling Whisper windows.

Audio is appended to a WAV file as it arrives (so memory stays small and the recording becomes a normal
meeting afterwards) and transcribed in windows of `window_seconds`. Whisper is told what was said so far
(`initial_prompt`) to keep names and wording consistent across windows. Silent windows are skipped, which
avoids Whisper's habit of hallucinating text on silence. The language is auto-detected once, then locked.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.services.transcription import TranscriptionResult

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM16 mono
SILENCE_RMS = 0.005  # float amplitude below which a window is treated as silence
MIN_FLUSH_SECONDS = 0.5  # a final remainder shorter than this is dropped
PROMPT_CHARS = 200


class LiveLimitError(Exception):
    """The session exceeded its maximum length."""


class AudioTranscriber(Protocol):
    def transcribe_audio(
        self, audio: np.ndarray, language: str | None = None, initial_prompt: str | None = None
    ) -> TranscriptionResult: ...


class LiveSession:
    def __init__(
        self,
        transcriber: AudioTranscriber,
        wav_path: Path,
        language: str | None = None,
        window_seconds: int = 8,
        max_seconds: int = 7200,
    ) -> None:
        self._transcriber = transcriber
        self._window_bytes = max(1, int(window_seconds)) * BYTES_PER_SECOND
        self._max_bytes = max_seconds * BYTES_PER_SECOND
        self.language = language or None
        self.wav_path = wav_path
        self.segments: list[dict[str, Any]] = []

        self._lock = threading.Lock()
        self._pending = bytearray()
        self._carry = b""  # a lone byte left over when a frame splits a 16-bit sample
        self._received = 0  # bytes written to the WAV
        self._taken = 0  # bytes already handed to Whisper (or skipped)
        self._wav = wave.open(str(wav_path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)

    # ---- receiving audio -------------------------------------------------
    @property
    def duration(self) -> float:
        return self._received / BYTES_PER_SECOND

    @property
    def text(self) -> str:
        return " ".join(s["text"] for s in self.segments).strip()

    @property
    def file_size(self) -> int:
        return self.wav_path.stat().st_size if self.wav_path.exists() else 0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            data = self._carry + pcm
            usable = len(data) - (len(data) % 2)
            data, self._carry = data[:usable], data[usable:]
            if self._received + len(data) > self._max_bytes:
                raise LiveLimitError("Maximum live session length reached.")
            self._wav.writeframes(data)
            self._pending += data
            self._received += len(data)

    def ready(self) -> bool:
        with self._lock:
            return len(self._pending) >= self._window_bytes

    # ---- transcribing ----------------------------------------------------
    def _take(self, force: bool) -> tuple[bytes, float] | None:
        with self._lock:
            size = len(self._pending)
            if size < self._window_bytes and not force:
                return None
            start = self._taken / BYTES_PER_SECOND
            if force and size < MIN_FLUSH_SECONDS * BYTES_PER_SECOND:
                self._taken += size
                self._pending.clear()
                return None
            chunk = bytes(self._pending[: self._window_bytes] if not force else self._pending)
            del self._pending[: len(chunk)]
            self._taken += len(chunk)
            return chunk, start

    def _transcribe_chunk(self, pcm: bytes, start: float) -> list[dict[str, Any]]:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(np.square(audio)))) < SILENCE_RMS:
            return []
        prompt = self.text[-PROMPT_CHARS:] or None
        result = self._transcriber.transcribe_audio(audio, language=self.language, initial_prompt=prompt)
        if self.language is None and result.language:
            self.language = result.language  # lock the detected language for the rest of the session
        new: list[dict[str, Any]] = []
        for seg in result.segments:
            text = seg["text"].strip()
            if not text:
                continue
            new.append(
                {
                    "id": len(self.segments) + len(new),
                    "start": round(start + seg["start"], 2),
                    "end": round(start + seg["end"], 2),
                    "text": text,
                }
            )
        self.segments.extend(new)
        return new

    def process_ready(self) -> list[dict[str, Any]]:
        """Transcribe every complete window received so far. Safe to call from a worker thread."""
        out: list[dict[str, Any]] = []
        while (chunk := self._take(force=False)) is not None:
            out.extend(self._transcribe_chunk(*chunk))
        return out

    def finish(self) -> list[dict[str, Any]]:
        """Transcribe what is left (including a partial last window) and close the recording."""
        out = self.process_ready()
        if (chunk := self._take(force=True)) is not None:
            out.extend(self._transcribe_chunk(*chunk))
        self.close()
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._wav.close()
            except Exception:  # already closed
                logger.debug("WAV already closed", exc_info=True)
