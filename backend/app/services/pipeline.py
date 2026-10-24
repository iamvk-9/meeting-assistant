"""Orchestrates transcription and summarization and keeps meeting status/DB rows consistent."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import database
from app.config import get_settings
from app.progress import clear_progress, set_progress
from app.models import BUSY_STATES, ActionItem, Meeting, Participant, Summary, Transcript
from app.services.diarization import DiarizationError, assign_speakers, get_diarizer
from app.services.summarizer import SummarizationError, get_summarizer
from app.services.transcription import TranscriptionError, get_transcriber

logger = logging.getLogger(__name__)


class MeetingStateError(Exception):
    """The requested step is not possible in the meeting's current state."""


def _mark_failed(db: Session, meeting: Meeting, message: str) -> None:
    meeting.status = "failed"
    meeting.error_message = message[:2000]
    db.commit()
    logger.error("Meeting %s failed: %s", meeting.id, message)


def transcribe_meeting(db: Session, meeting: Meeting, language: str | None = None) -> Transcript:
    settings = get_settings()
    path = Path(settings.upload_dir) / meeting.stored_filename
    if not path.exists():
        _mark_failed(db, meeting, "The uploaded file is missing on the server.")
        raise TranscriptionError("The uploaded file is missing on the server.")

    meeting.status, meeting.error_message = "transcribing", None
    db.commit()
    try:
        result = get_transcriber().transcribe(
            path,
            language or meeting.language or settings.default_language or None,
            on_progress=lambda fraction: set_progress(meeting.id, fraction),
        )
        if not result.text:
            raise TranscriptionError("No speech was detected in the recording.")
    except TranscriptionError as exc:
        _mark_failed(db, meeting, str(exc))
        raise
    finally:
        clear_progress(meeting.id)

    segments = result.segments
    if settings.diarization_enabled and segments:
        try:
            turns = get_diarizer().diarize(path)
            segments = assign_speakers(segments, turns)
            speakers = {seg.get("speaker") for seg in segments} - {None}
            logger.info("Meeting %s: identified %d speakers", meeting.id, len(speakers))
        except DiarizationError as exc:  # optional feature: never fail the meeting because of it
            logger.warning("Meeting %s: speaker identification skipped: %s", meeting.id, exc)

    if meeting.transcript is None:
        meeting.transcript = Transcript(text="")
    transcript = meeting.transcript
    transcript.text = result.text
    transcript.language = result.language
    transcript.segments = segments
    transcript.duration_seconds = result.duration
    meeting.language = result.language
    meeting.status = "transcribed"
    db.commit()
    logger.info("Meeting %s transcribed (%d chars, language=%s)", meeting.id, len(result.text), result.language)
    return transcript


def summarize_meeting(db: Session, meeting: Meeting) -> Summary:
    if meeting.transcript is None or not meeting.transcript.text:
        raise MeetingStateError("This meeting has no transcript yet. Transcribe it first.")

    meeting.status, meeting.error_message = "summarizing", None
    db.commit()
    try:
        data = get_summarizer().summarize(meeting.transcript.text)
    except SummarizationError as exc:
        _mark_failed(db, meeting, str(exc))
        raise

    if meeting.summary is None:
        meeting.summary = Summary()
    summary = meeting.summary
    summary.overview = data.overview
    summary.key_points = data.key_points
    summary.decisions = data.decisions
    summary.risks = data.risks
    summary.sentiment = data.sentiment
    summary.model_name = get_settings().llm_model

    meeting.action_items.clear()
    meeting.action_items.extend(
        ActionItem(task=a.task, owner=a.owner, due_date=a.due_date) for a in data.action_items if a.task.strip()
    )
    if data.title and not meeting.title_from_user:
        meeting.title = data.title.strip()[:255]
    meeting.status = "completed"
    db.commit()
    logger.info("Meeting %s summarized (%d action items)", meeting.id, len(meeting.action_items))
    return summary


def process_meeting_background(meeting_id: int, language: str | None = None) -> None:
    """Run whichever stages are still missing. Used as a FastAPI background task."""
    db = database.SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return
        if meeting.transcript is None:
            transcribe_meeting(db, meeting, language)
        summarize_meeting(db, meeting)
    except (TranscriptionError, SummarizationError, MeetingStateError) as exc:
        logger.error("Background processing stopped for meeting %s: %s", meeting_id, exc)
    except Exception:
        logger.exception("Unexpected error while processing meeting %s", meeting_id)
        try:
            db.rollback()
            meeting = db.get(Meeting, meeting_id)
            if meeting is not None:
                _mark_failed(db, meeting, "Unexpected error during processing. See server logs.")
        except Exception:
            logger.exception("Could not record failure for meeting %s", meeting_id)
    finally:
        db.close()


def save_live_meeting(
    *,
    title: str | None,
    participants: list[dict[str, str | None]],
    stored_filename: str,
    file_size: int,
    language: str | None,
    segments: list[dict],
    duration: float,
) -> int:
    """Store a finished live session as a normal, already-transcribed meeting. Returns the meeting id."""
    text = " ".join(s["text"] for s in segments).strip()
    clean = " ".join((title or "").split())[:255]
    with database.SessionLocal() as db:
        meeting = Meeting(
            title=clean or f"Live meeting {datetime.now(timezone.utc):%d %b %Y %H:%M}",
            title_from_user=bool(clean),
            original_filename="live-recording.wav",
            stored_filename=stored_filename,
            file_size=file_size,
            language=language,
            status="transcribed",
            participants=[Participant(name=p["name"], email=p["email"]) for p in participants],
            transcript=Transcript(text=text, language=language, segments=segments, duration_seconds=duration),
        )
        db.add(meeting)
        db.commit()
        logger.info("Live meeting %s saved (%.0fs, %d chars)", meeting.id, duration, len(text))
        return meeting.id


def recover_interrupted_meetings() -> int:
    """At startup, meetings stuck in a busy state were interrupted by a restart: mark them failed."""
    with database.SessionLocal() as db:
        stuck = db.scalars(select(Meeting).where(Meeting.status.in_(BUSY_STATES))).all()
        for meeting in stuck:
            meeting.status = "failed"
            meeting.error_message = "Processing was interrupted by a server restart. Please retry."
        db.commit()
        return len(stuck)
