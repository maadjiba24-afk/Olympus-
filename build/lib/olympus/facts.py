"""Verified-facts cache — Olympus gets cheaper to fact-check over time.

When Aletheia verifies a checkable claim, she can store the verdict here.
Future verification consults the cache first: a fact confirmed last week with
a source doesn't need re-searching today (until it goes stale).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import config

# Facts older than this are considered stale and re-verified.
TTL_SECONDS = 30 * 86400

# Hard cap on stored facts; the oldest are dropped past this (append-only file
# is compacted when it grows beyond ~2x the cap).
MAX_FACTS = 5000


def _path() -> Path:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return config.MEMORY_DIR / "verified_facts.jsonl"


def _norm(claim: str) -> str:
    return re.sub(r"\s+", " ", claim.lower()).strip()[:300]


_LOCK = threading.Lock()

# Cached line count per log path, so compaction stays EAGER (checked on every
# write) without re-scanning the whole file each time: the file is counted once
# per path per process, then tracked incrementally. Keyed by path string so a
# changed MEMORY_DIR (e.g. in tests) just starts a fresh count.
_line_counts: dict[str, int] = {}


def _count_lines(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _trim(path: Path) -> None:
    """Atomically keep only the newest MAX_FACTS entries. Temp file + os.replace
    so a crash mid-compaction can't corrupt/truncate the cache. Caller holds
    _LOCK."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines[-MAX_FACTS:]) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record(claim: str, verdict: str, source: str = "") -> str:
    entry = {
        "claim": claim[:500],
        "norm": _norm(claim),
        "verdict": verdict[:200],
        "source": source[:300],
        "ts": int(time.time()),
    }
    path = _path()
    with _LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        key = str(path)
        n = _line_counts.get(key)
        n = (n + 1) if n is not None else _count_lines(path)
        # Compact when the append-only log grows past 2x the cap.
        if n > MAX_FACTS * 2:
            _trim(path)
            n = _count_lines(path)
        _line_counts[key] = n
    return "Fact cached."


def lookup(query: str, limit: int = 5) -> str:
    path = _path()
    if not path.exists():
        return "No cached facts."
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    now = time.time()
    scored = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if now - e.get("ts", 0) > TTL_SECONDS:
            continue
        # Tolerate legacy / hand-edited / partially-written lines that lack a
        # field — a single malformed entry must not crash the whole lookup.
        score = sum(e.get("norm", "").count(t) for t in terms)
        if score:
            scored.append((score, e))
    if not scored:
        return "No matching cached facts (verify fresh)."
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, e in scored[:limit]:
        age_days = int((now - e.get("ts", 0)) / 86400)
        out.append(f"- {e.get('claim', '?')} → {e.get('verdict', '?')} "
                   f"(source: {e.get('source') or 'n/a'}; {age_days}d old)")
    return "\n".join(out)


def count() -> int:
    path = _path()
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:   # explicit close — no leaked handle
        return sum(1 for _ in f)
