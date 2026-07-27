"""Wave-2 capability C7 — unified admission policy on the ONE slot primitive.

What these tests prove:

1. **Flag-off identity.** With OLYMPUS_ADMISSION unset (the default),
   `usage.slot()` is byte-for-byte the pre-C7 primitive: the local
   BoundedSemaphore wrapped around `proclock.slot(_GLOBAL_SLOT,
   MAX_CONCURRENT_CALLS)`, no queue, no classes, no refusals, no admission
   state touched and no new files written.
2. **Capacity.** Global (MAX_CONCURRENT_CALLS) plus per-provider, per-model,
   per-tenant and per-class ceilings, all enforced by queueing (never by
   changing the request).
3. **Fairness (W2-I7.2).** Under saturation the queue drains round-robin over
   distinct tenant keys — a user who queued two requests first does not get
   both before the other users get one.
4. **Priority + reserved capacity (W2-I7.3).** "critical" drains before
   "background"; best-effort traffic can never consume the reserve, and the
   reserve is released back to best-effort when a best-effort holder leaves.
5. **Overload is typed, never silent.** queue_full / queue_timeout /
   no_capacity / cancelled / budget_exceeded / spend_rate_exceeded all raise
   `AdmissionRefused` with machine-readable retry guidance; cancellation frees
   the queue position for the next waiter.
6. **W2-I7.1.** Admission has no model/effort/route parameter at all, mutates
   nothing it is handed, and contains no assignment that could substitute a
   cheaper model or mode to make a request fit.
7. **admission_status()** counts what happened and is a pure read.
"""

import inspect
import re
import threading
import time
from pathlib import Path

import pytest

from olympus import config, proclock, usage


# --- helpers ---------------------------------------------------------------

class _Token:
    """A minimal cancel token: `.cancelled()` flips once `cancel()` is called."""

    def __init__(self) -> None:
        self._flag = threading.Event()

    def cancel(self) -> None:
        self._flag.set()

    def cancelled(self) -> bool:
        return self._flag.is_set()


class _Holder(threading.Thread):
    """Takes a slot, signals `entered`, holds it until `release` is set.

    Any AdmissionRefused raised on entry is captured in `.error` instead of
    dying in a thread nobody is watching."""

    daemon = True

    def __init__(self, **kw) -> None:
        super().__init__()
        self.kw = kw
        self.entered = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.admitted_at = None

    def run(self) -> None:
        try:
            with usage.slot(**self.kw):
                self.admitted_at = time.monotonic()
                self.entered.set()
                self.release.wait(5)
        except BaseException as err:               # noqa: BLE001 - reported
            self.error = err
            self.entered.set()
        finally:
            self.done.set()


