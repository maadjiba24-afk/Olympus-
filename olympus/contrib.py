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

from . import config, egress, prefs, security

MAX_SNAPSHOT_CHARS = 1500
MAX_QUEUE = 2000
_LOCK = threading.Lock()


def _pool_redact(user: str, text: str) -> str | None:
    """Pool-safe form of `text`, or None if it must not be pooled at all.

    Phase B (docs/DESIGN_BOUNDARY_LAYER.md): when the egress guard is enabled,
    the raw snapshot is routed through `egress.guard(..., POOLED)` so the
    redaction becomes a *policy decision recorded in the signed log* rather than
    an inline side effect — a HOLD (a stored-secret exfil the guard won't trust
    redaction to strip) drops the snapshot entirely. This is exactly equivalent
    to the direct `security.anonymize` path (guard's REDACT calls the same
    `anonymize`, and PUBLIC text is a no-op for both), so behavior is unchanged;
    it is now auditable and consistent with every other egress channel.

    When the guard is OFF (the default), fall back to the always-on direct
    `anonymize` — the pool's privacy floor never depends on the opt-in guard.
    """
    if config.egress_guard_enabled():
        d = egress.guard(str(text), egress.ChannelKind.POOLED, user=user)
        if d.verdict is egress.Verdict.HOLD:
            return None
        if d.verdict is egress.Verdict.REDACT:
            return d.redacted_text or ""
        return str(text)            # ALLOW: already within C0 (pool-safe)
    return security.anonymize(str(text))


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
    q_red = _pool_redact(user, str(question))
    a_red = _pool_redact(user, str(answer))
    # A guard HOLD (stored-secret exfil) on either half drops the whole snapshot.
    if q_red is None or a_red is None:
        return False
    q = q_red[:MAX_SNAPSHOT_CHARS]
    a = a_red[:MAX_SNAPSHOT_CHARS]
    if not q.strip() or not a.strip():
        return False
    # Always-on floor (independent of the opt-in egress guard): a stored secret
    # that survives redaction must never reach the shared pool — drop the
    # snapshot entirely, don't trust redaction.
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
