"""Per-user preferences (language, etc.), namespaced like memory.

Small JSON store so a user's choices — most importantly their language —
persist across sessions and interfaces. Kept separate from lessons/memory
because preferences are settings, not learned knowledge.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config, memory


def _path(user: str) -> Path:
    safe = memory.safe_id(user)
    base = (config.MEMORY_DIR / "users" / safe) if safe != "shared" \
        else config.MEMORY_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / "prefs.json"


def load(user: str) -> dict:
    path = _path(user)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def get(user: str, key: str, default=None):
    return load(user).get(key, default)


def set(user: str, key: str, value) -> None:
    data = load(user)
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    _path(user).write_text(json.dumps(data, indent=2), encoding="utf-8")
