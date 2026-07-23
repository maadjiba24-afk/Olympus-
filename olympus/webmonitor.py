"""Web change-monitoring — scheduled diffing of watched pages.

Firecrawl ships a hosted "monitor" that re-crawls URLs on a schedule and alerts
on change — but it removed its own audit trail (enforcement-only, no security
events) and runs in an open-by-default service. Olympus absorbs the *capability*
natively and keeps what Firecrawl dropped:

  * **Opt-in, never open-by-default.** The heartbeat scheduler does nothing
    unless `OLYMPUS_WEB_MONITOR` is set; a default install is byte-identical.
  * **Gated + wrapped by construction.** Each scheduled check fetches through
    `webctx.diff` → `tools._http_get` (SSRF/egress/rebinding-pinned), and the
    changed content is untrusted data reported to the operator, never executed.
  * **Auditable.** Every check and every change notification is a log line the
    heartbeat returns; the snapshot store is a diffable on-disk record, not a
    black box.
  * **Replay-safe.** The scheduler is a heartbeat behavior, off the council
    replay hot path, and forced inert under `OLYMPUS_REPLAY`.

Storage: `MEMORY_DIR/webmonitors.json` (the same atomic dataclass-list pattern
as goals.py / scheduler.py).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import config

MAX_MONITORS = 50                 # per install — a bound, not an ambition
_MIN_INTERVAL = 15 * 60           # 15 min floor; a watcher can't hammer a site
_SNAPSHOT_CAP = 40_000            # stored markdown per monitor


@dataclass
class Monitor:
    id: str
    user: str
    url: str
    interval: int = 3600
    created: float = 0.0
    last_checked: float = 0.0
    last_hash: str = ""
    last_markdown: str = ""
    active: bool = True
    changes: int = 0


def _path():
    return config.MEMORY_DIR / "webmonitors.json"


def _load() -> list[Monitor]:
    p = _path()
    if not p.exists():
        return []
    try:
        return [Monitor(**d) for d in json.loads(p.read_text(encoding="utf-8"))]
    except (json.JSONDecodeError, TypeError, OSError):
        return []


def _save(monitors: list[Monitor]) -> None:
    # Atomic publish (tmp + os.replace); readers run lock-free and a torn read
    # maps to [] (ADR 0005), same as goals.py.
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps([asdict(m) for m in monitors], indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def _mutex():
    from . import proclock
    return proclock.lock("webmonitors")


def enabled() -> bool:
    """The scheduled watcher runs only when opted in — and never during replay
    (a network+clock behavior must not diverge a replay)."""
    if os.environ.get("OLYMPUS_REPLAY"):
        return False
    return os.environ.get("OLYMPUS_WEB_MONITOR", "").strip().lower() in (
        "1", "true", "yes", "on")


def _every() -> int:
    try:
        return max(60, int(os.environ.get("OLYMPUS_WEB_MONITOR_EVERY", "900")))
    except ValueError:
        return 900


# --- CRUD ------------------------------------------------------------------

def add(user: str, url: str, interval: int = 3600) -> str:
    """Register a URL to watch. Does NOT fetch here (the heartbeat establishes
    the baseline on its first due check) — so this is an action confirmation,
    not an ingestion. An obviously-internal URL is refused up front."""
    import ipaddress
    from urllib.parse import urlparse
    from . import security
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Usage: a monitor URL must be http(s)."
    # Name-level SSRF check with NO DNS/IO (add() must not fetch or resolve — the
    # authoritative gated fetch happens later in run_due). This catches scheme,
    # blocked hostnames (localhost/metadata) and — via the literal-IP guard below
    # — internal IP literals; a public *hostname* that resolves internal is caught
    # at scheduled-fetch time by the pinned _http_get.
    reason = security.url_block_reason(url, resolve=False)
    if reason:
        return f"Refused: {reason}"
    host = urlparse(url).hostname or ""
    try:
        if not security._ip_is_public(ipaddress.ip_address(host)):
            return f"Refused: {host} is an internal/non-public address."
    except ValueError:
        pass                                    # not an IP literal — deferred to run_due
    try:
        interval = max(_MIN_INTERVAL, int(interval))
    except (TypeError, ValueError):
        interval = 3600
    with _mutex():
        monitors = _load()
        if sum(m.active for m in monitors) >= MAX_MONITORS:
            return (f"Already watching {MAX_MONITORS} pages — remove one first "
                    "(`olympus monitor list` / `remove`).")
        for m in monitors:
            if m.user == user and m.url == url and m.active:
                return f"Already watching {url} (monitor {m.id})."
        mon = Monitor(id=uuid.uuid4().hex[:8], user=user, url=url,
                      interval=interval, created=time.time())
        monitors.append(mon)
        _save(monitors)
    on = "" if enabled() else (" NOTE: scheduled checks are OFF until "
                               "OLYMPUS_WEB_MONITOR is set.")
    return (f"Watching {url} (monitor {mon.id}, every "
            f"{max(interval // 60, 1)} min).{on}")


def remove(user: str, monitor_id: str) -> str:
    with _mutex():
        monitors = _load()
        kept = [m for m in monitors if not (m.id == monitor_id and m.user == user)]
        if len(kept) == len(monitors):
            return f"No monitor {monitor_id} for you."
        _save(kept)
    return f"Stopped watching (monitor {monitor_id})."


def list_for(user: str) -> list[Monitor]:
    return [m for m in _load() if m.user == user]


def list_text(user: str) -> str:
    mine = list_for(user)
    if not mine:
        return "No watched pages. Add one with web_monitor_add / `olympus monitor add <url>`."
    lines = ["Watched pages:"]
    for m in mine:
        when = ("never" if not m.last_checked
                else f"{int((time.time() - m.last_checked) / 60)} min ago")
        lines.append(f"  {m.id}  {m.url}  (every {max(m.interval // 60, 1)}m; "
                     f"{m.changes} change(s); last checked {when})")
    return "\n".join(lines)


# --- scheduled check (heartbeat) ------------------------------------------

def run_due(now: float | None = None,
            notify: Callable[[str], object] | None = None) -> list[str]:
    """Check every active monitor whose interval has elapsed; notify on a real
    change. Called by heartbeat.tick. No-op (returns []) unless opted in. Never
    raises out — a single bad monitor is logged and skipped."""
    if not enabled():
        return []
    now = now or time.time()
    from . import webctx
    if notify is None:
        from . import gateway
        notify = gateway.notify_all
    out: list[str] = []
    changed_any = False
    with _mutex():
        monitors = _load()
        for m in monitors:
            if not m.active or (now - m.last_checked) < m.interval:
                continue
            try:
                d = webctx.diff(m.url, m.last_markdown)
            except Exception as err:
                out.append(f"monitor {m.id} {m.url}: check failed ({str(err)[:80]})")
                m.last_checked = now
                changed_any = True
                continue
            m.last_checked = now
            changed_any = True
            if d.get("error"):
                out.append(f"monitor {m.id} {m.url}: {d['error']}")
                continue
            new_hash = d.get("current_hash", "")
            had_baseline = bool(m.last_hash)
            if new_hash != m.last_hash:
                m.last_hash = new_hash
                m.last_markdown = (d.get("current_markdown", "") or "")[:_SNAPSHOT_CAP]
                if had_baseline:
                    m.changes += 1
                    # Neutralize any ``` in attacker-controlled page text so it
                    # can't break out of the code fence in the operator's chat
                    # client (cosmetic; the content reaches a person, not a model).
                    snippet = (d.get("diff") or "")[:1500].replace("```", "`​``")
                    out.append(f"monitor {m.id} {m.url}: CHANGED")
                    try:
                        notify(f"🔔 Page changed: {m.url}\n\n"
                               f"```diff\n{snippet}\n```")
                    except Exception:
                        pass
                else:
                    out.append(f"monitor {m.id} {m.url}: baseline captured")
        if changed_any:
            _save(monitors)
    return out