def _wait_for(predicate, timeout=3.0):
    """Spin (cheaply) until `predicate()` is true; fail loudly on expiry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    raise AssertionError("condition not reached within "
                         f"{timeout}s; status={usage.admission_status()}")


@pytest.fixture(autouse=True)
def _clean_admission():
    """Admission state is process-global; every test starts and ends clean."""
    usage.admission_reset()
    yield
    usage.admission_reset()


@pytest.fixture
def fast_slots(monkeypatch):
    """Isolate the admission POLICY from the (real, file-backed) mechanics:
    the cross-process proclock slot and the local semaphore stay exercised in
    the flag-off identity test, but concurrency tests here would otherwise be
    bounded by the import-time semaphore (6) instead of the cap under test."""
    import contextlib

    @contextlib.contextmanager
    def _noop(name, count, timeout=None):
        yield

    monkeypatch.setattr(usage.proclock, "slot", _noop)
    monkeypatch.setattr(usage, "_SEMAPHORE", threading.BoundedSemaphore(64))


@pytest.fixture
def admission_on(monkeypatch, fast_slots):
    monkeypatch.setenv("OLYMPUS_ADMISSION", "1")
    monkeypatch.setenv("OLYMPUS_ADMISSION_RESERVE", "0")
    return monkeypatch


def _cap(monkeypatch, n: int) -> None:
    monkeypatch.setattr(config, "MAX_CONCURRENT_CALLS", n)


# --- 1. flag OFF: exactly today's behaviour ---------------------------------

def test_flag_off_uses_the_unchanged_primitive(monkeypatch):
    """The default path is still `_SEMAPHORE` + `proclock.slot(_GLOBAL_SLOT,
    MAX_CONCURRENT_CALLS)` — same primitives, same arguments, same nesting."""
    monkeypatch.delenv("OLYMPUS_ADMISSION", raising=False)
    calls = []
    real = proclock.slot

    import contextlib

    @contextlib.contextmanager
    def spy(name, count, timeout=proclock.DEFAULT_TIMEOUT):
        calls.append((name, count, timeout))
        with real(name, count, timeout):
            yield

    monkeypatch.setattr(usage.proclock, "slot", spy)
    before = usage._SEMAPHORE._value
    with usage.slot():
        assert usage._SEMAPHORE._value == before - 1     # outer semaphore held
        assert calls == [(usage._GLOBAL_SLOT, config.MAX_CONCURRENT_CALLS,
                          proclock.DEFAULT_TIMEOUT)]
    assert usage._SEMAPHORE._value == before


def test_flag_off_touches_no_admission_state(monkeypatch, tmp_path):
    """No queueing, no counters, no classes — and no new files on disk."""
    monkeypatch.delenv("OLYMPUS_ADMISSION", raising=False)
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    threads = [_Holder() for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        assert t.entered.wait(3)
        assert t.error is None
    # All four are inside their slot simultaneously (cap is 6) and admission
    # recorded nothing at all.
    status = usage.admission_status()
    assert status["enabled"] is False
    assert status["in_flight"] == 0 and status["queued"] == 0
    assert all(v == 0 for v in status["counters"].values())
    assert all(row["admitted"] == 0 for row in status["priority"].values())
    for t in threads:
        t.release.set()
    for t in threads:
        assert t.done.wait(3)
    # The only thing the primitive may create is the proclock lock dir it
    # always created; no admission ledger/queue file appears.
    created = {p.name for p in (tmp_path / "mem").rglob("*") if p.is_file()}
    assert not any("admission" in n for n in created)


def test_flag_off_ignores_every_policy_knob(monkeypatch):
    """Policy arguments are accepted and inert when the flag is off — a caller
    that passes them cannot be refused by a system that isn't running."""
    monkeypatch.delenv("OLYMPUS_ADMISSION", raising=False)
    monkeypatch.setenv("OLYMPUS_ADMISSION_MAX_QUEUE", "0")
    monkeypatch.setenv("OLYMPUS_ADMISSION_TENANT_MAX", "1")
    token = _Token()
    token.cancel()                       # would refuse instantly if flag were on
    with usage.slot(cls="council", key="u1", priority="background",
                    timeout=0, cancel_token=token,
                    dims={"provider": "anthropic", "model": "m"}):
        pass
    assert usage.admission_status()["counters"]["refused"] == 0


# --- 2. capacity dimensions -------------------------------------------------

def test_global_capacity_queues_the_overflow(admission_on, monkeypatch):
    _cap(monkeypatch, 2)
    holders = [_Holder(key=f"u{i}") for i in range(3)]
    for h in holders[:2]:
        h.start()
        assert h.entered.wait(3)
    holders[2].start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)
    assert usage.admission_status()["in_flight"] == 2
    assert not holders[2].entered.is_set()
    holders[0].release.set()
    assert holders[2].entered.wait(3)
    assert holders[2].error is None
    for h in holders:
        h.release.set()
        assert h.done.wait(3)
    _wait_for(lambda: usage.admission_status()["in_flight"] == 0)


