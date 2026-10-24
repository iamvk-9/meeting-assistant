# API reference

Base URL `http://localhost:8000`. Interactive docs: `/docs`. If the server has `API_KEY` set, add
`-H "X-API-Key: <key>"` to every call. Errors are JSON: `{"detail": "..."}`.

## Typical flow

```bash
# 1. Upload (returns the meeting with its id)
curl -F file=@docs/sample/sample_meeting.mp3 -F title="Acme kickoff" \
     -F participants="Priya <priya@company.com>, anita@client.com" \
     http://localhost:8000/upload-meeting

# 2. Transcribe, then 3. summarize (synchronous; can take minutes for long recordings)
curl -X POST http://localhost:8000/transcribe -H 'Content-Type: application/json' -d '{"meeting_id": 1}'
curl -X POST http://localhost:8000/summarize  -H 'Content-Type: application/json' -d '{"meeting_id": 1}'

# Or do both in the background and poll GET /meeting/1 until status is "completed"
curl -X POST http://localhost:8000/meeting/1/process

# 4. Read, export, email
curl http://localhost:8000/meeting/1
curl -o report.pdf "http://localhost:8000/meeting/1/export/pdf?include_transcript=true"
curl -X POST http://localhost:8000/meeting/1/email -H 'Content-Type: application/json' -d '{"recipients":["boss@company.com"]}'
```

## Endpoints

### `POST /upload-meeting` → 201
Multipart form: `file` (required; `.mp3`, `.wav`, `.mp4`), `title`, `participants` (comma-separated names or
emails, `Name <email>` accepted), `language` (e.g. `en`), `auto_process` (`true` queues transcription +
summary in the background).
Errors: `415` wrong type or content doesn't match extension, `413` too large, `400` empty.

### `POST /transcribe` → 200
`{"meeting_id": 1, "language": "en"}` (`language` optional). Stores transcript text, detected language and
timestamped segments. Errors: `404`, `409` already processing, `500` transcription failed.

### `POST /summarize` → 200
`{"meeting_id": 1}`. Needs a transcript. Replaces any earlier summary/action items. A generated title replaces
the default one unless you supplied a title. Errors: `404`, `409` (no transcript / busy), `502` LLM failure.

### `POST /meeting/{id}/process` → 202
Queues the missing stages (transcribe if needed, then summarize). Poll `GET /meeting/{id}`.

### `GET /meetings`
Query: `q` (case-insensitive search over title, transcript, summary overview, action-item text),
`status`, `limit` (1–100, default 50), `offset`. Returns `{"items": [...], "total": n}`, newest first.

### `GET /meeting/{id}`
```jsonc
{
  "id": 1, "title": "Acme Retail pilot kickoff", "status": "completed",   // uploaded|queued|transcribing|transcribed|summarizing|completed|failed
  "original_filename": "sample_meeting.mp3", "language": "en", "error_message": null,
  "progress": null,   // 0.0-1.0 while status is "transcribing" (null before the model is loaded and in other states)
  "participants": [{"id": 1, "name": "Priya", "email": "priya@company.com"}],
  "transcript": {"text": "...", "language": "en", "duration_seconds": 103.4,
                 "segments": [{"id": 0, "start": 0.0, "end": 5.2, "text": "..."}]},
  "summary": {"overview": "...", "key_points": [], "decisions": [], "risks": [], "sentiment": "positive", "model_name": "..."},
  "action_items": [{"id": 1, "task": "Set up staging", "owner": "Ravi", "due_date": "November 5", "completed": false}]
}
```

### `GET /meeting/{id}/export/pdf`
`application/pdf` attachment. Requires a summary (`409` otherwise). `?include_transcript=true` appends the transcript.

### `POST /meeting/{id}/email`
`{"recipients": ["a@x.com"]}`; omit to use participants that have emails. `422` no recipients / invalid address,
`503` SMTP not configured, `502` SMTP error.

### `PATCH /action-items/{id}` · `{"completed": true}` · `DELETE /meeting/{id}` → 204


## Health

`GET /health` → `{"status": "ok"}`. No authentication.

## Calendar export

`GET /meeting/{id}/export/calendar` → an `.ics` file (`text/calendar`): one event for the meeting, plus an
all-day event "Due: <task> (<owner>)" for every open action item whose spoken deadline resolves to a date
("November 5", "this Friday", "tomorrow", ISO dates). Undated items are listed in the meeting event instead.

## Task export (Jira / Trello)

`POST /meeting/{id}/export/jira` and `POST /meeting/{id}/export/trello`

Body (optional): `{"item_ids": [1, 2]}`. Default: every action item that is not completed.

```bash
curl -X POST localhost:8000/meeting/1/export/jira -H 'Content-Type: application/json' -d '{}'
```

Response `200`: `{"exported": [{"item_id", "task", "ref", "url"}], "failed": [{"item_id", "task", "error"}]}`.
Errors: `409` no action items · `503` integration not configured (names the missing settings) ·
`502` nothing could be exported. One failing item does not stop the others. Credentials are never returned or logged.

## Live transcription (WebSocket)

`ws://host/ws/live` (add `?api_key=...` when `API_KEY` is set; browsers cannot send headers on WebSockets).

Client → server:
1. one text message: `{"title": "...", "participants": "a@x.com, Bob", "language": "en" | null}`
2. binary frames: raw little-endian PCM16, mono, 16 kHz
3. text `{"type": "stop"}` (closing the socket also saves what was recorded)

Server → client: `{"type":"ready"}`, `{"type":"segments","segments":[{id,start,end,text}]}`,
`{"type":"warning","detail"}`, `{"type":"done","meeting_id":n}`, `{"type":"error","detail"}`.

The finished recording becomes a normal meeting with status `transcribed`; call `POST /meeting/{id}/process` to
summarize it. A session with no speech saves nothing.

## Transcript segments

Each segment is `{id, start, end, text}` and, when speaker identification is enabled, also `speaker`.
