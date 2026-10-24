import pytest

from app.config import get_settings


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---- upload ---------------------------------------------------------------------------------
def test_upload_creates_meeting_with_participants(client, wav_bytes):
    resp = client.post(
        "/upload-meeting",
        files={"file": ("Weekly Sync.wav", wav_bytes, "audio/wav")},
        data={"title": "  Weekly   sync ", "participants": "Alice <alice@example.com>, Carol"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Weekly sync" and body["status"] == "uploaded"
    assert [(p["name"], p["email"]) for p in body["participants"]] == [
        ("Alice", "alice@example.com"),
        ("Carol", None),
    ]


def test_upload_defaults_title_to_filename(client, wav_bytes):
    resp = client.post("/upload-meeting", files={"file": ("client-call.wav", wav_bytes, "audio/wav")})
    assert resp.json()["title"] == "client-call"


@pytest.mark.parametrize(
    "name,data,status",
    [("notes.txt", b"hello", 415), ("fake.mp3", b"definitely not an mp3 file", 415), ("e.wav", b"", 400)],
)
def test_upload_rejects_invalid_files(client, name, data, status):
    assert client.post("/upload-meeting", files={"file": (name, data)}).status_code == status


def test_upload_with_auto_process_runs_pipeline(client, wav_bytes):
    resp = client.post(
        "/upload-meeting",
        files={"file": ("auto.wav", wav_bytes, "audio/wav")},
        data={"auto_process": "true"},
    )
    assert resp.status_code == 201 and resp.json()["status"] == "queued"
    detail = client.get(f"/meeting/{resp.json()['id']}").json()  # background task already ran
    assert detail["status"] == "completed"
    assert detail["title"] == "Launch planning"  # generated, because no title was given
    assert detail["summary"]["decisions"] == ["Launch on Friday"]


# ---- transcribe / summarize -----------------------------------------------------------------
def test_transcribe_stores_transcript(client, meeting_id):
    resp = client.post("/transcribe", json={"meeting_id": meeting_id, "language": "en"})
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "transcribed"
    assert "launch on Friday" in body["transcript"]["text"]
    assert body["transcript"]["segments"][0]["start"] == 0.0


def test_transcribe_failure_marks_meeting_failed(client, meeting_id, monkeypatch):
    from app.services.transcription import TranscriptionError

    class Broken:
        def transcribe(self, path, language=None, on_progress=None):
            raise TranscriptionError("boom")

    monkeypatch.setattr("app.services.pipeline.get_transcriber", lambda: Broken())
    resp = client.post("/transcribe", json={"meeting_id": meeting_id})
    assert resp.status_code == 500
    detail = client.get(f"/meeting/{meeting_id}").json()
    assert detail["status"] == "failed" and detail["error_message"] == "boom"


def test_summarize_requires_transcript(client, meeting_id):
    assert client.post("/summarize", json={"meeting_id": meeting_id}).status_code == 409


def test_summarize_creates_summary_and_action_items(client, summarized_meeting_id):
    body = client.get(f"/meeting/{summarized_meeting_id}").json()
    assert body["status"] == "completed"
    assert body["summary"]["overview"].startswith("The team agreed")
    assert body["action_items"][0]["owner"] == "Alice"


def test_resummarize_replaces_instead_of_duplicating(client, summarized_meeting_id):
    client.post("/summarize", json={"meeting_id": summarized_meeting_id})
    assert len(client.get(f"/meeting/{summarized_meeting_id}").json()["action_items"]) == 1


def test_summarize_llm_failure_returns_502_and_keeps_transcript(client, meeting_id, monkeypatch):
    from app.services.summarizer import SummarizationError

    client.post("/transcribe", json={"meeting_id": meeting_id})

    class Broken:
        def summarize(self, transcript):
            raise SummarizationError("llm down")

    monkeypatch.setattr("app.services.pipeline.get_summarizer", lambda: Broken())
    assert client.post("/summarize", json={"meeting_id": meeting_id}).status_code == 502
    detail = client.get(f"/meeting/{meeting_id}").json()
    assert detail["status"] == "failed" and detail["transcript"] is not None


def test_unknown_meeting_is_404(client):
    assert client.get("/meeting/9999").status_code == 404
    assert client.post("/transcribe", json={"meeting_id": 9999}).status_code == 404


def test_process_endpoint_runs_missing_stages(client, meeting_id):
    assert client.post(f"/meeting/{meeting_id}/process").status_code == 202
    assert client.get(f"/meeting/{meeting_id}").json()["status"] == "completed"


# ---- list / search --------------------------------------------------------------------------
def test_list_and_search(client, wav_bytes, summarized_meeting_id):
    client.post(
        "/upload-meeting", files={"file": ("other.wav", wav_bytes, "audio/wav")}, data={"title": "Budget review"}
    )
    assert client.get("/meetings").json()["total"] == 2

    by_title = client.get("/meetings", params={"q": "budget"}).json()
    assert [m["title"] for m in by_title["items"]] == ["Budget review"]

    by_transcript = client.get("/meetings", params={"q": "launch deck"}).json()
    assert by_transcript["total"] == 1 and by_transcript["items"][0]["id"] == summarized_meeting_id

    assert client.get("/meetings", params={"q": "prepare the launch"}).json()["total"] == 1  # action item text
    assert client.get("/meetings", params={"q": "zzz-nothing"}).json()["total"] == 0
    assert client.get("/meetings", params={"q": "%"}).json()["total"] == 0  # wildcards are escaped
    assert client.get("/meetings", params={"status": "completed"}).json()["total"] == 1


# ---- export / email / misc ------------------------------------------------------------------
def test_pdf_export(client, summarized_meeting_id):
    resp = client.get(f"/meeting/{summarized_meeting_id}/export/pdf", params={"include_transcript": "true"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF") and len(resp.content) > 1000


def test_pdf_export_requires_summary(client, meeting_id):
    assert client.get(f"/meeting/{meeting_id}/export/pdf").status_code == 409


def test_email_defaults_to_participants(client, summarized_meeting_id, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "app.routers.meetings.send_summary_email",
        lambda meeting, recipients, pdf: sent.update(recipients=recipients, pdf=pdf),
    )
    resp = client.post(f"/meeting/{summarized_meeting_id}/email", json={})
    assert resp.status_code == 200
    assert sent["recipients"] == ["alice@example.com", "bob@example.com"] and sent["pdf"].startswith(b"%PDF")


def test_email_explicit_recipients_and_validation(client, summarized_meeting_id, monkeypatch):
    monkeypatch.setattr("app.routers.meetings.send_summary_email", lambda *a, **k: None)
    ok = client.post(f"/meeting/{summarized_meeting_id}/email", json={"recipients": ["boss@example.com"]})
    assert ok.json() == {"sent_to": ["boss@example.com"]}
    resp = client.post(f"/meeting/{summarized_meeting_id}/email", json={"recipients": ["not-an-email"]})
    assert resp.status_code == 422


def test_email_without_smtp_config_returns_503(client, summarized_meeting_id):
    resp = client.post(f"/meeting/{summarized_meeting_id}/email", json={})
    assert resp.status_code == 503 and "SMTP_HOST" in resp.json()["detail"]


def test_toggle_action_item(client, summarized_meeting_id):
    item = client.get(f"/meeting/{summarized_meeting_id}").json()["action_items"][0]
    resp = client.patch(f"/action-items/{item['id']}", json={"completed": True})
    assert resp.json()["completed"] is True
    assert client.patch("/action-items/9999", json={"completed": True}).status_code == 404


def test_delete_meeting_removes_file_and_rows(client, summarized_meeting_id):
    files_before = set(get_settings().upload_dir.glob("*.wav"))
    assert client.delete(f"/meeting/{summarized_meeting_id}").status_code == 204
    assert client.get(f"/meeting/{summarized_meeting_id}").status_code == 404
    assert len(set(get_settings().upload_dir.glob("*.wav"))) == len(files_before) - 1


def test_api_key_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key", "s3cret")
    assert client.get("/meetings").status_code == 401
    assert client.get("/meetings", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/meetings", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # health stays open


def test_recover_interrupted_meetings(client, meeting_id, session_factory):
    from app.models import Meeting
    from app.services.pipeline import recover_interrupted_meetings

    with session_factory() as db:
        db.get(Meeting, meeting_id).status = "transcribing"
        db.commit()
    assert recover_interrupted_meetings() == 1
    assert client.get(f"/meeting/{meeting_id}").json()["status"] == "failed"


def test_real_mp3_sample_passes_upload_validation(client):
    from pathlib import Path

    sample = Path(__file__).resolve().parents[1] / "docs" / "sample" / "sample_meeting.mp3"
    if not sample.exists():
        pytest.skip("sample recording not present")
    resp = client.post("/upload-meeting", files={"file": (sample.name, sample.read_bytes(), "audio/mpeg")})
    assert resp.status_code == 201 and resp.json()["original_filename"] == "sample_meeting.mp3"
