"""Export action items to Jira (Cloud, REST v3) or Trello as tasks/cards.

Uses only the standard library. Credentials come from settings and are never logged. An item that fails
does not stop the others: the result lists what was created and what failed.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from app.services.calendar_export import parse_due_date

if TYPE_CHECKING:
    from app.config import Settings
    from app.models import ActionItem, Meeting

logger = logging.getLogger(__name__)
TRELLO_API = "https://api.trello.com/1"


class TaskExportError(Exception):
    pass


class TaskExportNotConfiguredError(TaskExportError):
    pass


@dataclass
class ExportResult:
    exported: list[dict[str, Any]] = field(default_factory=list)  # item_id, task, ref, url
    failed: list[dict[str, Any]] = field(default_factory=list)  # item_id, task, error


def _scrub(message: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


def _post_json(url: str, payload: dict[str, Any] | None, headers: dict[str, str]) -> dict[str, Any]:
    """POST and return the JSON reply. Isolated so tests can replace the network call."""
    data = json.dumps(payload).encode() if payload is not None else b""
    all_headers = {"Content-Type": "application/json", **headers}
    request = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise TaskExportError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TaskExportError(str(exc)) from exc


def _require(settings: Settings, service: str, names: list[str]) -> None:
    missing = [n.upper() for n in names if not getattr(settings, n)]
    if missing:
        raise TaskExportNotConfiguredError(f"{service} export is not configured. Set: {', '.join(missing)}.")


def _describe(meeting: Meeting, item: ActionItem) -> list[str]:
    lines = [f"From meeting: {meeting.title}"]
    if item.owner:
        lines.append(f"Owner: {item.owner}")
    if item.due_date:
        lines.append(f"Due (as said in the meeting): {item.due_date}")
    return lines


def _due(meeting: Meeting, item: ActionItem) -> date | None:
    return parse_due_date(item.due_date, meeting.created_at.date())


def export_to_jira(meeting: Meeting, items: list[ActionItem], settings: Settings) -> ExportResult:
    _require(settings, "Jira", ["jira_base_url", "jira_email", "jira_api_token", "jira_project_key"])
    base = (settings.jira_base_url or "").rstrip("/")
    if not base.startswith("https://"):
        raise TaskExportNotConfiguredError("JIRA_BASE_URL must start with https://")
    token = base64.b64encode(f"{settings.jira_email}:{settings.jira_api_token}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}

    result = ExportResult()
    for item in items:
        fields: dict[str, Any] = {
            "project": {"key": settings.jira_project_key},
            "summary": item.task.strip()[:250],
            "issuetype": {"name": settings.jira_issue_type},
            "labels": ["meeting-assistant"],
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": line}]}
                    for line in _describe(meeting, item)
                ],
            },
        }
        if due := _due(meeting, item):
            fields["duedate"] = due.isoformat()
        try:
            reply = _post_json(f"{base}/rest/api/3/issue", {"fields": fields}, headers)
            key = reply.get("key", "")
            result.exported.append({"item_id": item.id, "task": item.task, "ref": key, "url": f"{base}/browse/{key}"})
        except TaskExportError as exc:
            error = _scrub(str(exc), settings.jira_api_token)
            logger.warning("Jira export failed for action item %s: %s", item.id, error)
            result.failed.append({"item_id": item.id, "task": item.task, "error": error})
    return result


def export_to_trello(meeting: Meeting, items: list[ActionItem], settings: Settings) -> ExportResult:
    _require(settings, "Trello", ["trello_key", "trello_token", "trello_list_id"])
    result = ExportResult()
    for item in items:
        params = {
            "key": settings.trello_key,
            "token": settings.trello_token,
            "idList": settings.trello_list_id,
            "name": item.task.strip()[:250],
            "desc": "\n".join(_describe(meeting, item)),
        }
        if due := _due(meeting, item):
            params["due"] = f"{due.isoformat()}T12:00:00.000Z"
        try:
            url = f"{TRELLO_API}/cards?{urllib.parse.urlencode(params)}"
            reply = _post_json(url, None, {"Accept": "application/json"})
            card_url = reply.get("shortUrl") or reply.get("url")
            result.exported.append({"item_id": item.id, "task": item.task, "ref": reply.get("id", ""), "url": card_url})
        except TaskExportError as exc:
            error = _scrub(str(exc), settings.trello_token, settings.trello_key)
            logger.warning("Trello export failed for action item %s: %s", item.id, error)
            result.failed.append({"item_id": item.id, "task": item.task, "error": error})
    return result
