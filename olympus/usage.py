"""Cost accounting + global backpressure + the budget guard.

Every model call records token usage and an estimated dollar cost, attributed
to the active user and day. A process-wide semaphore caps concurrent model
calls so a burst of users can't trigger a rate-limit storm.

The budget guard protects the user's OWN API bill. Olympus is bring-your-own-
key: every model call bills the user's Anthropic/OpenAI account directly. A
runaway loop or a long scheduled task can quietly run that bill up. If the user
sets a daily budget, Olympus stops starting new work once today's estimated
spend reaches it — a seatbelt on their provider bill, not a charge from us.
"""

from __future__ import annotations

import atexit
import contextlib
import itertools
import json
import math
import os
import threading
import time
from collections import deque
from pathlib import Path

from . import atomicio, config, memory, proclock


class BudgetExceeded(RuntimeError):
    """Raised when today's estimated spend has reached the daily budget."""

# Approximate USD per 1M tokens (input, output). Used only for local
# estimation/visibility — never billed. Unknown models fall back to DEFAULT.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
}
DEFAULT_PRICE = (1.0, 3.0)

# Global cap on concurrent model calls across the whole process.
_SEMAPHORE = threading.BoundedSemaphore(config.MAX_CONCURRENT_CALLS)
_TOTALS_LOCK = threading.Lock()
_LEDGER_LOCK_TIMEOUT = 10.0    # reply path: bounded wait, never a hang


# Machine-global cap name. All processes sharing MEMORY_DIR (heartbeat, web,
# CLI) contend on this single semaphore, so their combined in-flight model
# calls cannot exceed MAX_CONCURRENT_CALLS (closes DEFERRED #9). On non-fcntl
# platforms the cross-process half degrades to a no-op with a one-time warning
# and only the per-process cap applies (DEFERRED #10 stays).
_GLOBAL_SLOT = "model-call"


@contextlib.contextmanager
def slot(*, cls: str = "default", key: str | None = None,
         priority: str | None = None, timeout: float | None = None,
         cancel_token=None, dims=None):
    """Acquire one of the limited model-call slots — capped both per-process
    (a local BoundedSemaphore) AND machine-globally (a proclock counting
    semaphore over MEMORY_DIR). The local semaphore is acquired first (outer)
    so a single process never holds more machine slots than its own cap, then
    the cross-process slot (inner); both release on exit.

    Every argument is keyword-only and defaults to today's behaviour, so the
    hot-path contract `with usage.slot():` (llm.py, openai_compat.py) is
    unchanged. With `OLYMPUS_ADMISSION` OFF (the default) this function is
    exactly the two lines it always was — no queue, no classes, no refusals,
    no state touched. With the flag ON, the admission POLICY below runs first
    (ruling R3: policy on top of the ONE slot primitive, never beside it):

    - `cls`      workload bucket (grouping + optional per-class cap).
    - `key`      tenant/user identity for fairness + per-tenant caps
                 (default: the active user).
    - `priority` one of PRIORITY_CLASSES (default "interactive").
    - `timeout`  max queue wait in seconds (default OLYMPUS_ADMISSION_QUEUE_
                 TIMEOUT); <= 0 means "do not wait".
    - `cancel_token` any object with `.cancelled() -> bool`; a waiter that is
                 cancelled abandons its queue position immediately.
    - `dims`     read-only labels for extra capacity dimensions, e.g.
                 `{"provider": "anthropic", "model": "claude-opus-4-8"}`.
                 READ ONLY — see W2-I7.1 below.

    Raises `AdmissionRefused` (never a downgrade) when the request cannot be
    admitted and cannot wait."""
    if not admission_enabled():
        with _SEMAPHORE:
            with proclock.slot(_GLOBAL_SLOT, config.MAX_CONCURRENT_CALLS):
                yield
        return
    waiter = _admit(cls=cls, key=key, priority=priority, timeout=timeout,
                    cancel_token=cancel_token, dims=dims)
    try:
        with _SEMAPHORE:
            with proclock.slot(_GLOBAL_SLOT, config.MAX_CONCURRENT_CALLS):
                yield
    finally:
        _release(waiter)


# --- W2-C7: unified admission policy --------------------------------------
#
# Ruling R3 (docs/absorption/00-SYNTHESIS.md): admission mechanics live in an
# EXTENDED `usage.slot` — there is no parallel admission system and no new
# module. Everything below is POLICY layered in front of the single primitive
# above; the local BoundedSemaphore and the machine-global `proclock.slot`
# still do the actual capacity work, unchanged and unconditional.
#
# W2-I7.1 — NO SILENT QUALITY DOWNGRADE. Admission decides WHEN a request
# runs, never WHAT runs. There is deliberately no `model`, `effort`, `route`
# or `mode` parameter anywhere in this API, and no statement in this section
# assigns one: there is no code path by which admission can substitute a
# cheaper model or a faster mode in order to make a request fit. When capacity
# is unavailable the request QUEUES; when it cannot queue it is REFUSED with a
# typed `AdmissionRefused` (ruling R6: refusal over silent degradation). The
# `dims` mapping is an input label set — read to choose which counters a
# request is measured against, never written back to the caller.
#
# W2-I7.2 — FAIRNESS. Under saturation the wait queue is drained round-robin
# over distinct `key`s (least-recently-admitted tenant first, arrival order as
# the tie-break), so one user cannot starve another.
#
# W2-I7.3 — RESERVED CAPACITY. `OLYMPUS_ADMISSION_RESERVE` slots are reserved
# for priority class "critical" (verification / refusal paths): best-effort
# traffic can hold at most `MAX_CONCURRENT_CALLS - reserve` slots, so the
# reserve is never consumable by it.

#: Priority classes, most important first.
PRIORITY_CLASSES: tuple[str, ...] = ("critical", "interactive", "background")
DEFAULT_PRIORITY = "interactive"
_PRIORITY_RANK = {name: i for i, name in enumerate(PRIORITY_CLASSES)}

_ADMISSION_POLL = 0.02          # cancel-token responsiveness while queued
_SPEND_WINDOW_S = 60.0          # sliding window for the denial-of-wallet rate


class AdmissionRefused(RuntimeError):
    """A request could not be admitted — a TYPED refusal carrying machine-
    readable retry guidance.

    Never a downgrade: `as_dict()["degraded"]` is always False, because
    admission has no authority to change what a request asks for (W2-I7.1).
    `reason` is one of: budget_exceeded, spend_rate_exceeded, queue_full,
    queue_timeout, no_capacity, cancelled."""

    def __init__(self, reason: str, retry_after_s: float = 0.0,
                 detail: str = "") -> None:
        self.reason = str(reason)
        try:
            self.retry_after_s = round(max(0.0, float(retry_after_s or 0.0)), 3)
        except (TypeError, ValueError):
            self.retry_after_s = 0.0
        self.detail = str(detail or "")
        super().__init__(self.detail or f"admission refused ({self.reason})")

    def as_dict(self) -> dict:
        """The gateway/HTTP body: machine-readable reason + retry guidance."""
        return {
            "error": "admission_refused",
            "reason": self.reason,
            "retry_after_s": self.retry_after_s,
            "detail": self.detail,
            "message": str(self),
            "degraded": False,          # W2-I7.1: nothing was downgraded
        }


def admission_http_response(exc: AdmissionRefused) -> tuple[int, dict, dict]:
    """(status, body, headers) for an HTTP transport: 429 + `Retry-After`.

    Kept here (not in web.py) so every channel renders one overload shape."""
    seconds = max(0, int(math.ceil(exc.retry_after_s)))
    return (429, exc.as_dict(), {
        "Retry-After": str(seconds),
        "X-Olympus-Admission": exc.reason,
    })


# --- knobs (read live; 0/unset = unlimited, flag default OFF) --------------

def _env_str(name: str, fallback: str = "") -> str:
    val = os.environ.get(name)
    if val is None or not str(val).strip():
        val = os.environ.get(fallback) if fallback else None
    return "" if val is None else str(val).strip()


def _env_int(name: str, default: int = 0, fallback: str = "") -> int:
    raw = _env_str(name, fallback)
    if not raw:
        return default
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float = 0.0, fallback: str = "") -> float:
    raw = _env_str(name, fallback)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def admission_enabled() -> bool:
    """Whether the unified admission policy runs at all (default OFF). With it
    off, `slot()` is byte-for-byte the pre-C7 primitive."""
    return os.environ.get("OLYMPUS_ADMISSION", "").strip().lower() in (
        "1", "on", "true", "yes")


