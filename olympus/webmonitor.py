"""Web change-monitoring — scheduled diffing of watched pages.

The surveyed scraper ships a hosted "monitor" that re-crawls URLs on a schedule and alerts
on change — but it removed its own audit trail (enforcement-only, no security
events) and runs in an open-by-default service. Olympus absorbs the *capability*
natively and keeps what the surveyed scraper dropped:

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
from dataclasses import asdict, dataclass, fields
from typing import Callable

from . import config

MAX_MONITORS = 50                 # per install — a bound, not an ambition
_MIN_INTERVAL = 15 * 60           # 15 min floor; a watcher can't hammer a site
_SNAPSHOT_CAP = 40_000            # stored markdown per monitor


_MAX_FAILS = 20                   # consecutive errors before a monitor auto-pauses


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
    fails: int = 0                # consecutive check failures (for back-off)
    schema: str = ""             # JSON-encoded schema → json (structured) mode
    last_json: str = ""          # last extracted object (json mode), JSON-encoded


def _path():
    return config.MEMORY_DIR / "webmonitors.json"


def _load() -> list[Monitor]:
    p = _path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A whole-file corruption is NOT a torn read: quarantine it instead of
        # letting the next _save silently clobber the user's whole watchlist.
        try:
            p.replace(p.with_name(p.name + ".corrupt"))
        except OSError:
            pass
        return []
    if not isinstance(raw, list):
        return []
    # Decode per-record so ONE bad/old-schema entry can't collapse the whole
    # list to [] (which the next _save would then persist as data loss).
    known = {f.name for f in fields(Monitor)}
    out: list[Monitor] = []
    for d in raw:
        if isinstance(d, dict):
            try:
                out.append(Monitor(**{k: v for k, v in d.items() if k in known}))
            except TypeError:
                continue
    return out


def _save(monitors: list[Monitor]) -> None:
    # Atomic publish (tmp + os.replace); readers run lock-free and a torn read
    # maps to [] (ADR 0005), same as goals.py.
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    from . import atomicio
    atomicio.publish(tmp, p,
                     json.dumps([asdict(m) for m in monitors], indent=2))


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


# --- CRUD ------------------------------------------------------------------

def add(user: str, url: str, interval: int = 3600,
        schema: dict | None = None) -> str:
    """Register a URL to watch. Does NOT fetch here (the heartbeat establishes
    the baseline on its first due check) — so this is an action confirmation,
    not an ingestion. An obviously-internal URL is refused up front. With
    `schema`, the monitor tracks structured (JSON-mode) change instead of text."""
    import ipaddress
    import json as _json
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
        schema_str = ""
        if isinstance(schema, dict) and schema:
            try:
                schema_str = _json.dumps(schema)[:20_000]
            except (TypeError, ValueError):
                schema_str = ""
        mon = Monitor(id=uuid.uuid4().hex[:8], user=user, url=url,
                      interval=interval, created=time.time(), schema=schema_str)
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

    import json as _json

    # Phase 1 — under the lock, pick the due monitors and snapshot just the
    # fields the check needs, then RELEASE the lock. Network I/O must never run
    # while the store mutex is held (one slow/hostile site would otherwise
    # freeze add/remove and every other run_due for minutes).
    with _mutex():
        due = [{"id": m.id, "url": m.url, "last_hash": m.last_hash,
                "last_markdown": m.last_markdown, "schema": m.schema,
                "last_json": m.last_json}
               for m in _load()
               if m.active and (now - m.last_checked) >= _effective_interval(m)]
    if not due:
        return []

    # Phase 2 — fetch + diff + notify with NO lock held. Accumulate per-id
    # updates to merge back afterward.
    out: list[str] = []
    updates: dict[str, dict] = {}
    for mon in due:
        mid, url = mon["id"], mon["url"]
        upd: dict = {"last_checked": now, "fail": False}
        json_mode = bool(mon["schema"])
        try:
            if json_mode:
                schema = _json.loads(mon["schema"])
                prev = _json.loads(mon["last_json"] or "null")
                d = webctx.diff(url, schema=schema, previous_json=prev)
            else:
                d = webctx.diff(url, mon["last_markdown"])
        except Exception as err:
            out.append(f"monitor {mid} {url}: check failed ({str(err)[:80]})")
            upd["fail"] = True
            updates[mid] = upd
            continue
        if d.get("error"):
            out.append(f"monitor {mid} {url}: {d['error']}")
            upd["fail"] = True
            updates[mid] = upd
            continue
        new_hash = d.get("current_hash", "")
        if new_hash != mon["last_hash"]:
            upd["last_hash"] = new_hash
            had_baseline = bool(mon["last_hash"])
            if json_mode:
                upd["last_json"] = _json.dumps(d.get("current_json", {}))[:_SNAPSHOT_CAP]
                detail = _json.dumps(d.get("changed_fields", []))[:1500]
            else:
                upd["last_markdown"] = (d.get("current_markdown", "") or "")[:_SNAPSHOT_CAP]
                detail = (d.get("diff") or "")[:1500]
            if had_baseline:
                upd["changed"] = True
                # Neutralize any ``` in attacker-controlled page text so it can't
                # break out of the code fence in the operator's chat client.
                snippet = detail.replace("```", "`​``")
                out.append(f"monitor {mid} {url}: CHANGED")
                try:
                    notify(f"🔔 Page changed: {url}\n\n```\n{snippet}\n```")
                except Exception as err:
                    # Surface the dropped alert rather than swallowing it; the
                    # hash still advances so we don't re-notify in a storm.
                    out.append(f"monitor {mid} {url}: change alert NOT delivered "
                               f"({str(err)[:60]})")
            else:
                out.append(f"monitor {mid} {url}: baseline captured")
        updates[mid] = upd

    # Phase 3 — re-acquire, reload, and merge by id (so a concurrent add/remove
    # during phase 2 is never lost or resurrected), then persist once.
    with _mutex():
        monitors = _load()
        for m in monitors:
            upd = updates.get(m.id)
            if upd is None:
                continue
            m.last_checked = upd["last_checked"]
            if upd["fail"]:
                m.fails += 1
                if m.fails >= _MAX_FAILS and m.active:
                    m.active = False               # auto-pause a dead monitor
                    out.append(f"monitor {m.id} {m.url}: paused after "
                               f"{m.fails} consecutive failures")
            else:
                m.fails = 0
            if "last_hash" in upd:
                m.last_hash = upd["last_hash"]
            if "last_markdown" in upd:
                m.last_markdown = upd["last_markdown"]
            if "last_json" in upd:
                m.last_json = upd["last_json"]
            if upd.get("changed"):
                m.changes += 1
        _save(monitors)
    return out


def _effective_interval(m: "Monitor") -> float:
    """A monitor's check cadence, widened (exponential, capped 8×) while it is
    failing so a permanently-broken URL stops burning cycles before it hits the
    auto-pause threshold."""
    if not m.fails:
        return m.interval
    return m.interval * min(8, 2 ** min(m.fails, 3))
