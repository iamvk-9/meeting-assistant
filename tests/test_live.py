import json
import wave

import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.services.live import BYTES_PER_SECOND, LiveLimitError, LiveSession
from app.services.transcription import TranscriptionResult


def tone(seconds: float) -> bytes:
    return np.full(int(16000 * seconds), 8000, dtype=np.int16).tobytes()


def silence(seconds: float) -> bytes:
    return bytes(int(16000 * seconds) * 2)


class FakeAudioTranscriber:
    def __init__(self, language="en"):
        self.calls = []
        self.language = language

    def transcribe_audio(self, audio, language=None, initial_prompt=None):
        self.calls.append({"samples": len(audio), "language": language, "prompt": initial_prompt})
        n = len(self.calls) - 1
        return TranscriptionResult(
            text=f"window {n}",
            language=self.language,
            segments=[{"id": 0, "start": 0.0, "end": 1.0, "text": f"window {n}"}],
        )


@pytest.fixture()
def make_session(tmp_path):
    def factory(transcriber=None, **kwargs):
        kwargs.setdefault("window_seconds", 2)
        return LiveSession(transcriber or FakeAudioTranscriber(), tmp_path / "live.wav", **kwargs)

    return factory


def test_windows_are_transcribed_with_offsets_and_context(make_session):
    fake = FakeAudioTranscriber()
    session = make_session(fake)
    session.write(tone(5))
    segments = session.process_ready()
    assert [(s["start"], s["text"]) for s in segments] == [(0.0, "window 0"), (2.0, "window 1")]
    assert fake.calls[0]["prompt"] is None and fake.calls[1]["prompt"] == "window 0"
    tail = session.finish()  # the remaining 1 s is flushed
    assert [(s["start"], s["id"]) for s in tail] == [(4.0, 2)]
    assert session.text == "window 0 window 1 window 2"


def test_silence_is_skipped(make_session):
    fake = FakeAudioTranscriber()
    session = make_session(fake)
    session.write(silence(4))
    assert session.process_ready() == [] and fake.calls == []


def test_tiny_remainder_is_dropped(make_session):
    session = make_session()
    session.write(tone(2.2))
    assert len(session.finish()) == 1


def test_language_is_locked_after_first_detection(make_session):
    fake = FakeAudioTranscriber(language="hi")
    session = make_session(fake)
    session.write(tone(4))
    session.process_ready()
    assert [c["language"] for c in fake.calls] == [None, "hi"]


def test_frames_may_split_samples_and_wav_is_valid(make_session):
    session = make_session()
    data = tone(1)
    for start in range(0, len(data), 999):  # odd-sized frames
        session.write(data[start : start + 999])
    session.finish()
    with wave.open(str(session.wav_path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == 16000
    assert session.duration == len(data) / BYTES_PER_SECOND


def test_session_length_limit(make_session):
    session = make_session(max_seconds=1)
    with pytest.raises(LiveLimitError):
        session.write(tone(2))


@pytest.fixture()
def live_client(client, monkeypatch):
    monkeypatch.setattr("app.routers.live.get_transcriber", lambda: FakeAudioTranscriber())
    monkeypatch.setattr(get_settings(), "live_window_seconds", 1)
    return client


def _collect(ws):
    segments = []
    while True:
        message = ws.receive_json()
        if message["type"] == "segments":
            segments += message["segments"]
        elif message["type"] in {"done", "error"}:
            return message, segments


def test_live_session_end_to_end_saves_a_meeting(live_client):
    with live_client.websocket_connect("/ws/live") as ws:
        ws.send_json({"title": "Live standup", "participants": "alice@example.com", "language": None})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(tone(2.2))
        ws.send_text(json.dumps({"type": "stop"}))
        done, segments = _collect(ws)
    assert done["type"] == "done" and len(segments) == 2
    meeting = live_client.get(f"/meeting/{done['meeting_id']}").json()
    assert meeting["status"] == "transcribed" and meeting["title"] == "Live standup"
    assert meeting["transcript"]["text"] == "window 0 window 1"
    assert meeting["participants"][0]["email"] == "alice@example.com"
    assert live_client.post(f"/meeting/{meeting['id']}/process").status_code == 202  # can be summarized next


def test_live_session_without_speech_saves_nothing(live_client):
    with live_client.websocket_connect("/ws/live") as ws:
        ws.send_json({})
        ws.receive_json()
        ws.send_bytes(silence(2))
        ws.send_text(json.dumps({"type": "stop"}))
        message, _ = _collect(ws)
    assert message["type"] == "error" and "No speech" in message["detail"]
    assert live_client.get("/meetings").json()["total"] == 0


def test_live_requires_settings_message_first(live_client):
    with live_client.websocket_connect("/ws/live") as ws:
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"


def test_live_enforces_api_key(live_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key", "secret")
    with pytest.raises(WebSocketDisconnect):
        with live_client.websocket_connect("/ws/live"):
            pass
    with live_client.websocket_connect("/ws/live?api_key=secret") as ws:
        ws.send_json({})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_text(json.dumps({"type": "stop"}))
        _collect(ws)
