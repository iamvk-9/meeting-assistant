"""Calendar export: an .ics file with the meeting plus one all-day event per dated action item.

Due dates are stored as spoken ("this Friday", "November 5"), so `parse_due_date` resolves them against the
meeting date on a best-effort basis; items whose deadline can't be resolved are listed in the event
description instead of being given a guessed date.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Meeting

MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
            ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
            ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b(?:,?\s+(\d{{4}}))?", re.I)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_due_date(text: str | None, reference: date) -> date | None:
    """Resolve a spoken deadline to a date relative to `reference` (the meeting date); None if unclear."""
    if not text:
        return None
    lowered = text.lower()

    if match := _ISO.search(lowered):
        return _safe_date(*(int(g) for g in match.groups()))

    for pattern, day_first in ((_DAY_MONTH, True), (_MONTH_DAY, False)):
        if match := pattern.search(lowered):
            first, second, year = match.groups()
            day, month = (int(first), MONTHS[second]) if day_first else (int(second), MONTHS[first])
            found = _safe_date(int(year) if year else reference.year, month, day)
            if found and not year and found < reference:  # "November 5" said in December means next year
                found = _safe_date(reference.year + 1, month, day)
            return found

    if re.search(r"\btoday\b", lowered):
        return reference
    if re.search(r"\btomorrow\b", lowered):
        return reference + timedelta(days=1)
    for name, weekday in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            ahead = (weekday - reference.weekday()) % 7
            return reference + timedelta(days=ahead or 7)
    return None


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545: lines are at most 75 octets; continuation lines start with a space."""
    out: list[str] = []
    current, size = "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > (75 if not out else 74):
            out.append(current)
            current, size = "", 0
        current += char
        size += width
    out.append(current)
    return [out[0]] + [" " + part for part in out[1:]]


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(meeting: Meeting, now: datetime | None = None) -> str:
    """Build the calendar file (CRLF line endings, folded lines) for a meeting."""
    now = now or datetime.now(timezone.utc)
    created = meeting.created_at if meeting.created_at.tzinfo else meeting.created_at.replace(tzinfo=timezone.utc)
    reference = created.astimezone(timezone.utc).date()
    transcript = meeting.transcript
    seconds = transcript.duration_seconds if transcript and transcript.duration_seconds else 3600
    length = timedelta(seconds=seconds)

    description: list[str] = []
    summary = meeting.summary
    if summary:
        description.append(summary.overview or "")
        if summary.decisions:
            description.append("Decisions:\n" + "\n".join(f"- {d}" for d in summary.decisions))
    pending = [a for a in meeting.action_items if not a.completed]
    if pending:
        bullets = [
            f"- {a.task}" + (f" ({a.owner})" if a.owner else "") + (f", due {a.due_date}" if a.due_date else "")
            for a in pending
        ]
        description.append("Action items:\n" + "\n".join(bullets))

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AI Meeting Assistant//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:meeting-{meeting.id}@meeting-assistant",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(created)}",
        f"DTEND:{_stamp(created + length)}",
        f"SUMMARY:{_escape(meeting.title)}",
        f"DESCRIPTION:{_escape(chr(10).join(p for p in description if p))}",
        "END:VEVENT",
    ]
    for item in pending:
        due = parse_due_date(item.due_date, reference)
        if due is None:
            continue
        who = f" ({item.owner})" if item.owner else ""
        origin = f"From meeting: {meeting.title}" + (f". Said: {item.due_date}" if item.due_date else "")
        lines += [
            "BEGIN:VEVENT",
            f"UID:action-{item.id}@meeting-assistant",
            f"DTSTAMP:{_stamp(now)}",
            f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
            f"DTEND;VALUE=DATE:{due + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_escape('Due: ' + item.task + who)}",
            f"DESCRIPTION:{_escape(origin)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(folded for line in lines for folded in _fold(line)) + "\r\n"