def admission_provider_max() -> int:
    """Max concurrent calls per provider (0 = unlimited)."""
    return _env_int("OLYMPUS_ADMISSION_PROVIDER_MAX", 0)


def admission_model_max() -> int:
    """Max concurrent calls per model (0 = unlimited)."""
    return _env_int("OLYMPUS_ADMISSION_MODEL_MAX", 0)


def admission_tenant_max() -> int:
    """Max concurrent calls per tenant/user key (0 = unlimited)."""
    return _env_int("OLYMPUS_ADMISSION_TENANT_MAX", 0)


def admission_class_max() -> int:
    """Max concurrent calls per workload class `cls` (0 = unlimited)."""
    return _env_int("OLYMPUS_ADMISSION_CLASS_MAX", 0)


def admission_reserve() -> int:
    """Slots reserved for priority class "critical" (default 1). Accepts the
    ruling-R3 spelling OLYMPUS_SLOT_RESERVE as a fallback. Clamped at call
    time to at most `MAX_CONCURRENT_CALLS - 1` so a misconfiguration can never
    wedge best-effort traffic to zero capacity (which would queue forever)."""
    return _env_int("OLYMPUS_ADMISSION_RESERVE", 1, "OLYMPUS_SLOT_RESERVE")


def admission_max_queue() -> int:
    """Max waiters in the admission queue (0 = unbounded). Accepts the
    ruling-R3 spelling OLYMPUS_MAX_QUEUE as a fallback."""
    return _env_int("OLYMPUS_ADMISSION_MAX_QUEUE", 64, "OLYMPUS_MAX_QUEUE")


def admission_queue_timeout() -> float:
    """Default max queue wait, seconds. One default across the platform (300 s,
    ruling R3); accepts the R3 spelling OLYMPUS_QUEUE_TIMEOUT as a fallback."""
    return _env_float("OLYMPUS_ADMISSION_QUEUE_TIMEOUT", 300.0,
                      "OLYMPUS_QUEUE_TIMEOUT")


def admission_spend_rate_max() -> float:
    """Denial-of-wallet guard: max USD/minute one key may sustain (0 = off)."""
    return _env_float("OLYMPUS_ADMISSION_SPEND_RATE_MAX", 0.0)


# --- admission state (per process; the machine-global cap stays proclock's) -

class _Waiter:
    """One request's place in the admission queue."""

    __slots__ = ("cls", "key", "priority", "provider_label", "model_label",
                 "seq",
                 "admitted", "queued_at", "wait_s")

    def __init__(self, cls: str, key: str, priority: str,
                 provider_label: str, model_label: str, seq: int) -> None:
        self.cls = cls
        self.key = key
        self.priority = priority
        # Labels the CALLER declared, used only to pick which counters this
        # request is measured against. Admission never chooses them (W2-I7.1),
        # hence the `_label` suffix: they are evidence, not a decision.
        self.provider_label = provider_label
        self.model_label = model_label
        self.seq = seq
        self.admitted = False
        self.queued_at = time.monotonic()
        self.wait_s = 0.0


def _new_priority_row() -> dict:
    return {"in_flight": 0, "queued": 0, "admitted": 0, "refused": 0,
            "timed_out": 0, "cancelled": 0}


_COND = threading.Condition()
_adm_queue: list[_Waiter] = []
_adm_seq = itertools.count(1)
_adm_ticket = 0                       # monotonic admission ticket (fairness)
_adm_in_flight = 0
_adm_last_admit: dict[str, int] = {}  # key -> ticket of its last admission
_adm_by_provider: dict[str, int] = {}
_adm_by_model: dict[str, int] = {}
_adm_by_key: dict[str, int] = {}
_adm_by_cls: dict[str, int] = {}
_adm_by_priority: dict[str, dict] = {p: _new_priority_row()
                                     for p in PRIORITY_CLASSES}
_adm_counters: dict[str, float] = {
    "admitted": 0, "refused": 0, "timed_out": 0, "cancelled": 0,
    "queue_full": 0, "budget_refused": 0, "spend_rate_refused": 0,
    "no_capacity": 0, "starvation_prevented": 0, "wait_s_total": 0.0,
}
_SPEND_LOCK = threading.Lock()
_SPEND_SAMPLES: dict[str, deque] = {}


def _bump(table: dict, name: str, delta: int) -> None:
    val = table.get(name, 0) + delta
    if val <= 0:
        table.pop(name, None)
    else:
        table[name] = val


def _label(value) -> str:
    return str(value).strip()[:80] if value else ""


def _resolve_key(key: str | None) -> str:
    """The fairness / per-tenant identity. Defaults to the active user, which
    is the tenant identity the rest of the tree already uses."""
    if key:
        return _label(key)
    try:
        return memory.safe_id(memory.current_user())
    except Exception:                            # pragma: no cover - defensive
        return "default"


def _fits_locked(w: _Waiter) -> bool:
    """Can this waiter be admitted RIGHT NOW under every capacity dimension?"""
    total = max(1, int(config.MAX_CONCURRENT_CALLS))
    if _adm_in_flight >= total:
        return False
    if w.priority != "critical":
        # W2-I7.3: best-effort traffic tops out below the reserve, so the
        # reserved slots are never consumable by it.
        reserve = min(max(0, admission_reserve()), total - 1)
        best_effort = sum(_adm_by_priority[p]["in_flight"]
                          for p in PRIORITY_CLASSES if p != "critical")
        if best_effort >= total - reserve:
            return False
    for table, name, cap in (
            (_adm_by_provider, w.provider_label, admission_provider_max()),
            (_adm_by_model, w.model_label, admission_model_max()),
            (_adm_by_key, w.key, admission_tenant_max()),
            (_adm_by_cls, w.cls, admission_class_max())):
        if cap > 0 and name and table.get(name, 0) >= cap:
            return False
    return True


def _rank_locked(w: _Waiter) -> tuple:
    """Selection order: priority class first, then the least-recently-admitted
    tenant (round-robin fairness, W2-I7.2), then arrival order."""
    return (_PRIORITY_RANK.get(w.priority, len(PRIORITY_CLASSES)),
            _adm_last_admit.get(w.key, 0),
            w.seq)


def _promote_locked() -> None:
    """Admit as many queued waiters as current capacity allows, in policy
    order. Called under _COND on every arrival and every release; the winner
    is chosen and its capacity charged HERE, so the drain order is decided by
    policy alone and not by thread wake-up races."""
    global _adm_in_flight, _adm_ticket
    promoted = 0
    while _adm_queue:
        eligible = [w for w in _adm_queue if _fits_locked(w)]
        if not eligible:
            break
        best = min(eligible, key=_rank_locked)
        if best is not eligible[0] and best.priority == eligible[0].priority:
            # Fairness overrode arrival order: an older waiter from a
            # recently-served tenant yielded its turn (W2-I7.2).
            _adm_counters["starvation_prevented"] += 1
        _adm_queue.remove(best)
        _adm_by_priority[best.priority]["queued"] -= 1
        _adm_in_flight += 1
        _adm_by_priority[best.priority]["in_flight"] += 1
        _adm_by_priority[best.priority]["admitted"] += 1
        _bump(_adm_by_provider, best.provider_label, 1)
        _bump(_adm_by_model, best.model_label, 1)
        _bump(_adm_by_key, best.key, 1)
        _bump(_adm_by_cls, best.cls, 1)
        _adm_ticket += 1
        _adm_last_admit[best.key] = _adm_ticket
        best.wait_s = max(0.0, time.monotonic() - best.queued_at)
        best.admitted = True
        _adm_counters["admitted"] += 1
        _adm_counters["wait_s_total"] += best.wait_s
        promoted += 1
    if promoted:
        _COND.notify_all()


def _drop_locked(w: _Waiter) -> None:
    """Remove a waiter that gave up (timeout / cancellation), freeing its
    queue position for everyone else."""
    if w in _adm_queue:
        _adm_queue.remove(w)
        _adm_by_priority[w.priority]["queued"] -= 1


