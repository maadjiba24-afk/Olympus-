"""Progress-based watchdog (Wave-2 Capability C6).

The real defect this addresses is in `heartbeat.tick()`: every cadence runs
serially and inline, so ONE wedged job (a network hang inside a provider SDK,
a provider that accepts the request and never streams a token) stalls the
scheduler, goals, backups and every other cadence *silently and indefinitely*.
`proclock.DEFAULT_TIMEOUT` bounds lock waits and its own docstring names the
residual pathology: "the only unbounded wait is a LIVE-but-wedged holder."
A wedged holder is alive. Liveness is therefore not the question.

The question is **progress** — absorption doc 11 (O5), whose load-bearing
distinction is that zombies are processes that are alive but not *advancing*.
Olympus has a second currency the shell babysitter never had: **spend**. A run
can burn the user's API bill for minutes while producing nothing verifiable.
So this module measures a unit of work in two currencies at once (time and
money) and asks only one thing of it: *did anything verifiable advance?*

W2-I6.1 — MODEL-GENERATED TEXT ALONE IS NEVER PROOF OF PROGRESS.
    Tokens arriving is not advancement; a model can emit fluent text forever.
    Only the signals in `PROGRESS_SIGNALS` count, each of which is something a
    *verifier* (not the model) accepted: a plan node completed, a tool result
    validated, a specialist output accepted, a response section verified, a
    replay state advanced, a bounded token generation completed. Anything in
    `NON_PROGRESS_SIGNALS` (and anything unrecognised) is recorded in the
    timeline for forensics and counts for NOTHING.

Detections (each a pure classifier over an immutable `LeaseState` snapshot, so
each is independently testable without building a lease): DEADLOCK, RETRY_LOOP,
IDENTICAL_CALLS, STALLED_STREAM, RUNAWAY_RECURSION, DENIAL_OF_WALLET,
PROVIDER_HANG, VERIFIER_LOOP, PROGRESS_FREE_SPEND, QUEUE_STARVATION.

Action ladder (in order, `_act`): warn -> cancel -> preserve forensic state
(`MEMORY_DIR/watchdog/forensics/<lease_id>.json`, proclock-guarded, never
raises) -> refuse unsafe continuation (a TERMINAL state callers must honour)
-> release admission slots (via the caller-supplied `release_fn`; this module
deliberately knows NOTHING about `usage.slot` internals) -> quarantine the
repeated failure pattern (LAZY import of `olympus.experiments`, treated as an
OPTIONAL dependency: absent, broken or reshaped, the ladder still completes)
-> expose the exact reason AND the operator action in the Verdict.

Modes. `OLYMPUS_WATCHDOG=off|observe|enforce`, default **off**.
  off      fully inert: no classification, no files, every verdict is OK.
  observe  classifies and preserves forensics; NEVER cancels, refuses,
           releases a slot or quarantines.
  enforce  the full ladder.

PROVISIONAL THRESHOLDS. Every numeric threshold below is PROVISIONAL: none of
them is derived from measurement of this system. They are seeded from the
absorption doc's operational figures (Colibri's 180 s stall kill, the doc's
`OLYMPUS_JOB_STALL_SECS=300`) and from conservative judgement, and they are
calibrated in **Phase-4 reliability validation**, whose acceptance gate (W2-A9)
is an explicit FALSE-POSITIVE rate — a slow-but-progressing unit of work must
never trip. Until that calibration lands, the default mode is `off` and every
threshold is env-overridable. The PROVISIONAL set is exactly:
    OLYMPUS_WATCHDOG_STALL_SECS, OLYMPUS_WATCHDOG_RETRY_MAX,
    OLYMPUS_WATCHDOG_IDENTICAL_MAX, OLYMPUS_WATCHDOG_STREAM_STALL_SECS,
    OLYMPUS_WATCHDOG_MAX_TOOL_DEPTH, OLYMPUS_WATCHDOG_SPEND_RATE_USD,
    OLYMPUS_WATCHDOG_SPEND_WINDOW_SECS, OLYMPUS_WATCHDOG_CALL_CEILING_SECS,
    OLYMPUS_WATCHDOG_VERIFY_CYCLE_MAX, OLYMPUS_WATCHDOG_PROGRESS_FREE_USD,
    OLYMPUS_WATCHDOG_QUEUE_WAIT_SECS.

SCOPE. WIRED (W2-PR13) into the two live seams that can wedge:
`orchestrator._pipeline` opens one lease per RUN (fed `plan_node_completed`,
`specialist_output_accepted`, `response_section_verified`,
`tokens_generated_bounded`, and spend measured against the run's budget
baseline), and `heartbeat.tick` opens one lease per JOB — the serial loop this
module was written for. `usage.slot` still knows nothing about leases: slot
release stays the caller's contract, injected as `release_fn`. Still NOT wired:
`doctor` and `cli`. The wiring changes nothing at the shipped default, because
the default mode is `off` and a lease is only ever constructed when the flag is
armed: a component with cancel authority earns `enforce` only after Phase-4
measures its false-positive rate.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import config

# --- modes -----------------------------------------------------------------
OFF = "off"
OBSERVE = "observe"
ENFORCE = "enforce"
MODES = (OFF, OBSERVE, ENFORCE)


def mode() -> str:
    """`OLYMPUS_WATCHDOG` resolved to one of MODES; default (and any
    unrecognised value) is `off` — the watchdog is inert until Phase-4
    calibration measures its false-positive rate."""
    raw = os.environ.get("OLYMPUS_WATCHDOG", "").strip().lower()
    if raw in ("1", "true", "yes", "on", ENFORCE):
        return ENFORCE
    if raw == OBSERVE:
        return OBSERVE
    return OFF


def enabled() -> bool:
    """True when the watchdog does anything at all (observe or enforce)."""
    return mode() != OFF


# --- the progress currency (W2-I6.1) ---------------------------------------
# ONLY these count as progress. Each is an advancement a VERIFIER accepted,
# never something the model asserted about itself.
PROGRESS_SIGNALS = (
    "plan_node_completed",          # a DAG node finished and was accepted
    "tool_result_validated",        # a tool result passed schema validation
    "specialist_output_accepted",   # an output contract check passed
    "response_section_verified",    # a verifier accepted a response section
    "replay_state_advanced",        # replay cursor moved forward
    "tokens_generated_bounded",     # a bounded generation completed
)

# Explicitly NOT progress. Text arriving proves the provider is alive; it does
# not prove the work advanced. A lease fed only these while spending money is
# the canonical PROGRESS_FREE_SPEND pathology (W2-I6.1).
NON_PROGRESS_SIGNALS = ("model_text",)


def is_progress(signal: str) -> bool:
    """True only for the verifiable signals. Unknown signals are NOT progress
    (fail-closed: a new signal name must be added to PROGRESS_SIGNALS with the
    verifier that backs it, never accepted by accident)."""
    return str(signal) in PROGRESS_SIGNALS


# --- verdict kinds ---------------------------------------------------------
OK = "OK"
DEADLOCK = "DEADLOCK"
RETRY_LOOP = "RETRY_LOOP"
IDENTICAL_CALLS = "IDENTICAL_CALLS"
STALLED_STREAM = "STALLED_STREAM"
RUNAWAY_RECURSION = "RUNAWAY_RECURSION"
DENIAL_OF_WALLET = "DENIAL_OF_WALLET"
PROVIDER_HANG = "PROVIDER_HANG"
VERIFIER_LOOP = "VERIFIER_LOOP"
PROGRESS_FREE_SPEND = "PROGRESS_FREE_SPEND"
QUEUE_STARVATION = "QUEUE_STARVATION"
# Not a detection: the terminal state a bare `refuse_continuation()` produces.
REFUSED = "REFUSED"

DETECTION_KINDS = (DEADLOCK, RETRY_LOOP, IDENTICAL_CALLS, STALLED_STREAM,
                   RUNAWAY_RECURSION, DENIAL_OF_WALLET, PROVIDER_HANG,
                   VERIFIER_LOOP, PROGRESS_FREE_SPEND, QUEUE_STARVATION)


# --- thresholds (ALL PROVISIONAL — see the module docstring) ---------------

def _num(env: str, default: float) -> float:
    try:
        val = float(os.environ.get(env, "").strip() or default)
    except (TypeError, ValueError):
        return float(default)
    return val if val > 0 else float(default)


def stall_secs() -> float:
    """PROVISIONAL. No beat of any kind for this long => DEADLOCK. Seeded from
    absorption doc 11's `OLYMPUS_JOB_STALL_SECS=300`."""
    return _num("OLYMPUS_WATCHDOG_STALL_SECS", 300.0)


