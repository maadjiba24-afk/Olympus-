# Design — Hard Output Contracts (Primitive)

**Status:** implemented (`olympus/contracts.py`, wired in `orchestrator.py`; **ON by default** since ADR 0005 hardening — `OLYMPUS_CONTRACTS=off` is the kill switch) · **Scope:** deliberately minimal · **Depends on:** nothing new
**Note (ADR 0005):** this doc was written for the original off-by-default rollout; the flag now defaults **on** — enforcement mechanisms never ship dormant, and the shipped contracts encode already-true invariants, so enabling changes no happy-path behavior. Part 7 below preserves the original design rationale; the live default is `on`.
**Companion doc:** `DESIGN_BOUNDARY_LAYER.md` (phase two — do not read into this one)

---

## Part 0 — What this is, and what it is deliberately *not*

This document specifies **one** primitive: a hard, pre-emission contract that a
specialist's output must satisfy before it is allowed to leave the specialist
and re-enter the orchestrator. A violation **fails closed** (the turn is
replaced with a typed failure string), and the pass/fail is **recorded into the
audit trail that already exists**.

It is scoped to compete with nothing. In particular it is **off by default**,
adds **no new service, no new file format, no config DSL, no dependency**, and
touches the smallest possible surface of the codebase. It is dormant code until
an operator deliberately turns it on. That is the whole point: the distribution
milestone (OpenAI-compatible endpoint + frictionless install) must not slow down
because this exists.

### Explicitly out of scope (these belong to the boundary layer, phase two)

- Data-class tagging / egress classes.
- Provenance stamping of inputs.
- Tiered policy engine (hard vs. soft constraints).
- Anything that touches *tool inputs* or *ingestion*. This primitive only
  governs a specialist's **final text output**.

If you find yourself adding any of the above while implementing this, **stop** —
you are building phase two early, and you are reintroducing the scope creep this
doc was written to prevent.

---

## Part 1 — The single decisive finding that shapes everything

The codebase **already has a signed, re-executable decision log.** It is not a
TODO; it is built and tested.

`olympus/trace.py` defines a `Trace` object that records, per run, a list of
**decision records** — each pairing a rationale with a content-addressed
reference to the exact LLM call that produced it — and on `flush()` signs the
decision path with the witness root-of-trust (`witness.sign_log`, Ed25519) so
the trail is tamper-evident (`trace.py` lines 101–121). The orchestrator already
writes `route`, `plan`, and `review` decisions into it (`orchestrator.py` lines
408, 421, 455).