def _cancelled(token) -> bool:
    if token is None:
        return False
    try:
        return bool(token.cancelled())
    except Exception:                            # pragma: no cover - defensive
        return False


def _seconds_to_local_midnight() -> float:
    now = time.localtime()
    left = 86400 - (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec)
    return float(max(60, min(86400, left)))


def _capacity_retry_after() -> float:
    """Retry guidance for capacity refusals: a tenth of the queue timeout,
    clamped to a sane 1–30 s so clients neither hammer nor sleep for minutes."""
    return round(min(30.0, max(1.0, admission_queue_timeout() / 10.0)), 3)


def spend_rate(key: str, *, window_s: float = _SPEND_WINDOW_S) -> float:
    """This key's recent spend in USD/minute, derived from the session totals
    already recorded by `record()` — no new measurement, no new store.

    Sampled at admission time into a small sliding window per key. The window
    floor is one second, so a burst measured over a few milliseconds cannot
    report an absurd rate. Keys that are not user ids have no recorded spend
    and therefore always read 0.0 (they cannot trip the guard)."""
    now = time.monotonic()
    try:
        cost = float(session_totals(key).get("cost", 0.0))
    except Exception:                            # pragma: no cover - defensive
        return 0.0
    with _SPEND_LOCK:
        samples = _SPEND_SAMPLES.setdefault(key, deque(maxlen=64))
        while len(samples) > 1 and now - samples[0][0] > window_s:
            samples.popleft()
        rate = 0.0
        if samples:
            t0, c0 = samples[0]
            elapsed = max(now - t0, 1.0)
            rate = max(0.0, (cost - c0) / (elapsed / 60.0))
        samples.append((now, cost))
    return rate


def _refuse(reason: str, retry_after_s: float, detail: str,
            priority: str | None = None, counter: str | None = None
            ) -> AdmissionRefused:
    """Count and build a typed refusal (never a downgrade — W2-I7.1)."""
    with _COND:
        _adm_counters["refused"] += 1
        if counter:
            _adm_counters[counter] = _adm_counters.get(counter, 0) + 1
        if priority in _adm_by_priority:
            _adm_by_priority[priority]["refused"] += 1
            if reason == "queue_timeout":
                _adm_by_priority[priority]["timed_out"] += 1
            elif reason == "cancelled":
                _adm_by_priority[priority]["cancelled"] += 1
    return AdmissionRefused(reason, retry_after_s, detail)