def retry_max() -> int:
    """PROVISIONAL. Retries of ONE key beyond this => RETRY_LOOP."""
    return int(_num("OLYMPUS_WATCHDOG_RETRY_MAX", 5))


def identical_max() -> int:
    """PROVISIONAL. One request fingerprint repeated beyond this =>
    IDENTICAL_CALLS."""
    return int(_num("OLYMPUS_WATCHDOG_IDENTICAL_MAX", 3))


def stream_stall_secs() -> float:
    """PROVISIONAL. A stream lease with no delta for this long =>
    STALLED_STREAM."""
    return _num("OLYMPUS_WATCHDOG_STREAM_STALL_SECS", 45.0)


def max_tool_depth() -> int:
    """PROVISIONAL. Tool nesting deeper than this => RUNAWAY_RECURSION."""
    return int(_num("OLYMPUS_WATCHDOG_MAX_TOOL_DEPTH", 8))


def spend_rate_usd() -> float:
    """PROVISIONAL. Sustained USD/second above this => DENIAL_OF_WALLET."""
    return _num("OLYMPUS_WATCHDOG_SPEND_RATE_USD", 0.05)


def spend_window_secs() -> float:
    """PROVISIONAL. Minimum observation window before a spend RATE is judged —
    without it one expensive call at t+0 reads as an infinite rate (the
    false-positive guard on DENIAL_OF_WALLET)."""
    return _num("OLYMPUS_WATCHDOG_SPEND_WINDOW_SECS", 30.0)


def call_ceiling_secs() -> float:
    """PROVISIONAL. A SINGLE provider call open longer than this =>
    PROVIDER_HANG (a hard ceiling, not a stall heuristic)."""
    return _num("OLYMPUS_WATCHDOG_CALL_CEILING_SECS", 600.0)


def verify_cycle_max() -> int:
    """PROVISIONAL. verify->rework cycles beyond this => VERIFIER_LOOP."""
    return int(_num("OLYMPUS_WATCHDOG_VERIFY_CYCLE_MAX", 3))


def progress_free_usd() -> float:
    """PROVISIONAL. USD spent with ZERO accepted progress signals =>
    PROGRESS_FREE_SPEND (W2-I6.1's enforcement threshold)."""
    return _num("OLYMPUS_WATCHDOG_PROGRESS_FREE_USD", 0.25)


def queue_wait_secs() -> float:
    """PROVISIONAL. A queued (never admitted) lease waiting longer than this
    fairness deadline => QUEUE_STARVATION."""
    return _num("OLYMPUS_WATCHDOG_QUEUE_WAIT_SECS", 120.0)


