"""Small helpers."""

import re
from email.utils import parseaddr

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def parse_participants(raw: str | None) -> list[dict[str, str | None]]:
    """Parse 'Alice <a@x.com>, bob@y.com, Carol' into [{name, email}, ...] (deduplicated)."""
    if not raw:
        return []
    result: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for part in re.split(r"[,;\n]", raw):
        part = part.strip()
        if not part:
            continue
        name, addr = parseaddr(part)
        if addr and _EMAIL_RE.match(addr):
            entry: dict[str, str | None] = {"name": (name or None), "email": addr.lower()}
            key = addr.lower()
        else:
            entry = {"name": part[:255], "email": None}
            key = part.lower()
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result
