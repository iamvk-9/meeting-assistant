"""LLM summarization: transcript -> structured meeting notes (via LiteLLM, so any provider works).

Long meetings are handled map-reduce style: the transcript is split into chunks, each chunk is
condensed to notes, and the notes are merged into one final structured summary.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SummarizationError(Exception):
    pass


_EMPTY_VALUES = {"", "null", "none", "n/a", "na", "nil", "-", "unknown", "unassigned", "tbd",
                 "not stated", "not specified", "not mentioned", "no owner", "no deadline"}

class ActionItemData(BaseModel):
    task: str
    owner: str | None = None
    due_date: str | None = None

    @field_validator("task", mode="before")
    @classmethod
    def _strip_task(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("owner", "due_date", mode="before")
    @classmethod
    def _placeholder_to_none(cls, value: Any) -> Any:
        """Models often write "null", "N/A" or "not stated" as text; store those as no value."""
        if isinstance(value, str):
            value = value.strip()
            return None if value.lower().rstrip(".") in _EMPTY_VALUES else value
        return value


class SummaryData(BaseModel):
    title: str | None = None
    overview: str = ""
    key_points: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    action_items: list[ActionItemData] = Field(default_factory=list)
    sentiment: str | None = None

    @field_validator("key_points", "decisions", "risks", "action_items", mode="before")
    @classmethod
    def _none_to_list(cls, value: Any) -> Any:
        return value or []

    @field_validator("sentiment", mode="before")
    @classmethod
    def _normalise_sentiment(cls, value: Any) -> str | None:
        value = str(value).strip().lower() if value else None
        return value if value in {"positive", "neutral", "negative", "mixed"} else None


SYSTEM_PROMPT = (
    "You are a meticulous meeting-minutes specialist. You turn raw meeting transcripts into clear, "
    "accurate notes. Only use information present in the transcript; never invent names, dates or "
    "decisions. If something is not mentioned, leave it out or use null. The transcript may contain "
    "speech-recognition errors; interpret sensibly. Write in the same language as the transcript."
)

FINAL_PROMPT = """Below is the {label} of a meeting. Produce structured meeting notes.

Return ONLY a JSON object (no markdown fences, no commentary) with exactly these keys:
{{
  "title": "short descriptive meeting title (max 10 words)",
  "overview": "2-4 sentence summary of the purpose and outcome of the meeting",
  "key_points": ["main topics discussed, one per item"],
  "decisions": ["decisions that were made"],
  "risks": ["risks, blockers or concerns that were raised"],
  "action_items": [{{"task": "what must be done", "owner": "person responsible or null",
                    "due_date": "deadline as stated, or null"}}],
  "sentiment": "positive | neutral | negative | mixed"
}}
Use empty lists when a section has nothing.

{label_upper}:
\"\"\"
{content}
\"\"\""""

CHUNK_PROMPT = """This is part {index} of {total} of a long meeting transcript. Write concise bullet-point notes
capturing: topics discussed, decisions made, risks/concerns raised, and action items (with owner and
deadline if stated). Keep names, numbers and dates exact. Output plain text bullets only.

TRANSCRIPT PART:
\"\"\"
{content}
\"\"\""""

RETRY_SUFFIX = "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object, nothing else."


def is_rate_limit_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "429" in text


def _backoff_seconds(exc: Exception, attempt: int) -> float:
    """Honour a 'try again in 12.5s' hint from the provider when present, else back off 10s, 20s."""
    match = re.search(r"try again in\s+(?:(\d+)m)?\s*([\d.]+)s", str(exc), flags=re.IGNORECASE)
    if match:
        minutes, seconds = int(match.group(1) or 0), float(match.group(2))
        return min(minutes * 60 + seconds + 1, 60.0)
    return 10.0 * attempt


