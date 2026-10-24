from app.config import Settings


def test_empty_env_values_fall_back_to_defaults(monkeypatch):
    """Blank entries in a copied .env.example must not override defaults (empty DATABASE_URL crashed startup)."""
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("WHISPER_MODEL", "")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./meetings.db"
    assert settings.whisper_model == "base"
