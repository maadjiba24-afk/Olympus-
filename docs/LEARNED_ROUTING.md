# Learned routing — Phase A (instrumentation only)

Olympus picks a model for each specialist with a **static keyword heuristic**
(`config.py:capability_score`, scoring models by name substring). The eventual
moat is a router that learns which model/specialist actually **succeeds** on
which kind of task, from Olympus's own recorded outcomes — data a hosted
competitor cannot access.

That data does not exist yet: nothing links a routing decision to its eventual
outcome. **Phase A builds exactly that link and a readiness counter, and changes
nothing about how routing works.** It is a passive sensor: it records, it does
not decide.

> ⛔ **Phase B (the learned selector) is not built and must not be built** until
> the data gate below is MET **from real adoption**. A selector trained on
> little/no data is strictly worse than the current heuristic. `olympus
> routing-stats` is the gate check.

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
| `signal_source` | which tier produced the signal (`feedback` / `review` / `action` / `none`). |
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
3. **An action outcome** from `outcomes.py` (`approved` → positive,
   `approved_after_edit` → its own label, `rejected`/`undone` → negative) — the
   lowest tier, used when nothing stronger exists.

`resolve_signal()` implements this order; `record_run()` emits the review-tier
signal, and `apply_feedback()` later upgrades it to the feedback tier.

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

- It does **not** change which model is selected — `capability_score`,
  `ModelPool.for_role`/`for_specialist`, and the SPEC-02 sovereign eligibility
  filter behave byte-for-byte as before (proven by a regression test).
- It does **not** train, fit, or consult any model to route.
- A telemetry failure is swallowed and **never breaks a run**.

Phase B remains unbuilt until `olympus routing-stats` shows the gate MET from
real usage.
