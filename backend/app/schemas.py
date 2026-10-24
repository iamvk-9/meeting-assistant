"""Pydantic request/response models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ParticipantOut(ORMModel):
    id: int
    name: str | None = None
    email: str | None = None


class ActionItemOut(ORMModel):
    id: int
    task: str
    owner: str | None = None
    due_date: str | None = None
    completed: bool = False


class SummaryOut(ORMModel):
    overview: str
    key_points: list[str]
    decisions: list[str]
    risks: list[str]
    sentiment: str | None = None
    model_name: str | None = None
    created_at: datetime


class TranscriptOut(ORMModel):
    text: str
    language: str | None = None
    segments: list[dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float | None = None


class MeetingListItem(ORMModel):
    id: int
    title: str
    status: str
    original_filename: str
    language: str | None = None
    created_at: datetime


class MeetingList(BaseModel):
    items: list[MeetingListItem]
    total: int


class MeetingDetail(MeetingListItem):
    file_size: int
    error_message: str | None = None
    progress: float | None = Field(
        default=None, description="Transcription progress 0-1 while status is 'transcribing'"
    )
    updated_at: datetime
    participants: list[ParticipantOut] = Field(default_factory=list)
    transcript: TranscriptOut | None = None
    summary: SummaryOut | None = None
    action_items: list[ActionItemOut] = Field(default_factory=list)


class TranscribeRequest(BaseModel):
    meeting_id: int
    language: str | None = Field(default=None, description="Spoken language code, e.g. 'en'. Omit to auto-detect.")


class SummarizeRequest(BaseModel):
    meeting_id: int


class EmailRequest(BaseModel):
    recipients: list[EmailStr] | None = Field(
        default=None, description="Defaults to the meeting participants that have an email address."
    )


class ActionItemUpdate(BaseModel):
    completed: bool


class ExportRequest(BaseModel):
    item_ids: list[int] | None = Field(
        default=None, description="Action items to export. Defaults to every item that is not completed."
    )


class ExportedTaskOut(BaseModel):
    item_id: int
    task: str
    ref: str = Field(description="Jira issue key or Trello card id")
    url: str | None = None


class FailedTaskOut(BaseModel):
    item_id: int
    task: str
    error: str


class ExportResponse(BaseModel):
    exported: list[ExportedTaskOut] = Field(default_factory=list)
    failed: list[FailedTaskOut] = Field(default_factory=list)