@pytest.mark.parametrize("env,dims,label", [
    ("OLYMPUS_ADMISSION_PROVIDER_MAX", {"provider": "anthropic"}, "by_provider"),
    ("OLYMPUS_ADMISSION_MODEL_MAX", {"model": "claude-opus-4-8"}, "by_model"),
])
def test_per_provider_and_per_model_caps(admission_on, monkeypatch, env, dims,
                                         label):
    _cap(monkeypatch, 8)
    monkeypatch.setenv(env, "1")
    first = _Holder(key="u1", dims=dims)
    first.start()
    assert first.entered.wait(3)
    second = _Holder(key="u2", dims=dims)
    second.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)
    status = usage.admission_status()
    assert sum(status[label].values()) == 1        # the cap, not the global one
    # A different provider/model is unaffected by the saturated dimension.
    other = _Holder(key="u3", dims={k: "other" for k in dims})
    other.start()
    assert other.entered.wait(3)
    first.release.set()
    assert second.entered.wait(3)
    for h in (first, second, other):
        h.release.set()
        assert h.done.wait(3)


def test_per_tenant_cap(admission_on, monkeypatch):
    _cap(monkeypatch, 8)
    monkeypatch.setenv("OLYMPUS_ADMISSION_TENANT_MAX", "1")
    a1, a2, b1 = _Holder(key="alice"), _Holder(key="alice"), _Holder(key="bob")
    a1.start()
    assert a1.entered.wait(3)
    a2.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)
    b1.start()                                  # a different tenant sails past
    assert b1.entered.wait(3)
    assert not a2.entered.is_set()
    a1.release.set()
    assert a2.entered.wait(3)
    for h in (a1, a2, b1):
        h.release.set()
        assert h.done.wait(3)


def test_per_class_cap(admission_on, monkeypatch):
    _cap(monkeypatch, 8)
    monkeypatch.setenv("OLYMPUS_ADMISSION_CLASS_MAX", "1")
    a = _Holder(key="u1", cls="research")
    a.start()
    assert a.entered.wait(3)
    b = _Holder(key="u2", cls="research")
    b.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)
    c = _Holder(key="u3", cls="chat")
    c.start()
    assert c.entered.wait(3)
    a.release.set()
    assert b.entered.wait(3)
    for h in (a, b, c):
        h.release.set()
        assert h.done.wait(3)


# --- 3. fairness under saturation (W2-I7.2) ---------------------------------

def test_fairness_rotates_across_users_under_saturation(admission_on,
                                                        monkeypatch):
    """Six waiters, three users, queued in a deliberately unfair FIFO order
    (u0,u0,u1,u1,u2,u2) behind one blocking holder. Pure FIFO would serve
    u0 twice before u1 ran at all; the policy must rotate: u0,u1,u2,u0,u1,u2.

    The drain order is decided under the admission lock at promotion time, so
    it is a property of the policy and not of thread wake-up races — the
    assertion is exact, not statistical."""
    _cap(monkeypatch, 1)
    order: list[str] = []
    order_lock = threading.Lock()

    blocker = _Holder(key="blocker")
    blocker.start()
    assert blocker.entered.wait(3)

    def waiter(user: str):
        with usage.slot(key=user, timeout=5):
            with order_lock:
                order.append(user)

    threads = []
    queued = 0
    for user in ("u0", "u0", "u1", "u1", "u2", "u2"):
        t = threading.Thread(target=waiter, args=(user,), daemon=True)
        t.start()
        threads.append(t)
        queued += 1
        # Enqueue strictly one at a time so arrival order is deterministic.
        _wait_for(lambda q=queued: usage.admission_status()["queued"] == q)

    blocker.release.set()
    for t in threads:
        t.join(5)
        assert not t.is_alive()

    assert order == ["u0", "u1", "u2", "u0", "u1", "u2"], order
    # No user starved: every user got both of its requests.
    assert {u: order.count(u) for u in ("u0", "u1", "u2")} == {
        "u0": 2, "u1": 2, "u2": 2}
    status = usage.admission_status()
    assert status["counters"]["starvation_prevented"] >= 2
    assert status["counters"]["refused"] == 0     # fairness queues, never sheds
    assert blocker.done.wait(3)


