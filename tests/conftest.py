import io
import os
import tempfile
import wave

_TMP = tempfile.mkdtemp(prefix="meeting-tests-")
os.environ.update(
    {
        "DATABASE_URL": "sqlite://",
        "UPLOAD_DIR": f"{_TMP}/uploads",
        "LOG_DIR": f"{_TMP}/logs",
        "API_KEY": "",
        "GEMINI_API_KEY": "test-key",
        "LLM_MODEL": "gemini/test-model",
        "SMTP_HOST": "",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

TRANSCRIPT_TEXT = "We decided to launch on Friday. Alice will prepare the launch deck by Thursday."


class FakeTranscriber:
    def transcribe(self, path, language=None, on_progress=None):
        from app.services.transcription import TranscriptionResult

        if on_progress:
            on_progress(0.5)
        return TranscriptionResult(
            text=TRANSCRIPT_TEXT,
            language=language or "en",
            segments=[{"id": 0, "start": 0.0, "end": 4.0, "text": TRANSCRIPT_TEXT}],
            duration=4.0,
        )


class FakeSummarizer:
    def summarize(self, transcript):
        from app.services.summarizer import ActionItemData, SummaryData

        return SummaryData(
            title="Launch planning",
            overview="The team agreed to launch on Friday.",
            key_points=["Launch date", "Launch deck"],
            decisions=["Launch on Friday"],
            risks=["Tight timeline"],
            action_items=[ActionItemData(task="Prepare the launch deck", owner="Alice", due_date="Thursday")],
            sentiment="positive",
        )


def make_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return buf.getvalue()


@pytest.fixture()
def wav_bytes() -> bytes:
    return make_wav()


@pytest.fixture()
def session_factory(monkeypatch):
    from app import database, models  # noqa: F401

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    yield factory
    database.Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(session_factory, monkeypatch):
    from app.database import get_db
    from app.main import app

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.services.pipeline.get_transcriber", lambda: FakeTranscriber())
    monkeypatch.setattr("app.services.pipeline.get_summarizer", lambda: FakeSummarizer())
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def meeting_id(client, wav_bytes) -> int:
    resp = client.post(
        "/upload-meeting",
        files={"file": ("standup.wav", wav_bytes, "audio/wav")},
        data={"participants": "Alice <alice@example.com>, bob@example.com"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture()
def summarized_meeting_id(client, meeting_id) -> int:
    assert client.post("/transcribe", json={"meeting_id": meeting_id}).status_code == 200
    assert client.post("/summarize", json={"meeting_id": meeting_id}).status_code == 200
    return meeting_id
