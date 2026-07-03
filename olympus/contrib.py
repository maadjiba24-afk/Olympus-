"""Opt-in cross-model learning pool.

When a user explicitly opts in (default OFF), Olympus stores an anonymized
snapshot of their exchange — plus which model produced the answer — into a
shared queue. Metis's daily cycle distills genuinely novel, cross-model
insights from this queue into provisional shared skills (tagged with the
source model); the benchmark gate then keeps only the ones that measurably
help every model, including weaker ones.

The result: the best knowledge of *every* frontier model a user brings
(Claude, GPT, Gemini, …) is distilled into one shared, quality-gated skill
layer that lifts the whole system — without any user's raw data entering the
pool, and only with their consent.

Privacy invariants:
- Opt-in only. No contribution unless the user turned it on.
- Anonymized at write time (security.anonymize) and truncated.
- The pool holds short snapshots to distill from, never long verbatim data;
  Metis turns them into *methods*, and the raw queue is pruned regularly.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config, prefs, security

MAX_SNAPSHOT_CHARS = 1500
MAX_QUEUE = 2000
_LOCK = threading.Lock()


def _path() -> Path:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return config.MEMORY_DIR / "contributions.jsonl"


def is_enabled(user: str) -> bool:
    return bool(prefs.get(user, "contribute", False))


def set_enabled(user: str, on: bool) -> str:
    prefs.set(user, "contribute", bool(on))
    if on:
        return ("Cross-model contribution ON. Anonymized insights from your "
                "chats may improve Olympus's shared skills for everyone. No raw "
                "data is shared, and only proven skills are kept. Turn off any "
                "time.")
    return "Cross-model contribution OFF. Your chats stay private to you."


def offer(user: str, model: str, question: str, answer: str) -> bool:
    """Queue one anonymized exchange snapshot if the user opted in.

    Returns True if something was contributed."""
    if not is_enabled(user):
        return False
    q = security.anonymize(str(question))[:MAX_SNAPSHOT_CHARS]
    a = security.anonymize(str(answer))[:MAX_SNAPSHOT_CHARS]
    if not q.strip() or not a.strip():
        return False
    # A stored secret that survives anonymization (raw or encoded) must never
    # reach the shared pool — drop the snapshot entirely, don't trust redaction.
    if security.secret_exfil_reason(q + "\n" + a, user):
        return False
    entry = {"model": model or "unknown", "q": q, "a": a, "ts": int(time.time())}
    path = _path()
    with _LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        # bound the queue
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_QUEUE * 2:
            path.write_text("\n".join(lines[-MAX_QUEUE:]) + "\n",
                            encoding="utf-8")
    return True


def recent(n: int = 30) -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def digest(n: int = 30) -> str:
    """A readable, grouped-by-model digest for Metis to distill from."""
    items = recent(n)
    if not items:
        return "(no cross-model contributions queued)"
    by_model: dict[str, list[dict]] = {}
    for it in items:
        by_model.setdefault(it.get("model", "unknown"), []).append(it)
    blocks = []
    for model, rows in by_model.items():
        lines = [f"### From model: {model} ({len(rows)} exchanges)"]
        for r in rows[:12]:
            lines.append(f"- Q: {r['q'][:300]}\n  A: {r['a'][:500]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def count() -> int:
    path = _path()
    return sum(1 for _ in path.open(encoding="utf-8")) if path.exists() else 0


def clear_old(keep: int = 200) -> int:
    """Trim the queue after a distillation pass."""
    path = _path()
    if not path.exists():
        return 0
    with _LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        removed = max(0, len(lines) - keep)
        if removed:
            path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    return removed