def _short(exc: Exception | None) -> str:
    return str(exc)[:400] if exc else "unknown error"


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text on sentence boundaries into chunks of at most max_chars."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > max_chars:  # pathological: one huge "sentence"
            if current:
                chunks.append(" ".join(current))
                current, size = [], 0
            chunks.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if current and size + len(sentence) + 1 > max_chars:
            chunks.append(" ".join(current))
            current, size = [], 0
        current.append(sentence)
        size += len(sentence) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def parse_llm_json(raw: str) -> dict:
    """Extract a JSON object from an LLM reply, tolerating code fences and stray prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise SummarizationError("The model's reply did not contain a JSON object.")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SummarizationError(f"The model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SummarizationError("The model returned JSON that is not an object.")
    return data


class MeetingSummarizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _complete_gemini(self, system: str, user: str) -> str:
        """Call the Gemini REST API directly.

        Google's newer 'AQ.' keys must be sent in the x-goog-api-key header; going through
        litellm returned 401 ACCESS_TOKEN_TYPE_UNSUPPORTED for them. Plain AIza keys work too.
        """
        s = self.settings
        model = s.llm_model.split("/", 1)[1]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = json.dumps(
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": s.llm_temperature},
            }
        ).encode()
        headers = {"Content-Type": "application/json", "x-goog-api-key": s.llm_api_key or ""}
        last_error = "unknown error"
        for attempt in range(3):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code not in (429, 500, 502, 503, 504):
                    raise SummarizationError(f"Gemini request failed: {last_error}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = str(exc)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        else:
            raise SummarizationError(f"Gemini request failed: {last_error}")
        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError) as exc:
            raise SummarizationError(f"Unexpected Gemini response: {str(data)[:300]}") from exc
        if not content.strip():
            raise SummarizationError("The model returned an empty response.")
        return content

    def _complete(self, system: str, user: str) -> str:
        s = self.settings
        if s.llm_provider in {"gemini", "openai", "anthropic", "groq"} and not s.llm_api_key:
            raise SummarizationError(
                f"No API key configured for '{s.llm_model}'. Set {s.llm_provider.upper()}_API_KEY."
            )
        if s.llm_provider == "gemini":
            return self._complete_gemini(system, user)
        try:
            import litellm
        except ImportError as exc:
            raise SummarizationError("litellm is not installed.") from exc
        response = None
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = litellm.completion(
                    model=s.llm_model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=s.llm_temperature,
                    api_key=s.llm_api_key,
                    num_retries=2,
                    timeout=120,
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 3 and is_rate_limit_error(exc):
                    wait = _backoff_seconds(exc, attempt)
                    logger.warning("LLM rate limit hit (attempt %d/3); waiting %.0fs", attempt, wait)
                    time.sleep(wait)
                    continue
                logger.exception("LLM request failed")
                raise SummarizationError(f"LLM request failed: {_short(exc)}") from exc
        if response is None:  # pragma: no cover - loop always breaks or raises
            raise SummarizationError(f"LLM request failed: {_short(last_exc)}")
        content = response.choices[0].message.content
        if not content:
            raise SummarizationError("The model returned an empty response.")
        return content

    def summarize(self, transcript: str) -> SummaryData:
        transcript = (transcript or "").strip()
        if not transcript:
            raise SummarizationError("The transcript is empty, so there is nothing to summarize.")

        chunks = chunk_text(transcript, self.settings.llm_chunk_chars)
        if len(chunks) > 1:
            logger.info("Long transcript (%d chars): condensing %d chunks first", len(transcript), len(chunks))
            notes = [
                f"[Part {i}/{len(chunks)}]\n"
                + self._complete(SYSTEM_PROMPT, CHUNK_PROMPT.format(index=i, total=len(chunks), content=chunk))
                for i, chunk in enumerate(chunks, 1)
            ]
            content, label = "\n\n".join(notes), "condensed notes (in order) from a long transcript"
        else:
            content, label = transcript, "transcript"

        prompt = FINAL_PROMPT.format(label=label, label_upper=label.upper(), content=content)
        last_error: Exception | None = None
        for attempt in (1, 2):
            raw = self._complete(SYSTEM_PROMPT, prompt if attempt == 1 else prompt + RETRY_SUFFIX)
            try:
                return SummaryData.model_validate(parse_llm_json(raw))
            except (SummarizationError, ValidationError) as exc:
                last_error = exc
                logger.warning("Summary attempt %d unusable: %s", attempt, exc)
        raise SummarizationError(f"Could not get a valid structured summary from the model: {last_error}")


@lru_cache
def get_summarizer() -> MeetingSummarizer:
    return MeetingSummarizer(get_settings())