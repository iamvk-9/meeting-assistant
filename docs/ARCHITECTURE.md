# Architecture & design notes

## Components

```
frontend/index.html ─┐
                     ▼
              FastAPI (app/main.py, routers/meetings.py)
                │ validates upload (security.py), stores file under a random name
                ▼
   services/pipeline.py  ── status machine: uploaded → queued → transcribing → transcribed
                │                                          → summarizing → completed | failed
        ┌───────┴────────┐
        ▼                ▼
 transcription.py   summarizer.py ──> LiteLLM ──> Gemini / OpenAI / Anthropic
 (Whisper, local)   (structured JSON, validated with Pydantic)
        └───────┬────────┘
                ▼
          PostgreSQL (SQLAlchemy)         pdf_export.py (ReportLab)   email_service.py (SMTP)
```

## Database

| Table | Key columns |
|---|---|
| `meetings` | id, title, title_from_user, original_filename, stored_filename (random UUID), file_size, language, status, error_message, timestamps |
| `transcripts` | meeting_id (1:1), text, language, segments (JSON: start/end/text), duration_seconds |
| `summaries` | meeting_id (1:1), overview, key_points / decisions / risks (JSON lists), sentiment, model_name |
| `action_items` | meeting_id, task, owner, due_date (kept as spoken, e.g. "this Friday"), completed |
| `participants` | meeting_id, name, email |

Tables are created at startup with `create_all`; there is no migration tool yet (see limitations).

## Key decisions

- **Whisper locally** rather than a hosted STT API: no per-minute cost, recordings never leave the server.
  Model size is a setting (`tiny`…`large`). ffmpeg decodes MP3/WAV/MP4 alike. A lock serialises inference
  because a loaded Whisper model isn't safe for concurrent use.
- **LiteLLM** for the LLM call, so Gemini/OpenAI/Anthropic are one setting (`LLM_MODEL`), not a code change.
- **Structured output:** the prompt demands one JSON object; the reply is fence-stripped, parsed and validated
  with Pydantic (null → empty list, sentiment normalised). One automatic retry with a "valid JSON only" nudge;
  after that the meeting is marked `failed` with a readable message and the transcript is kept, so retrying
  costs only the LLM call.
- **Prompt design:** system prompt forbids inventing names/dates/decisions, tolerates ASR errors, and replies in
  the transcript's language (multilingual bonus). Output sections follow the brief: Overview, Decisions, Risks,
  Action Items (plus key points and sentiment).
- **Progress bar:** Whisper updates a progress counter after each 30-second window; `services/transcription.py` temporarily swaps that counter for a callback that stores a 0-1 fraction per meeting. `GET /meeting/{id}` returns it as `progress`, and the UI polls every 1.5 s. If a future Whisper version changes its internals the hook turns into a no-op and the bar simply stays indeterminate.
- **Sync endpoints + background mode:** `/transcribe` and `/summarize` are synchronous (simple, matches the spec);
  `/upload-meeting?auto_process` and `/meeting/{id}/process` run in a FastAPI background task, and the UI polls.

## Speaker identification (optional)

With `DIARIZATION_ENABLED=true`, `services/diarization.py` runs pyannote.audio after Whisper and stores a
`speaker` ("Speaker 1", "Speaker 2", ...) on each transcript segment by largest time overlap. No schema change:
segments are JSON. The UI and PDF show the labels. It is best-effort: if pyannote or the token is missing, a
warning is logged and the meeting continues without labels. Names are not guessed; labels are anonymous.

## Live transcription

`/ws/live` receives 16 kHz PCM16 from the browser (AudioWorklet), appends it to a WAV file as it arrives and
transcribes it in windows (`LIVE_WINDOW_SECONDS`, default 8 s) with Whisper. A receive loop only buffers; a
separate worker runs Whisper in a thread, so audio is never dropped while a window is being transcribed. Each
window gets the last ~200 characters as `initial_prompt` for consistent wording; silent windows are skipped
(Whisper hallucinates on silence); the language is detected once and then locked. Known limits: words cut at a
window boundary can be split, and if Whisper is slower than real time the text lags (use a smaller model or a GPU).

