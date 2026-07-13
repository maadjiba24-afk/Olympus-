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

from . import store

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


_TUNABLES: dict[str, Tunable] = {}


def register_tunable(t: Tunable) -> None:
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
        with _LOCK:
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
            with _LOCK:
                for t in params:
                    cur = current(feature, t.name)
                    step = max((t.hi - t.lo) / 6.0, 1e-9)
                    nxt = cur + step if t.on_fail == "increase" else cur - step
                    nxt = round(max(t.lo, min(t.hi, nxt)), 3)
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
