"""Verified-facts cache — Olympus gets cheaper to fact-check over time.

When Aletheia verifies a checkable claim, she can store the verdict here.
Future verification consults the cache first: a fact confirmed last week with
a source doesn't need re-searching today (until it goes stale).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config

# Facts older than this are considered stale and re-verified.
TTL_SECONDS = 30 * 86400


def _path() -> Path:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return config.MEMORY_DIR / "verified_facts.jsonl"


def _norm(claim: str) -> str:
    return re.sub(r"\s+", " ", claim.lower()).strip()[:300]


def record(claim: str, verdict: str, source: str = "") -> str:
    entry = {
        "claim": claim[:500],
        "norm": _norm(claim),
        "verdict": verdict[:200],
        "source": source[:300],
        "ts": int(time.time()),
    }
    with _path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
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
        score = sum(e["norm"].count(t) for t in terms)
        if score:
            scored.append((score, e))
    if not scored:
        return "No matching cached facts (verify fresh)."
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, e in scored[:limit]:
        age_days = int((now - e["ts"]) / 86400)
        out.append(f"- {e['claim']} → {e['verdict']} "
                   f"(source: {e['source'] or 'n/a'}; {age_days}d old)")
    return "\n".join(out)


def count() -> int:
    path = _path()
    if not path.exists():
        return 0
    return sum(1 for _ in path.open(encoding="utf-8"))
