"""Persistent file-based memory — the substrate of self-improvement.

Categories:
    lessons/      knowledge distilled from YouTube videos and experience
    corrections/  fixes recorded by the hallucination controller
    reports/      opportunity scans and world-event briefings
    upgrades/     improvement proposals written by Prometheus
    prompt_backups/  prior versions of any prompt Prometheus rewrites
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config

CATEGORIES = ("lessons", "corrections", "reports", "upgrades", "prompt_backups")


def _dir(category: str) -> Path:
    if category not in CATEGORIES:
        raise ValueError(f"unknown memory category: {category}")
    d = config.MEMORY_DIR / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"


def save(category: str, title: str, content: str) -> Path:
    path = _dir(category) / f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}.md"
    path.write_text(f"# {title}\n\n{content.strip()}\n", encoding="utf-8")
    return path


def search(query: str, limit: int = 5) -> str:
    """Naive keyword search across all memory files; returns matching excerpts."""
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    scored: list[tuple[int, Path, str]] = []
    for category in CATEGORIES:
        for path in _dir(category).glob("*.md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            lower = text.lower()
            score = sum(lower.count(t) for t in terms)
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
    files = sorted(_dir(category).glob("*.md"), reverse=True)[:n]
    if not files:
        return f"(no {category} recorded yet)"
    return "\n\n".join(
        f"--- {p.name} ---\n{p.read_text(encoding='utf-8', errors='replace')[:1500]}"
        for p in files
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
