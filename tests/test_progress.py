import sys
import types

from app.progress import clear_progress, get_progress, set_progress
from app.services.transcription import _whisper_progress


def test_progress_store_clamps_and_clears():
    set_progress(7, 1.7)
    assert get_progress(7) == 1.0
    set_progress(7, -3)
    assert get_progress(7) == 0.0
    clear_progress(7)
    assert get_progress(7) is None


def test_whisper_progress_hook_reports_fraction_and_restores(monkeypatch):
    original = types.SimpleNamespace(tqdm=lambda **kw: None)
    fake_module = types.ModuleType("whisper.transcribe")
    fake_module.tqdm = original
    monkeypatch.setitem(sys.modules, "whisper.transcribe", fake_module)

    seen = []
    with _whisper_progress(seen.append):
        with fake_module.tqdm.tqdm(total=100, unit="frames", disable=False) as bar:
            bar.update(25)
            bar.set_description("ignored")  # unknown methods must be harmless
            bar.update(75)
    assert seen == [0.25, 1.0]
    assert fake_module.tqdm is original  # restored


def test_whisper_progress_hook_is_noop_when_whisper_internals_differ(monkeypatch):
    fake_module = types.ModuleType("whisper.transcribe")
    fake_module.tqdm = object()  # e.g. `from tqdm import tqdm` in a future version
    monkeypatch.setitem(sys.modules, "whisper.transcribe", fake_module)
    sentinel = fake_module.tqdm
    with _whisper_progress(lambda f: None):
        pass
    assert fake_module.tqdm is sentinel


def test_api_exposes_progress_only_while_transcribing(client, meeting_id, session_factory):
    from app.models import Meeting

    set_progress(meeting_id, 0.42)
    assert client.get(f"/meeting/{meeting_id}").json()["progress"] is None  # status is 'uploaded'
    with session_factory() as db:
        db.get(Meeting, meeting_id).status = "transcribing"
        db.commit()
    assert client.get(f"/meeting/{meeting_id}").json()["progress"] == 0.42
    clear_progress(meeting_id)


def test_pipeline_reports_progress_during_transcription_then_clears(client, meeting_id, monkeypatch):
    seen = {}

    class Watching:
        def transcribe(self, path, language=None, on_progress=None):
            from app.services.transcription import TranscriptionResult

            on_progress(0.6)
            seen["during"] = get_progress(meeting_id)
            return TranscriptionResult(text="Hello there.", language="en", segments=[], duration=1.0)

    monkeypatch.setattr("app.services.pipeline.get_transcriber", lambda: Watching())
    assert client.post("/transcribe", json={"meeting_id": meeting_id}).status_code == 200
    assert seen["during"] == 0.6
    assert get_progress(meeting_id) is None