# --- 4. priority + reserved capacity ---------------------------------------

def test_critical_drains_before_background(admission_on, monkeypatch):
    _cap(monkeypatch, 1)
    blocker = _Holder(key="blocker", priority="interactive")
    blocker.start()
    assert blocker.entered.wait(3)

    bg = _Holder(key="u1", priority="background", timeout=5)
    bg.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)
    crit = _Holder(key="u2", priority="critical", timeout=5)
    crit.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 2)

    blocker.release.set()
    assert crit.entered.wait(3)                  # arrived last, admitted first
    assert not bg.entered.is_set()
    crit.release.set()
    assert bg.entered.wait(3)
    for h in (blocker, crit, bg):
        h.release.set()
        assert h.done.wait(3)


def test_reserved_capacity_is_never_taken_by_best_effort(monkeypatch,
                                                         fast_slots):
    """W2-I7.3: with cap 2 and reserve 1, best-effort tops out at one slot —
    a background request queues while a critical one is admitted straight
    into the reserve. Releasing the best-effort holder hands the slot back."""
    monkeypatch.setenv("OLYMPUS_ADMISSION", "1")
    monkeypatch.setenv("OLYMPUS_ADMISSION_RESERVE", "1")
    _cap(monkeypatch, 2)

    busy = _Holder(key="u1", priority="interactive")
    busy.start()
    assert busy.entered.wait(3)

    bg = _Holder(key="u2", priority="background", timeout=5)
    bg.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)
    assert usage.admission_status()["in_flight"] == 1     # reserve untouched

    crit = _Holder(key="u3", priority="critical", timeout=5)
    crit.start()
    assert crit.entered.wait(3)                  # the reserve is for critical
    assert usage.admission_status()["in_flight"] == 2
    assert not bg.entered.is_set()

    busy.release.set()                           # best-effort slot frees up
    assert bg.entered.wait(3)
    for h in (busy, crit, bg):
        h.release.set()
        assert h.done.wait(3)


def test_reserve_is_clamped_so_best_effort_cannot_be_wedged(monkeypatch,
                                                            fast_slots):
    """A reserve >= the global cap would queue best-effort traffic forever;
    it is clamped to cap-1 so there is always forward progress."""
    monkeypatch.setenv("OLYMPUS_ADMISSION", "1")
    monkeypatch.setenv("OLYMPUS_ADMISSION_RESERVE", "9")
    _cap(monkeypatch, 2)
    with usage.slot(key="u1", priority="background", timeout=1):
        assert usage.admission_status()["in_flight"] == 1


# --- 5. queue limit, timeout, cancellation ---------------------------------

def test_queue_full_refuses_with_retry_guidance(admission_on, monkeypatch):
    _cap(monkeypatch, 1)
    monkeypatch.setenv("OLYMPUS_ADMISSION_MAX_QUEUE", "1")
    blocker = _Holder(key="blocker")
    blocker.start()
    assert blocker.entered.wait(3)
    waiting = _Holder(key="u1", timeout=5)
    waiting.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)

    with pytest.raises(usage.AdmissionRefused) as excinfo:
        with usage.slot(key="u2", timeout=5):
            pass
    err = excinfo.value
    assert err.reason == "queue_full"
    assert err.retry_after_s > 0
    assert usage.admission_status()["counters"]["queue_full"] == 1
    assert usage.admission_status()["queued"] == 1      # refusal left no trace

    blocker.release.set()
    assert waiting.entered.wait(3)
    waiting.release.set()
    assert waiting.done.wait(3)