**Therefore the contract primitive MUST NOT invent its own log.** A contract
check is, definitionally, an orchestration decision ("was this specialist's
output admissible?"). It belongs in the same `Trace` as a new
`decision_type="contract"` record. Building a parallel `contracts.jsonl` would
give Olympus two competing sources of truth, break the single-signature
guarantee, and violate the project's own "no dangling threads" standard.

This is the difference between a spec written from the README (which lists 14
files and "~2.5K lines" and would have invented a new log) and one written from
the source (207 files, ~90 modules, an existing Ed25519-signed decision log).

---

## Part 2 — The exact attachment point

There is **one** function through which every specialist's final output passes,
and it already has failure isolation around it:

`orchestrator.py`, `_run_one` (lines 328–339):

```python
def _run_one(self, key: str, task: str) -> str:
    """Run a single specialist with failure isolation, on its best model."""
    memory.set_user(self.user)  # worker threads get their own context
    try:
        return SPECIALISTS[key].run(task, settings=self.pool.for_specialist(key))
    except replaystore.ReplayDivergence:
        raise                       # never mask a replay divergence
    except Exception as err:
        self.report(f"⚠️ {SPECIALISTS[key].name} failed: {str(err)[:120]}")
        return (f"[{SPECIALISTS[key].name} could not complete this task: "
                f"{err}. Treat this part as missing and answer from the "
                "other specialists.]")
```

Both dispatch paths funnel through here: `_dispatch` (rework, line 347) and
`_dispatch_dag` (the normal DAG path, line 390) both call `self._run_one(...)`.
So **`_run_one` is the single chokepoint.** Clamp here and you cover every
specialist invocation, parallel or serial, first-pass or rework, with no
per-call-site edits.

This mirrors a precedent that already exists one layer down. In `agent.py`
(lines 112–113), tool *output* is already passed through a designated clamp:

```python
content = str(output)
if security.should_wrap(block.name):
    content = security.wrap_untrusted(content, source=block.name)
```

The contract primitive is the same architectural move — a designated clamp at a
known chokepoint — applied one level up, to specialist output instead of tool
output. We are not introducing a new *kind* of thing; we are reusing an
established pattern.

### Why `_run_one` and not `specialists.py::Specialist.run`

`Specialist.run` (specialists.py lines 84–91) is provider-agnostic and has no
access to the `Trace`. `_run_one` is a method on the orchestrator, which owns the
`Trace` and the `report()` channel. The contract check needs both (to record the
decision and to surface a failure to the user), so it lives in `_run_one`.
`Specialist.run` stays untouched.

---

## Part 3 — The contract model

A contract is a small frozen dataclass attached to a `Specialist`. Three checks,
no more, each independently skippable:

| Check | Field | Semantics |
|---|---|---|
| Size ceiling | `max_chars: int \| None` | Output length in characters must be `<= max_chars`. `None` = no limit. (Characters, not tokens — stdlib-only, deterministic, no tokenizer dependency. A token estimate ≈ `chars // 4` already exists in the orchestrator at line 542 if a token-framed message is wanted in the report.) |
| Schema-or-fail | `must_be_json: bool` + `json_schema: dict \| None` | If `must_be_json`, the output must parse as JSON. If `json_schema` is also set, the parsed object must validate against it. Validation is a **minimal stdlib check** (required keys + top-level types), *not* a new `jsonschema` dependency. |
| Tool-call cap | `max_tool_calls: int \| None` | The number of client-side tool calls the specialist made this run must be `<= max_tool_calls`. Requires a count to be threaded out of `agent.run_agent` (see Part 6, the one signature change). `None` = no limit. |

```python
@dataclass(frozen=True)
class OutputContract:
    max_chars: int | None = None
    must_be_json: bool = False
    json_schema: dict | None = None
    max_tool_calls: int | None = None

    def is_noop(self) -> bool:
        return (self.max_chars is None and not self.must_be_json
                and self.json_schema is None and self.max_tool_calls is None)
```

A specialist with no contract, or a no-op contract, costs **zero** — the check
function returns immediately (Part 4, step 1). This preserves the
"empty thing costs nothing" discipline the profile card and memory system
already follow.

### Where the contract is declared

Add **one optional field** to the existing frozen `Specialist` dataclass
(specialists.py, after line 31):

```python
    contract: OutputContract | None = None
```

Every one of the 12 existing specialist entries (specialists.py lines 97–191)
keeps working unchanged — the field defaults to `None`, i.e. no contract, i.e.
no behavior change. Contracts are added opt-in, one specialist at a time, when
someone wants one. **Ship with all 12 at `None`.** The primitive is live code
with zero active contracts on day one.

---

## Part 4 — The check function (the heart of it)

New module `olympus/contracts.py`. Pure, no I/O, fully unit-testable in
isolation:

```python
"""Hard output contracts: a specialist's final output must satisfy its
contract before the orchestrator accepts it. A violation fails CLOSED.

Enforcement is gated by config.contracts_enabled() and is off by default, so
this module is inert until an operator turns it on. It records each check as a
`contract` decision in the existing Trace (trace.py) — never a separate log.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class OutputContract:
    max_chars: int | None = None
    must_be_json: bool = False
    json_schema: dict | None = None
    max_tool_calls: int | None = None

    def is_noop(self) -> bool:
        return (self.max_chars is None and not self.must_be_json
                and self.json_schema is None and self.max_tool_calls is None)


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    violations: tuple[str, ...] = ()      # human-readable, for the trace + report


def _check_schema(obj, schema: dict) -> list[str]:
    """Minimal, dependency-free structural check: required keys + top-level
    JSON-type of named properties. NOT full JSON Schema — deliberately small.
    """
    problems: list[str] = []
    if schema.get("type") == "object" and not isinstance(obj, dict):
        return [f"expected a JSON object, got {type(obj).__name__}"]
    for key in schema.get("required", []):
        if not isinstance(obj, dict) or key not in obj:
            problems.append(f"missing required key: {key!r}")
    _PY = {"string": str, "number": (int, float), "integer": int,
           "boolean": bool, "object": dict, "array": list}
    for key, spec in (schema.get("properties") or {}).items():
        if isinstance(obj, dict) and key in obj and "type" in spec:
            want = _PY.get(spec["type"])
            if want and not isinstance(obj[key], want):
                problems.append(
                    f"key {key!r} should be {spec['type']}, "
                    f"got {type(obj[key]).__name__}")
    return problems


def check(output: str, contract: OutputContract | None,
          *, tool_calls: int | None = None) -> ContractResult:
    """Evaluate `output` against `contract`. Pure; no side effects."""
    if contract is None or contract.is_noop():
        return ContractResult(ok=True)

    violations: list[str] = []

    if contract.max_chars is not None and len(output) > contract.max_chars:
        violations.append(
            f"output is {len(output)} chars, limit is {contract.max_chars}")

    if contract.must_be_json or contract.json_schema is not None:
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            violations.append("output is not valid JSON")
        else:
            if contract.json_schema is not None:
                violations.extend(_check_schema(parsed, contract.json_schema))

    if (contract.max_tool_calls is not None and tool_calls is not None
            and tool_calls > contract.max_tool_calls):
        violations.append(
            f"made {tool_calls} tool calls, limit is {contract.max_tool_calls}")

    return ContractResult(ok=not violations, violations=tuple(violations))
```

Design notes that matter:

- **`check` is pure.** It does no logging and reads no config. The orchestrator
  decides whether to call it (config gate) and what to do with the result
  (record + fail). This keeps the testable core free of environment and I/O,
  the same separation `security.py` keeps.
- **Schema validation is intentionally shallow.** Required keys + top-level
  types catches the realistic failure ("the agent returned prose instead of the
  JSON object we promised a caller") without dragging in `jsonschema`. If a
  future need is real, swapping `_check_schema` for the library is a one-function
  change behind a stable interface. Do not pre-build it.
- **`tool_calls` is optional in the signature** so the function is usable and
  testable before the Part 6 signature change lands. If the count isn't
  available, the tool-call check simply doesn't fire.

---

## Part 5 — Wiring it into `_run_one` (fail-closed + record)

The enforcement site. `_run_one` becomes:

```python
def _run_one(self, key: str, task: str) -> str:
    """Run a single specialist with failure isolation, on its best model."""
    memory.set_user(self.user)  # worker threads get their own context
    try:
        output = SPECIALISTS[key].run(task, settings=self.pool.for_specialist(key))
    except replaystore.ReplayDivergence:
        raise                       # never mask a replay divergence
    except Exception as err:
        self.report(f"⚠️ {SPECIALISTS[key].name} failed: {str(err)[:120]}")
        return (f"[{SPECIALISTS[key].name} could not complete this task: "
                f"{err}. Treat this part as missing and answer from the "
                "other specialists.]")

    # --- hard output contract (off unless enabled) ----------------------
    if config.contracts_enabled():
        spec = SPECIALISTS[key]
        result = contracts.check(output, spec.contract)   # tool_calls: Part 6
        self._tr.decision(
            "contract",
            {"name": spec.name, "role": "specialist", "key": key},
            {"violations": list(result.violations)},
            status="ok" if result.ok else "violation",
            inputs=task)
        if not result.ok:
            reasons = "; ".join(result.violations)
            self.report(
                f"⛔ {spec.name}'s output failed its contract ({reasons}).")
            return (f"[{spec.name}'s output was rejected by its output "
                    f"contract: {reasons}. Treat this part as missing and "
                    "answer from the other specialists.]")
    return output
```

Why this shape:

- **Fail-closed, but degrade gracefully.** A violation does not crash the run;
  it returns the *same typed "treat this part as missing" string* the existing
  exception path already returns (lines 337–339). The DAG and verifier already
  know how to handle a specialist that came back empty — we reuse that contract
  with the rest of the pipeline instead of inventing a new failure mode. This is
  why the primitive is safe to drop into a multi-specialist flow: one
  specialist's rejected output is already a survivable condition.

- **The decision is recorded whether it passes or fails.** `status="ok"` vs.
  `status="violation"`. Note `trace.py` line 75 makes `status` a required
  argument *precisely so a failure path can't silently record success* — we
  honor that: a violation records `"violation"`, not `"ok"`. This is what makes
  "Olympus can prove its outputs were contract-checked" a real, signed,
  replayable claim rather than a slogan.

- **`self._tr`** — `_run_one` needs the run's `Trace`. The orchestrator already
  threads `tr` through `_pipeline` and `_dispatch_dag`; `_run_one` must reach the
  current run's trace. Two options, pick the smaller one when you see the
  surrounding code: (a) store it as `self._tr` at the top of `_pipeline` (line
  ~404) and read it here, or (b) pass `tr` through `_dispatch`/`_dispatch_dag`
  into `_run_one`. (a) is fewer signatures touched; (b) is more explicit and
  avoids per-instance mutable state across threads. **Recommendation: (b)** —
  `_run_one` is called from worker threads (lines 347, 390), and threading `tr`
  as a parameter is safer than sharing `self._tr` across a ThreadPoolExecutor.
  The `Trace` object's `.decision()` is already lock-guarded (trace.py lines
  97–98), so concurrent appends from parallel specialists are safe.

---

## Part 6 — The one signature change (for the tool-call cap only)

The size and schema checks need nothing new. The **tool-call cap** needs to know
how many client-side tool calls a specialist made, and that number currently
lives only inside `agent.run_agent`'s loop and is discarded.

`agent.py::run_agent` (lines 31–77) counts tool-use blocks implicitly at line
70–71 but returns only the final string (line 66 / 74). To support the cap,
thread the count out. The minimal, backward-compatible way:

- Keep `run_agent` returning `str` by default (every existing caller is
  unchanged).
- Add an internal counter in the loop and expose it via an **optional**
  out-parameter or a sibling function `run_agent_counted(...) -> tuple[str, int]`
  that `run_agent` wraps. `backend.run_agent` (backend.py line 38) and
  `Specialist.run` (specialists.py line 88) then need to forward the count only
  on the Anthropic path.

**Scope discipline:** if threading the count cleanly touches more than
`agent.py`, `backend.py`, and `specialists.py`, **ship the primitive without the
tool-call cap** (max_chars + schema only) and add the cap when the boundary
layer lands — it needs the same plumbing anyway. The cap is the *least* load-
bearing of the three checks for a v1; do not let it block the other two. This is
the one place where "complete" yields to "scoped": two checks shipped beats three
checks stalled.

---

## Part 7 — The config gate, kill-switch, and default

Follow the **exact** env-flag convention already in `config.py` (the
`fast_mode` / `require_byok` pattern, lines 336–347):

> **Superseded by ADR 0005:** the flag now defaults **on**. The current
> implementation is:
>
> ```python
> def contracts_enabled() -> bool:
>     """... ON BY DEFAULT (ADR 0005 hardening). OLYMPUS_CONTRACTS=off kill switch."""
>     return os.environ.get("OLYMPUS_CONTRACTS", "on").strip().lower() not in (
>         "0", "off", "false", "no")
> ```
>
> The original off-by-default design is retained below for its rationale.

```python
def contracts_enabled() -> bool:
    """Enforce hard output contracts on specialist outputs (OLYMPUS_CONTRACTS=1).
    OFF BY DEFAULT: contracts are inert until an operator opts in, so the
    feature can't surprise a fresh install or a public BYOK instance."""
    return os.environ.get("OLYMPUS_CONTRACTS", "").strip().lower() in (
        "1", "true", "yes", "on")
```

- **Default (original design was off; now on per ADR 0005).** Three layers of
  "this won't bite a new user" still hold: even when on, a specialist with
  `contract=None` is a no-op; and even a violation degrades to "treat this part
  as missing" rather than crashing.
- **Kill-switch is the same flag.** Unset `OLYMPUS_CONTRACTS` (or set it to `0`)
  and all enforcement vanishes instantly, no redeploy of agent code. That is the
  operability requirement for a security primitive on a system whose milestone is
  frictionless install.
- **Per-specialist opt-out is implicit:** set that specialist's `contract` back
  to `None`. No separate disable list to maintain.

---

## Part 8 — Replay safety (do not skip this)

Olympus's decision log is *re-executable*: `trace.py` `canonical_log` /
`diff_decisions` (lines 132–180) prove a recorded run replays byte-identically,
and `replaygate.py` gates on it in CI (`.github/workflows/replay-gate.yml`
exists). A new decision record type **will change the canonical decision path**,
so:

1. **`contract` records must be deterministic under replay.** `contracts.check`
   is pure and depends only on the output string and the static contract, both
   of which are already frozen/reproduced in replay. Good — no nondeterminism
   introduced.
2. **The record's volatile fields must already be excluded.** `_VOLATILE`
   (trace.py line 29) drops `record_id`, `ts`, `duration_ms`, etc. The
   `contract` record uses only those volatile fields plus deterministic content
   (`decision_type`, `agent`, `rationale`, `status`), so its *core*
   (`decision_core`, line 126) is replay-stable. Verify this with the replay
   test below before merging.
3. **Enforcement state is part of the run.** A run recorded with
   `OLYMPUS_CONTRACTS=1` and replayed with it unset would diverge (the contract
   decisions vanish). Treat the flag as run metadata: record whether contracts
   were enabled in `tr.meta` (trace.py line 45/107) so a replay can set the same
   mode. This is the one genuinely easy-to-miss interaction — call it out in the
   PR.

---

## Part 9 — Tests (the definition of done)

New file `tests/test_output_contracts.py`. The bar is the project's own
"holy shit, that's done": the primitive ships with tests and the replay
invariant is proven, not assumed.

Pure-unit (no orchestrator):

1. `contract=None` and a no-op contract → `ok=True`, zero violations.
2. `max_chars`: output one under / exactly at / one over the limit.
3. `must_be_json`: valid JSON passes; prose fails with the right message.
4. `json_schema`: missing required key fails; wrong top-level type fails;
   correct object passes; a nested/irrelevant key is ignored (shallow by design).
5. `max_tool_calls`: under/at/over, and `tool_calls=None` → check doesn't fire.
6. Multiple simultaneous violations → all reported in `violations`.

Integration (orchestrator-level, mirroring `tests/test_orchestrator.py` and
`tests/test_dag.py` style):

7. With `OLYMPUS_CONTRACTS` unset, a contract-violating specialist output is
   returned **unchanged** (enforcement off by default — this is the
   distribution-safety guarantee, test it explicitly).
8. With `OLYMPUS_CONTRACTS=1` and a specialist whose contract it violates,
   `_run_one` returns the typed "rejected by its output contract" string and the
   rest of the pipeline still completes from the other specialists.
9. A `contract` decision record is appended with `status="violation"` on failure
   and `status="ok"` on pass.

Replay (the one that protects the existing guarantee):

10. Record a run with `OLYMPUS_CONTRACTS=1`, replay it, assert
    `diff_decisions(original, fresh) == []` — i.e. adding contract records did
    not break byte-identical replay. This is the gate that must be green before
    merge; if it's red, Part 8 was gotten wrong.

---

## Part 10 — Why this is a moat seed, not just a feature

Stated plainly so the strategic point isn't lost in the engineering: a contract
check on its own is copyable in an afternoon. What is **not** copyable is the
combination this rides on — a self-hosted system (you see and control the full
execution, so you *can* clamp every specialist boundary) plus an Ed25519-signed,
re-executable decision log (so "this output was contract-checked and here is the
signed, replayable proof" is a verifiable claim a hosted competitor structurally
cannot make about a boundary that never leaves their box).

This primitive's real job is to start *populating that signed log with
admissibility decisions.* Each `contract` record is a brick in the wall of
"auditable orchestration." The boundary layer (phase two) widens each brick to
also carry data-class and provenance — but the load-bearing structure is the
signed-log-of-boundary-decisions, and this is the smallest honest thing that
starts building it.

Do not ship it on by default. Do not let the tool-call cap block the other two
checks. Do not give it its own log. Build the three-check `contracts.py`, add the
optional `Specialist.contract` field, clamp `_run_one`, gate it off by default,
prove replay still holds. That is the whole job.
