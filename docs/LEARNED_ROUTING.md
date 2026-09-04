# Learned routing — outcomes instrumentation (Phase A) + the evidence-gated selector (Phase B)

Olympus picks a model for each specialist with a **static keyword heuristic**
(`config.py:capability_score`, scoring models by name substring). The moat is a
router that learns which model actually **succeeds** on which kind of task, from
Olympus's own recorded outcomes — data a hosted competitor cannot access.

- **Phase A** (the sensor) records the link between each routing decision and
  its eventual outcome, and counts readiness. It decides nothing.
- **Phase B** (the selector, `olympus/learned_routing.py`) is implemented but
  ships **dormant and evidence-gated**: it can only override the heuristic when
  the operator opts in (`OLYMPUS_LEARNED_ROUTING=1`) **and** the data gate below
  is met on that deployment **and** the specific decision has statistical
  evidence on *both* candidates. Anything short of that falls back to the
  keyword heuristic, byte-for-byte.

> The original SPEC-04 gate ("do not build Phase B until real adoption produces
> the data") was overridden by an explicit owner instruction to implement Phase
> B in full. The gate's *reason* — a selector trained on little/no data is
> strictly worse than the heuristic — is preserved by moving the gate into the
> runtime: with no data the selector never engages, so shipping it is
> behavior-neutral. `olympus routing-stats` shows both the data gate and the
> selector's live status.

## What is logged (the row schema)

For each delegated run, one row **per dispatched specialist**, stored exactly
like `outcomes.py` (same `store` backend, per-user scoping via `memory.safe_id`,
an append-only list capped at 2000, best-effort writes that never raise):

| field | meaning |
| ----- | ------- |
| `run_id` | the trace id — the join key to the decision log (`trace.py`). |
| `specialist` | which council member handled the task (the routing decision). |
| `model` | the model the routing **actually used** (`pool.for_specialist(key)`, read-only). |
| `role` | the model role it routed on (`config.specialist_role`). |
| `task_type` | a coarse tag grouping the specialist's domain (code / research / finance / …). |
| `length_bucket` | input-length bucket (`xs`/`s`/`m`/`l`/`xl`). |
| `outcome_signal` | `positive` / `negative` / `approved_after_edit` / `pending`. |
| `signal_source` | which tier produced the signal (`feedback` / `review` / `none`). |
| `synthetic` | true for self-generated traffic — excluded from the gate. |
| `ts`, `user` | timestamp and per-user namespace. |

`task_features` is deliberately minimal: `specialist` + `task_type` +
`length_bucket`. Phase A captures signal cheaply; it does **not** do feature
engineering (that would be leaving scope).

## The `outcome_signal` precedence

A routing outcome can be labeled by more than one signal. Highest wins:

1. **Explicit user feedback** (👍/👎 via the CLI or `/api/feedback`) — top tier;
   it upgrades the row for that run when it arrives later.
2. **The verify/review verdict** from the pipeline (`approve` → `positive`,
   `retry` → `negative`) — the default signal emitted at verify/review
   completion.

`resolve_signal()` implements this order; `record_run()` emits the review-tier
signal, and `apply_feedback()` later upgrades it to the feedback tier.

Evidence is accepted only when the complete stored row matches this schema and
the user in the row matches its per-user storage key. Unknown verdicts remain
`pending`; unknown signals, sources, inconsistent task tags, non-finite
timestamps, incomplete rows, and non-list store payloads are excluded from the
gate and every learning consumer. `/api/feedback` rejects a missing or unknown
verdict with HTTP 400 instead of manufacturing positive or negative evidence.

> An action-outcome tier (from `outcomes.py`) was originally envisioned as a
> third, lowest source. It is **not** wired: prepared actions aren't linked to a
> run id in this store, so there's no reliable join. The code and this doc
> reflect only the two sources that are actually emitted. (`approved_after_edit`
> remains a supported signal value the learned selector weights at 0.5, ready
> if such a source is ever added.)

## Privacy posture (identical to `outcomes.py`)

- **Per-user scoped**, same namespace/key discipline, same rolling cap.
- **Features, not content.** A row stores the specialist, model, role, a coarse
  task-type tag, and an input-length *bucket* — **not** the user's prompt or the
  answer. It retains no more raw content than `outcomes.py` already does (which
  is none of the message body).
- **Local telemetry.** It lives in the same local store as the rest of Olympus's
  memory; it is never sent anywhere by Phase A.

## The Phase B data gate (and its justification)

`olympus routing-stats` reports totals, per-task-type and per-(specialist,model)
breakdowns, and a **GATE READINESS** verdict. The gate is MET only when **all**
hold, counting **labeled, non-synthetic** rows only:

| threshold | value | why |
| --------- | ----- | --- |
| labeled real outcomes | **≥ 300** | Below a few hundred labeled examples, per-(specialist, model, task-type) cells are too sparse to beat a static score without overfitting — a learned selector would regress. |
| distinct task-types | **≥ 3** | A selector fit on one task-type doesn't generalize; require spread. |
| distinct real sources | **≥ 2** | One user's traffic is idiosyncratic; a moat needs signal across real adopters, not self-play. |

**Synthetic / self-generated rows never count.** Set
`OLYMPUS_ROUTING_SYNTHETIC=1` for eval runs, load tests, demos, and any
self-generated traffic; replays emit **no** telemetry at all. This is what makes
"real adoption" measurable rather than gameable by our own test traffic.

One-line justification: *~300 labeled outcomes across ≥3 task-types and ≥2 real
sources is the minimum at which a learned selector can plausibly beat the keyword
heuristic instead of regressing it.*

## What Phase A explicitly does NOT do

- It does **not** change which model is selected — the telemetry hooks read the
  deterministic selection, never influence it.
- It does **not** train, fit, or consult any model to route.
- A telemetry failure is swallowed and **never breaks a run**.

## Phase B — the evidence-gated selector

`olympus/learned_routing.py` turns the ledger into a routing preference. It is
**off by default** and layered so that every missing piece of evidence means
"use the heuristic":

### Selection precedence (in `ModelPool.for_specialist`)

```
explicit pin (OLYMPUS_SPECIALIST_MODELS)   — always wins
   > learned selector (this module)        — only under ALL gates below
      > keyword heuristic (capability_score) — the default, and every fallback
```

The SPEC-02 **sovereign filter runs before any of this** (it constrains the
pool's members), so the selector can only ever choose among already-eligible
(e.g. local-only) members — evidence favoring a remote model cannot pull data
off-box.

### Activation gates (ALL required, checked per decision)

1. **Operator opt-in**: `OLYMPUS_LEARNED_ROUTING=1` (default off — routing is
   byte-for-byte the keyword heuristic, proven by regression test).
2. **Not replaying**: under `OLYMPUS_REPLAY` the selector is forced off, so a
   recorded run always replays its recorded decisions even after the ledger
   grows.
3. **The data gate above is met** on this deployment (labeled, non-synthetic,
   multi-task-type, multi-source) — the SPEC-04 gate, enforced at runtime.
4. **Per-cell evidence**: a `(specialist, model)` cell participates only with
   **≥ 25 labeled real outcomes** (`MIN_CELL_SAMPLES`), and the selector
   overrides the heuristic **only when the incumbent's cell is also known and
   strictly worse**. One-sided evidence never displaces the heuristic.

### The statistics (deliberately boring, stdlib-only)

Outcomes are weighted — `positive` = 1.0, `approved_after_edit` = 0.5,
`negative` = 0.0 — and candidates are ranked by the **Wilson score lower bound
(95%)** of their success rate. Wilson's lower bound is pessimistic on small
samples, so a lucky 3-for-3 cell can never outrank a solid 65-for-80 cell. There
is no model fitting, no gradient descent, no new dependency: the "learning" is
honest per-cell success accounting, which at a few hundred to a few thousand
outcomes is *more* defensible than an ML fit.

### Observability & kill switch

`olympus routing-stats` shows the flag, the live active/dormant status, and the
full evidence table (every cell with `n`, rate, Wilson lower bound, and whether
it is large enough to matter). Unset `OLYMPUS_LEARNED_ROUTING` to revert to the
pure heuristic instantly; the ledger keeps accumulating either way. Any internal
selector failure (unreadable ledger, bad rows) silently falls back to the
heuristic — routing can never break on telemetry.
