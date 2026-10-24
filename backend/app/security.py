"""Upload validation (extension, magic bytes, size) and optional API-key authentication."""

import logging
import re
import secrets
import uuid
from pathlib import Path

from fastapi import Header, HTTPException, UploadFile

from app.config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".mp4"}
_CHUNK = 1024 * 1024


def sanitize_filename(name: str | None) -> str:
    """Strip any path components and unsafe characters from a client-supplied filename."""
    base = Path((name or "").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
    return base[:200] or "upload"


def has_valid_signature(ext: str, head: bytes) -> bool:
    """Check the file's leading bytes really match the claimed type."""
    if ext == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".mp4":
        return head[4:8] == b"ftyp"
    if ext == ".mp3":
        return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    return False


def _reject(status: int, detail: str, filename: str | None) -> HTTPException:
    logger.warning("Upload rejected (%s): %s [file=%r]", status, detail, filename)
    return HTTPException(status_code=status, detail=detail)


def save_upload(upload: UploadFile, dest_dir: Path, max_bytes: int) -> tuple[str, str, int]:
    """Validate and store an upload under a random name. Returns (original, stored, size)."""
    original = sanitize_filename(upload.filename)
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise _reject(415, "Unsupported file type. Allowed: MP3, WAV, MP4.", original)

    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{ext}"
    target = dest_dir / stored
    size = 0
    checked = False
    try:
        with target.open("wb") as out:
            while chunk := upload.file.read(_CHUNK):
                if not checked:
                    if not has_valid_signature(ext, chunk[:16]):
                        raise _reject(415, "File content does not match its extension.", original)
                    checked = True
                size += len(chunk)
                if size > max_bytes:
                    raise _reject(413, f"File too large (limit {max_bytes // (1024 * 1024)} MB).", original)
                out.write(chunk)
        if size == 0:
            raise _reject(400, "Uploaded file is empty.", original)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return original, stored, size


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Optional authentication: enforced only when API_KEY is configured."""
    expected = get_settings().api_key
    if not expected:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
