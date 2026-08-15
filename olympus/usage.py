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


def _fsync_ledger() -> bool:
    """OLYMPUS_USAGE_FSYNC — `auto` (the default) or `always`.

    THE ONE W1-1 SITE THAT DOES NOT FSYNC BY DEFAULT. Every other durable store
    syncs unconditionally; this one is opt-in, and the reason is measurement.

    W1-1 shipped `always` on the strength of a local benchmark (1000 calls x 5
    repeats, best-of, NTFS on a local SSD, py3.10):

        no fsync    730 us/call
        fsync      2269 us/call     +1.539 ms

    The Windows CI leg then measured the same fsync at **19.5 ms/call** —
    **12.7x** the local figure — in
    `test_observability_overhead_absolute_cost_bounded`, which attributes
    overhead per component and found `usage` responsible for 77% of the total
    with the lowest noise of any component (19.519 +/- 1.095 ms, the only one
    resolved cleanly). Cloud block storage (EBS, Azure Disk) is slower than a
    hosted runner, not faster, so 19.5 ms is the realistic figure and 1.5 ms was
    the outlier.

    The latency alone would be affordable — 19.5 ms against a ~2 s provider call
    is 1%. What is not affordable is WHERE it sits: `record()` does a
    read-modify-write of the whole ledger INSIDE a cross-process lock, and
    Olympus runs specialists in parallel. Twenty concurrent calls do not each pay
    19.5 ms simultaneously; they serialize on that lock and pay it in sequence —
    roughly **400 ms welded onto the critical path of every council turn**, plus
    contention. The W1-1 record justified `always` with "~30 ms on a
    20-specialist council turn". That estimate was wrong by an order of
    magnitude, because it multiplied the local per-call cost and ignored the
    lock.

    `OLYMPUS_USAGE_FSYNC=always` restores the sync for an operator who has
    measured their own storage and wants it. Any unrecognised value reads as
    off, so a typo cannot silently re-arm a 400 ms serialized cost.

    RESIDUAL RISK, stated plainly: with the default, a power cut can return an
    empty ledger. That resets the day's recorded spend to 0 and disables the
    budget cap until the next write. This is the cost of the decision. It is
    also exactly where `main` stood before W1-1 — the change declines to make
    one thing better at a price that was mis-measured by 13x, rather than
    regressing anything. W1-1c is the real fix: append + fsync one record like
    `sessionlog` does, which is both cheaper and more crash-safe than rewriting
    the whole file under a lock."""
    return os.environ.get("OLYMPUS_USAGE_FSYNC", "auto").strip().lower() \
        == "always"


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON via a temp file + os.replace so a reader never sees a torn
    ledger — a truncated ledger would silently reset the day's spend to 0 and
    disable the budget guard.

    The fsync that would also make this survive a power cut is OFF by default
    here, alone among the W1-1 sites, because it costs ~19.5 ms inside a
    cross-process lock on CI storage. See `_fsync_ledger`."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    atomicio.publish(tmp, path, json.dumps(obj, indent=1),
                     fsync=_fsync_ledger())


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
            ledger = {}
            if path.exists():
                try:
                    ledger = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    ledger = {}
            for key in ("__all__", f"user:{user}", f"model:{model}"):
                row = ledger.setdefault(
                    key, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
                row["calls"] += 1
                row["in"] += in_tokens
                row["out"] += out_tokens
                # Optional additive keys (migration-free: absent reads as 0
                # on old files, and old readers ignore the new keys).
                row["cache_read"] = row.get("cache_read", 0) + cache_read
                row["cache_creation"] = (row.get("cache_creation", 0)
                                         + cache_creation)
                row["cost"] = round(row["cost"] + cost, 6)
            if prefix_fp:
                fp_row = ledger.setdefault("prefix", {}).setdefault(
                    prefix_fp, {"calls": 0, "hits": 0, "cache_read": 0})
                fp_row["calls"] = fp_row.get("calls", 0) + 1
                if cache_read > 0:
                    fp_row["hits"] = fp_row.get("hits", 0) + 1
                fp_row["cache_read"] = fp_row.get("cache_read", 0) + cache_read
            _atomic_write_json(path, ledger)
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


# --- the budget guard (protects the user's own API bill) -----------------

def today_spend() -> float:
    """Total estimated USD spent across the whole instance today."""
    day = time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    if not path.exists():
        return 0.0
    # Deliberately lock-free with respect to record()'s cross-process flock:
    # correctness rests entirely on the atomic tmp+os.replace publish (a
    # reader sees the old or the new ledger, never a torn one). Taking the
    # flock here would couple every budget check to the other process's
    # liveness for no consistency gain. _TOTALS_LOCK only serializes against
    # in-process session-total updates.
    with _TOTALS_LOCK:
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0.0
    return float(ledger.get("__all__", {}).get("cost", 0.0))


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
    files = sorted(base.glob("*.json"), reverse=True)[:days]
    lines = ["Usage (estimated, USD):", ""]
    grand = 0.0
    for path in sorted(files):
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        allrow = ledger.get("__all__", {})
        cost = allrow.get("cost", 0.0)
        grand += cost
        lines.append(f"  {path.stem}: ${cost:.4f}  "
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
    files = sorted(base.glob("*.json"), reverse=True)[:days]
    for path in sorted(files):               # ascending: last_day wins below
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(ledger, dict):
            continue
        out["days"] += 1
        for key, row in ledger.items():
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
        prefixes = ledger.get("prefix")
        if not isinstance(prefixes, dict):
            continue
        for fp, row in prefixes.items():
            if not isinstance(row, dict):
                continue
            agg = out["by_fp"].setdefault(
                fp, {"calls": 0, "hits": 0, "cache_read": 0,
                     "first_day": path.stem, "last_day": path.stem})
            agg["calls"] += int(row.get("calls", 0) or 0)
            agg["hits"] += int(row.get("hits", 0) or 0)
            agg["cache_read"] += int(row.get("cache_read", 0) or 0)
            agg["last_day"] = path.stem
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
