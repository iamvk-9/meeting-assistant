"""Calendar (.ics) download and action-item export to Jira / Trello."""

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import ActionItem, Meeting
from app.schemas import ExportRequest, ExportResponse
from app.security import require_api_key
from app.services.calendar_export import build_ics
from app.services.task_export import (
    ExportResult,
    TaskExportError,
    TaskExportNotConfiguredError,
    export_to_jira,
    export_to_trello,
)

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_api_key)], tags=["exports"])


def _meeting(db: Session, meeting_id: int) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    return meeting


def _select_items(meeting: Meeting, payload: ExportRequest) -> list[ActionItem]:
    if payload.item_ids is not None:
        items = [a for a in meeting.action_items if a.id in set(payload.item_ids)]
    else:
        items = [a for a in meeting.action_items if not a.completed]
    if not items:
        raise HTTPException(status_code=409, detail="There are no action items to export.")
    return items


def _run_export(
    exporter: Callable[[Meeting, list[ActionItem], Settings], ExportResult],
    meeting_id: int,
    payload: ExportRequest,
    db: Session,
) -> ExportResult:
    meeting = _meeting(db, meeting_id)
    items = _select_items(meeting, payload)
    try:
        result = exporter(meeting, items, get_settings())
    except TaskExportNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except TaskExportError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not result.exported:
        first = result.failed[0]["error"] if result.failed else "unknown error"
        raise HTTPException(status_code=502, detail=f"Nothing could be exported: {first}")
    logger.info(
        "Meeting %s: exported %d action items (%d failed)", meeting_id, len(result.exported), len(result.failed)
    )
    return result


@router.get("/meeting/{meeting_id}/export/calendar", summary="Download an .ics calendar file")
def export_calendar(meeting_id: int, db: Session = Depends(get_db)) -> Response:
    meeting = _meeting(db, meeting_id)
    return Response(
        content=build_ics(meeting),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="meeting_{meeting.id}.ics"'},
    )


@router.post("/meeting/{meeting_id}/export/jira", response_model=ExportResponse)
def export_jira(
    meeting_id: int, payload: ExportRequest | None = None, db: Session = Depends(get_db)
) -> ExportResponse:
    result = _run_export(export_to_jira, meeting_id, payload or ExportRequest(), db)
    return ExportResponse(exported=result.exported, failed=result.failed)


@router.post("/meeting/{meeting_id}/export/trello", response_model=ExportResponse)
def export_trello(
    meeting_id: int, payload: ExportRequest | None = None, db: Session = Depends(get_db)
) -> ExportResponse:
    result = _run_export(export_to_trello, meeting_id, payload or ExportRequest(), db)
    return ExportResponse(exported=result.exported, failed=result.failed)