def test_queue_timeout_refuses(admission_on, monkeypatch):
    _cap(monkeypatch, 1)
    blocker = _Holder(key="blocker")
    blocker.start()
    assert blocker.entered.wait(3)
    started = time.monotonic()
    with pytest.raises(usage.AdmissionRefused) as excinfo:
        with usage.slot(key="u1", timeout=0.05):
            pass
    assert time.monotonic() - started < 2.0
    assert excinfo.value.reason == "queue_timeout"
    assert excinfo.value.retry_after_s > 0
    status = usage.admission_status()
    assert status["counters"]["timed_out"] == 1
    assert status["queued"] == 0
    assert status["priority"]["interactive"]["timed_out"] == 1
    blocker.release.set()
    assert blocker.done.wait(3)


def test_zero_timeout_refuses_immediately(admission_on, monkeypatch):
    _cap(monkeypatch, 1)
    blocker = _Holder(key="blocker")
    blocker.start()
    assert blocker.entered.wait(3)
    with pytest.raises(usage.AdmissionRefused) as excinfo:
        with usage.slot(key="u1", timeout=0):
            pass
    assert excinfo.value.reason == "no_capacity"
    blocker.release.set()
    assert blocker.done.wait(3)


def test_cancellation_releases_the_queue_position(admission_on, monkeypatch):
    _cap(monkeypatch, 1)
    blocker = _Holder(key="blocker")
    blocker.start()
    assert blocker.entered.wait(3)

    token = _Token()
    cancelled = _Holder(key="u1", timeout=30, cancel_token=token)
    cancelled.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)
    behind = _Holder(key="u2", timeout=5)
    behind.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 2)

    token.cancel()
    assert cancelled.done.wait(3)
    assert isinstance(cancelled.error, usage.AdmissionRefused)
    assert cancelled.error.reason == "cancelled"
    assert cancelled.error.retry_after_s == 0.0
    _wait_for(lambda: usage.admission_status()["queued"] == 1)

    blocker.release.set()
    assert behind.entered.wait(3)                # the position really freed
    behind.release.set()
    assert behind.done.wait(3)
    assert usage.admission_status()["counters"]["cancelled"] == 1


# --- 6. denial-of-wallet ----------------------------------------------------

