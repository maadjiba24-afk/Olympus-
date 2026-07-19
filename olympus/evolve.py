"""Feature self-evolution — capabilities that measure and improve themselves.

Olympus already self-improves in three narrow places: prompts (Prometheus,
benchmark-gated), skills (Metis + the curator, benchmark-gated), and action
defaults (`outcomes.py`, which *surfaces* friction rather than silently
changing behaviour). This module extends that same honest loop to the
*features* themselves — goals, MoA, /learn, the browser harness, and the
rest — so each one accrues a track record and gets better over time.

Doctrine, inherited from outcomes.py and enforced here:

  * **Measure everything, impose nothing risky.** Every feature records
    success / failure / degraded outcomes. A periodic review computes health
    and writes a report. It NEVER silently changes security-relevant
    behaviour — those stay suggestions to the operator.
  * **Auto-tune only within hard guardrails, and reversibly.** A parameter
    may self-adjust ONLY if it is registered here with an explicit
    [min, max] range and is not security-relevant (no egress, no auth, no
    capability gate). Every adjustment is bounded, logged, and clamped, so a
    runaway feedback loop is structurally impossible.
  * **Everything is bounded.** The telemetry log is capped per feature; a
    review is idempotent and cheap; a broken feature's telemetry can never
    take down the reviewer (each step is isolated).

Storage: the shared store backend, one blob, capped — same substrate as
outcomes.py.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import proclock, store

# Cross-process guard for the telemetry/tunables blob (ADR 0005): both the
# heartbeat process and the web process write it, and a lost update or a torn
# read would silently rebuild the dataset — including the tighten_only
# security tunables — from defaults. Bounded wait: these are best-effort
# telemetry paths, so losing one write under contention beats letting a
# wedged peer process hang a reply. Always acquired BEFORE _LOCK so the
# in-process mutex is never held across a flock wait.
_PROC_TIMEOUT = 2.0


def _guard():
    return proclock.lock("evolve", timeout=_PROC_TIMEOUT)

_NS = "evolve"
_KEY = "telemetry"
_LOCK = threading.Lock()
_MAX_PER_FEATURE = 500          # bounded ring per feature (oldest dropped)
_MIN_SAMPLES = 8                # don't infer from a tiny history
_DEGRADE_FLOOR = 0.75           # success rate below this warrants attention

# Outcome kinds.
OK = "ok"
FAIL = "fail"
DEGRADED = "degraded"           # produced a result, but a worse/fallback one


# --- tunable-parameter registry --------------------------------------------
# A parameter is auto-tunable ONLY if registered here. Security-relevant
# settings are deliberately absent and can never be reached by the reviewer.

@dataclass(frozen=True)
class Tunable:
    feature: str
    name: str
    lo: float
    hi: float
    default: float
    # direction: on sustained failure, should the value go up or down to help?
    # e.g. MoA reference count → down (fewer, more reliable members) on failure.
    on_fail: str                # "increase" | "decrease"
    note: str = ""
    # A SECURITY-relevant knob that may auto-move ONLY toward its safe (more
    # conservative) extreme, never back. The reviewer only ever steps a param in
    # its `on_fail` direction on degradation and never reverses on health, so
    # this holds by construction as long as `default` sits at the LOOSE bound and
    # `on_fail` points at the TIGHT bound — which register_tunable enforces.
    # Auto-tightening a security knob is safe (its worst case is "ask a human
    # more often", the safe default); auto-loosening one never is. Only a human
    # widens a tightened knob, via `reset()`.
    tighten_only: bool = False


_TUNABLES: dict[str, Tunable] = {}


def register_tunable(t: Tunable) -> None:
    if t.lo > t.hi or not (t.lo <= t.default <= t.hi):
        raise ValueError(f"tunable {t.feature}.{t.name}: default/range invalid")
    if t.on_fail not in ("increase", "decrease"):
        raise ValueError(f"tunable {t.feature}.{t.name}: bad on_fail {t.on_fail!r}")
    if t.tighten_only:
        # The loose bound is the end `on_fail` moves AWAY from. Pinning `default`
        # there means the [lo, hi] clamp can never let the value drift looser
        # than default — the tighten-only guarantee is structural, not a matter
        # of the reviewer behaving.
        loose_bound = t.lo if t.on_fail == "increase" else t.hi
        if t.default != loose_bound:
            raise ValueError(
                f"tighten_only tunable {t.feature}.{t.name}: default "
                f"({t.default}) must equal the loose bound ({loose_bound}) so it "
                "can only ever move toward the safe extreme")
    _TUNABLES[f"{t.feature}.{t.name}"] = t


# The initial safe, non-security tunables. Each has a hard [lo, hi] clamp.
for _t in (
    Tunable("moa", "reference_count", lo=2, hi=6, default=4, on_fail="decrease",
            note="fewer reference models when the ensemble is flaky"),
    Tunable("goals", "check_backoff", lo=1.0, hi=4.0, default=1.0,
            on_fail="increase",
            note="widen the work cadence for goals that keep failing to close"),
    Tunable("curator", "prune_per_run", lo=1, hi=3, default=3, on_fail="decrease",
            note="prune more cautiously if prunes keep getting reverted"),
    Tunable("ace", "max_bullets", lo=20, hi=60, default=60, on_fail="decrease",
            note="shrink the delta-context playbook when compaction degrades "
                 "(pinned facts are exempt from the cap)"),
    # Integration-depth components (D1-D8). All non-security resource knobs —
    # they bound cost/breadth, never a safety gate (the side-effect halt, egress
    # guard, allowlists, and signing stay hard constants outside the tuner).
    Tunable("treesearch", "max_nodes", lo=10, hi=100, default=50,
            on_fail="decrease",
            note="explore fewer nodes when searches keep degrading"),
    Tunable("dytopo", "max_out_degree", lo=1, hi=3, default=2,
            on_fail="decrease",
            note="sparser collaboration graph when routing degrades"),
    Tunable("emem", "max_fragments", lo=4, hi=12, default=12,
            on_fail="decrease",
            note="reconstruct fewer episode fragments when it degrades"),
    Tunable("liveeval", "sample_size", lo=10, hi=50, default=20,
            on_fail="increase",
            note="sample more recent runs when the quality signal is noisy"),
    Tunable("scaffold_evolve", "max_archive", lo=50, hi=200, default=200,
            on_fail="decrease",
            note="keep a smaller variant archive when proposals keep failing"),
    # Earned-autonomy policy knobs (trust.py). SECURITY-relevant, so tighten_only:
    # when the operator degrades, the earned-autonomy envelope auto-narrows
    # (higher bar to earn trust, longer settle after a surprise, fewer unattended
    # runs/day) and never auto-widens. A human widens it back with `evolve reset`.
    Tunable("operator", "establish_after", lo=20, hi=100, default=20,
            on_fail="increase", tighten_only=True,
            note="require more clean runs to fully trust a site when the "
                 "operator is failing"),
    Tunable("operator", "cooldown_secs", lo=3600, hi=86400, default=3600,
            on_fail="increase", tighten_only=True,
            note="lengthen the post-surprise settle window when the operator "
                 "is failing"),
    Tunable("operator", "daily_ceiling", lo=5, hi=25, default=25,
            on_fail="decrease", tighten_only=True,
            note="cap unattended earned auto-runs/day tighter when the operator "
                 "is failing"),
):
    register_tunable(_t)


# --- telemetry --------------------------------------------------------------

def _load() -> dict:
    blob = store.backend().get(_NS, _KEY)
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    store.backend().put(_NS, _KEY, json.dumps(data).encode())


def record(feature: str, outcome: str, detail: str = "") -> None:
    """Append one feature outcome (best-effort; never raises into the caller).
    `feature` is a short slug ('moa', 'learn', 'browser_open', ...)."""
    if outcome not in (OK, FAIL, DEGRADED):
        return
    try:
        with _guard(), _LOCK:
            data = _load()
            feat = data.setdefault(feature, {"events": [], "tunes": []})
            feat["events"].append({"ts": _now(), "o": outcome,
                                   "d": (detail or "")[:200]})
            feat["events"] = feat["events"][-_MAX_PER_FEATURE:]
            _save(data)
    except Exception:
        pass


def _now() -> float:
    return time.time()


# --- structured event log (Phase 3 evolution governance) --------------------
# Machine-readable counterpart to record()'s human string: each event is a flat
# dict of named fields (delta counters, rewrite metrics, …), so the evolution
# layer's behaviour can be queried and audited, not just skimmed. Same bounded
# store substrate; never raises into the caller.

_EVENTS_KEY = "events"
_MAX_EVENTS = 1000


def log_event(feature: str, kind: str, fields: dict | None = None) -> None:
    """Append one structured evolution event ({ts, feature, kind, **fields}).
    `fields` values should be scalars (numbers/strings/bools); anything else is
    stringified. Best-effort: a telemetry failure never breaks the caller."""
    try:
        flat = {}
        for k, v in (fields or {}).items():
            flat[str(k)[:40]] = (v if isinstance(v, (int, float, bool))
                                 else str(v)[:200])
        with _guard(), _LOCK:
            blob = store.backend().get(_NS, _EVENTS_KEY)
            events = json.loads(blob) if blob else []
            if not isinstance(events, list):
                events = []
            events.append({"ts": _now(), "feature": str(feature)[:40],
                           "kind": str(kind)[:40], **flat})
            store.backend().put(_NS, _EVENTS_KEY,
                                json.dumps(events[-_MAX_EVENTS:]).encode())
    except Exception:
        pass


def events(feature: str | None = None, limit: int = 100) -> list[dict]:
    """Recent structured events, newest last, optionally filtered by feature."""
    try:
        blob = store.backend().get(_NS, _EVENTS_KEY)
        all_events = json.loads(blob) if blob else []
    except (ValueError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(all_events, list):
        return []
    if feature:
        all_events = [e for e in all_events
                      if isinstance(e, dict) and e.get("feature") == feature]
    limit = max(1, min(int(limit), _MAX_EVENTS))
    return all_events[-limit:]


def health(feature: str | None = None) -> dict:
    """Per-feature health: sample count, success/degraded/fail rates, and the
    most recent failure detail. With no feature, returns every feature."""
    data = _load()
    feats = [feature] if feature else sorted(data)
    out = {}
    for f in feats:
        events = (data.get(f) or {}).get("events", [])
        n = len(events)
        if n == 0:
            out[f] = {"samples": 0}
            continue
        ok = sum(1 for e in events if e["o"] == OK)
        deg = sum(1 for e in events if e["o"] == DEGRADED)
        fail = sum(1 for e in events if e["o"] == FAIL)
        last_fail = next((e["d"] for e in reversed(events)
                          if e["o"] == FAIL and e["d"]), "")
        out[f] = {"samples": n,
                  "ok_rate": round(ok / n, 3),
                  "degraded_rate": round(deg / n, 3),
                  "fail_rate": round(fail / n, 3),
                  "last_failure": last_fail}
    return out


# --- current tuned values (features read these) ----------------------------

def current(feature: str, name: str) -> float:
    """The current auto-tuned value of a registered parameter (its default
    until the reviewer has adjusted it). Features call this to pick up their
    self-tuned setting; a value is always within [lo, hi]."""
    t = _TUNABLES.get(f"{feature}.{name}")
    if t is None:
        raise KeyError(f"no tunable '{feature}.{name}'")
    data = _load()
    val = ((data.get(feature) or {}).get("params", {}) or {}).get(name)
    if val is None:
        return t.default
    return max(t.lo, min(t.hi, float(val)))


def _set_param(feature: str, name: str, value: float) -> None:
    with _guard(), _LOCK:
        data = _load()
        feat = data.setdefault(feature, {"events": [], "tunes": []})
        feat.setdefault("params", {})[name] = value
        feat.setdefault("tunes", []).append({"ts": _now(), "name": name,
                                             "value": value})
        feat["tunes"] = feat["tunes"][-100:]
        _save(data)


# --- the periodic review (run by the heartbeat) ----------------------------

def review() -> str:
    """Compute feature health, auto-tune the safe registered parameters within
    their guardrails, and return a human-readable report (also the value the
    heartbeat records). Suggestions for anything NOT auto-tunable are surfaced,
    never applied. Idempotent and cheap."""
    h = health()
    lines: list[str] = []
    tuned: list[str] = []
    suggestions: list[str] = []

    for feature, stats in sorted(h.items()):
        if stats.get("samples", 0) < _MIN_SAMPLES:
            continue
        ok_rate = stats["ok_rate"]
        healthy = ok_rate >= _DEGRADE_FLOOR
        flag = "ok" if healthy else "DEGRADED"
        lines.append(
            f"  {feature}: {flag} — {int(ok_rate*100)}% ok over "
            f"{stats['samples']} runs"
            + (f" (last fail: {stats['last_failure'][:60]})"
               if stats["last_failure"] and not healthy else ""))
        if healthy:
            continue
        # Degraded: auto-tune any safe registered param for this feature,
        # else surface a suggestion (never touch a non-registered setting).
        params = [t for k, t in _TUNABLES.items() if t.feature == feature]
        if params:
            # No outer _LOCK here: _set_param acquires _guard()+_LOCK itself,
            # and threading.Lock is not reentrant — holding it around the
            # loop would self-deadlock (found by the ADR 0005 race tests).
            for t in params:
                cur = current(feature, t.name)
                step = max((t.hi - t.lo) / 6.0, 1e-9)
                nxt = cur + step if t.on_fail == "increase" else cur - step
                nxt = round(max(t.lo, min(t.hi, nxt)), 3)
                # Defense in depth for a security knob: a tighten_only param
                # may never step toward its loose end, whatever the arithmetic
                # above produced. (on_fail already points at the tight end, so
                # this only ever fires if that invariant is later broken.)
                if t.tighten_only:
                    looser = nxt < cur if t.on_fail == "increase" else nxt > cur
                    if looser:
                        continue
                if nxt != cur:
                    _set_param(feature, t.name, nxt)
                    tuned.append(f"{feature}.{t.name} {cur}→{nxt}")
        else:
            suggestions.append(
                f"{feature} is degraded ({int(ok_rate*100)}% ok) but has no "
                "auto-tunable parameter — review it manually.")

    report = ["Feature self-evolution review:"]
    report += lines or ["  (no feature has enough samples yet)"]
    if tuned:
        report.append("Auto-tuned (within guardrails): " + "; ".join(tuned))
    if suggestions:
        report.append("Suggestions: " + " ".join(suggestions))
    text = "\n".join(report)
    try:
        from . import memory
        memory.save("reports", "feature evolution", text)
    except Exception:
        pass
    return text


def reset(feature: str | None = None) -> str:
    """Restore auto-tuned parameters to their defaults — the human lever that
    *widens* a knob the reviewer has tightened (the reviewer itself only ever
    tightens a security-relevant `tighten_only` param, never loosens it). With a
    feature, resets just that feature's params; with none, resets all. Telemetry
    history is left intact. Returns a short status."""
    with _guard(), _LOCK:
        data = _load()
        cleared: list[str] = []
        for feat_name, feat in data.items():
            if feature and feat_name != feature:
                continue
            params = (feat or {}).get("params") or {}
            for name in list(params):
                cleared.append(f"{feat_name}.{name}")
            if params:
                feat["params"] = {}
                feat.setdefault("tunes", []).append(
                    {"ts": _now(), "name": "*reset*", "value": None})
                feat["tunes"] = feat["tunes"][-100:]
        if cleared:
            _save(data)
    if not cleared:
        return (f"No auto-tuned parameters to reset"
                f"{f' for {feature}' if feature else ''}.")
    return ("Reset to defaults (widened back): " + ", ".join(sorted(cleared)))


def summary() -> dict:
    """Compact health board for the admin panel / CLI."""
    h = health()
    return {"features": h,
            "tunables": {k: {"lo": t.lo, "hi": t.hi,
                             "current": current(t.feature, t.name)}
                         for k, t in _TUNABLES.items()}}


# --- convenience wrapper for instrumenting a call --------------------------

def track(feature: str, fn: Callable[[], object],
          is_ok: Callable[[object], bool] | None = None) -> object:
    """Run `fn`, record OK/FAIL around it, and return its result (or re-raise).
    `is_ok(result)` lets a caller mark a returned-but-degraded result; default
    treats any non-exception return as OK."""
    try:
        result = fn()
    except Exception as err:
        record(feature, FAIL, f"{type(err).__name__}: {err}")
        raise
    if is_ok is not None and not is_ok(result):
        record(feature, DEGRADED)
    else:
        record(feature, OK)
    return result
