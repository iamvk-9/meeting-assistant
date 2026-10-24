# AI Meeting Assistant

## Description

Upload a meeting recording (MP3 / WAV / MP4) and get a transcript, a structured summary (overview, key points,
decisions, risks), extracted action items with owners and deadlines, a PDF report, and an optional email to
participants. Built for Simha Services & Solutions' daily client meetings.

**Stack:** Python 3.11 · FastAPI · OpenAI Whisper (local speech-to-text) · LLM via LiteLLM (Groq by default;
Gemini/OpenAI/Anthropic by changing one setting) · PostgreSQL (SQLite for local dev) · ReportLab · Docker

```
Browser UI ──> FastAPI ──> Whisper (STT) ──> LLM (summary + action items) ──> PostgreSQL
                  │                                                              │
                  └──────────── PDF export / SMTP email <────────────────────────┘
```

## Installation

### Option A: Docker (one command)

```bash
cp .env.example .env          # then put your GROQ_API_KEY (or another provider's key) in .env
docker compose up --build
```
Open **http://localhost:8000** (UI) or **http://localhost:8000/docs** (interactive API docs).
The first transcription downloads the Whisper model once (cached in a Docker volume).

### Option B: Local Python

Requires Python 3.10+ (ffmpeg is bundled automatically via `imageio-ffmpeg` if not installed).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cp .env.example .env                                   # add GROQ_API_KEY (or another provider's key)
cd backend && uvicorn app.main:app --reload
```
Local runs use SQLite (`meetings.db`) unless `DATABASE_URL` points at PostgreSQL.

## Configuration

All settings are environment variables (or `.env`). See `.env.example` for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODEL` | `groq/llama-3.3-70b-versatile` | Any [LiteLLM](https://docs.litellm.ai/docs/providers) model, e.g. `gemini/...`, `openai/gpt-4o-mini`, `anthropic/claude-...` |
| `GROQ_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | – | Key for the provider you chose |
| `DIARIZATION_ENABLED` / `HF_TOKEN` | off | Optional speaker labels ("Speaker 1", ...). Needs `pip install -r backend/requirements-diarization.txt` and a Hugging Face token |
| `WHISPER_MODEL` | `base` | `tiny`…`large`: accuracy vs speed/RAM |
| `DEFAULT_LANGUAGE` | auto-detect | Force a spoken language (`en`, `hi`, `te`, …) |
| `LIVE_WINDOW_SECONDS` / `LIVE_MAX_MINUTES` | `8` / `120` | Live transcription window length and maximum session length |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` | off | Needed only for "Export to Jira" (Jira Cloud, https) |
| `TRELLO_KEY`, `TRELLO_TOKEN`, `TRELLO_LIST_ID` | off | Needed only for "Export to Trello" |
| `API_KEY` | off | If set, all API calls need header `X-API-Key` (the UI has an "API key" button) |
| `MAX_UPLOAD_MB` | `200` | Upload size limit |
| `SMTP_*` | off | Needed only for emailing summaries (587 = STARTTLS, 465 = SSL) |
| `DATABASE_URL` | SQLite file | `postgresql+psycopg2://user:pass@host:5432/db` |

## Usage

1. **New meeting** → pick a recording, optionally add a title and participants, keep *Transcribe and summarize right away* checked.
2. The meeting page shows a live progress bar with a percentage while transcribing (and an animated bar while the summary is written). Long recordings take minutes on CPU.
3. Read the **Summary**, tick off **Action items**, search the **Transcript**.
4. **Download PDF** or **Email summary**. The dashboard's search box looks through titles, transcripts, summaries and action items.

**Live transcription:** open **Live**, allow the microphone (needs https or `localhost`), and press *Start recording*.
Text appears every few seconds. *Stop and save* stores it as a normal meeting and can summarize it straight away.
If you close the tab, what was recorded so far is still saved.

**Calendar and task export:** on a summarized meeting, *Add to calendar* downloads an `.ics` file (the meeting plus
an all-day event for each action item whose deadline could be resolved to a date). *Export to Jira* and
*Export to Trello* create one task or card per open action item. Exporting twice creates duplicates.

A ready-made demo is in `docs/sample/`: `sample_meeting.mp3` (~100 s) and `expected_summary.md`.

## APIs

| Method | Path | Description |
|---|---|---|
| POST | `/upload-meeting` | Upload recording (`file`, optional `title`, `participants`, `language`, `auto_process`) |
| POST | `/transcribe` | `{meeting_id, language?}` → run speech-to-text (synchronous) |
| POST | `/summarize` | `{meeting_id}` → summary, decisions, risks, action items (synchronous) |
| GET | `/meetings?q=&status=&limit=&offset=` | History + search |
| GET | `/meeting/{id}` | Full detail: transcript, summary, action items, participants |
| POST | `/meeting/{id}/process` | Run missing stages in the background (used by the UI) |
| GET | `/meeting/{id}/export/pdf` | PDF report (`?include_transcript=true`) |
| POST | `/meeting/{id}/email` | `{recipients?}` → email summary + PDF |
| PATCH | `/action-items/{id}` | `{completed}` |
| DELETE | `/meeting/{id}` | Delete meeting and its recording |
| GET | `/meeting/{id}/export/calendar` | `.ics` calendar file |
| POST | `/meeting/{id}/export/jira` | `{item_ids?}` → create Jira issues for open action items |
| POST | `/meeting/{id}/export/trello` | `{item_ids?}` → create Trello cards |
| WS | `/ws/live` | Live transcription (PCM16 16 kHz audio in, transcript segments out); `?api_key=` if auth is on |
| GET | `/health` | Liveness |

Details and curl examples: [`docs/API.md`](docs/API.md). Interactive docs at `/docs`.

## Testing

```bash
pip install -r backend/requirements-dev.txt     # or just: pip install pytest httpx + runtime deps
pytest
```
50 test functions cover upload validation (extension, magic bytes, size, path tricks), the transcribe → summarize
pipeline and its failure states, LLM-output parsing/retry and long-transcript chunking, search, PDF export,
email, API-key auth, restart recovery speaker labelling, live sessions, calendar output and Jira/Trello export. Whisper and the LLM are replaced by fakes, so tests run in seconds
with no model downloads or API keys.

## Code quality

PEP 8 (120-column limit), full type hints and import ordering are configured in `pyproject.toml`:

```bash
ruff check . && ruff format --check .   # style, imports, common bugs
mypy                                    # type check (disallow_untyped_defs)
```

Code is split into thin routers (`routers/`) and reusable services (`services/`: transcription, diarization,
summarizer, pipeline, pdf_export, email_service) that don't depend on FastAPI, so they can be reused from a CLI or worker.

## Project layout

```
backend/app/        main.py, config.py, models.py, schemas.py, security.py, routers/, services/
frontend/           single-page UI (served by FastAPI at /)
docker/             Dockerfile            (docker-compose.yml is at the repo root)
docs/               API.md, ARCHITECTURE.md, sample/ (recording + expected summary)
tests/              pytest suite
```

More on design decisions, long-meeting handling, scaling and known limitations: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
