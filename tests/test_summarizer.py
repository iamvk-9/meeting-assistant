import json

import pytest

from app.config import Settings
from app.services.summarizer import MeetingSummarizer, SummarizationError, chunk_text, parse_llm_json

GOOD = json.dumps(
    {
        "title": "Kickoff",
        "overview": "Kickoff meeting.",
        "key_points": ["Scope"],
        "decisions": None,
        "risks": [],
        "action_items": [{"task": "Send proposal", "owner": "Priya", "due_date": "Friday"}],
        "sentiment": "Positive",
    }
)


def make(chunk_chars=12000, **kw) -> MeetingSummarizer:
    return MeetingSummarizer(Settings(llm_chunk_chars=chunk_chars, **kw))


def test_chunk_text_respects_limit_and_keeps_content():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = chunk_text(text, 300)
    assert len(chunks) > 1 and all(len(c) <= 300 for c in chunks)
    assert " ".join(chunks).split() == text.split()
    assert chunk_text("short", 300) == ["short"]
    assert chunk_text("   ", 300) == []


def test_chunk_text_splits_one_giant_sentence():
    assert all(len(c) <= 50 for c in chunk_text("x" * 220, 50))


def test_parse_llm_json_handles_fences_and_prose():
    assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_llm_json('Sure! Here it is: {"a": 1} Hope that helps.') == {"a": 1}
    for bad in ("no json here", "{broken json}"):
        with pytest.raises(SummarizationError):
            parse_llm_json(bad)


def test_summarize_parses_and_normalises(monkeypatch):
    s = make()
    monkeypatch.setattr(s, "_complete", lambda system, user: GOOD)
    result = s.summarize("Some transcript.")
    assert result.title == "Kickoff"
    assert result.decisions == []  # null -> []
    assert result.sentiment == "positive"
    assert result.action_items[0].owner == "Priya"


def test_summarize_retries_once_on_bad_json(monkeypatch):
    s = make()
    replies = iter(["definitely not json", GOOD])
    calls = []
    monkeypatch.setattr(s, "_complete", lambda system, user: calls.append(user) or next(replies))
    assert s.summarize("Some transcript.").overview == "Kickoff meeting."
    assert len(calls) == 2 and "not valid JSON" in calls[1]


def test_summarize_gives_up_after_two_bad_replies(monkeypatch):
    s = make()
    monkeypatch.setattr(s, "_complete", lambda system, user: "nope")
    with pytest.raises(SummarizationError):
        s.summarize("Some transcript.")


def test_long_transcript_uses_map_reduce(monkeypatch):
    s = make(chunk_chars=200)
    transcript = " ".join(f"Point {i} was discussed at length." for i in range(60))
    prompts = []

    def fake(system, user):
        prompts.append(user)
        return GOOD if "Return ONLY a JSON object" in user else "- bullet notes"

    monkeypatch.setattr(s, "_complete", fake)
    s.summarize(transcript)
    chunk_calls = [p for p in prompts if "TRANSCRIPT PART" in p]
    assert len(chunk_calls) > 1
    assert len(prompts) == len(chunk_calls) + 1  # one reduce call
    assert "condensed notes" in prompts[-1]


def test_empty_transcript_and_missing_key():
    with pytest.raises(SummarizationError):
        make().summarize("   ")
    with pytest.raises(SummarizationError, match="API key"):
        make(llm_model="gemini/x", gemini_api_key=None)._complete("s", "u")


def test_friendly_transcription_error_for_silent_video():
    from app.services.transcription import friendly_transcription_error

    raw = (
        "Failed to load audio: ffmpeg version 7.1 ...\nOutput #0, s16le, to 'pipe:':\n"
        "Output file does not contain any stream"
    )
    assert friendly_transcription_error(RuntimeError(raw)) == (
        "Transcription failed: this file has no audio track (is it a silent video?)."
    )


def test_rate_limit_detection_and_backoff_hint():
    from app.services.summarizer import _backoff_seconds, is_rate_limit_error

    err = RuntimeError("RateLimitError: Rate limit reached ... Please try again in 7.5s.")
    assert is_rate_limit_error(err)
    assert _backoff_seconds(err, 1) == 8.5
    assert not is_rate_limit_error(RuntimeError("401 invalid api key"))
    assert _backoff_seconds(RuntimeError("429"), 2) == 20.0