def _admit(*, cls: str, key: str | None, priority: str | None,
           timeout: float | None, cancel_token, dims) -> _Waiter:
    """The admission decision. Returns the admitted waiter or raises
    `AdmissionRefused`. Never inspects or alters what the request asks for."""
    priority = (priority or DEFAULT_PRIORITY).strip().lower()
    if priority not in _PRIORITY_RANK:
        raise ValueError(
            f"unknown priority {priority!r}; expected one of "
            f"{', '.join(PRIORITY_CLASSES)}")
    cls = _label(cls) or "default"
    key = _resolve_key(key)
    # `dims` is READ here and nowhere else — never written back, never used to
    # pick a model or a mode (W2-I7.1).
    provider_label = model_label = ""
    if dims:
        try:
            provider_label = _label(dims.get("provider"))
            model_label = _label(dims.get("model"))
        except AttributeError:
            provider_label = model_label = ""

    # --- denial-of-wallet: refuse, never quietly downgrade (ruling R6) ------
    try:
        check_budget()
    except BudgetExceeded as err:
        raise _refuse("budget_exceeded", _seconds_to_local_midnight(),
                      str(err), priority, "budget_refused") from err
    rate_max = admission_spend_rate_max()
    if rate_max > 0:
        rate = spend_rate(key)
        if rate > rate_max:
            raise _refuse(
                "spend_rate_exceeded", _SPEND_WINDOW_S,
                f"spend rate for '{key}' is about ${rate:.2f}/min, over the "
                f"${rate_max:.2f}/min admission ceiling "
                f"(OLYMPUS_ADMISSION_SPEND_RATE_MAX). Nothing was downgraded "
                f"to fit — retry when the rate falls.",
                priority, "spend_rate_refused")

    wait_s = admission_queue_timeout() if timeout is None else float(timeout)
    max_queue = admission_max_queue()
    if _cancelled(cancel_token):
        raise _refuse("cancelled", 0.0, "cancelled before admission",
                      priority, "cancelled")

    with _COND:
        w = _Waiter(cls, key, priority, provider_label, model_label,
                    next(_adm_seq))
        _adm_queue.append(w)
        _adm_by_priority[priority]["queued"] += 1
        _promote_locked()
        if not w.admitted:
            if wait_s <= 0:
                _drop_locked(w)
                raise _refuse(
                    "no_capacity", _capacity_retry_after(),
                    "no free slot and this caller asked not to wait "
                    "(timeout <= 0).", priority, "no_capacity")
            if max_queue > 0 and len(_adm_queue) > max_queue:
                _drop_locked(w)
                raise _refuse(
                    "queue_full", _capacity_retry_after(),
                    f"admission queue is full ({max_queue} waiting). The "
                    f"request was refused, not downgraded — retry after the "
                    f"interval below.", priority, "queue_full")
        deadline = time.monotonic() + wait_s
        while not w.admitted:
            if _cancelled(cancel_token):
                _drop_locked(w)
                raise _refuse("cancelled", 0.0,
                              "waiter cancelled; queue position released",
                              priority, "cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _drop_locked(w)
                raise _refuse(
                    "queue_timeout", _capacity_retry_after(),
                    f"waited {wait_s:.2f}s for a slot without one freeing.",
                    priority, "timed_out")
            _COND.wait(min(remaining, _ADMISSION_POLL))
        return w


def _release(w: _Waiter) -> None:
    """Give the slot back and re-run the policy over the wait queue."""
    global _adm_in_flight
    with _COND:
        _adm_in_flight = max(0, _adm_in_flight - 1)
        row = _adm_by_priority[w.priority]
        row["in_flight"] = max(0, row["in_flight"] - 1)
        _bump(_adm_by_provider, w.provider_label, -1)
        _bump(_adm_by_model, w.model_label, -1)
        _bump(_adm_by_key, w.key, -1)
        _bump(_adm_by_cls, w.cls, -1)
        if len(_adm_last_admit) > 1024:
            live = set(_adm_by_key) | {q.key for q in _adm_queue}
            for stale in [k for k in _adm_last_admit if k not in live]:
                _adm_last_admit.pop(stale, None)
        _promote_locked()
        _COND.notify_all()


def admission_status() -> dict:
    """Observability snapshot — a PURE read (copies only; feeds C9 liveness /
    doctor). Safe to call from any thread at any time."""
    with _COND:
        admitted = _adm_counters["admitted"]
        wait_total = _adm_counters["wait_s_total"]
        return {
            "enabled": admission_enabled(),
            "flag": "OLYMPUS_ADMISSION",
            "in_flight": _adm_in_flight,
            "queued": len(_adm_queue),
            "limits": {
                "global": max(1, int(config.MAX_CONCURRENT_CALLS)),
                "provider": admission_provider_max(),
                "model": admission_model_max(),
                "tenant": admission_tenant_max(),
                "cls": admission_class_max(),
                "reserve": admission_reserve(),
                "max_queue": admission_max_queue(),
                "queue_timeout_s": admission_queue_timeout(),
                "spend_rate_max_usd_min": admission_spend_rate_max(),
            },
            "priority": {p: dict(row)
                         for p, row in _adm_by_priority.items()},
            "by_provider": dict(_adm_by_provider),
            "by_model": dict(_adm_by_model),
            "by_tenant": dict(_adm_by_key),
            "by_cls": dict(_adm_by_cls),
            "counters": {k: (round(v, 4) if isinstance(v, float) else v)
                         for k, v in _adm_counters.items()},
            "queue_wait_ms_avg": round(
                (wait_total / admitted) * 1000.0, 3) if admitted else 0.0,
            # W2-I7.1, machine-readable: admission never downgrades.
            "downgrades": 0,
        }


def admission_reset() -> None:
    """Clear admission counters and per-key fairness memory. For tests and for
    an operator resetting statistics — the runtime never calls it."""
    global _adm_in_flight, _adm_ticket
    with _COND:
        _adm_queue.clear()
        _adm_in_flight = 0
        _adm_ticket = 0
        _adm_last_admit.clear()
        for table in (_adm_by_provider, _adm_by_model, _adm_by_key,
                      _adm_by_cls):
            table.clear()
        for p in PRIORITY_CLASSES:
            _adm_by_priority[p] = _new_priority_row()
        for name in list(_adm_counters):
            _adm_counters[name] = 0.0 if name == "wait_s_total" else 0
        _COND.notify_all()
    with _SPEND_LOCK:
        _SPEND_SAMPLES.clear()


# Cache-token price multipliers, applied to the model's INPUT price. The
# defaults mirror published provider pricing (reads ~10% of input, writes
# ~125%); both are runtime-flippable knobs, and deliberately NOT tied to any
# cache-lifetime constant — pricing here is a multiplier, nothing else.

def cache_read_mult() -> float:
    """Input-price multiplier for cache-READ tokens (OLYMPUS_CACHE_READ_MULT,
    default 0.1)."""
    try:
        return max(0.0, float(os.environ.get("OLYMPUS_CACHE_READ_MULT", "0.1")))
    except (TypeError, ValueError):
        return 0.1


def cache_write_mult() -> float:
    """Input-price multiplier for cache-CREATION tokens
    (OLYMPUS_CACHE_WRITE_MULT, default 1.25)."""
    try:
        return max(0.0, float(os.environ.get("OLYMPUS_CACHE_WRITE_MULT",
                                             "1.25")))
    except (TypeError, ValueError):
        return 1.25


def estimate_cost(model: str, in_tokens: int, out_tokens: int, *,
                  cache_read: int = 0, cache_creation: int = 0) -> float:
    """Estimated USD for one call. `in_tokens` is the UNCACHED input count at
    cache-aware call sites (legacy positional callers pass totals with zero
    cache fields — identical arithmetic to before). Cache reads/creations are
    priced off the input price via the multiplier knobs above."""
    price_in, price_out = PRICES.get(model, DEFAULT_PRICE)
    total = in_tokens * price_in + out_tokens * price_out
    if cache_read:
        total += cache_read * price_in * cache_read_mult()
    if cache_creation:
        total += cache_creation * price_in * cache_write_mult()
    return total / 1_000_000


def _journal_path(path: Path) -> Path:
    """The append journal beside a day's snapshot: usage/<day>.jsonl."""
    return path.with_suffix(".jsonl")


def _open_append(path):
    return open(path, "ab")             # seam for fault injection in tests


def _append_usage(path: Path, event: dict) -> None:
    """Append ONE usage event and sync it. Copied from
    `sessionlog._append_records`: one write() per record ending in "\\n",
    flush(), then os.fsync(fd).

    This is the whole point of W2-2. `record()` used to read-modify-write the
    ENTIRE day ledger under a cross-process lock, once per model call: O(ledger)
    work per call, which is why W1-1a had to turn fsync off (the sync cost ~15 ms
    inside that lock and serialized to ~300 ms across a council turn). An append
    touches ONE block, so durability becomes affordable and fsync comes back on.

    The line-oriented append is also what makes a torn tail survivable: only the
    LAST record can be partial, `_replay_journal` discards it, and every record
    before it is intact. That is stronger than the old whole-file rewrite, where
    an interrupted publish could leave the day's total at zero.
    """
    journal = _journal_path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    # Q5: fsyncing a FILE does not make its DIRECTORY ENTRY durable, so an
    # append that CREATES the journal needs a parent-directory sync or a crash
    # can lose the whole journal even though its bytes were flushed. Appends do
    # NOT go through atomicio.publish() -- that is the replace path -- so
    # nothing else covers this.
    #
    # ON EVERY CREATE, NOT ONCE A DAY. This comment used to say "the day's first
    # append", which is false: `created` is evaluated per append and `compact()`
    # UNLINKS the journal (see its `journal.unlink()`), so the barrier fires on
    # the first append after EVERY compaction -- roughly 1-in-450 records at the
    # default threshold, not 1-in-a-day. The cost is unchanged and still
    # negligible; the claim was simply wrong, in a comment explaining a
    # durability barrier, which is where a wrong claim does the most damage.
    #
    # What matters is that it is NOT PER APPEND: paying it on every call would
    # reintroduce exactly the per-call barrier this design removed.
    created = not journal.exists()
    line = (json.dumps(event, separators=(",", ":"), sort_keys=True)
            + "\n").encode("utf-8")
    f = _open_append(journal)
    try:
        f.write(line)
        if _should_sync_now():
            f.flush()
            os.fsync(f.fileno())
    finally:
        f.close()
    if created:
        atomicio.fsync_dir(journal.parent)


#: Wall-clock of the last ledger fsync, and the mutex guarding it. Module-level
#: because the interval is PER PROCESS -- see `_should_sync_now`.
_LAST_FSYNC = [0.0]
_FSYNC_CLOCK_LOCK = threading.Lock()


#: Compact the journal once it passes this many BYTES. Size, never a line
#: count: counting lines is O(N) and would reintroduce exactly the unbounded
#: per-call read this threshold exists to bound.
#:
#: DERIVED, not chosen. MEASURED ON A LOCAL NTFS SSD, 2026-08-17 -- these are
#: properties of that disk, NOT of this code. W1-1a exists because a local-SSD
#: figure was used to justify a default without saying it was local, and the CI
#: runner turned out ~6x slower (bounded at <7.6x in W1-5).
#:
#: MEASURED (local NTFS SSD): 146 bytes/record, so 64 KiB is ~450 records --
#:
#:   today_spend() at 450 records   ~2.0 ms   (ceiling 2.5 ms -- ~0.1% of the
#:                                             ~2 s provider call it guards,
#:                                             and <1/10 of the 25 ms
#:                                             observability limit, so it
#:                                             cannot push that contract over
#:                                             on its own)
#:   compaction at 450 records      5.23 ms   -> 11.6 us/record amortized
#:
#: REASONED, not measured -- how these should travel to slower storage:
#:
#:   * COMPACTION contains an fsync, so it is the figure that moves most. At a
#:     ~6x runner ratio that is ~30 ms, i.e. ~60-70 us/record amortized: still
#:     small against the ~1.66 ms an append already costs, and still 1-in-450.
#:   * TODAY_SPEND() is a page-cache READ with no barrier, so it should travel
#:     far better than compaction does -- but this is reasoning about where the
#:     cost comes from, not a measurement on that hardware.
#:
#: An operator who has measured their own storage sets
#: OLYMPUS_USAGE_COMPACT_BYTES rather than inheriting these numbers.
#:
#: The two constraints do not BOTH hold cleanly on this storage: compaction has
#: a ~2.5 ms fixed cost (flock + fsync), so amortized cost only falls under
#: 10 us/record at ~900 records -- by which point today_spend() is 4.4 ms, well
#: over its ceiling. Resolved in favour of the ceiling, because today_spend()
#: is on the PER-MODEL-CALL budget-guard path while compaction is 1-in-450; and
#: 11.6 us/record is 0.7% of the ~1.66 ms an append already costs, so the
#: overshoot is invisible next to the work it accompanies.
_COMPACT_AT_BYTES = 64 * 1024


def compact_at_bytes() -> int:
    """OLYMPUS_USAGE_COMPACT_BYTES, default 64 KiB."""
    raw = os.environ.get("OLYMPUS_USAGE_COMPACT_BYTES", "").strip()
    if not raw:
        return _COMPACT_AT_BYTES
    try:
        return max(1024, int(raw))
    except ValueError:
        return _COMPACT_AT_BYTES


def fsync_interval_ms() -> float:
    """OLYMPUS_USAGE_FSYNC_INTERVAL_MS, default 1000 (one second)."""
    raw = os.environ.get("OLYMPUS_USAGE_FSYNC_INTERVAL_MS", "").strip()
    if not raw:
        return 1000.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1000.0


def _should_sync_now(*, now: float | None = None) -> bool:
    """Whether THIS append should pay for a device flush.

    GROUP COMMIT BY TIME. `sessionlog._append_records` amortises a sync over a
    BATCH of records, which it can because it is handed many at once. `record()`
    is called singly, once per model call, so there is no batch to close --
    the equivalent is to amortise over TIME instead: at most one fsync per
    `fsync_interval_ms`, however many calls arrive inside it.

    `always` and `auto` remain as explicit overrides for either extreme."""
    mode = _fsync_ledger()
    if mode == "auto":
        return False
    if mode == "always":
        return True
    interval = fsync_interval_ms() / 1000.0
    if interval <= 0:
        return True                       # a zero interval IS per-call
    stamp = time.time() if now is None else now
    with _FSYNC_CLOCK_LOCK:
        if stamp - _LAST_FSYNC[0] >= interval:
            _LAST_FSYNC[0] = stamp
            return True
    return False


#: One report per process for a failing exit flush. `flush()` is called from an
#: `atexit` handler AND from the heartbeat's SIGTERM handler, so an unguarded
#: report is a flood at exactly the moment nobody is reading. Same shape as
#: `_FSYNC_MODE_WARNED`.
_FLUSH_FAILURE_WARNED = False


def _report_flush_failure(exc: BaseException) -> None:
    """Report a failed exit flush ONCE per process, and never raise doing it.

    NEVER RAISE, NEVER SILENT. `flush()`'s bare `except Exception: return False`
    was correct for atexit safety -- a traceback on every process exit is not an
    acceptable failure mode -- and it is ALSO what hid the O_RDONLY defect for a
    whole commit: on Windows every exit flush failed, returned False, and said
    nothing. The bare except stays; the swallow is now instrumented.

    ITS OWN try/except, because this runs at interpreter shutdown where the
    IMPORT itself can fail: atexit handlers run after module globals may already
    have been torn down, so `from . import errors` is not guaranteed to succeed.
    A failure to report a failure must still not raise.
    """
    global _FLUSH_FAILURE_WARNED
    if _FLUSH_FAILURE_WARNED:
        return
    # Set BEFORE reporting, like `_FSYNC_MODE_WARNED`: if `capture` itself is
    # what fails, the flag must still have moved or a retry loop re-enters here.
    _FLUSH_FAILURE_WARNED = True
    try:
        from . import errors
        errors.capture(
            "usage.flush", exc,
            context="the ledger journal could not be synced at shutdown, so "
                    "records written since the last interval sync are "
                    "unflushed and an unclean stop loses them. Reported once "
                    "per process.")
    except Exception:                    # noqa: BLE001 - atexit must not throw
        pass


def flush() -> bool:
    """Force the ledger journal to stable storage NOW.

    Called on clean shutdown so the final records of a run are not the ones the
    interval loses. W1-4 gave the heartbeat a real SIGTERM handler, so there is
    now somewhere to hang this; `heartbeat._install_shutdown` calls it.

    AN UNCLEAN SHUTDOWN IS NOT COVERED, and this must not be read as if it
    were: SIGKILL, a power cut or a panic take whatever is still unflushed.
    That residue is bounded by the interval -- see `_fsync_ledger`.

    NEVER RAISES, NEVER SILENT (F2). This runs from an `atexit` handler, where a
    traceback would print on every process exit, and where module globals may
    already be torn down -- so it cannot raise. But a swallowed failure here is
    a durability guarantee quietly not happening, so a failure is reported once
    through `errors.capture`; see `_report_flush_failure`. The journal can also
    be GONE by now -- a peer compaction unlinks it -- so a missing file is a
    normal outcome and is NOT reported. It reopens the journal because the
    append path opens and closes per append: there is no long-lived descriptor
    to sync.
    """
    try:
        day = time.strftime("%Y-%m-%d")
        journal = _journal_path(config.MEMORY_DIR / "usage" / f"{day}.json")
        if not journal.exists():
            return False
        # O_WRONLY, NOT O_RDONLY. `os.fsync` is `FlushFileBuffers` on Windows,
        # and FlushFileBuffers requires GENERIC_WRITE on the handle -- so a
        # read-only descriptor fails with EBADF and this entire function was
        # INERT on Windows for the whole of 3d15637, returning False on every
        # exit while the commit message claimed the hook was proven. O_APPEND on
        # top of it because the only legal write to a journal is at the end: a
        # stray write through this descriptor cannot land mid-file and cannot
        # truncate. Minimum privilege that still satisfies the syscall, and the
        # same flags `_open_append`'s "ab" opens with.
        fd = os.open(str(journal), os.O_WRONLY | os.O_APPEND)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        with _FSYNC_CLOCK_LOCK:
            _LAST_FSYNC[0] = time.time()
        return True
    except FileNotFoundError:
        # Unlinked by a peer compaction between `exists()` and `open()`. The
        # same normal outcome as the `exists()` check above, so it is not a
        # failure to report.
        return False
    except Exception as exc:             # noqa: BLE001 - atexit must not throw
        _report_flush_failure(exc)
        return False


# Q4: EVERY process, not just the heartbeat. `_LAST_FSYNC` is per-process, so a
# SHORT-LIVED one is the gap the interval creates: a CLI invocation that records
# a model call and exits inside the interval loses it with no crash at all -- a
# clean exit that drops spend. Registering here, where the timer lives, covers
# every entry point uniformly and is cheaper than auditing each one (the web
# server's shutdown path and `cli.main` would each have needed their own hook).
#
# atexit does NOT run on SIGKILL, a power cut or os._exit. The bound there is
# still one interval, which is the designed behaviour rather than a gap.
atexit.register(flush)


def _replay_journal(path: Path) -> list[dict]:
    """Every intact event in a day's journal, in order.

    A TORN TAIL IS DISCARDED AND EVERY PRIOR RECORD SURVIVES. Records are
    appended whole and newline-terminated, so a crash mid-write can only damage
    the final line; anything that does not parse, or that arrives without its
    terminating newline, is dropped and the rest stands. Silently tolerating a
    torn record ANYWHERE else would hide corruption, so only the last line is
    forgiven.
    """
    journal = _journal_path(path)
    try:
        raw = journal.read_bytes()
    except OSError:
        return []
    if not raw:
        return []
    lines = raw.split(b"\n")
    # A complete file ends with "\n", so the final split element is empty. If it
    # is NOT empty the last record was never finished being written.
    if lines and lines[-1] == b"":
        lines.pop()
    else:
        lines = lines[:-1]              # drop the torn tail
    out: list[dict] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if index == len(lines) - 1:
                continue                # torn final record: forgiven
            raise                       # mid-file corruption is NOT forgiven
        if isinstance(event, dict):
            out.append(event)
    return out


def _fold(ledger: dict, event: dict) -> None:
    """Fold one journal event into an aggregate ledger, in place.

    THE SINGLE AGGREGATION. `record` writes events and this reconstructs the
    same row shape the snapshot has always had, so the on-disk snapshot format
    is unchanged and every historical reader still understands it.
    """
    user = str(event.get("user", ""))
    model = str(event.get("model", ""))
    in_tokens = int(event.get("in", 0) or 0)
    out_tokens = int(event.get("out", 0) or 0)
    cache_read = int(event.get("cache_read", 0) or 0)
    cache_creation = int(event.get("cache_creation", 0) or 0)
    cost = float(event.get("cost", 0.0) or 0.0)
    for key in ("__all__", f"user:{user}", f"model:{model}"):
        row = ledger.setdefault(
            key, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
        row["calls"] = row.get("calls", 0) + 1
        row["in"] = row.get("in", 0) + in_tokens
        row["out"] = row.get("out", 0) + out_tokens
        row["cache_read"] = row.get("cache_read", 0) + cache_read
        row["cache_creation"] = row.get("cache_creation", 0) + cache_creation
        row["cost"] = round(row.get("cost", 0.0) + cost, 6)
    fp = str(event.get("prefix_fp", "") or "")
    if fp:
        fp_row = ledger.setdefault("prefix", {}).setdefault(
            fp, {"calls": 0, "hits": 0, "cache_read": 0})
        fp_row["calls"] = fp_row.get("calls", 0) + 1
        if cache_read > 0:
            fp_row["hits"] = fp_row.get("hits", 0) + 1
        fp_row["cache_read"] = fp_row.get("cache_read", 0) + cache_read


def ledger(day: str | None = None) -> dict:
    """THE day's totals: the compacted snapshot PLUS the un-compacted journal.

    Every reader must come through here. The snapshot alone is stale between
    compactions, and reading it directly would UNDER-REPORT spend -- the
    direction that costs money, because `today_spend` feeds `daily_budget`,
    which is what stops a runaway. `adminpanel`, `codegraph_gate` and `doctor`
    each used to parse the day file themselves and now call this instead.

    MIGRATION ACROSS THE SEAM. A day file written by the pre-W2-2 code is
    already in snapshot shape, so it is the base and the journal is layered on
    top. A running instance that upgrades mid-day therefore keeps its morning's
    spend and adds the afternoon's, with no special case: old format + new
    events = correct total.
    """
    day = day or time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    out: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            out = loaded
    except (json.JSONDecodeError, OSError, ValueError):
        out = {}
    for event in _replay_journal(path):
        _fold(out, event)
    return out


def compact(day: str | None = None) -> dict:
    """Fold a day's journal into its snapshot and truncate the journal.

    Runs from the heartbeat's daily maintenance job (W1-2 built that home) and
    from `record()` when the journal passes its size threshold. The total is
    IDENTICAL before and after: compaction moves where the numbers live, never
    what they are.

    SAFE AGAINST LIVE WRITERS ONLY WHERE proclock ACTUALLY EXCLUDES. The
    read -> publish -> unlink sequence below runs under
    `proclock.lock("usage-ledger")`, the same lock `record()` takes around its
    append, so on POSIX the two serialize and a concurrent append cannot be
    dropped.

    ON WINDOWS THAT IS NOT TRUE. `proclock` is fcntl-based and degrades to an
    in-process `threading.Lock` when fcntl is unavailable (proclock.py:96,
    "Windows: thread-safe fallback"), so it excludes THREADS but not PROCESSES.
    With two Olympus processes on Windows, a record appended between the
    `_replay_journal` read and the `unlink` below lands in a journal that is
    then deleted: SPEND LOST, silently, on a budget guard.

    Why this is nonetheless shipped as-is:

      * The supported Windows configuration is SINGLE-PROCESS -- ADR 0005
        documents the heartbeat-vs-web split as unsupported there, and
        `tests/test_proclock_races.py` skips its cross-process cases on
        Windows for the same reason. In one process the threading fallback
        DOES exclude `record()` from `compact()`, so the sequence is safe.
      * Multi-process Windows was ALREADY lossy before W2-2: the old
        read-modify-write had the same unprotected window, and two processes
        publishing a rewritten ledger lost an increment to last-writer-wins.
        This is a change of shape, not a new class of failure.
      * The window here is the compaction itself (~5 ms measured locally) and
        it opens roughly 1-in-450 records, so what is at risk is the handful
        of records appended during those milliseconds -- not the batch.

    What W2-2 DID change is frequency: compaction used to run once a day from
    the heartbeat and now also fires from the per-model-call path. On a
    multi-process Windows instance that moves the window from daily to
    ~1-in-450 calls. If that configuration ever becomes supported, this needs a
    real cross-process lock (DEFERRED #10) before it is safe -- gating the
    auto-compaction trigger on POSIX is NOT the answer, because it would leave
    Windows with an unbounded journal and an unbounded `today_spend()`, which
    is the defect this threshold exists to prevent.
    """
    day = day or time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    journal = _journal_path(path)
    if not journal.exists():
        return {"day": day, "compacted": 0, "bytes_reclaimed": 0}
    try:
        with proclock.lock("usage-ledger", timeout=_LEDGER_LOCK_TIMEOUT):
            events = _replay_journal(path)
            if not events:
                return {"day": day, "compacted": 0, "bytes_reclaimed": 0}
            size = journal.stat().st_size
            merged = ledger(day)          # snapshot + journal, the real total
            _atomic_write_json(path, merged)
            # Only AFTER the snapshot is durably published may the journal go:
            # the reverse order would lose every un-compacted event on a crash
            # in between.
            journal.unlink()
            # The unlink itself must be durable. Without this, a crash in the
            # window after the removal but before the directory entry reaches
            # disk can RESURRECT a journal whose records are already in the
            # snapshot -- double-counting the day's spend. (POSIX-only; a
            # no-op on Windows, see atomicio.fsync_dir.)
            atomicio.fsync_dir(journal.parent)
            return {"day": day, "compacted": len(events),
                    "bytes_reclaimed": size}
    except (TimeoutError, OSError) as err:
        from . import errors
        errors.capture("usage.compact", err, context=day)
        return {"day": day, "compacted": 0, "bytes_reclaimed": 0,
                "error": type(err).__name__}


#: The complete, CLOSED set of fsync modes. Anything else is a typo, and
#: `_fsync_ledger` reports it rather than quietly choosing for the operator.
_FSYNC_MODES = ("always", "auto", "interval")
_FSYNC_MODE_WARNED = False


def _fsync_ledger() -> str:
    """OLYMPUS_USAGE_FSYNC -- `interval` (the default), `always`, or `auto`.

    WHAT THE APPEND ALREADY BANKED, regardless of this setting. Before W2-2,
    `record()` republished the WHOLE day ledger with tmp + os.replace. Unsynced,
    that could return an EMPTY file after a power cut -- the rename reaching
    disk ahead of the data -- which reset the day's spend to zero and disabled
    the cap. Appending cannot do that: prior records are never rewritten, so a
    crash costs only the unflushed TAIL and everything before it survives. That
    is a real durability improvement held at every setting, including `auto`,
    and it is why the exemption is no longer pure loss.

    WHY NOT PER-CALL FSYNC. Measured on this benchmark, same machine:

        old rewrite design, fsync off    2.26 ms
        old rewrite design, fsync on     4.82 ms
        append design,      fsync off    1.15 ms
        append design,      fsync on     4.13 ms

    The append halved the non-durable cost by removing the O(ledger)
    read-modify-write -- that half worked. But the sync itself did not get
    cheaper (~3.0 ms vs ~2.56 ms before), because an fsync is a DEVICE FLUSH
    BARRIER: its cost is a round trip, not a function of how many bytes
    preceded it. Writing 600 bytes instead of 50 KB removes work, not latency.
    Extrapolated at W1-5's bounded runner-to-local ratio (<7.6x), ~3 ms locally
    is ~18 ms on CI -- about where the contract failed before, against a 25 ms
    limit shared with `ctx` and `guard`.

    SO THE DEFAULT IS GROUP COMMIT BY TIME: at most one fsync per
    OLYMPUS_USAGE_FSYNC_INTERVAL_MS (default 1000), however many calls arrive
    inside it. `sessionlog` amortises a sync over a batch; `record()` is called
    singly, so it amortises over time instead.

    WORST-CASE LOSS IS BOUNDED BY THE INTERVAL: an unclean shutdown -- SIGKILL,
    power cut, kernel panic -- loses at most the last `interval` of records,
    one second by default. A CLEAN shutdown loses nothing: `flush()` is called
    from the heartbeat's SIGTERM handler (W1-4). Nothing here covers the
    unclean case; it is bounded, not prevented.

    THE INTERVAL IS PER PROCESS, NOT GLOBAL. Two processes writing the same
    ledger each keep their own timer and each sync once per interval, so the
    instance pays up to N x one sync per interval for N processes. That is
    bounded and small (the web app and the heartbeat make two), and the
    alternative -- a shared cross-process timer -- would need a lock on the hot
    path to save one flush per second, which is the trade this change exists to
    avoid.
    """
    raw = os.environ.get("OLYMPUS_USAGE_FSYNC", "").strip().lower()
    if not raw:
        return "interval"
    if raw in _FSYNC_MODES:
        return raw
    # AN UNRECOGNISED VALUE IS REPORTED, NOT SILENTLY ACCEPTED. `interval` used
    # to be the catch-all, so `OLYMPUS_USAGE_FSYNC=alwasy` (typo) resolved to
    # `interval` while the operator believed they had `always` -- they asked
    # for a zero-width tail and silently got a one-second one. That is a check
    # that cannot fail, in the config layer, on a durability knob.
    #
    # Reported ONCE: this is read on the per-append path, so capturing every
    # time would turn one typo into a flood. Same shape as proclock's _WARNED.
    global _FSYNC_MODE_WARNED
    if not _FSYNC_MODE_WARNED:
        _FSYNC_MODE_WARNED = True
        try:
            from . import errors
            errors.capture(
                "usage.fsync_mode",
                ValueError("unrecognised OLYMPUS_USAGE_FSYNC value"),
                context=f"expected one of {sorted(_FSYNC_MODES)}; "
                        f"falling back to 'interval'")
        except Exception:                # noqa: BLE001 - never break a record
            pass
    return "interval"


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON via a temp file + os.replace so a reader never sees a torn
    ledger — a truncated ledger would silently reset the day's spend to 0 and
    disable the budget guard.

    ALWAYS FSYNCS, unconditionally. This is the once-a-day compaction publish,
    not the per-call path, so the argument that took fsync off `record()` does
    not apply: one sync per compaction is free, and this snapshot is about to
    become the ONLY copy of everything the journal held.

    It also used to pass `fsync=_fsync_ledger()`, which silently became a BUG
    when that function started returning a mode STRING instead of a bool --
    "interval", "always" and "auto" are all truthy, so it happened to sync in
    every case including the one that asked not to. Correct by accident is not
    correct; it is stated explicitly now."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    atomicio.publish(tmp, path, json.dumps(obj, indent=1), fsync=True)


# In-process per-user session totals (since process start). The per-day
# ledger on disk stays the durable record; this powers per-reply and
# per-session footers without a disk read per turn. Worker threads set their
# user context (memory.set_user), so parallel specialist calls attribute here.
_SESSION: dict[str, dict] = {}


def _bump_session(user: str, in_tokens: int, out_tokens: int,
                  cost: float, cache_read: int = 0,
                  cache_creation: int = 0) -> None:
    row = _SESSION.setdefault(
        user, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
    row["calls"] += 1
    row["in"] += in_tokens
    row["out"] += out_tokens
    # Additive cache split (C5): absent = 0, existing keys untouched, so
    # footers/session totals for legacy positional callers are unchanged.
    row["cache_read"] = row.get("cache_read", 0) + cache_read
    row["cache_creation"] = row.get("cache_creation", 0) + cache_creation
    row["cost"] = round(row["cost"] + cost, 6)


def session_totals(user: str) -> dict:
    """This user's model usage since the process started (a copy)."""
    with _TOTALS_LOCK:
        return dict(_SESSION.get(memory.safe_id(user),
                                 {"calls": 0, "in": 0, "out": 0, "cost": 0.0}))


def delta(before: dict, after: dict) -> dict:
    return {k: round(after.get(k, 0) - before.get(k, 0), 6)
            for k in ("calls", "in", "out", "cost")}


def _fmt_tokens(n: float) -> str:
    n = int(n)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def footer(reply_delta: dict, user: str) -> str:
    """One-line cost footer for a chat reply: this reply, this session, today."""
    session = session_totals(user)
    return (f"⏱ {_fmt_tokens(reply_delta.get('in', 0))} in / "
            f"{_fmt_tokens(reply_delta.get('out', 0))} out · "
            f"~${reply_delta.get('cost', 0.0):.4f} this reply · "
            f"${session['cost']:.2f} session · ${today_spend():.2f} today")


def record(model: str, in_tokens: int, out_tokens: int, *,
           cache_read: int = 0, cache_creation: int = 0,
           provider: str = "", prefix_fp: str = "") -> None:
    """Append usage to the per-day ledger, attributed to the active user.

    Cache-aware call sites (C5) pass `in_tokens` = the UNCACHED input count
    plus the provider-reported cache split; legacy positional callers pass
    totals with zero cache fields and keep identical behaviour (I-U1). When
    `prefix_fp` is given (the sha256[:12] of the cacheable system prefix),
    per-fingerprint call/hit counts aggregate under the day file's "prefix"
    key so a prompt-layout change is visible as a hit-rate cliff."""
    cost = estimate_cost(model, in_tokens, out_tokens,
                         cache_read=cache_read, cache_creation=cache_creation)
    day = time.strftime("%Y-%m-%d")
    user = memory.current_user()
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    from . import proclock
    # Bound BEFORE the try, so `if oversized:` below cannot raise
    # UnboundLocalError on any path -- including one where the lock or the
    # append raises something outside the caught tuple. It was previously
    # bound only inside the `with`, which made the handler's `return`
    # load-bearing for correctness rather than merely for control flow.
    oversized = False
    with _TOTALS_LOCK:
        _bump_session(user, in_tokens, out_tokens, cost,
                      cache_read=cache_read, cache_creation=cache_creation)
    # The ledger read-modify-write holds ONLY the cross-process lock — never
    # nested inside _TOTALS_LOCK. Holding the in-process mutex across an
    # unbounded flock wait would couple session_totals()/today_spend() (the
    # per-reply hot path) to the OTHER process's liveness: a wedged heartbeat
    # holding the flock would freeze every reply here. The session bump and
    # the ledger write need no mutual atomicity (ADR 0005). llm calls this
    # after every model call with NO try/except, so a lock timeout must never
    # raise into the reply: one lost ledger increment under a wedged peer
    # beats a broken reply — captured, never silent.
    try:
        with proclock.lock("usage-ledger", timeout=_LEDGER_LOCK_TIMEOUT):
            _append_usage(path, {
                "ts": round(time.time(), 3),
                "user": user, "model": model,
                "in": in_tokens, "out": out_tokens,
                "cache_read": cache_read,
                "cache_creation": cache_creation,
                "cost": round(cost, 6),
                **({"prefix_fp": prefix_fp} if prefix_fp else {}),
            })
            # One stat(), never a line count -- counting lines is O(N) and is
            # the very thing this bound exists to prevent.
            try:
                oversized = (_journal_path(path).stat().st_size
                             >= compact_at_bytes())
            except OSError:
                oversized = False
    except (TimeoutError, OSError) as err:
        # Accounting must NEVER escape into the caller. `_atomic_write_json`
        # raises OSError on a full/read-only disk; letting that propagate made a
        # DISK fault look like a PROVIDER fault to `openai_compat._post` (whose
        # retry handler catches OSError) and to `backend._should_failover` — the
        # measured result was 4 billed HTTP POSTs for one logical call, i.e. a
        # disk fault converted into the denial-of-wallet shape the watchdog
        # exists to catch (Phase-4 Stage-C defect D-1). Capture and continue,
        # the same contract `record_repair` and `ctxbudget.observe` already use:
        # losing a usage row is strictly better than re-billing the user.
        from . import errors
        # Distinct contexts: a wedged lock and a full disk need different
        # operator actions, so they must not collapse into one message.
        errors.capture("usage.record", err,
                       context=("ledger lock wedged" if isinstance(err, TimeoutError)
                                else "ledger write failed (spend not recorded)"))
        # No `return` here. `oversized` is False on this path, so the trigger
        # below is skipped anyway, and an early return would silently become a
        # skip-the-rest if anything is ever appended to this function.
    # OUTSIDE the `with` block, deliberately. `record()` holds
    # proclock.lock("usage-ledger") for its append and `compact()` acquires the
    # SAME lock; calling it from inside would risk a self-deadlock on the
    # per-model-call path, and flock reentrancy is not something to assume --
    # the failure mode is Olympus HANGING, not erroring. Two acquisitions on
    # the 1-in-N crossing is nothing, and a peer that compacts first is
    # harmless: compact() returns 0 when the journal is already gone.
    if oversized:
        try:
            compact()
        except Exception as err:         # noqa: BLE001 - never raises, but
            # NEVER SILENT either. A compaction that fails every time -- disk
            # full, permissions, a wedged lock -- would let the journal grow
            # unbounded with nothing reporting it, which is the bound quietly
            # not existing. Same contract as the sibling handler above.
            from . import errors
            errors.capture("usage.record.autocompact", err,
                           context="journal over threshold but compaction "
                                   "failed; today_spend() cost is unbounded "
                                   "until this succeeds")


# --- the budget guard (protects the user's own API bill) -----------------

def today_spend() -> float:
    """Total estimated USD spent across the whole instance today."""
    # NO `path.exists()` SHORT-CIRCUIT. There used to be one, and with the W2-2
    # journal it silently returned 0.0 for a whole day: the SNAPSHOT does not
    # exist until the first compaction, while every model call is already
    # appending to the journal beside it. That is the exact failure this design
    # has to avoid -- a budget guard reporting no spend while spend is
    # happening, which disables the cap without telling anyone. `ledger()`
    # handles a missing snapshot itself.
    #
    # Deliberately lock-free with respect to record()'s cross-process flock:
    # taking it here would couple every budget check to the other process's
    # liveness for no consistency gain. `_TOTALS_LOCK` only serializes against
    # in-process session-total updates.
    with _TOTALS_LOCK:
        today = ledger()
    return float(today.get("__all__", {}).get("cost", 0.0))


def daily_budget() -> float:
    """Resolved daily USD budget. A saved setting (set via `olympus budget`)
    wins over the OLYMPUS_DAILY_BUDGET env var; 0 means no cap (the default)."""
    from . import prefs
    val = prefs.get("shared", "daily_budget", None)
    if val is None:
        val = config.DAILY_BUDGET
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return 0.0


def budget_headroom_low() -> bool:
    """True when a daily budget is set and less than 10% of it remains.

    The effort scorer consults this before RAISING a run above its
    specialist's floor (ADR 0005 amendment 7): "thinks harder" must never
    defeat the spend guard. Total: budget errors read as headroom-fine, and
    with no budget set (limit 0, the default) there is nothing to protect."""
    limit = daily_budget()
    if limit <= 0:
        return False
    return (limit - today_spend()) < limit * 0.10


def budget_status() -> dict:
    """Snapshot for display: limit, spent, remaining, and whether it's hit."""
    limit = daily_budget()
    spent = round(today_spend(), 4)
    return {
        "enabled": limit > 0,
        "limit": round(limit, 2),
        "spent": spent,
        "remaining": round(max(0.0, limit - spent), 4) if limit else None,
        "exceeded": limit > 0 and spent >= limit,
    }


def check_budget() -> None:
    """Raise BudgetExceeded if today's spend has reached the daily budget.
    Called before starting new work; a single in-flight request may overshoot
    by its own cost, so treat the budget as a soft 'stop starting' line."""
    limit = daily_budget()
    if limit > 0:
        spent = today_spend()
        if spent >= limit:
            raise BudgetExceeded(
                f"Daily budget of ${limit:.2f} reached (about ${spent:.2f} "
                f"spent today on your API key). Olympus paused to protect your "
                f"bill. Raise it with `olympus budget <amount>`, set "
                f"`olympus budget 0` to remove the cap, or wait until tomorrow.")


def run_budget() -> float:
    """Resolved per-run USD ceiling (OLYMPUS_RUN_BUDGET_USD; 0 = no cap).

    Unlike the daily budget, this is not persisted as a saved setting — it is a
    guardrail an operator arms for a session, not a standing account cap."""
    return config.run_budget_usd()


def run_over_budget(baseline: float) -> float | None:
    """How much a single run has spent past its per-run ceiling, or None.

    `baseline` is `today_spend()` snapshotted at run start; the run's own spend
    is the delta against it. Returns the overage (>= 0) once the run has added
    at least the per-run budget, else None. Like the daily guard this is a soft
    'stop starting new work' line — one in-flight specialist may overshoot by
    its own cost. No cap set → always None."""
    limit = run_budget()
    if limit <= 0:
        return None
    added = today_spend() - baseline
    return (added - limit) if added >= limit else None


def set_budget(amount: float) -> str:
    """Persist the daily budget (0 disables the cap)."""
    from . import prefs
    amount = max(0.0, float(amount))
    prefs.set("shared", "daily_budget", amount)
    if amount <= 0:
        return ("Daily budget removed — Olympus will not cap spend on your "
                "API key. (You can set one anytime with `olympus budget 5`.)")
    return (f"Daily budget set to ${amount:.2f}. Olympus will pause new "
            f"requests once today's estimated spend on your API key reaches it.")


def report(days: int = 7) -> str:
    """Human-readable spend summary over the last N days."""
    base = config.MEMORY_DIR / "usage"
    if not base.exists():
        return "No usage recorded yet."
    # Snapshots AND journals (W2-2): an uncompacted day has only a .jsonl, and
    # globbing *.json alone would omit it from the report entirely.
    known = {q.stem for q in base.glob("*.json")} | {
        q.stem for q in base.glob("*.jsonl")}
    files = sorted(known, reverse=True)[:days]
    lines = ["Usage (estimated, USD):", ""]
    grand = 0.0
    for day_name in sorted(files):
        led = ledger(day_name)
        if not led:
            continue
        allrow = led.get("__all__", {})
        cost = allrow.get("cost", 0.0)
        grand += cost
        lines.append(f"  {day_name}: ${cost:.4f}  "
                     f"({allrow.get('calls', 0)} calls, "
                     f"{allrow.get('in', 0)+allrow.get('out', 0)} tokens)")
    lines.append("")
    lines.append(f"  total ({len(files)}d): ${grand:.4f}")
    b = budget_status()
    if b["enabled"]:
        flag = "  ⚠ reached" if b["exceeded"] else ""
        lines.append(f"  today's budget: ${b['spent']:.4f} / ${b['limit']:.2f}"
                     f"{flag}")
    return "\n".join(lines)


# --- prompt-cache liveness (C5) -------------------------------------------

def cache_stats(days: int = 7) -> dict:
    """Is prompt caching actually working? Answered from recorded day files.

    Returns totals over the last `days` ledgers, the hit rate over
    fingerprint-carrying calls (a "hit" = a call whose provider reported
    cache_read > 0), an estimated savings figure (cache-read tokens repriced
    at the read multiplier instead of full input price), a per-fingerprint
    breakdown (a layout change shows as a new fp with a hit-rate cliff), and
    a verdict:

    - "active":    cache reads observed — caching is working.
    - "inert":     >= 20 fingerprint-carrying calls, zero cache reads —
                   configured but producing nothing.
    - "no_signal": no fingerprint-carrying calls recorded, or the provider
                   reports no cache fields at all.
    """
    out: dict = {"days": 0, "fp_calls": 0, "hits": 0, "hit_rate": 0.0,
                 "cache_read": 0, "cache_creation": 0, "savings_usd": 0.0,
                 "verdict": "no_signal", "by_fp": {}}
    base = config.MEMORY_DIR / "usage"
    if not base.exists():
        return out
    read_mult = cache_read_mult()
    savings = 0.0
    known = {q.stem for q in base.glob("*.json")} | {
        q.stem for q in base.glob("*.jsonl")}
    files = sorted(known, reverse=True)[:days]
    for day_name in sorted(files):           # ascending: last_day wins below
        led = ledger(day_name)
        if not isinstance(led, dict) or not led:
            continue
        out["days"] += 1
        for key, row in led.items():
            if not (key.startswith("model:") and isinstance(row, dict)):
                continue
            try:
                cr = int(row.get("cache_read", 0) or 0)
                cc = int(row.get("cache_creation", 0) or 0)
            except (TypeError, ValueError):
                continue
            out["cache_read"] += cr
            out["cache_creation"] += cc
            price_in, _ = PRICES.get(key[len("model:"):], DEFAULT_PRICE)
            savings += cr * price_in * (1.0 - read_mult) / 1_000_000
        prefixes = led.get("prefix")
        if not isinstance(prefixes, dict):
            continue
        for fp, row in prefixes.items():
            if not isinstance(row, dict):
                continue
            agg = out["by_fp"].setdefault(
                fp, {"calls": 0, "hits": 0, "cache_read": 0,
                     "first_day": day_name, "last_day": day_name})
            agg["calls"] += int(row.get("calls", 0) or 0)
            agg["hits"] += int(row.get("hits", 0) or 0)
            agg["cache_read"] += int(row.get("cache_read", 0) or 0)
            agg["last_day"] = day_name
    for agg in out["by_fp"].values():
        agg["hit_rate"] = round(agg["hits"] / agg["calls"], 4) \
            if agg["calls"] else 0.0
    out["fp_calls"] = sum(a["calls"] for a in out["by_fp"].values())
    out["hits"] = sum(a["hits"] for a in out["by_fp"].values())
    out["hit_rate"] = round(out["hits"] / out["fp_calls"], 4) \
        if out["fp_calls"] else 0.0
    out["savings_usd"] = round(max(0.0, savings), 6)
    if out["hits"] > 0 or out["cache_read"] > 0:
        out["verdict"] = "active"
    elif out["fp_calls"] >= 20 and out["cache_read"] == 0:
        out["verdict"] = "inert"
    return out
