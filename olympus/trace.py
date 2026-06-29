"""Per-run observability + the re-executable decision log.

Each run gets a trace id; stages (route, plan, specialist, verify, review,
synthesize) append timed events. On top of that, every orchestration *decision*
is recorded as a structured record that pairs the rationale (the verbatim
`_route` / `_plan` / `_review` return) with a content-addressed reference to the
exact LLM request/response that produced it (see `replaystore`). That pairing
is what makes a past run *re-executable*: re-run the real code against the
frozen responses and prove the decision path is byte-identical, or pinpoint the
exact divergence.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager

from . import config

SCHEMA_VERSION = 1

# The run's Trace, published per-thread so deep actuators (e.g. the egress
# gateway, called inside a specialist's tool loop in a worker thread) can record
# a decision into the *current* run's signed log without threading `tr` through
# the whole agent stack. Mirrors memory.current_user(). Default None = no run in
# scope → such a recorder is a no-op. NOTE: ThreadPoolExecutor does not copy
# context to its workers, so the orchestrator sets this inside the worker
# (_run_one), not in _pipeline.
_CURRENT: "contextvars.ContextVar[Trace | None]" = contextvars.ContextVar(
    "olympus_trace", default=None)


def set_current(tr: "Trace | None"):
    """Publish `tr` as the current run's Trace for this context; returns a token
    to pass to reset_current()."""
    return _CURRENT.set(tr)


def reset_current(token) -> None:
    _CURRENT.reset(token)


def current() -> "Trace | None":
    """The current run's Trace, or None if no run is in scope."""
    return _CURRENT.get()

# Fields that legitimately differ between a recorded run and its replay (a new
# run id, fresh record ids, wall-clock timing, re-estimated cost). They're
# excluded when proving the *decision path* is byte-identical.
_VOLATILE = ("record_id", "run_id", "parent_record_id", "duration_ms", "cost", "ts")


def _hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


class Trace:
    def __init__(self, kind: str, user: str = "shared"):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.user = user
        self.events: list[dict] = []
        self.decisions: list[dict] = []
        self.meta: dict = {}            # e.g. the original input, for replay
        self._lock = threading.Lock()
        self._t0 = time.time()

    def event(self, stage: str, **fields) -> None:
        with self._lock:
            self.events.append({
                "t": round(time.time() - self._t0, 3),
                "stage": stage,
                **fields,
            })

    @contextmanager
    def span(self, stage: str, **fields):
        start = time.time()
        self.event(stage + ".start", **fields)
        try:
            yield
        except Exception as err:
            self.event(stage + ".error", error=str(err)[:300])
            raise
        finally:
            self.event(stage + ".end", secs=round(time.time() - start, 2))

    def decision(self, decision_type: str, agent: dict, rationale,
                 *, status: str, parent_record_id: str | None = None,
                 inputs=None, model: dict | None = None,
                 request_hash: str | None = None, response_ref: str | None = None,
                 cost: float = 0.0, error: str = "",
                 duration_ms: int = 0) -> dict:
        # `status` is REQUIRED (no default): a failure path that forgets it must
        # not silently record success — that would poison per-agent trust scoring.
        """Record one orchestration decision. `rationale` is the verbatim
        `_route`/`_plan`/`_review` return; `request_hash`/`response_ref` come
        from `replaystore` (the frozen LLM call that produced the decision)."""
        rec = {
            "schema_version": SCHEMA_VERSION,
            "record_id": uuid.uuid4().hex[:12],
            "run_id": self.id,
            "parent_record_id": parent_record_id,
            "decision_type": decision_type,
            "agent": agent,
            "rationale": rationale,
            "inputs_hash": _hash(inputs) if inputs is not None else None,
            "model": model,
            "model_request_hash": request_hash,
            "model_response_ref": response_ref,
            "cost": round(float(cost), 6),
            "outcome": {"status": status, "error": error},
            "duration_ms": int(duration_ms),
            "ts": round(time.time() - self._t0, 3),
        }
        with self._lock:
            self.decisions.append(rec)
        return rec

    def flush(self) -> None:
        path = config.MEMORY_DIR / "traces" / f"{time.strftime('%Y%m%d')}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": self.id, "kind": self.kind, "user": self.user,
            "total_secs": round(time.time() - self._t0, 2),
            "meta": self.meta,
            "events": self.events,
            "decisions": self.decisions,
        }
        # Sign the decision path with the witness root-of-trust so the audit
        # trail is tamper-evident. Best-effort: if crypto is unavailable the run
        # is still recorded, just unsigned.
        try:
            from . import witness
            if witness.available():
                record["log_signature"] = witness.sign_log(self.decisions)
        except Exception:
            pass
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


# --- reading & comparing recorded runs (used by `olympus replay`/`explain`) --

def decision_core(rec: dict) -> dict:
    """The replay-invariant part of a decision record — everything that
    represents the *reasoning path*, dropping run-specific/volatile fields."""
    return {k: v for k, v in rec.items() if k not in _VOLATILE}


def canonical_log(decisions: list[dict]) -> str:
    """A byte-stable string of a run's decision path (cores in order)."""
    return json.dumps([decision_core(d) for d in decisions],
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_run(run_id: str) -> dict | None:
    """Find a recorded run across the dated trace files (newest first)."""
    base = config.MEMORY_DIR / "traces"
    if not base.exists():
        return None
    for path in sorted(base.glob("*.jsonl"), reverse=True):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") == run_id:
                return rec
    return None


def find_record(record_id: str) -> dict | None:
    """Find a single decision record by id across all recorded runs."""
    base = config.MEMORY_DIR / "traces"
    if not base.exists():
        return None
    for path in sorted(base.glob("*.jsonl"), reverse=True):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for d in rec.get("decisions", []):
                if d.get("record_id") == record_id:
                    return {"run_id": rec.get("id"), "decision": d}
    return None


def diff_decisions(original: list[dict], fresh: list[dict]) -> list[dict]:
    """Compare two decision paths by core. Returns a list of differences
    (position, original core, fresh core); empty list means byte-identical."""
    diffs = []
    for i in range(max(len(original), len(fresh))):
        a = decision_core(original[i]) if i < len(original) else None
        b = decision_core(fresh[i]) if i < len(fresh) else None
        if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
            diffs.append({"index": i, "original": a, "replayed": b})
    return diffs
