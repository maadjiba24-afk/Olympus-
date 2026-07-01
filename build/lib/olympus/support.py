"""User problem reports (complaints) — captured durably and alerted.

A public instance needs a place for users to say "this is broken," and the
operator needs to actually receive it. Reports are appended to
`MEMORY_DIR/reports/complaints.jsonl` (never lost) and pushed to the operator
over Telegram if configured. Review them with `olympus reports`.
"""

from __future__ import annotations

import json
import time

from . import config, telegram


def _path():
    d = config.MEMORY_DIR / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d / "complaints.jsonl"


def report(message: str, *, user: str = "anon", contact: str = "",
           context: str = "") -> dict:
    """Record a problem report and alert the operator. Raises ValueError on an
    empty message."""
    message = (message or "").strip()
    if not message:
        raise ValueError("empty report")
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user": str(user)[:64],
        "contact": str(contact).strip()[:200],
        "message": message[:4000],
        "context": str(context)[:500],
    }
    with _path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    try:
        who = rec["user"] + (f" ({rec['contact']})" if rec["contact"] else "")
        telegram.notify(
            "📣 Olympus problem report\n\n"
            f"from: {who}\n{rec['message']}"
            + (f"\n\ncontext: {rec['context']}" if rec["context"] else ""))
    except Exception:
        pass            # alerting is best-effort; the report is already saved
    return rec


def recent(n: int = 50) -> list[dict]:
    """The most recent reports, oldest-first."""
    path = _path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-n:]