## Calendar and task export

`services/calendar_export.py` resolves spoken deadlines against the meeting date (best effort; unclear ones are
left undated, never guessed) and writes RFC 5545 `.ics`. `services/task_export.py` posts to Jira Cloud (REST v3,
Basic auth with an API token) and Trello using only the standard library; per-item failures are collected, and
tokens are scrubbed from errors and logs. There is no record of what was exported, so exporting twice duplicates.

## Reliability

LLM rate-limit errors are retried with backoff (honouring the provider's "try again in Ns" hint). Whisper/ffmpeg
failures are reduced to a short message (for example, "this file has no audio track").

## Long meetings

Whisper itself processes audio in 30-second windows, so recording length is only a time cost. For the LLM, a
transcript above `LLM_CHUNK_CHARS` (default 12,000 characters, ≈ 10 minutes of speech) is split on sentence
boundaries; each chunk is condensed to bullet notes (map), then the notes are merged into the final structured
summary (reduce). Names, numbers and dates are told to stay exact. If a meeting is so long that the combined
notes still exceed the model's context, a second reduce level would be needed (not implemented).

## Security

- Uploads: extension allow-list **and** magic-byte check (a renamed `.exe` is rejected), streaming size limit,
  random server-side filenames, client filename sanitised and never used as a path.
- Optional API-key auth (`X-API-Key`, constant-time comparison). API keys for LLM/SMTP live only in env vars.
- UI escapes all dynamic text (no `innerHTML` with raw data); search input has LIKE-wildcards escaped; SQL goes
  through the ORM; email headers are newline-stripped; container runs as a non-root user; CORS is off by default.
- Logging (`logs/app.log`, rotating + console): uploads accepted/rejected, transcription and LLM failures,
  email failures, unhandled API errors.

## Scaling path

Today: one API process, one in-process Whisper model, background tasks inside the web process. To scale:
move jobs to a queue (Celery/RQ/Arq) with separate GPU/CPU workers, store recordings in S3-compatible object
storage instead of a local volume, add Alembic migrations, and run several API replicas behind a load balancer.
The pipeline functions in `services/pipeline.py` are already separated from HTTP, so they can become the worker tasks.

## Accuracy notes

Bigger Whisper models and a forced `language` improve accuracy; the summary is only as good as the transcript.
Action-item owners and dates come only from what was said, never inferred. Review before sending to clients.

## Known limitations

- No speaker diarization, live transcription, calendar or Jira/Trello integration (listed as bonus features).
- No user accounts: one shared API key at most. All meetings are visible to anyone with access.
- No schema migrations; changing models on an existing database needs manual migration.
- Transcription progress lives in memory (`app/progress.py`), so it works with a single API process; with several workers it would need Redis. Summarizing has no percentage (an LLM call can't report one), so the UI shows an animated bar for that stage.
- Background jobs are lost on restart (affected meetings are marked `failed` at startup, then can be retried).
- PDF uses DejaVu Sans when installed (the Docker image installs it): good for Latin/Cyrillic/Greek, but Indic
  scripts (Hindi, Telugu…) won't render in the PDF; the on-screen UI and transcript are unaffected.
- Not verified here: a real Whisper run and a real LLM/SMTP call (they need model downloads, API keys and
  network access). Everything around them is covered by tests with fakes.

## Interview cheat-sheet

- *Transcription approach?* Local Whisper via ffmpeg; timestamped segments stored; model size configurable.
- *Prompt design?* Role + "only what's in the transcript" + strict JSON schema + validation + one retry.
- *Long meetings?* Map-reduce summarization over sentence-bounded chunks; background processing with status polling.
- *Scalability?* Queue + workers + object storage + migrations (see above).
- *Accuracy?* Larger model, language hint, low temperature (0.3), no inferred facts, human review.
