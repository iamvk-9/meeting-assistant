"""Application settings, loaded from environment variables / a local .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root (.../meeting-assistant), so the .env is found no matter which
# directory uvicorn is started from (the README runs it from backend/).
_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(_ROOT / ".env", ".env"), extra="ignore", env_ignore_empty=True)

    # Storage
    database_url: str = "sqlite:///./meetings.db"
    upload_dir: Path = Path("data/uploads")
    log_dir: Path = Path("logs")
    max_upload_mb: int = 200

    # Security
    api_key: str | None = None  # when set, every API call needs an X-API-Key header
    cors_origins: str = ""  # comma-separated; empty disables CORS (UI is same-origin)

    # Speech-to-text
    whisper_model: str = "base"
    default_language: str | None = None  # empty = let Whisper auto-detect

    # Speaker identification (optional; needs pyannote.audio and a Hugging Face token)
    diarization_enabled: bool = False
    hf_token: str | None = None

    # LLM (any LiteLLM model string, e.g. gemini/..., openai/gpt-4o-mini, anthropic/claude-...)
    llm_model: str = "groq/llama-3.3-70b-versatile"
    llm_temperature: float = 0.3
    llm_chunk_chars: int = 12000
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    groq_api_key: str | None = None

    # Live transcription (browser microphone -> WebSocket)
    live_window_seconds: int = 8  # audio is transcribed in windows of this length
    live_max_minutes: int = 120  # hard limit per live session

    # Task export (optional integrations)
    jira_base_url: str | None = None  # e.g. https://yourcompany.atlassian.net
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str | None = None
    jira_issue_type: str = "Task"
    trello_key: str | None = None
    trello_token: str | None = None
    trello_list_id: str | None = None

    # Email (SMTP)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_provider(self) -> str:
        return self.llm_model.split("/", 1)[0].lower()

    @property
    def llm_api_key(self) -> str | None:
        keys = {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "groq": self.groq_api_key,
        }
        return keys.get(self.llm_provider) or None


@lru_cache
def get_settings() -> Settings:
    return Settings()