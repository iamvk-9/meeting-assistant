import io

import pytest
from fastapi import HTTPException, UploadFile

from app.security import has_valid_signature, sanitize_filename, save_upload
from app.utils import parse_participants


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def test_sanitize_filename_strips_paths_and_unsafe_chars():
    assert sanitize_filename("../../etc/passwd.mp3") == "passwd.mp3"
    assert sanitize_filename("C:\\temp\\my meeting?.wav") == "my meeting_.wav"
    assert sanitize_filename(None) == "upload"


def test_signatures():
    assert has_valid_signature(".wav", b"RIFF\x00\x00\x00\x00WAVEfmt ")
    assert has_valid_signature(".mp3", b"ID3\x03\x00")
    assert has_valid_signature(".mp3", b"\xff\xfb\x90\x00")
    assert has_valid_signature(".mp4", b"\x00\x00\x00\x18ftypisom")
    assert not has_valid_signature(".wav", b"MZ\x90\x00 not audio")
    assert not has_valid_signature(".exe", b"MZ")


def test_save_upload_accepts_valid_wav(tmp_path, wav_bytes):
    original, stored, size = save_upload(_upload("a.wav", wav_bytes), tmp_path, 10_000_000)
    assert original == "a.wav" and stored.endswith(".wav") and size == len(wav_bytes)
    assert (tmp_path / stored).exists()


@pytest.mark.parametrize(
    "name,data,status",
    [
        ("evil.exe", b"MZ\x90\x00", 415),
        ("fake.wav", b"this is not audio at all", 415),
        ("empty.wav", b"", 400),
    ],
)
def test_save_upload_rejects_bad_files(tmp_path, name, data, status):
    with pytest.raises(HTTPException) as exc:
        save_upload(_upload(name, data), tmp_path, 10_000_000)
    assert exc.value.status_code == status
    assert list(tmp_path.iterdir()) == []  # nothing left behind


def test_save_upload_enforces_size_limit(tmp_path, wav_bytes):
    with pytest.raises(HTTPException) as exc:
        save_upload(_upload("big.wav", wav_bytes), tmp_path, 100)
    assert exc.value.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_parse_participants():
    parsed = parse_participants("Alice <A@x.com>, bob@y.com; Carol\nbob@y.com")
    assert parsed == [
        {"name": "Alice", "email": "a@x.com"},
        {"name": None, "email": "bob@y.com"},
        {"name": "Carol", "email": None},
    ]
    assert parse_participants(None) == []
