"""Optional speaker identification ("who spoke when") with pyannote.audio.

Disabled by default. Enable with DIARIZATION_ENABLED=true, install requirements-diarization.txt and
provide HF_TOKEN. Speakers are labelled "Speaker 1", "Speaker 2", ... in order of first appearance.
Any failure here is non-fatal: the meeting is still transcribed and summarized without speaker labels.
"""

import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

Turn = tuple[float, float, str]  # (start, end, raw speaker label)


class DiarizationError(Exception):
    pass


def assign_speakers(segments: list[dict], turns: list[Turn]) -> list[dict]:
    """Label each transcript segment with the speaker whose turns overlap it the most."""
    labels: dict[str, str] = {}
    for _, _, raw in sorted(turns, key=lambda t: t[0]):
        labels.setdefault(raw, f"Speaker {len(labels) + 1}")

    result = []
    for seg in segments:
        overlap: dict[str, float] = {}
        for start, end, raw in turns:
            shared = min(seg["end"], end) - max(seg["start"], start)
            if shared > 0:
                overlap[raw] = overlap.get(raw, 0.0) + shared
        labelled = dict(seg)
        if overlap:
            labelled["speaker"] = labels[max(overlap, key=overlap.get)]
        result.append(labelled)
    return result


def speaker_labelled_text(segments: list[dict]) -> str:
    """Render segments as 'Speaker 1: ...' paragraphs, merging consecutive turns of one speaker."""
    lines: list[str] = []
    last = None
    for seg in segments:
        speaker = seg.get("speaker")
        if speaker and speaker == last and lines:
            lines[-1] += " " + seg["text"]
        else:
            lines.append(f"{speaker}: {seg['text']}" if speaker else seg["text"])
        last = speaker
    return "\n".join(lines)


class PyannoteDiarizer:
    def __init__(self, token: str | None) -> None:
        self._token = token
        self._pipeline = None
        self._lock = threading.Lock()

    def _get_pipeline(self) -> Any:
        with self._lock:
            if self._pipeline is None:
                if not self._token:
                    raise DiarizationError("HF_TOKEN is not set (needed to download the pyannote model).")
                try:
                    from pyannote.audio import Pipeline
                except ImportError as exc:
                    raise DiarizationError("pyannote.audio is not installed.") from exc
                name = "pyannote/speaker-diarization-3.1"
                try:
                    try:
                        self._pipeline = Pipeline.from_pretrained(name, token=self._token)
                    except TypeError:  # older pyannote releases use `use_auth_token`
                        self._pipeline = Pipeline.from_pretrained(name, use_auth_token=self._token)
                except Exception as exc:
                    raise DiarizationError(f"Could not load the diarization model: {exc}") from exc
                if self._pipeline is None:
                    raise DiarizationError("Could not load the diarization model (accept its terms on Hugging Face).")
            return self._pipeline

    def diarize(self, audio_path: Path) -> list[Turn]:
        pipeline = self._get_pipeline()
        try:
            import torch
            import whisper
            from app.services.ffmpeg import ensure_ffmpeg

            ensure_ffmpeg()
            audio = whisper.load_audio(str(audio_path))  # ffmpeg-decoded 16 kHz mono, works for mp3/wav/mp4
            output = pipeline({"waveform": torch.from_numpy(audio)[None, :], "sample_rate": 16000})
            annotation = getattr(output, "speaker_diarization", output)  # pyannote 4 wraps the annotation
            return [(t.start, t.end, spk) for t, _, spk in annotation.itertracks(yield_label=True)]
        except DiarizationError:
            raise
        except Exception as exc:
            raise DiarizationError(f"Speaker identification failed: {exc}") from exc


@lru_cache
def get_diarizer() -> PyannoteDiarizer:
    return PyannoteDiarizer(get_settings().hf_token)
