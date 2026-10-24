from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.calendar_export import build_ics, parse_due_date

REF = date(2026, 10, 24)  # a Saturday


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-05", date(2026, 11, 5)),
        ("November 5", date(2026, 11, 5)),
        ("5th of November", date(2026, 11, 5)),
        ("Nov 5, 2027", date(2027, 11, 5)),
        ("January 3", date(2027, 1, 3)),  # already past this year -> next year
        ("today", REF),
        ("tomorrow", date(2026, 10, 25)),
        ("this Friday", date(2026, 10, 30)),
        ("Saturday", date(2026, 10, 31)),  # the same weekday means a week from now
        ("soon", None),
        ("February 30", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_due_date(text, expected):
    assert parse_due_date(text, REF) == expected


def _meeting():
    item = lambda i, task, owner, due, done=False: SimpleNamespace(  # noqa: E731
        id=i, task=task, owner=owner, due_date=due, completed=done
    )
    return SimpleNamespace(
        id=7,
        title="Pilot, kickoff; plan",
        created_at=datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc),
        transcript=SimpleNamespace(duration_seconds=1800),
        summary=SimpleNamespace(overview="Launch plan.", decisions=["Use PostgreSQL"]),
        action_items=[
            item(1, "Set up staging", "Ravi", "November 5"),
            item(2, "Already done", "Anita", "November 6", done=True),
            item(3, "Vague task", None, "soon"),
        ],
    )


def test_build_ics_structure_escaping_and_dated_items():
    text = build_ics(_meeting(), now=datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    unfolded = text.replace("\r\n ", "")
    assert "SUMMARY:Pilot\\, kickoff\\; plan" in unfolded
    assert "DTSTART:20261024T090000Z" in unfolded and "DTEND:20261024T093000Z" in unfolded
    assert "DTSTART;VALUE=DATE:20261105" in unfolded
    assert "SUMMARY:Due: Set up staging (Ravi)" in unfolded
    assert "action-2@" not in unfolded  # completed items are skipped
    assert "action-3@" not in unfolded  # undated items are described, not guessed


def test_calendar_endpoint(client, summarized_meeting_id):
    resp = client.get(f"/meeting/{summarized_meeting_id}/export/calendar")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    assert "attachment" in resp.headers["content-disposition"]
    assert "Due: Prepare the launch deck (Alice)" in resp.text.replace("\r\n ", "")


def test_calendar_unknown_meeting(client):
    assert client.get("/meeting/9999/export/calendar").status_code == 404
