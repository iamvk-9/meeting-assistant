"""REST API for meetings."""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import BUSY_STATES, ActionItem, Meeting, Participant, Summary, Transcript
from app.schemas import (
    ActionItemOut,
    ActionItemUpdate,
    EmailRequest,
    MeetingDetail,
    MeetingList,
    SummarizeRequest,
    TranscribeRequest,
)
from app.security import require_api_key, save_upload
from app.services import pipeline
from app.services.email_service import EmailNotConfiguredError, EmailSendError, send_summary_email
from app.services.pdf_export import build_meeting_pdf
from app.services.summarizer import SummarizationError
from app.services.transcription import TranscriptionError
from app.utils import parse_participants

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_api_key)], tags=["meetings"])


def _get_meeting(db: Session, meeting_id: int) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    return meeting


def _ensure_not_busy(meeting: Meeting) -> None:
    if meeting.status in BUSY_STATES:
        raise HTTPException(status_code=409, detail="This meeting is already being processed.")


@router.post("/upload-meeting", response_model=MeetingDetail, status_code=201)
def upload_meeting(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="MP3, WAV or MP4 recording"),
    title: str | None = Form(None),
    participants: str | None = Form(None, description="Comma-separated names or emails"),
    language: str | None = Form(None, description="Spoken language code; omit to auto-detect"),
    auto_process: bool = Form(False, description="Transcribe and summarize in the background"),
    db: Session = Depends(get_db),
) -> Meeting:
    settings = get_settings()
    original, stored, size = save_upload(file, settings.upload_dir, settings.max_upload_bytes)

    clean_title = " ".join((title or "").split())[:255]
    meeting = Meeting(
        title=clean_title or Path(original).stem[:255],
        title_from_user=bool(clean_title),
        original_filename=original,
        stored_filename=stored,
        file_size=size,
        language=(language or "").strip() or None,
        status="queued" if auto_process else "uploaded",
        participants=[Participant(name=p["name"], email=p["email"]) for p in parse_participants(participants)],
    )
    db.add(meeting)
    db.commit()
    logger.info("Upload accepted: meeting=%s file=%r size=%d auto_process=%s", meeting.id, original, size, auto_process)

    if auto_process:
        background_tasks.add_task(pipeline.process_meeting_background, meeting.id, meeting.language)
    return meeting


@router.post("/transcribe", response_model=MeetingDetail)
def transcribe(payload: TranscribeRequest, db: Session = Depends(get_db)) -> Meeting:
    """Transcribe the uploaded recording (runs synchronously)."""
    meeting = _get_meeting(db, payload.meeting_id)
    _ensure_not_busy(meeting)
    try:
        pipeline.transcribe_meeting(db, meeting, payload.language)
    except TranscriptionError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return meeting


@router.post("/summarize", response_model=MeetingDetail)
def summarize(payload: SummarizeRequest, db: Session = Depends(get_db)) -> Meeting:
    """Generate the structured summary and action items from the transcript (runs synchronously)."""
    meeting = _get_meeting(db, payload.meeting_id)
    _ensure_not_busy(meeting)
    try:
        pipeline.summarize_meeting(db, meeting)
    except pipeline.MeetingStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SummarizationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return meeting


@router.post("/meeting/{meeting_id}/process", response_model=MeetingDetail, status_code=202)
def process(meeting_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> Meeting:
    """Run any missing stages (transcribe, then summarize) in the background."""
    meeting = _get_meeting(db, meeting_id)
    _ensure_not_busy(meeting)
    meeting.status, meeting.error_message = "queued", None
    db.commit()
    background_tasks.add_task(pipeline.process_meeting_background, meeting.id, meeting.language)
    return meeting


@router.get("/meetings", response_model=MeetingList)
def list_meetings(
    q: str | None = Query(None, description="Search titles, transcripts, summaries and action items"),
    status: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Meeting)
    if status:
        stmt = stmt.where(Meeting.status == status)
    if q and q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                Meeting.title.ilike(like, escape="\\"),
                Meeting.transcript.has(Transcript.text.ilike(like, escape="\\")),
                Meeting.summary.has(Summary.overview.ilike(like, escape="\\")),
                Meeting.action_items.any(ActionItem.task.ilike(like, escape="\\")),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(Meeting.created_at.desc(), Meeting.id.desc()).limit(limit).offset(offset)).all()
    return {"items": items, "total": total}


@router.get("/meeting/{meeting_id}", response_model=MeetingDetail)
def get_meeting(meeting_id: int, db: Session = Depends(get_db)) -> Meeting:
    return _get_meeting(db, meeting_id)


@router.get("/meeting/{meeting_id}/export/pdf")
def export_pdf(meeting_id: int, include_transcript: bool = False, db: Session = Depends(get_db)) -> Response:
    meeting = _get_meeting(db, meeting_id)
    if meeting.summary is None:
        raise HTTPException(status_code=409, detail="Generate a summary before exporting a report.")
    pdf = build_meeting_pdf(meeting, include_transcript)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="meeting_{meeting.id}_summary.pdf"'},
    )


@router.post("/meeting/{meeting_id}/email")
def email_summary(meeting_id: int, payload: EmailRequest, db: Session = Depends(get_db)) -> dict[str, list[str]]:
    meeting = _get_meeting(db, meeting_id)
    if meeting.summary is None:
        raise HTTPException(status_code=409, detail="Generate a summary before emailing it.")
    recipients = [str(r) for r in payload.recipients] if payload.recipients else [
        p.email for p in meeting.participants if p.email
    ]
    if not recipients:
        raise HTTPException(status_code=422, detail="No recipients: add email addresses or participants with emails.")
    try:
        send_summary_email(meeting, recipients, build_meeting_pdf(meeting))
    except EmailNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except EmailSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"sent_to": recipients}


@router.patch("/action-items/{item_id}", response_model=ActionItemOut)
def update_action_item(item_id: int, payload: ActionItemUpdate, db: Session = Depends(get_db)) -> ActionItem:
    item = db.get(ActionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Action item not found.")
    item.completed = payload.completed
    db.commit()
    return item


@router.delete("/meeting/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int, db: Session = Depends(get_db)) -> Response:
    meeting = _get_meeting(db, meeting_id)
    (Path(get_settings().upload_dir) / meeting.stored_filename).unlink(missing_ok=True)
    db.delete(meeting)
    db.commit()
    logger.info("Meeting %s deleted", meeting_id)
    return Response(status_code=204)
