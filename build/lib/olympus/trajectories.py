"""Training-trajectory export — turn Olympus's own runs into training data.

Olympus already records two rich streams: persisted **conversations**
(input → final answer) and per-run **traces** (the route → plan → verify → review
decision path, each step with its agent and rationale). This module distills
them into JSONL trajectories suitable for fine-tuning tool-calling models —
the capability Nous's Hermes ships and Olympus was missing.

Two export shapes:

  * ``kind="messages"`` — clean SFT pairs from conversations:
        {"messages": [{"role": "user", ...}, {"role": "assistant", ...}]}
  * ``kind="trace"`` — the orchestration trajectory from traces:
        {"input": "...", "path": [{"step": "route", "agent": "zeus", ...}, ...]}

A light **compression** pass drops empty/sub-threshold turns and exact
duplicates so the dataset is clean, not raw. Nothing here calls a model; it
only reads what Olympus has already produced.
"""

from __future__ import annotations

import json

from . import config

MIN_CHARS = 12


def _conversations() -> list[tuple[str, list]]:
    d = config.MEMORY_DIR / "conversations"
    out = []
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            out.append((path.stem, json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _traces() -> list[dict]:
    base = config.MEMORY_DIR / "traces"
    out = []
    if not base.exists():
        return out
    for path in sorted(base.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def conversation_trajectories() -> list[dict]:
    """Pair each user turn with the assistant turn that follows it."""
    traj = []
    for _cid, history in _conversations():
        msgs = [m for m in history
                if str(m.get("content", "")).strip()
                and not str(m.get("content", "")).startswith("[Conversation state")]
        i = 0
        while i < len(msgs) - 1:
            a, b = msgs[i], msgs[i + 1]
            if a.get("role") == "user" and b.get("role") == "assistant":
                if (len(str(a["content"])) >= MIN_CHARS
                        and len(str(b["content"])) >= MIN_CHARS):
                    traj.append({"messages": [
                        {"role": "user", "content": str(a["content"])},
                        {"role": "assistant", "content": str(b["content"])}]})
                i += 2
            else:
                i += 1
    return traj


def trace_trajectories() -> list[dict]:
    """Extract the decision path (the orchestration trajectory) from each run."""
    traj = []
    for rec in _traces():
        decisions = rec.get("decisions") or []
        if not decisions:
            continue
        path = []
        for d in decisions:
            agent = d.get("agent") or {}
            path.append({
                "step": d.get("type") or d.get("decision_type", ""),
                "agent": agent.get("name", ""),
                "role": agent.get("role", ""),
                "rationale": d.get("rationale"),
            })
        traj.append({"input": (rec.get("meta") or {}).get("input", ""),
                     "path": path})
    return traj


def compress(records: list[dict]) -> list[dict]:
    """Drop exact duplicates, preserving order."""
    seen, out = set(), []
    for r in records:
        key = json.dumps(r, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def build(kind: str = "messages") -> list[dict]:
    if kind == "trace":
        return compress(trace_trajectories())
    return compress(conversation_trajectories())


def export(path: str, kind: str = "messages") -> int:
    """Write trajectories to `path` as JSONL. Returns the record count."""
    records = build(kind)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)
