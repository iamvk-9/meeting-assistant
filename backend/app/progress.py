"""In-memory transcription progress, keyed by meeting id (0.0 - 1.0).

Kept in memory on purpose: it is short-lived, changes many times per minute, and the background
tasks run inside the API process. With several API workers you would move this to Redis.
"""

import threading

_lock = threading.Lock()
_progress: dict[int, float] = {}


def set_progress(meeting_id: int, fraction: float) -> None:
    with _lock:
        _progress[meeting_id] = max(0.0, min(1.0, float(fraction)))


def get_progress(meeting_id: int) -> float | None:
    with _lock:
        return _progress.get(meeting_id)


def clear_progress(meeting_id: int) -> None:
    with _lock:
        _progress.pop(meeting_id, None)
