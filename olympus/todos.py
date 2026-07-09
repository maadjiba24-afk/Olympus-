"""Notes, todos, and reminders — a small per-user checklist store.

The documents workspace holds long-form Markdown; this holds the short, tickable
things: quick notes, todo items, and reminders (a todo with a due time). Storage
mirrors the memory/documents convention — one JSON file per user under
`memory/users/<safe_id>/todos.json` — so it is dependency-free and inspectable.

An item is just data: `{id, text, kind, done, due, created}`. `kind` is "note"
(a kept scrap, never "done") or "todo" (tickable); a todo with a `due` timestamp
is a reminder, and `due_items()` surfaces the ones that are ready so the agenda
can show them and the scheduler can nudge. Nothing here has a side effect beyond
the user's own file, so the agent tools are first-party and ungated — the user
managing their own list, not an actuator.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import config, memory

_MAX_ITEMS = 500                 # bound the file
_MAX_TEXT = 2000                 # a single item's text cap


def _path(user: str) -> Path:
    safe = memory.safe_id(user)
    base = (config.MEMORY_DIR / "users" / safe) if safe != "shared" \
        else config.MEMORY_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / "todos.json"


def _load(user: str) -> list[dict]:
    p = _path(user)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user: str, items: list[dict]) -> None:
    if len(items) > _MAX_ITEMS:                 # keep the most recent
        items = sorted(items, key=lambda d: d.get("created", 0))[-_MAX_ITEMS:]
    _path(user).write_text(json.dumps(items, indent=2), encoding="utf-8")


def _parse_due(due: str | float | None) -> float | None:
    """Accept an epoch number or a forgiving date/datetime string; None if
    unparseable (the item is simply un-dated rather than rejected)."""
    if due is None or due == "" or isinstance(due, bool):
        return None                    # bool is an int subclass — never a time
    if isinstance(due, (int, float)):
        return float(due)
    s = str(due).strip()
    import datetime
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def add(user: str, text: str, *, kind: str = "todo",
        due: str | float | None = None, now: float | None = None) -> dict:
    text = (text or "").strip()[:_MAX_TEXT]
    if not text:
        raise ValueError("an item needs text")
    now = now if now is not None else time.time()
    kind = "note" if kind == "note" else "todo"
    item = {"id": uuid.uuid4().hex[:12], "text": text, "kind": kind,
            "done": False, "due": _parse_due(due), "created": now}
    items = _load(user)
    items.append(item)
    _save(user, items)
    return item


def complete(user: str, item_id: str, done: bool = True) -> bool:
    items = _load(user)
    found = False
    for it in items:
        if it["id"] == item_id:
            it["done"] = bool(done)
            found = True
    if found:
        _save(user, items)
    return found


def delete(user: str, item_id: str) -> bool:
    items = _load(user)
    kept = [it for it in items if it["id"] != item_id]
    if len(kept) == len(items):
        return False
    _save(user, kept)
    return True


def clear_done(user: str) -> int:
    items = _load(user)
    kept = [it for it in items if not it.get("done")]
    removed = len(items) - len(kept)
    if removed:
        _save(user, kept)
    return removed


def listing(user: str, *, include_done: bool = True) -> list[dict]:
    """Items newest-first; open items before done ones, due-soonest first."""
    items = _load(user)
    if not include_done:
        items = [it for it in items if not it.get("done")]

    def sort_key(it):
        due = it.get("due")
        return (it.get("done", False),
                due if due is not None else float("inf"),
                -it.get("created", 0))

    return sorted(items, key=sort_key)


def due_items(user: str, now: float | None = None) -> list[dict]:
    """Open reminders (todos with a due time) that are now due — for the agenda
    and the scheduler nudge."""
    now = now if now is not None else time.time()
    return [it for it in _load(user)
            if not it.get("done") and it.get("due") is not None
            and it["due"] <= now]


def render_list(user: str) -> str:
    items = listing(user)
    if not items:
        return ("Nothing on your list. Add one: "
                "olympus todo add \"buy milk\" [--due 2026-07-10].")
    import datetime
    lines = []
    for it in items:
        box = "[x]" if it.get("done") else "[ ]"
        tag = "" if it["kind"] == "todo" else " (note)"
        when = ""
        if it.get("due") is not None:
            when = "  ⏰ " + datetime.datetime.fromtimestamp(
                it["due"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{box} {it['text']}{tag}{when}   [{it['id']}]")
    return "\n".join(lines)