def test_refuses_when_the_budget_check_fails(admission_on, monkeypatch,
                                             tmp_path):
    """A real budget breach (ledger + daily cap), refused at admission — the
    request is never quietly run on a cheaper model instead (ruling R6)."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    day = time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"__all__": {"calls": 1, "in": 1, "out": 1, '
                    '"cost": 9.0}}', encoding="utf-8")
    monkeypatch.setattr(config, "DAILY_BUDGET", 1.0)
    assert usage.budget_status()["exceeded"] is True

    with pytest.raises(usage.AdmissionRefused) as excinfo:
        with usage.slot(key="u1"):
            pass
    err = excinfo.value
    assert err.reason == "budget_exceeded"
    assert err.retry_after_s >= 60          # come back after the day rolls over
    assert "budget" in err.detail.lower()
    assert usage.admission_status()["counters"]["budget_refused"] == 1
    assert usage.admission_status()["in_flight"] == 0


def test_refuses_when_the_spend_rate_is_too_high(admission_on, monkeypatch):
    monkeypatch.setenv("OLYMPUS_ADMISSION_SPEND_RATE_MAX", "2")
    monkeypatch.setattr(usage, "spend_rate", lambda key, **kw: 12.5)
    with pytest.raises(usage.AdmissionRefused) as excinfo:
        with usage.slot(key="whale"):
            pass
    err = excinfo.value
    assert err.reason == "spend_rate_exceeded"
    assert err.retry_after_s == 60.0
    assert "whale" in err.detail and "downgraded" in err.detail
    assert usage.admission_status()["counters"]["spend_rate_refused"] == 1


def test_spend_rate_is_zero_without_history_and_off_by_default(admission_on):
    assert usage.spend_rate("nobody") == 0.0
    assert usage.admission_spend_rate_max() == 0.0      # guard off by default
    with usage.slot(key="nobody"):                      # so admission proceeds
        pass


# --- 7. structured refusal shape -------------------------------------------

def test_admission_refused_as_dict_shape():
    err = usage.AdmissionRefused("queue_full", 7.25, "too many waiting")
    body = err.as_dict()
    assert body == {
        "error": "admission_refused",
        "reason": "queue_full",
        "retry_after_s": 7.25,
        "detail": "too many waiting",
        "message": "too many waiting",
        "degraded": False,
    }
    assert isinstance(err, RuntimeError)          # catchable by existing code
    code, http_body, headers = usage.admission_http_response(err)
    assert code == 429
    assert http_body == body
    assert headers["Retry-After"] == "8"          # whole seconds, rounded up
    assert headers["X-Olympus-Admission"] == "queue_full"


def test_admission_refused_defaults_are_safe():
    err = usage.AdmissionRefused("cancelled")
    assert err.retry_after_s == 0.0
    assert err.as_dict()["detail"] == ""
    assert "cancelled" in str(err)


def test_web_renders_admission_refusals_as_429(monkeypatch):
    """The web seam uses the shared renderer (both /api/chat and /v1 catch
    AdmissionRefused and emit exactly this)."""
    from olympus import web
    src = Path(web.__file__).read_text(encoding="utf-8")
    assert src.count("except usage.AdmissionRefused as err:") == 2
    assert src.count("usage.admission_http_response(err)") == 2


# --- 8. W2-I7.1 — no silent quality downgrade -------------------------------

def test_admission_api_has_no_model_or_effort_parameters():
    """The API cannot downgrade what it cannot name: `slot()` takes no model,
    effort, route or mode parameter, and every parameter is keyword-only with
    a default that reproduces the pre-C7 behaviour."""
    sig = inspect.signature(usage.slot)
    params = sig.parameters
    assert set(params) == {"cls", "key", "priority", "timeout",
                           "cancel_token", "dims"}
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY
               for p in params.values())
    assert params["cls"].default == "default"
    assert params["key"].default is None
    assert params["priority"].default is None
    assert params["timeout"].default is None
    assert params["cancel_token"].default is None
    assert params["dims"].default is None
    for banned in ("model", "effort", "route", "mode", "downgrade",
                   "fallback", "cheap"):
        assert not any(banned in name for name in params), banned


def test_admission_does_not_mutate_the_request_labels(admission_on):
    """`dims` is read, never written back — nothing about the request changes
    because it passed through admission."""
    dims = {"provider": "anthropic", "model": "claude-opus-4-8"}
    snapshot = dict(dims)
    with usage.slot(key="u1", dims=dims):
        pass
    assert dims == snapshot
    assert usage.admission_status()["downgrades"] == 0


def test_no_admission_code_path_alters_model_or_effort():
    """Source-level proof for W2-I7.1: nothing in the admission section
    assigns a model / effort / route / mode, and it imports no router or
    model-selection helper."""
    src = Path(usage.__file__).read_text(encoding="utf-8")
    start = src.index("# --- W2-C7: unified admission policy")
    end = src.index("# Cache-token price multipliers")
    section = src[start:end]
    assert len(section) > 2000
    banned = re.compile(
        r"^\s*(?:self\.)?(?:model|effort|route|mode|provider_choice)\s*"
        r"(?::[^=\n]+)?=(?!=)", re.MULTILINE)
    offenders = [m.group(0).strip() for m in banned.finditer(section)]
    assert offenders == [], offenders
    # No model-selection machinery is reachable from admission at all: no
    # router/grader import, no pricing table lookup, no "make it cheaper" call.
    for forbidden in ("from . import router", "routing.", "modelgrade",
                      "PRICES[", "estimate_cost(", "downgrade(",
                      "cheaper_model", "fallback_model", "set_model",
                      "switch_model", "set_effort"):
        assert forbidden not in section, forbidden


# --- 9. observability -------------------------------------------------------

def test_admission_status_counters(admission_on, monkeypatch):
    _cap(monkeypatch, 1)
    monkeypatch.setenv("OLYMPUS_ADMISSION_MAX_QUEUE", "1")
    blocker = _Holder(key="blocker", cls="council", priority="interactive",
                      dims={"provider": "anthropic", "model": "m1"})
    blocker.start()
    assert blocker.entered.wait(3)

    waiting = _Holder(key="u1", priority="background", timeout=5)
    waiting.start()
    _wait_for(lambda: usage.admission_status()["queued"] == 1)

    status = usage.admission_status()
    assert status["enabled"] is True
    assert status["in_flight"] == 1 and status["queued"] == 1
    assert status["priority"]["interactive"]["in_flight"] == 1
    assert status["priority"]["background"]["queued"] == 1
    assert status["by_provider"] == {"anthropic": 1}
    assert status["by_model"] == {"m1": 1}
    assert status["by_tenant"] == {"blocker": 1}
    assert status["by_cls"] == {"council": 1}
    assert status["limits"]["global"] == 1
    assert status["limits"]["max_queue"] == 1
    assert status["counters"]["admitted"] == 1

    with pytest.raises(usage.AdmissionRefused):
        with usage.slot(key="u2", timeout=5):
            pass                                  # queue_full -> refused

    # A pure read: mutating the snapshot cannot corrupt the live state.
    snap = usage.admission_status()
    snap["counters"]["admitted"] = 999
    snap["by_tenant"]["blocker"] = 42
    assert usage.admission_status()["counters"]["admitted"] == 1
    assert usage.admission_status()["by_tenant"] == {"blocker": 1}
    assert usage.admission_status()["counters"]["refused"] == 1

    blocker.release.set()
    assert waiting.entered.wait(3)
    waiting.release.set()
    assert waiting.done.wait(3)
    final = usage.admission_status()
    assert final["counters"]["admitted"] == 2
    assert final["in_flight"] == 0 and final["queued"] == 0
    assert final["queue_wait_ms_avg"] >= 0.0


def test_unknown_priority_is_a_loud_error(admission_on):
    with pytest.raises(ValueError):
        with usage.slot(key="u1", priority="urgent-ish"):
            pass


def test_knob_defaults_match_the_ruling(monkeypatch):
    """One env set, R3 spellings accepted as fallbacks, 300 s single default."""
    for name in ("OLYMPUS_ADMISSION", "OLYMPUS_ADMISSION_QUEUE_TIMEOUT",
                 "OLYMPUS_QUEUE_TIMEOUT", "OLYMPUS_ADMISSION_MAX_QUEUE",
                 "OLYMPUS_MAX_QUEUE", "OLYMPUS_ADMISSION_RESERVE",
                 "OLYMPUS_SLOT_RESERVE"):
        monkeypatch.delenv(name, raising=False)
    assert usage.admission_enabled() is False
    assert usage.admission_queue_timeout() == 300.0
    assert usage.admission_max_queue() == 64
    assert usage.admission_reserve() == 1
    assert usage.admission_provider_max() == 0    # 0/unset = unlimited
    assert usage.admission_model_max() == 0
    assert usage.admission_tenant_max() == 0
    monkeypatch.setenv("OLYMPUS_QUEUE_TIMEOUT", "12")
    monkeypatch.setenv("OLYMPUS_MAX_QUEUE", "3")
    monkeypatch.setenv("OLYMPUS_SLOT_RESERVE", "2")
    assert usage.admission_queue_timeout() == 12.0
    assert usage.admission_max_queue() == 3
    assert usage.admission_reserve() == 2
    monkeypatch.setenv("OLYMPUS_ADMISSION_QUEUE_TIMEOUT", "nonsense")
    assert usage.admission_queue_timeout() == 300.0   # bad value -> the default