def thresholds() -> dict:
    """The threshold set in force right now — recorded into every forensics
    file so a verdict can be re-read against the configuration that produced
    it. Every entry is PROVISIONAL (Phase-4 reliability validation)."""
    return {
        "stall_secs": stall_secs(),
        "retry_max": retry_max(),
        "identical_max": identical_max(),
        "stream_stall_secs": stream_stall_secs(),
        "max_tool_depth": max_tool_depth(),
        "spend_rate_usd": spend_rate_usd(),
        "spend_window_secs": spend_window_secs(),
        "call_ceiling_secs": call_ceiling_secs(),
        "verify_cycle_max": verify_cycle_max(),
        "progress_free_usd": progress_free_usd(),
        "queue_wait_secs": queue_wait_secs(),
        "PROVISIONAL": True,
    }


# --- verdict ---------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """The classification of one lease at one instant.

    `reason` is the exact evidence ("no beat for 412s"); `operator_action` is
    what a human should DO about it (absorption doc 09's verdict-names-the-knob
    doctrine) and is REQUIRED on every non-OK verdict; `forensics` carries the
    machine-readable evidence for the preserved state file."""
    kind: str
    reason: str = ""
    operator_action: str = ""
    forensics: dict = field(default_factory=dict)

    @property
    def tripped(self) -> bool:
        return self.kind != OK

    def to_dict(self) -> dict:
        return {"kind": self.kind, "reason": self.reason,
                "operator_action": self.operator_action,
                "forensics": dict(self.forensics)}


def _ok(note: str = "") -> Verdict:
    return Verdict(OK, reason=note)


class WatchdogRefusal(RuntimeError):
    """Raised by `raise_if_terminal()` on a lease the watchdog has refused.
    Continuing a refused lease is the unsafe continuation the ladder exists to
    prevent, so the refusal is available as an exception for callers that
    prefer to be stopped rather than to check a flag."""


# --- the immutable snapshot the classifiers read ---------------------------

@dataclass(frozen=True)
class LeaseState:
    """An immutable view of one lease at one instant. The classifiers below
    read ONLY this — they never touch a lease, the clock, the filesystem or the
    environment beyond the threshold accessors, so each is a pure function of
    (state, now) and independently testable."""
    lease_id: str
    now: float
    created: float
    kind: str = "run"
    deadline: float | None = None
    queued: bool = False
    admitted_at: float | None = None
    last_beat: float = 0.0
    last_progress: float | None = None
    progress_count: int = 0
    progress_amount: float = 0.0
    non_progress_count: int = 0
    spend_usd: float = 0.0
    spend_since_progress: float = 0.0
    first_spend_at: float | None = None
    retry_counts: dict = field(default_factory=dict)
    call_counts: dict = field(default_factory=dict)
    tool_depth: int = 0
    verify_cycles: int = 0
    stream: bool = False
    stream_deltas: int = 0
    last_stream_delta: float | None = None
    open_call_started: float | None = None
    longest_call_secs: float = 0.0
    closed: bool = False

    def at(self, now: float | None) -> "LeaseState":
        return self if now is None else replace(self, now=float(now))

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id, "kind": self.kind, "now": self.now,
            "created": self.created, "deadline": self.deadline,
            "queued": self.queued, "admitted_at": self.admitted_at,
            "last_beat": self.last_beat, "last_progress": self.last_progress,
            "progress_count": self.progress_count,
            "progress_amount": round(self.progress_amount, 6),
            "non_progress_count": self.non_progress_count,
            "spend_usd": round(self.spend_usd, 6),
            "spend_since_progress": round(self.spend_since_progress, 6),
            "first_spend_at": self.first_spend_at,
            "retry_counts": dict(self.retry_counts),
            "call_counts": dict(self.call_counts),
            "tool_depth": self.tool_depth,
            "verify_cycles": self.verify_cycles,
            "stream": self.stream, "stream_deltas": self.stream_deltas,
            "last_stream_delta": self.last_stream_delta,
            "open_call_started": self.open_call_started,
            "longest_call_secs": round(self.longest_call_secs, 3),
            "closed": self.closed,
        }


def _worst(counts: dict) -> tuple[str, int]:
    if not counts:
        return "", 0
    key = max(counts, key=lambda k: counts[k])
    return key, int(counts[key])


# --- the ten pure classifiers ----------------------------------------------
# Each returns a Verdict: OK for a healthy lease, otherwise the named kind with
# the exact reason, the operator action, and machine-readable forensics.

def detect_provider_hang(state: LeaseState, now: float | None = None) -> Verdict:
    """One provider call open past a HARD ceiling. Not a heuristic: a single
    call that has not returned in `call_ceiling_secs` is hung, whatever else
    the lease is doing."""
    now = state.at(now).now
    ceiling = call_ceiling_secs()
    open_for = 0.0 if state.open_call_started is None else now - state.open_call_started
    worst = max(open_for, state.longest_call_secs)
    if worst <= ceiling:
        return _ok()
    return Verdict(
        PROVIDER_HANG,
        reason=(f"a single provider call has been open {worst:.0f}s, past the "
                f"{ceiling:.0f}s hard ceiling"),
        operator_action=(
            "Cancel the call and check provider status. If this model/endpoint "
            "is legitimately this slow, raise OLYMPUS_WATCHDOG_CALL_CEILING_SECS; "
            "otherwise treat it as a provider incident (the call is not "
            "progressing and holds an admission slot)."),
        forensics={"open_secs": round(worst, 1), "ceiling_secs": ceiling,
                   "call_started": state.open_call_started})


def detect_denial_of_wallet(state: LeaseState, now: float | None = None) -> Verdict:
    """Spend RATE over the budget/second threshold. Deliberately gated on a
    minimum observation window: one expensive call at t+0 is not a runaway."""
    now = state.at(now).now
    if state.spend_usd <= 0 or state.first_spend_at is None:
        return _ok()
    window = now - state.first_spend_at
    min_window = spend_window_secs()
    if window < min_window:
        return _ok(f"spend window {window:.1f}s < {min_window:.0f}s")
    rate = state.spend_usd / window if window > 0 else 0.0
    limit = spend_rate_usd()
    if rate <= limit:
        return _ok()
    return Verdict(
        DENIAL_OF_WALLET,
        reason=(f"spend rate ${rate:.4f}/s over {window:.0f}s exceeds the "
                f"${limit:.4f}/s ceiling (${state.spend_usd:.2f} total)"),
        operator_action=(
            "Stop the run and inspect what is calling: this is the "
            "denial-of-wallet shape (a loop billing the user's own API key). "
            "Arm OLYMPUS_RUN_BUDGET_USD / `olympus budget`, or raise "
            "OLYMPUS_WATCHDOG_SPEND_RATE_USD if this workload is genuinely "
            "this expensive per second."),
        forensics={"rate_usd_per_s": round(rate, 6), "limit": limit,
                   "spend_usd": round(state.spend_usd, 6),
                   "window_secs": round(window, 1)})


def detect_progress_free_spend(state: LeaseState,
                               now: float | None = None) -> Verdict:
    """W2-I6.1. Money moving, nothing verifiable advancing.

    Two shapes: (a) the lease has NEVER produced an accepted progress signal
    and has spent past the threshold — the model-text-only pathology; (b) the
    lease progressed once, then spent past the threshold with no accepted
    progress for a full stall window. Both require real money AND zero accepted
    progress, so a lease that keeps advancing can never trip regardless of how
    slowly it spends."""
    now = state.at(now).now
    limit = progress_free_usd()
    if state.progress_count == 0:
        if state.spend_usd < limit:
            return _ok()
        return Verdict(
            PROGRESS_FREE_SPEND,
            reason=(f"${state.spend_usd:.2f} spent with ZERO accepted progress "
                    f"signals ({state.non_progress_count} non-progress beat(s) "
                    f"— model text alone is not progress, W2-I6.1)"),
            operator_action=(
                "Cancel and inspect the transcript: the unit of work is billing "
                "without producing anything a verifier accepted. If the work "
                "legitimately emits no progress signal, instrument it with one "
                "(see watchdog.PROGRESS_SIGNALS) rather than raising "
                "OLYMPUS_WATCHDOG_PROGRESS_FREE_USD."),
            forensics={"spend_usd": round(state.spend_usd, 6),
                       "limit_usd": limit, "progress_count": 0,
                       "non_progress_count": state.non_progress_count,
                       "invariant": "W2-I6.1"})
    since = now - (state.last_progress or state.created)
    if state.spend_since_progress >= limit and since > stall_secs():
        return Verdict(
            PROGRESS_FREE_SPEND,
            reason=(f"${state.spend_since_progress:.2f} spent in the "
                    f"{since:.0f}s since the last accepted progress signal "
                    f"(stall window {stall_secs():.0f}s)"),
            operator_action=(
                "Cancel and inspect: the run progressed and then began paying "
                "for nothing. Check for a retry/verifier loop first; "
                "OLYMPUS_WATCHDOG_PROGRESS_FREE_USD tunes the money side and "
                "OLYMPUS_WATCHDOG_STALL_SECS the time side."),
            forensics={"spend_since_progress":
                       round(state.spend_since_progress, 6),
                       "limit_usd": limit, "secs_since_progress": round(since, 1),
                       "progress_count": state.progress_count,
                       "invariant": "W2-I6.1"})
    return _ok()


def detect_runaway_recursion(state: LeaseState,
                             now: float | None = None) -> Verdict:
    """Tool calling tools calling tools past the depth ceiling."""
    limit = max_tool_depth()
    if state.tool_depth <= limit:
        return _ok()
    return Verdict(
        RUNAWAY_RECURSION,
        reason=(f"tool recursion reached depth {state.tool_depth}, past the "
                f"maximum of {limit}"),
        operator_action=(
            "Cancel: a tool is invoking itself (directly or via a cycle). "
            "Inspect the last tool chain in the forensics timeline; raise "
            "OLYMPUS_WATCHDOG_MAX_TOOL_DEPTH only if the chain is genuinely "
            "this deep by design."),
        forensics={"tool_depth": state.tool_depth, "limit": limit})


def detect_identical_calls(state: LeaseState,
                           now: float | None = None) -> Verdict:
    """The SAME request fingerprint issued over and over — a loop that is not
    even varying its input, so no amount of waiting changes the outcome."""
    limit = identical_max()
    fp, count = _worst(state.call_counts)
    if count <= limit:
        return _ok()
    return Verdict(
        IDENTICAL_CALLS,
        reason=(f"request fingerprint {fp!r} issued {count} times "
                f"(limit {limit}) with no varying input"),
        operator_action=(
            "Cancel: repeating a byte-identical request cannot produce a "
            "different accepted result. Inspect the caller's loop condition; "
            "OLYMPUS_WATCHDOG_IDENTICAL_MAX tunes the trip point."),
        forensics={"fingerprint": fp, "count": count, "limit": limit,
                   "call_counts": dict(state.call_counts)})


def detect_retry_loop(state: LeaseState, now: float | None = None) -> Verdict:
    """One retry key attempted past the retry ceiling."""
    limit = retry_max()
    key, count = _worst(state.retry_counts)
    if count <= limit:
        return _ok()
    return Verdict(
        RETRY_LOOP,
        reason=f"retry key {key!r} attempted {count} times (limit {limit})",
        operator_action=(
            "Cancel and read the last recorded error for this key: the retry "
            "ladder is not converging. Fix the underlying failure (auth, "
            "schema, quota) rather than raising OLYMPUS_WATCHDOG_RETRY_MAX."),
        forensics={"retry_key": key, "count": count, "limit": limit,
                   "retry_counts": dict(state.retry_counts)})


def detect_verifier_loop(state: LeaseState, now: float | None = None) -> Verdict:
    """verify -> rework -> verify cycles past the ceiling: the verifier and the
    worker disagree in a way more attempts will not settle."""
    limit = verify_cycle_max()
    if state.verify_cycles <= limit:
        return _ok()
    return Verdict(
        VERIFIER_LOOP,
        reason=(f"{state.verify_cycles} verify->rework cycles "
                f"(limit {limit}) without acceptance"),
        operator_action=(
            "Cancel and surface the verifier's last objection to the operator: "
            "an unsatisfiable verification contract must be reported, never "
            "worked around by retrying. OLYMPUS_WATCHDOG_VERIFY_CYCLE_MAX tunes "
            "the ceiling."),
        forensics={"verify_cycles": state.verify_cycles, "limit": limit})


def detect_stalled_stream(state: LeaseState, now: float | None = None) -> Verdict:
    """A stream lease that has stopped delivering deltas while the connection
    stays open. Only stream leases are eligible."""
    now = state.at(now).now
    if not state.stream:
        return _ok()
    limit = stream_stall_secs()
    last = state.last_stream_delta or state.created
    idle = now - last
    if idle <= limit:
        return _ok()
    return Verdict(
        STALLED_STREAM,
        reason=(f"stream delivered no delta for {idle:.0f}s "
                f"(limit {limit:.0f}s, {state.stream_deltas} delta(s) so far)"),
        operator_action=(
            "Cancel the stream and retry from the last verified section — an "
            "open connection with no deltas is a hung provider, not a slow one. "
            "OLYMPUS_WATCHDOG_STREAM_STALL_SECS tunes the idle window."),
        forensics={"idle_secs": round(idle, 1), "limit_secs": limit,
                   "deltas": state.stream_deltas,
                   "last_delta": state.last_stream_delta})


def detect_queue_starvation(state: LeaseState,
                            now: float | None = None) -> Verdict:
    """A queued lease that was never admitted past its fairness deadline. This
    is the ONLY detection about work that has not started; it exists so
    starvation is visible as a watchdog verdict rather than as a user
    complaint."""
    now = state.at(now).now
    if not state.queued or state.admitted_at is not None:
        return _ok()
    limit = queue_wait_secs()
    waited = now - state.created
    if waited <= limit:
        return _ok()
    return Verdict(
        QUEUE_STARVATION,
        reason=(f"queued {waited:.0f}s without admission, past the "
                f"{limit:.0f}s fairness deadline"),
        operator_action=(
            "Inspect admission: this lease is being starved by other traffic. "
            "Raise capacity (OLYMPUS_MAX_CONCURRENT_CALLS), shed load, or "
            "raise OLYMPUS_WATCHDOG_QUEUE_WAIT_SECS if this queue depth is "
            "expected. Do NOT silently downgrade quality to admit it."),
        forensics={"waited_secs": round(waited, 1), "limit_secs": limit,
                   "created": state.created})


def detect_deadlock(state: LeaseState, now: float | None = None) -> Verdict:
    """No signal of ANY kind and no spend for a full stall window — the classic
    wedged holder: alive, holding its slot, advancing nothing, costing nothing.
    Queued (never admitted) leases are excluded: waiting to start is
    QUEUE_STARVATION, not deadlock."""
    now = state.at(now).now
    if state.queued and state.admitted_at is None:
        return _ok()
    limit = stall_secs()
    last = max(state.last_beat, state.admitted_at or 0.0, state.created)
    idle = now - last
    if idle <= limit:
        return _ok()
    return Verdict(
        DEADLOCK,
        reason=(f"no progress signal and no spend for {idle:.0f}s "
                f"(stall window {limit:.0f}s)"),
        operator_action=(
            "Cancel and let the work resume from its last verified state: a "
            "live-but-wedged holder stalls every other cadence in the serial "
            "heartbeat tick. Check for a blocked network call or a lock held "
            "by a stopped process; OLYMPUS_WATCHDOG_STALL_SECS tunes the "
            "window."),
        forensics={"idle_secs": round(idle, 1), "limit_secs": limit,
                   "last_beat": state.last_beat,
                   "progress_count": state.progress_count})


# Precedence: the most acute / most specific first, so a lease that matches
# several pathologies is reported as the one an operator must act on.
CLASSIFIERS = (
    detect_provider_hang,
    detect_denial_of_wallet,
    detect_progress_free_spend,
    detect_runaway_recursion,
    detect_identical_calls,
    detect_retry_loop,
    detect_verifier_loop,
    detect_stalled_stream,
    detect_queue_starvation,
    detect_deadlock,
)


def classify(state: LeaseState, now: float | None = None) -> Verdict:
    """Run every classifier in precedence order; the first trip wins. Pure:
    no I/O, no mutation, no cancellation."""
    for fn in CLASSIFIERS:
        verdict = fn(state, now)
        if verdict.tripped:
            return verdict
    return _ok("healthy: progressing (or within every threshold)")


# --- forensics (preserved state) -------------------------------------------

_LOCK = "watchdog-forensics"
_TIMELINE_MAX = 200          # bounded: a runaway loop must not fill the disk


def forensics_dir() -> Path:
    return config.MEMORY_DIR / "watchdog" / "forensics"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(name))[:80] or "lease"


def forensics_path(lease_id: str) -> Path:
    return forensics_dir() / f"{_safe(lease_id)}.json"


def _capture(where: str, err: BaseException, context: str = "") -> None:
    """Best-effort error capture that itself cannot raise."""
    try:
        from . import errors
        errors.capture(where, err, context=context)
    except Exception:
        pass


def write_forensics(state: LeaseState, verdict: Verdict, *,
                    timeline: list | None = None,
                    outcome: str = "") -> Path | None:
    """Preserve the lease's state for the operator. NEVER RAISES — forensics is
    the step that runs *because* something already went wrong, so a failure
    here (wedged proclock, read-only disk, unserialisable payload) is captured
    and swallowed. Returns the path written, or None."""
    try:
        record = {
            "schema_version": 1,
            "lease_id": state.lease_id,
            "written_at": time.time(),
            "mode": mode(),
            "outcome": outcome,
            "verdict": verdict.to_dict(),
            "reason": verdict.reason,
            "operator_action": verdict.operator_action,
            "state": state.to_dict(),
            "spend_usd": round(state.spend_usd, 6),
            "progress_signals_accepted": state.progress_count,
            "last_signals": list(timeline or [])[-10:],
            "timeline": list(timeline or [])[-_TIMELINE_MAX:],
            "thresholds": thresholds(),
        }
        blob = json.dumps(record, indent=1, default=str)
    except Exception as err:                       # noqa: BLE001 - never raise
        _capture("watchdog.forensics.encode", err, context=str(state.lease_id))
        return None
    path = forensics_path(state.lease_id)
    try:
        from . import proclock
        with proclock.lock(_LOCK, timeout=10.0):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            from . import atomicio
            atomicio.publish(tmp, path, blob)
        return path
    except Exception as err:                       # noqa: BLE001 - never raise
        # A wedged/broken lock must not cost us the evidence AND the process:
        # try one unguarded write, then give up quietly.
        _capture("watchdog.forensics", err, context=str(state.lease_id))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(blob, encoding="utf-8")
            return path
        except Exception:
            return None


def read_forensics(lease_id: str) -> dict | None:
    """The preserved record for a lease, or None. Never raises."""
    try:
        return json.loads(forensics_path(lease_id).read_text(encoding="utf-8"))
    except Exception:
        return None


# --- quarantine sink (LAZY, optional) --------------------------------------

# The experiments-registry entry this module belongs to (`experiments.json`,
# declared by W2-PR3 alongside the OLYMPUS_WATCHDOG flag). Quarantine records
# land against it in the append-only retest ledger.
EXPERIMENT_ID = "watchdog-progress-leases"


def quarantine(state: LeaseState, verdict: Verdict) -> bool:
    """Record a repeated failure pattern in the experiments registry.

    `olympus.experiments` (W2-PR3) is an OPTIONAL dependency of this module: it
    may be absent (an older tree, a rollback) or may still be changing shape, so
    the import is LAZY and every failure mode — module absent, import error,
    missing/renamed function, a raising sink — degrades to `False`. The
    watchdog's own ladder must never depend on an optional module being
    present, and quarantining evidence must never be able to break a
    cancellation.

    Two sink shapes are accepted, in order: a dedicated
    `experiments.quarantine(pattern, reason=, evidence=)` if the registry ever
    grows one, else the registry's actual retest sink
    `experiments.record_test(EXPERIMENT_ID, outcome=, evidence=)`."""
    try:
        from . import experiments            # noqa: PLC0415 - deliberately lazy
    except ImportError:
        return False
    except Exception as err:                 # noqa: BLE001 - never raise
        _capture("watchdog.quarantine.import", err, context=state.lease_id)
        return False
    if experiments is None:
        return False
    pattern = f"watchdog:{verdict.kind}"
    evidence = json.dumps(
        {"pattern": pattern, "lease_id": state.lease_id, "kind": state.kind,
         "verdict": verdict.to_dict(), "state": state.to_dict()},
        sort_keys=True, default=str)
    try:
        sink = getattr(experiments, "quarantine", None)
        if callable(sink):
            try:
                sink(pattern, reason=verdict.reason, evidence=evidence)
            except TypeError:                # a sink with a narrower signature
                sink(pattern)
            return True
        record = getattr(experiments, "record_test", None)
        if callable(record):
            record(getattr(experiments, "WATCHDOG_ENTRY_ID", EXPERIMENT_ID),
                   outcome=f"{verdict.kind}: {verdict.reason}"[:500],
                   evidence=evidence)
            return True
    except Exception as err:                 # noqa: BLE001 - never raise
        _capture("watchdog.quarantine", err, context=state.lease_id)
    return False


# --- the lease -------------------------------------------------------------

class ProgressLease:
    """One unit of work (a run, a heartbeat job, a stream) under supervision.

    Created with an id, a deadline and a progress cursor; fed `beat()` as work
    advances; classified by `check()`; ended by `close()`.

    Thread-safe (the orchestrator dispatches specialists across a thread pool,
    so a lease is beaten from several threads).
    """

    def __init__(self, lease_id: str, *, deadline_secs: float | None = None,
                 kind: str = "run", stream: bool = False, queued: bool = False,
                 release_fn=None, warn_fn=None, clock=None):
        self.lease_id = str(lease_id)
        self.kind = str(kind)
        self._clock = clock or time.time
        self.created = float(self._clock())
        self.deadline = (self.created + float(deadline_secs)
                         if deadline_secs else None)
        self._stream = bool(stream)
        self._queued = bool(queued)
        self._admitted_at: float | None = None if queued else self.created
        # Injected by the CALLER. This module knows nothing about usage.slot
        # internals — releasing an admission slot is the caller's contract.
        self._release_fn = release_fn
        self._warn_fn = warn_fn
        self._lock = threading.RLock()

        self._last_beat = self.created
        self._last_progress: float | None = None
        self._progress_count = 0
        self._progress_amount = 0.0
        self._non_progress_count = 0
        self._spend = 0.0
        self._spend_since_progress = 0.0
        self._first_spend_at: float | None = None
        self._retry_counts: dict[str, int] = {}
        self._call_counts: dict[str, int] = {}
        self._tool_depth = 0
        self._verify_cycles = 0
        self._stream_deltas = 0
        self._last_stream_delta: float | None = None
        self._open_call_started: float | None = None
        self._longest_call = 0.0
        self._timeline: list[dict] = []

        self.closed = False
        self.outcome = ""
        self.cancelled = False
        self.refused = False
        self.refusal_reason = ""
        self.released = False
        self.quarantined = False
        self.warnings: list[Verdict] = []
        self._terminal: Verdict | None = None

    # -- recording ---------------------------------------------------------
    def _now(self, now: float | None = None) -> float:
        return float(self._clock()) if now is None else float(now)

    def _note(self, event: str, **fields) -> None:
        self._timeline.append({"t": round(self._now() - self.created, 3),
                               "event": event, **fields})
        if len(self._timeline) > _TIMELINE_MAX * 2:
            del self._timeline[:-_TIMELINE_MAX]

    @property
    def timeline(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._timeline]

    def beat(self, signal: str, amount: float = 1.0, *, spend: float = 0.0,
             now: float | None = None) -> bool:
        """Record one unit of work. Returns True if it counted as PROGRESS.

        `signal` counts only if it is in PROGRESS_SIGNALS (W2-I6.1): anything
        else — `model_text` above all — is recorded for forensics and advances
        the progress cursor by NOTHING. `spend` is recorded in either case,
        which is precisely how progress-free spend becomes visible.

        A terminal lease (refused/cancelled/closed) ignores beats and returns
        False: continuing a refused lease is the unsafe continuation the ladder
        exists to prevent."""
        with self._lock:
            ts = self._now(now)
            if self._terminal is not None or self.closed:
                self._note("beat_rejected", signal=str(signal)[:60],
                           reason="lease is terminal")
                return False
            self._last_beat = ts
            if spend:
                self._spend += float(spend)
                self._spend_since_progress += float(spend)
                if self._first_spend_at is None:
                    self._first_spend_at = ts
            progress = is_progress(signal)
            if progress:
                self._progress_count += 1
                self._progress_amount += float(amount)
                self._last_progress = ts
                self._spend_since_progress = 0.0
            else:
                self._non_progress_count += 1
            self._note("beat", signal=str(signal)[:60], progress=progress,
                       amount=float(amount), spend=round(float(spend), 6))
            return progress

    def record_retry(self, key: str, now: float | None = None) -> int:
        """Count one retry attempt under `key`; returns the new count."""
        with self._lock:
            k = str(key)[:120]
            self._retry_counts[k] = self._retry_counts.get(k, 0) + 1
            self._last_beat = self._now(now)
            self._note("retry", key=k, count=self._retry_counts[k])
            return self._retry_counts[k]

    def record_call(self, fingerprint: str, now: float | None = None) -> int:
        """Count one request by content fingerprint; returns the new count."""
        with self._lock:
            fp = str(fingerprint)[:120]
            self._call_counts[fp] = self._call_counts.get(fp, 0) + 1
            self._last_beat = self._now(now)
            self._note("call", fingerprint=fp, count=self._call_counts[fp])
            return self._call_counts[fp]

    def record_tool_depth(self, depth: int) -> None:
        """Report the CURRENT tool-nesting depth (the lease keeps the max)."""
        with self._lock:
            self._tool_depth = max(self._tool_depth, int(depth))
            self._note("tool_depth", depth=int(depth))

    def record_verify_cycle(self) -> int:
        """Count one verify->rework cycle; returns the new count."""
        with self._lock:
            self._verify_cycles += 1
            self._note("verify_cycle", count=self._verify_cycles)
            return self._verify_cycles

    def record_stream_delta(self, now: float | None = None) -> None:
        """A stream delta arrived. NOTE: this is liveness, not progress — it
        resets only the STALLED_STREAM clock (W2-I6.1)."""
        with self._lock:
            self._stream_deltas += 1
            self._last_stream_delta = self._now(now)
            self._note("stream_delta", n=self._stream_deltas)

    def call_started(self, now: float | None = None) -> None:
        with self._lock:
            self._open_call_started = self._now(now)
            self._note("call_started")

    def call_finished(self, now: float | None = None) -> None:
        with self._lock:
            if self._open_call_started is not None:
                self._longest_call = max(
                    self._longest_call, self._now(now) - self._open_call_started)
            self._open_call_started = None
            self._note("call_finished",
                       longest_secs=round(self._longest_call, 3))

    def admit(self, now: float | None = None) -> None:
        """The lease left the queue and holds an admission slot."""
        with self._lock:
            self._admitted_at = self._now(now)
            self._queued = False
            self._note("admitted")

    # -- classification ----------------------------------------------------
    def snapshot(self, now: float | None = None) -> LeaseState:
        with self._lock:
            return LeaseState(
                lease_id=self.lease_id, now=self._now(now),
                created=self.created, kind=self.kind, deadline=self.deadline,
                queued=self._queued, admitted_at=self._admitted_at,
                last_beat=self._last_beat, last_progress=self._last_progress,
                progress_count=self._progress_count,
                progress_amount=self._progress_amount,
                non_progress_count=self._non_progress_count,
                spend_usd=self._spend,
                spend_since_progress=self._spend_since_progress,
                first_spend_at=self._first_spend_at,
                retry_counts=dict(self._retry_counts),
                call_counts=dict(self._call_counts),
                tool_depth=self._tool_depth,
                verify_cycles=self._verify_cycles,
                stream=self._stream, stream_deltas=self._stream_deltas,
                last_stream_delta=self._last_stream_delta,
                open_call_started=self._open_call_started,
                longest_call_secs=self._longest_call,
                closed=self.closed)

    def check(self, now: float | None = None) -> Verdict:
        """Classify the lease and run the action ladder for the current mode.

        `off`     -> inert: no classification, no files, always OK.
        `observe` -> classify + warn + preserve forensics. Never cancels.
        `enforce` -> the full ladder (warn, cancel, forensics, refuse, release,
                     quarantine), after which the verdict is TERMINAL.
        """
        with self._lock:
            if self._terminal is not None:
                return self._terminal
            current = mode()
            if current == OFF:
                return _ok("watchdog off (OLYMPUS_WATCHDOG=off)")
            state = self.snapshot(now)
            verdict = classify(state, now)
            if not verdict.tripped:
                return verdict
            self._act(verdict, state, current)
            return verdict

    # -- the action ladder -------------------------------------------------
    def _act(self, verdict: Verdict, state: LeaseState, current: str) -> None:
        # 1. warn — always, in every non-off mode.
        self.warnings.append(verdict)
        self._note("warn", kind=verdict.kind, reason=verdict.reason[:200])
        if self._warn_fn is not None:
            try:
                self._warn_fn(verdict)
            except Exception as err:             # noqa: BLE001 - never raise
                _capture("watchdog.warn_fn", err, context=self.lease_id)
        if current == OBSERVE:
            # 3. preserve forensic state — observe records, and stops there.
            self._note("forensics", mode=OBSERVE)
            write_forensics(state, verdict, timeline=self._timeline,
                            outcome="observed")
            return
        # 2. cancel
        self.cancelled = True
        self._note("cancel", kind=verdict.kind)
        # 3. preserve forensic state (before anything can lose it)
        write_forensics(state, verdict, timeline=self._timeline,
                        outcome="cancelled")
        # 4. refuse unsafe continuation (terminal)
        self.refuse_continuation(verdict.reason, verdict=verdict)
        # 5. release admission slots (caller-supplied; exactly once)
        self._release()
        # 6. quarantine the repeated failure pattern (optional sink)
        self.quarantined = quarantine(state, verdict)
        self._note("quarantine", recorded=self.quarantined)
        # 7. the reason AND the operator action are already on the Verdict,
        #    and are re-written into the forensics file with the close outcome.
        write_forensics(state, verdict, timeline=self._timeline,
                        outcome="cancelled")

    def _release(self) -> None:
        """Release the admission slot EXACTLY ONCE, via the injected callable.

        Only the cancel ladder calls this: a lease that ends normally leaves
        slot release to whoever acquired it (`with usage.slot(): ...`). A
        raising `release_fn` is captured, never propagated — the ladder must
        not die between refusing and quarantining."""
        if self.released:
            return
        if self._release_fn is None:
            self._note("slot_release_skipped", reason="no release_fn supplied")
            return
        self.released = True
        try:
            self._release_fn()
            self._note("slot_released")
        except Exception as err:                 # noqa: BLE001 - never raise
            self._note("slot_release_failed", error=str(err)[:120])
            _capture("watchdog.release", err, context=self.lease_id)

    def refuse_continuation(self, reason: str = "",
                            verdict: Verdict | None = None) -> Verdict:
        """Mark the lease UNSAFE TO CONTINUE. Terminal: further beats are
        rejected, `continue_allowed()` is False for good, and `check()` returns
        this verdict forever. Callers must honour it (or call
        `raise_if_terminal()` and be stopped)."""
        with self._lock:
            if self._terminal is not None:
                return self._terminal
            self.refused = True
            self.refusal_reason = str(reason)
            action = (
                "Do not resume this lease. Inspect "
                f"{forensics_path(self.lease_id)} and restart the work from "
                "its last verified state.")
            if verdict is not None:
                final = Verdict(
                    verdict.kind, reason=verdict.reason,
                    operator_action=verdict.operator_action or action,
                    forensics={**verdict.forensics, "refused": True,
                               "lease_id": self.lease_id})
            else:
                final = Verdict(
                    REFUSED, reason=str(reason) or "continuation refused",
                    operator_action=action,
                    forensics={"lease_id": self.lease_id, "refused": True})
            self._terminal = final
            self._note("refuse_continuation", reason=str(reason)[:200])
            return final

    def continue_allowed(self) -> bool:
        """False once the watchdog has refused (or the lease closed)."""
        return not (self.refused or self.cancelled or self.closed)

    def raise_if_terminal(self) -> None:
        if self._terminal is not None:
            raise WatchdogRefusal(
                f"{self.lease_id}: {self._terminal.kind} — "
                f"{self._terminal.reason}")

    # -- end ---------------------------------------------------------------
    def close(self, outcome: str = "ok") -> LeaseState:
        """End the lease. Records the outcome; re-writes forensics (with the
        outcome) if the lease ever tripped and the watchdog is not off."""
        with self._lock:
            self.outcome = str(outcome)
            self._note("close", outcome=self.outcome)
            self.closed = True
            state = self.snapshot()
            if self.warnings and mode() != OFF:
                write_forensics(state, self.warnings[-1],
                                timeline=self._timeline, outcome=self.outcome)
            return state

    def __enter__(self) -> "ProgressLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close("error" if exc_type else "ok")
        return False


def status() -> dict:
    """Operator-facing summary of the watchdog's configuration (no I/O)."""
    return {"mode": mode(), "enabled": enabled(),
            "progress_signals": list(PROGRESS_SIGNALS),
            "non_progress_signals": list(NON_PROGRESS_SIGNALS),
            "detections": list(DETECTION_KINDS),
            "thresholds": thresholds(),
            # W2-PR13: wired into the live path (orchestrator run leases +
            # heartbeat job leases). Inert at the default mode regardless.
            "wired": True,
            "wired_seams": ["orchestrator._pipeline", "heartbeat.tick"]}
