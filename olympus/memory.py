"""Persistent file-based memory — the substrate of self-improvement.

Layout:
    memory/
      lessons/ corrections/ feedback/        shared (system-generated)
      users/<user-id>/lessons|corrections|feedback/   per-user namespaces
      reports/ upgrades/ prompt_backups/ evals/       always shared (system)
      conversations/<id>.json                persisted chat histories
      skills/                                the self-built skill library

User-scoped categories keep one person's lessons, corrections, and feedback
out of everyone else's sessions. The active user is a context variable set by
the orchestrator at the start of each conversation turn.
"""

from __future__ import annotations

import contextvars
import json
import re
import time
from pathlib import Path

from . import config

CATEGORIES = ("lessons", "corrections", "feedback", "reports", "upgrades",
              "prompt_backups", "evals")
USER_SCOPED = {"lessons", "corrections", "feedback"}

_USER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "olympus_user", default="shared"
)


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")[:64] or "shared"


def set_user(user: str) -> None:
    """Set the memory namespace for the current thread/conversation."""
    _USER.set(safe_id(user))


def current_user() -> str:
    return _USER.get()


def _dir(category: str, user: str = "shared") -> Path:
    if category not in CATEGORIES:
        raise ValueError(f"unknown memory category: {category}")
    if category in USER_SCOPED and user != "shared":
        d = config.MEMORY_DIR / "users" / user / category
    else:
        d = config.MEMORY_DIR / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"


def save(category: str, title: str, content: str) -> Path:
    """Save into the current user's namespace (or shared for system work)."""
    d = _dir(category, current_user())
    path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}.md"
    path.write_text(f"# {title}\n\n{content.strip()}\n", encoding="utf-8")
    return path


def _search_dirs() -> list[Path]:
    dirs = [_dir(c) for c in CATEGORIES]
    if current_user() != "shared":
        dirs += [_dir(c, current_user()) for c in USER_SCOPED]
    return dirs


def search(query: str, limit: int = 5) -> str:
    """Ranked keyword search across shared memory + the current user's own."""
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    scored: list[tuple[float, Path, str]] = []
    for d in _search_dirs():
        for path in d.glob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            lower = text.lower()
            # diminishing returns per term + title hits weighted higher
            title = lower.splitlines()[0] if lower else ""
            score = 0.0
            for t in terms:
                hits = lower.count(t)
                if hits:
                    score += 1 + min(hits, 5) * 0.2 + (2 if t in title else 0)
            if score:
                scored.append((score, path, text))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return "No memory entries match that query."
    out = []
    for _, path, text in scored[:limit]:
        out.append(f"--- {path.parent.name}/{path.name} ---\n{text[:1500]}")
    return "\n\n".join(out)


def recent(category: str, n: int = 5) -> str:
    files = list(_dir(category).glob("*.md"))
    if category in USER_SCOPED and current_user() != "shared":
        files += list(_dir(category, current_user()).glob("*.md"))
    files = sorted(files, key=lambda p: p.name, reverse=True)[:n]
    if not files:
        return f"(no {category} recorded yet)"
    return "\n\n".join(
        f"--- {p.name} ---\n{p.read_text(encoding='utf-8', errors='replace')[:1500]}"
        for p in files
    )


# --- persisted conversations ---------------------------------------------

def _conversation_path(conversation_id: str) -> Path:
    d = config.MEMORY_DIR / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe_id(conversation_id)}.json"


def load_conversation(conversation_id: str) -> list[dict]:
    path = _conversation_path(conversation_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_conversation(conversation_id: str, history: list[dict]) -> None:
    _conversation_path(conversation_id).write_text(
        json.dumps(history, indent=1), encoding="utf-8"
    )


# --- YouTube watch queue ------------------------------------------------

def _watchlist_path() -> Path:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return config.MEMORY_DIR / "watchlist.txt"


def watchlist_add(url: str) -> None:
    with _watchlist_path().open("a", encoding="utf-8") as f:
        f.write(url.strip() + "\n")


def watchlist_pop() -> str | None:
    path = _watchlist_path()
    if not path.exists():
        return None
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return None
    url, rest = lines[0], lines[1:]
    path.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
    return url


# --- Heartbeat state -----------------------------------------------------

def load_state() -> dict:
    path = config.MEMORY_DIR / "heartbeat_state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = config.MEMORY_DIR / "heartbeat_state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
