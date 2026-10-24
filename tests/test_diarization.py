from app.config import get_settings
from app.services.diarization import DiarizationError, assign_speakers, speaker_labelled_text

SEGMENTS = [
    {"id": 0, "start": 0.0, "end": 4.0, "text": "Welcome everyone."},
    {"id": 1, "start": 4.0, "end": 8.0, "text": "Thanks for joining."},
    {"id": 2, "start": 8.0, "end": 12.0, "text": "Let's start."},
]
TURNS = [(0.0, 4.5, "SPEAKER_07"), (4.5, 12.0, "SPEAKER_02")]


def test_assign_speakers_uses_largest_overlap_and_numbers_in_order_of_appearance():
    result = assign_speakers(SEGMENTS, TURNS)
    assert [s["speaker"] for s in result] == ["Speaker 1", "Speaker 2", "Speaker 2"]
    assert "speaker" not in SEGMENTS[0]  # input is not mutated


def test_segment_without_overlap_gets_no_speaker():
    result = assign_speakers([{"id": 0, "start": 50.0, "end": 52.0, "text": "late"}], TURNS)
    assert "speaker" not in result[0]


def test_speaker_labelled_text_merges_consecutive_turns():
    text = speaker_labelled_text(assign_speakers(SEGMENTS, TURNS))
    assert text == "Speaker 1: Welcome everyone.\nSpeaker 2: Thanks for joining. Let's start."


def test_diarization_failure_does_not_fail_the_meeting(client, meeting_id, monkeypatch):
    class Broken:
        def diarize(self, path):
            raise DiarizationError("no token")

    monkeypatch.setattr(get_settings(), "diarization_enabled", True)
    monkeypatch.setattr("app.services.pipeline.get_diarizer", lambda: Broken())
    resp = client.post("/transcribe", json={"meeting_id": meeting_id})
    assert resp.status_code == 200
    assert resp.json()["status"] == "transcribed"


def test_speakers_are_stored_when_diarization_works(client, meeting_id, monkeypatch):
    class Working:
        def diarize(self, path):
            return [(0.0, 4.0, "A")]

    monkeypatch.setattr(get_settings(), "diarization_enabled", True)
    monkeypatch.setattr("app.services.pipeline.get_diarizer", lambda: Working())
    resp = client.post("/transcribe", json={"meeting_id": meeting_id})
    assert resp.json()["transcript"]["segments"][0]["speaker"] == "Speaker 1"
