import base64

import pytest

from app.config import get_settings
from app.services.task_export import TaskExportError


@pytest.fixture()
def calls(monkeypatch):
    recorded = []

    def fake_post(url, payload, headers):
        recorded.append({"url": url, "payload": payload, "headers": headers})
        if "atlassian" in url:
            return {"id": "10001", "key": f"MA-{len(recorded)}"}
        return {"id": f"card{len(recorded)}", "shortUrl": f"https://trello.com/c/card{len(recorded)}"}

    monkeypatch.setattr("app.services.task_export._post_json", fake_post)
    return recorded


@pytest.fixture()
def jira_configured(monkeypatch):
    settings = get_settings()
    for name, value in {
        "jira_base_url": "https://acme.atlassian.net/",
        "jira_email": "me@acme.com",
        "jira_api_token": "tok",
        "jira_project_key": "MA",
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture()
def trello_configured(monkeypatch):
    settings = get_settings()
    for name, value in {"trello_key": "K", "trello_token": "T", "trello_list_id": "LIST"}.items():
        monkeypatch.setattr(settings, name, value)


def test_jira_creates_one_issue_per_open_action_item(client, summarized_meeting_id, calls, jira_configured):
    resp = client.post(f"/meeting/{summarized_meeting_id}/export/jira")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["task"] for t in body["exported"]] == ["Prepare the launch deck"]
    assert body["exported"][0]["url"] == "https://acme.atlassian.net/browse/MA-1"
    call = calls[0]
    assert call["url"] == "https://acme.atlassian.net/rest/api/3/issue"
    assert call["headers"]["Authorization"] == "Basic " + base64.b64encode(b"me@acme.com:tok").decode()
    fields = call["payload"]["fields"]
    assert fields["project"] == {"key": "MA"} and fields["summary"] == "Prepare the launch deck"
    assert "duedate" in fields  # "Thursday" resolves to a real date


def test_trello_creates_cards(client, summarized_meeting_id, calls, trello_configured):
    resp = client.post(f"/meeting/{summarized_meeting_id}/export/trello")
    assert resp.status_code == 200, resp.text
    assert resp.json()["exported"][0]["url"].startswith("https://trello.com/c/")
    url = calls[0]["url"]
    assert url.startswith("https://api.trello.com/1/cards?") and "idList=LIST" in url and "name=Prepare" in url


def test_not_configured_returns_503_naming_the_settings(client, summarized_meeting_id, calls):
    resp = client.post(f"/meeting/{summarized_meeting_id}/export/jira")
    assert resp.status_code == 503 and "JIRA_API_TOKEN" in resp.json()["detail"]
    resp = client.post(f"/meeting/{summarized_meeting_id}/export/trello")
    assert resp.status_code == 503 and "TRELLO_LIST_ID" in resp.json()["detail"]
    assert calls == []


def test_jira_requires_https(client, summarized_meeting_id, calls, jira_configured, monkeypatch):
    monkeypatch.setattr(get_settings(), "jira_base_url", "http://acme.atlassian.net")
    assert client.post(f"/meeting/{summarized_meeting_id}/export/jira").status_code == 503


def test_all_failures_return_502_and_hide_nothing_else(client, summarized_meeting_id, jira_configured, monkeypatch):
    def boom(url, payload, headers):
        raise TaskExportError("HTTP 401: bad credentials")

    monkeypatch.setattr("app.services.task_export._post_json", boom)
    resp = client.post(f"/meeting/{summarized_meeting_id}/export/jira")
    assert resp.status_code == 502 and "bad credentials" in resp.json()["detail"]


def test_one_failure_does_not_stop_the_other_items():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.services import task_export

    meeting = SimpleNamespace(title="Kickoff", created_at=datetime(2026, 10, 24, tzinfo=timezone.utc))
    items = [
        SimpleNamespace(id=1, task="First", owner=None, due_date=None),
        SimpleNamespace(id=2, task="Second", owner="Ravi", due_date="November 5"),
    ]
    settings = SimpleNamespace(trello_key="K", trello_token="SECRET", trello_list_id="L")
    seen = []

    def flaky(url, payload, headers):
        seen.append(url)
        if len(seen) == 1:
            raise TaskExportError("HTTP 429: slow down, token=SECRET")
        return {"id": "c2", "shortUrl": "https://trello.com/c/c2"}

    original = task_export._post_json
    task_export._post_json = flaky
    try:
        result = task_export.export_to_trello(meeting, items, settings)
    finally:
        task_export._post_json = original
    assert [t["item_id"] for t in result.exported] == [2] and "due=2026-11-05" in seen[1]
    assert [t["item_id"] for t in result.failed] == [1]
    assert "SECRET" not in result.failed[0]["error"]  # the token never leaks into responses


def test_no_action_items_returns_409(client, meeting_id, jira_configured):
    assert client.post(f"/meeting/{meeting_id}/export/jira").status_code == 409


def test_unknown_item_ids_return_409(client, summarized_meeting_id, calls, jira_configured):
    resp = client.post(f"/meeting/{summarized_meeting_id}/export/jira", json={"item_ids": [9999]})
    assert resp.status_code == 409 and calls == []


def test_explicit_item_ids_can_export_completed_items(client, summarized_meeting_id, calls, jira_configured):
    item = client.get(f"/meeting/{summarized_meeting_id}").json()["action_items"][0]
    client.patch(f"/action-items/{item['id']}", json={"completed": True})
    assert client.post(f"/meeting/{summarized_meeting_id}/export/jira").status_code == 409  # default skips done items
    resp = client.post(f"/meeting/{summarized_meeting_id}/export/jira", json={"item_ids": [item["id"]]})
    assert resp.status_code == 200 and len(calls) == 1
