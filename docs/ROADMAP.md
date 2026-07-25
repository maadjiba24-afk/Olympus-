# Olympus Engineering Roadmap

**Derived from:** `docs/NORTH_STAR.md` (architecture) as amended by
`docs/NORTH_STAR_REVIEW.md` (adversarial review, verdict: *reject as written*).

**Rule of construction:** no feature appears here that does not trace to a named
NORTH_STAR engine **and** survive the review. Every API below extends a module
that exists today; every data model extends a file Olympus already writes. Where
this roadmap **departs** from NORTH_STAR, the cut is stated with its finding ID —
nothing is silently dropped.

**Scope discipline.** This is written for the organization that actually exists
(small; one to a few engineers) — not the 4–6 specialist teams NORTH_STAR
implicitly assumed (F13). Engines are sequenced so that **each phase ships user-
visible value on its own** and no phase depends on an unsolved research problem.

---

## 0. What was cut, and why

| NORTH_STAR item | Disposition | Finding |
|---|---|---|
| §3.A weight-level fine-tune / DPO loop | **CUT.** Olympus is an API client (`backend.py` dispatches by `(provider, model, api_key, base_url)`); frontier weights are not trainable through an inference API. Engine A is rescoped to what *is* reachable. | F2, F3 |
| §3.C internal compute market / economy | **CUT entirely.** Manufactures the collusion + Goodhart pathologies §8.4 then cures. Agents are subroutines, not self-interested parties. | F6 |
| §3.C-ii cross-instance BFT consensus | **CUT.** One operator's identical instances fail in correlation; BFT assumes independent Byzantine failures. Replaced by replication + staged rollout in Engine E. | F7 |
| §3.B-iii proof-assistant (Lean/Coq) kernel | **CUT.** No path for a ~230-module Python codebase under daily change; proof-maintenance cost unpriced. | F5 |
| §3.E "counterfactual replay falls out for free" | **CUT as stated.** LLM divergence propagates from the first differing token; there is no cheap divergent-subtree replay. Replay is retained for *audit and crash-recovery*, not free experimentation. | F9 |
| §3.D-ii "the durable answer to hallucination" | **DOWNGRADED** to contradiction detection over a typed subset; soundness is bounded by grammar coverage. | F11 |
| "One integration pass from a moat" framing | **DELETED.** ~230 modules with one maintainer is debt, not inventory. | F1, F16 |

**Two rules adopted from the review, binding on every item below:**

1. **Seed-citation rule (F1).** No module may be cited as a foundation without
   reading its code and tests. If a proposal *reverses* a module's documented
   intent (as NORTH_STAR did with `rlscaffold` and `consensus`), that reversal
   must be argued against the original decision record — never counted as
   existing progress.
2. **Gate-cost rule (F8).** Every gate added has a stated wall-clock and API cost
   budget. The measurement substrate is the system's true bottleneck and is
   already its flakiest component; nothing may be gated "for free."

---

## 1. Engine map and dependency order

```
E1 Effect Typing ──┬──> E2 Protocol Verification (spec bounded by E1's vocabulary)
   (foundation)    ├──> E4 Oversight (audit sampling classifies by effect)
                   └──> E5 Knowledge (facts carry effect-typed provenance)
E3 Durable Execution ── independent; unblocks E4 (replayable audit) + E6
E6 Adaptive Loop ── depends on E1 (safe action space) + E3 (trustworthy traces)
```

**Critical path: E1 → everything.** Effect typing is the only item that is
cheap, compounding, buildable now, and a prerequisite for three other engines.
**Nothing depends on an unsolved research problem** — the fix for F4, which
rooted NORTH_STAR's DAG in the open "immutable value core" question.

| Engine | NORTH_STAR origin | Phase | Ships value alone? |
|---|---|---|---|
| E1 Capability-Effect Typing | §9.2, §3.D-iv | 0–12 mo | Yes — CI catches unsafe compositions |
| E2 Protocol Verification | §3.B-i, B-ii | 6–18 mo | Yes — real concurrency bugs found |
| E3 Durable Execution | §3.E | 0–18 mo | Yes — crash-safe resumable runs |
| E4 Scalable Oversight | §3.F | 12–30 mo | Yes — bounded-cost audit guarantee |
| E5 Knowledge Consistency | §3.D-i | 18–36 mo | Yes — contradiction detection |
| E6 Adaptive Loop | §3.A (rescoped) | 24–48 mo | Yes — measurable routing/context gains |

---

## E1 — Capability-Effect Typing

> *NORTH_STAR §9.2 / §3.D-iv, scoped by F12: this is a **declaration-checked
> lint**, not a proof. Static effect inference over Python (`getattr`, dynamic
> dispatch, C extensions) is undecidable; LLM-generated skill effects are runtime
> properties. Claiming "CI proves it for all compositions" was false.*

**Why first.** The effect vocabulary **already exists** in `security.py` as
`ACTION_TOOLS`, `INGESTION_TOOLS`, `TRUSTED_TOOLS` (frozensets over tool names),
and `capabilities.py` already binds a manifest to CI (`check_repo()`). E1 turns
three ad-hoc sets into one declared, checked type lattice.

### Dependencies
`security.py` (existing classification sets), `capabilities.py` (`manifest()`,
`check_repo()`), `tools.HANDLERS`, `codegraph` (call edges, with its documented
precision limits), `capprofile.filter_tools`.

### Data model
Extend the existing `capabilities.json` manifest — no new store:

```jsonc
// capabilities.json → tools[name].effects  (NEW key; manifest is already CI-bound)
{ "name": "web_fetch",
  "effects": {
    "reads":   ["untrusted"],           // ⊒ security.INGESTION_TOOLS
    "writes":  [],                       // "fs:workspace" | "fs:config" | "store"
    "egress":  ["confined:target"],     // "none" | "confined:<scope>" | "open"
    "spends":  ["usd:api"],
    "mutates": []                        // "policy" | "capability" | "skill"
  },
  "declared_by": "hand", "verified_by": "test:test_effects.py::test_web_fetch" }
```

Effects form a **lattice** with a partial order (`none ⊑ confined ⊑ open`), so
composition is a join and containment is a subset check.

### APIs
```python
# olympus/effects.py  (NEW — pure, no deps, mirrors security.py's style)
Effect      = frozenset[str]
def of_tool(name: str) -> dict[str, Effect]      # declared effects, {} if none
def join(*effects: dict) -> dict                  # composition = per-axis union
def within(actual: dict, granted: dict) -> bool   # containment (the typing rule)
def violations(chain: list[str], profile: str) -> list[str]
def undeclared() -> list[str]                     # tools missing a declaration

# olympus/capabilities.py  (EXTEND)
def check_effects() -> list[str]   # joins check_repo(): fail CI on drift
```

### Milestones
- **M1 Prototype (wk 1–6).** `effects.py` + declarations for the ~20 highest-risk
  tools (all of `ACTION_TOOLS` + `INGESTION_TOOLS`). A CI test asserting the core
  invariant: *no tool declaring `reads:untrusted` reaches `egress:open`.*
- **M2 MVP (mo 2–6).** All 130 `tools.HANDLERS` declared; `check_effects()` wired
  into the existing capability CI gate; `capprofile` denies by effect rather than
  by hand-listed name; a drift test fails when a new tool ships undeclared.
- **M3 Production (mo 6–12).** Skill-level effects: `skillpack` import gate
  rejects a skill whose *declared* effects exceed the importing profile's grant.
  Effect deltas surface in `codegraph` impact reports.

### Research required
**None.** Effect systems are 1990s PL technology (Gifford–Lucassen). This is
engineering, which is precisely why it is first.

### Risks
| Risk | Mitigation |
|---|---|
| Declarations drift from reality (F12 — nothing validates them) | Every declaration names a `verified_by` test; undeclared tools fail CI; treat as lint, never claim proof |
| LLM-generated skills untypeable (F12) | Out of scope by construction — those stay under runtime guards; documented, not papered over |
| Annotation burden across 130 tools | Bootstrap from existing `security.py` sets; only the delta is hand-written |

### Success metrics
- 100% of `tools.HANDLERS` carry declarations; **0** undeclared tools in CI.
- ≥1 unsafe composition caught pre-merge that the current review process missed.
- `capprofile` deny-lists derived, not hand-maintained (measurable LOC deletion).
- Gate cost: **< 5 s**, no API calls (pure static check) — satisfies the F8 rule.

---

## E2 — Protocol Verification (bounded)

> *NORTH_STAR §3.B-i/B-ii, scoped by F5: **one** protocol, timeboxed, with a
> pre-committed stop rule. The verified kernel (B-iii) is cut. TLA+ verifies the
> model, not the code — so this is a **design-bug finder**, not a safety proof.*

### Dependencies
E1 (the effect vocabulary is the spec's alphabet), `approvals.py`, `proclock.py`,
`heartbeat.py`, `behavioral_contracts.py`.

### Data model
`specs/approval_spine.tla` + `specs/approval_spine.cfg` — versioned alongside
the code, with a `SPEC_VERSION` constant pinned to the module it models.

### APIs
```python
# scripts/check_specs.py  (NEW — mirrors scripts/check_threat_model.py)
def spec_drift() -> list[str]   # spec's pinned constants vs live code constants
def main() -> int               # 0 pass / 1 drift / 2 model-check failure
```
No runtime API. Verification is a CI artifact, not a library.

### Milestones
- **M1 Prototype (mo 6–9).** TLA+ spec of the **approval spine only** (propose →
  approve → execute → ledger), model-checked locally. **Pre-committed stop rule:**
  if it finds no bug that code review would have missed, *stop here and do not
  proceed to M2.* Written down before starting, to prevent sunk-cost escalation.
- **M2 MVP (mo 9–14).** If M1 passes the stop rule: add the heartbeat-vs-web
  multi-process topology (the ADR 0005 concurrency case, already known-degraded
  on Windows). Model-check in CI on spec change only, not per-commit.
- **M3 Production (mo 14–18).** `check_specs.py` in CI; a spec-drift test fails
  when a modeled constant changes in code without the spec updating — the same
  binding discipline `check_threat_model.py` already enforces for tools.

### Research required
None novel. TLA+ on distributed protocols is standard industrial practice. The
*open* question is empirical and local: **does it pay for itself here?** M1's stop
rule is the experiment that answers it.

### Risks
| Risk | Mitigation |
|---|---|
| Spec/implementation gap makes it theater (F5) | Scope to *protocol* bugs (ordering, races, deadlock) — never claim code correctness |
| Proof/spec maintenance cost as code changes (F5) | Model-check on spec change only; pinned-constant drift test keeps them honest |
| Sunk-cost escalation into a verification program | The M1 stop rule is pre-committed and binding |

### Success metrics
- M1: ≥1 **real** concurrency/ordering defect found that review missed — or an
  explicit, documented **STOP**. Both are successful outcomes.
- Spec-drift test catches ≥1 divergence within 6 months of M3.
- CI cost: **< 3 min**, spec-change-triggered only (F8 rule).

---

## E3 — Durable Execution

> *NORTH_STAR §3.E, minus the false "counterfactual replay for free" claim (F9).
> This is ordinary, high-value systems engineering: crash-safe resumable runs.*

### Dependencies
`ledger.py` (`open_run`, `checkpoint`, `verify_ledger` — a signed WAL already
exists), `replaystore.py`, `hibernate.py` (`run_once`, `next_due_in`),
`proclock.py`, `store.py`.

### Data model
The ledger is already a hash-chained, signed, per-run node list. Add a resume
envelope:

```jsonc
// ledger node (EXTEND): the existing {run_id, seq, parent, step, state, sig}
{ "resume": {
    "step_kind": "tool_call",
    "idempotency_key": "sha256(run_id|seq|tool|args_hash)",  // exactly-once
    "committed": true,          // side effect confirmed durable
    "code_version": "a73d28e"   // detect upgrade-across-resume
} }
```

### APIs
```python
# olympus/durable.py  (NEW — thin layer over ledger.py, no new store)
def resume(run_id: str) -> dict          # replay committed prefix, continue live
def resumable() -> list[str]             # runs interrupted mid-flight
def idempotency_key(run_id, seq, tool, args) -> str
def commit_effect(run_id: str, seq: int, result: str) -> dict  # WAL-then-act

# olympus/ledger.py  (EXTEND)
def uncommitted_tail(run_id: str) -> list[dict]   # crash forensics
```

### Milestones
- **M1 Prototype (mo 0–4).** Write-ahead-then-act for **side-effecting tools
  only** (`security.ACTION_TOOLS`): log intent → execute → mark committed. Crash
  between the two is detectable via `uncommitted_tail`.
- **M2 MVP (mo 4–10).** `resume(run_id)` replays the committed prefix from the
  ledger and continues live from the first uncommitted step; idempotency keys
  suppress duplicate side effects. Kill -9 during a multi-step run resumes without
  double-sending.
- **M3 Production (mo 10–18).** Upgrade-across-resume (`code_version` mismatch →
  refuse-or-migrate, never silently resume under changed semantics); resume across
  process restart via `proclock`; staged-rollout support (the honest replacement
  for the cut BFT work, F7).

### Research required
None. Prior art is mature (Temporal, AWS SWF, event sourcing).

### Risks
| Risk | Mitigation |
|---|---|
| Non-idempotent external effects (an email sent twice) | Idempotency keys + WAL-then-act; tools that cannot be made idempotent are declared and refuse resume |
| Semantics change across upgrade mid-run | `code_version` pin → refuse-or-migrate, never silent |
| Ledger growth | Existing `_trim`/backup discipline; checkpoint compaction |

### Success metrics
- 100% of `ACTION_TOOLS` calls WAL-logged before execution.
- Kill-9 injection test: **0** duplicated side effects across 1000 resumes.
- Interrupted runs resumable: from ~0% today to **> 95%**.
- Overhead: **< 50 ms** per side-effecting step.

---

## E4 — Scalable Oversight (statistical audit)

> *NORTH_STAR §3.F-i. Debate-based oversight (F-ii) is **deferred**, not cut —
> it needs an independent adversary that does not yet exist, and its
> false-positive rate is uncharacterized (the exact failure mode that made the
> eval refusal-floor misfire).*

### Dependencies
E1 (sample **stratified by effect class** — the point of typing), E3 (replayable
evidence), `ledger.verify_ledger`, `attest.attest/verify_attestation/export_receipt`,
`trust.py` (`enabled`, `establish_after`, `daily_ceiling`), `witness.py`.

### Data model
```jsonc
// audit_samples.jsonl  (NEW — mirrors outcomes.jsonl conventions)
{ "run_id": "...", "seq": 12, "effect_class": "egress:open+writes:fs",
  "sampled_because": "stratum:high_risk", "sample_p": 0.25,
  "verdict": "clean|violation|unclear", "reviewer": "operator",
  "attestation": "<attest.attest() record id>" }
```

### APIs
```python
# olympus/audit.py  (NEW)
def plan(period_days: int = 7) -> dict     # per-stratum sample sizes for a target bound
def sample(period_days: int = 7) -> list[dict]
def record_verdict(run_id: str, seq: int, verdict: str, reviewer: str) -> dict
def coverage_report() -> dict              # {stratum: (n, k, upper_bound_violation_rate)}
def bound(stratum: str) -> float           # one-sided upper confidence bound
```

### Milestones
- **M1 Prototype (mo 12–16).** Stratified sampler over the ledger, strata defined
  by E1 effect classes (high-risk = `egress:open` ∨ `mutates:policy`). Sample sizes
  from a one-sided binomial bound.
- **M2 MVP (mo 16–24).** Operator review workflow + `coverage_report()` publishing
  a **statistical guarantee**: *"with 95% confidence, the violation rate in
  high-risk actions is below X%."* Each reviewed sample gets an `attest` receipt.
- **M3 Production (mo 24–30).** `trust.py` becomes a **closed-loop controller**:
  measured violation upper-bound drives per-domain autonomy (`establish_after`,
  `daily_ceiling`) — autonomy expands only as evidence justifies, contracts
  automatically when the bound worsens.

### Research required
Applied statistics only (stratified sampling, binomial confidence bounds) —
standard audit methodology, not novel research. **Deferred research:** debate/
amplification (F-ii), which requires characterizing an adversary's false-positive
rate first.

### Risks
| Risk | Mitigation |
|---|---|
| Guarantee is only as good as the action distribution (F10-adjacent) | Publish strata + assumptions with every bound; re-plan when the distribution shifts |
| Human review capacity is the binding constraint | Sample sizes derived *from* available reviewer-minutes; the metric is autonomy-hours per oversight-minute |
| Reviewer becomes a rubber stamp | Inject known-bad canaries; measure reviewer detection rate |

### Success metrics
- Published upper bound on high-risk violation rate, refreshed weekly.
- **Autonomy-hours per oversight-minute** rises ≥3× over 12 months with the bound
  held flat or improving.
- Canary detection rate **> 90%**.

---

## E5 — Knowledge Consistency

> *NORTH_STAR §3.D-i. Sound neuro-symbolic planning (D-iii) is **cut** for this
> horizon (F11): soundness is capped by grammar coverage, so the honest deliverable
> is **contradiction detection over a typed subset**, not an answer to hallucination.*

### Dependencies
E1 (facts carry effect-typed provenance), `facts.py` (`record`, `lookup`, `count`),
`relgraph.py` (`add_node`, `add_edge`), `witness.py` (signing), `embed`/`annindex`.

### Data model
```jsonc
// facts.jsonl (EXTEND — today: claim/verdict/source)
{ "claim": "...", "verdict": "true|false|unverified",
  "predicate": "employs(Org,Person)",       // NEW: typed form, null if untypeable
  "justification": {"run_id": "...", "evidence_span": "...", "confidence": 0.8},
  "derived_from": ["<fact_id>", ...],       // NEW: TMS support set
  "retracted_by": null }
```

### APIs
```python
# olympus/kb.py  (NEW — pure, deterministic; datalog-fragment only)
def assert_fact(claim: str, predicate: str | None, justification: dict) -> str
def contradictions(limit: int = 50) -> list[dict]   # mutually inconsistent pairs
def retract(fact_id: str) -> list[str]              # TMS: cascade to dependents
def query(pattern: str) -> list[dict]               # bounded datalog query
def typed_coverage() -> float                       # % of facts with a predicate
```

### Milestones
- **M1 Prototype (mo 18–24).** A **fixed, small** predicate vocabulary (5–10
  relations); grammar-constrained extraction via the existing structured-output
  path (`_GEN_SCHEMA`-style); pairwise contradiction detection.
- **M2 MVP (mo 24–30).** Truth maintenance: `retract()` cascades through
  `derived_from`; contradictions open a **belief-revision task** routed to the
  owning specialist rather than crashing or silently picking a side.
- **M3 Production (mo 30–36).** Contradiction checks run in the `sleeptime`
  consolidation pass; `typed_coverage()` is a tracked metric — the *honest ceiling
  on soundness*, published, never hidden.

### Research required
**Real research risk here.** The neural→symbolic grounding interface (which claims
enter the grammar at all) is unsolved (F11). Mitigation: keep the vocabulary tiny
and the query language in a decidable fragment (datalog), and treat
`typed_coverage()` as the honest bound on what the checker can see.

### Risks
| Risk | Mitigation |
|---|---|
| Grammar coverage caps soundness (F11) | Publish `typed_coverage()`; never claim hallucination is "solved" |
| Constrained decoding degrades generation | Extract in a *separate* pass from generation, not inline |
| Inference cost explodes | Datalog fragment only; bounded query depth |

### Success metrics
- `typed_coverage()` **> 40%** of newly recorded facts by M3.
- ≥1 real contradiction surfaced per 1000 facts.
- Retraction cascade correctness: **100%** on a seeded test corpus.
- Consolidation cost: **< 60 s** per `sleeptime` pass (F8 rule).

---

## E6 — Adaptive Loop (rescoped)

> *NORTH_STAR §3.A, **fundamentally rescoped** by F2/F3: no weight training.
> Olympus reaches models through APIs it cannot fine-tune, and at 10³–10⁴
> trajectories would only overfit its own eval. What **is** reachable: context,
> retrieval, exemplar selection, routing — plus small auxiliary models where that
> data volume genuinely suffices.*
>
> **Seed-citation rule applied (F1):** `rlscaffold.py` states it is "emphatically
> NOT a live training loop" with "NO path that writes to routing, config, prompts,
> or any decision." **This roadmap does not reverse that.** `rlscaffold` stays an
> advisory, human-read reward model. E6 builds on `bandit_routing` and
> `learned_routing`, which are *designed* to be decision-carrying and evidence-gated.

### Dependencies
E1 (bounded action space), E3 (trustworthy traces), `bandit_routing`,
`learned_routing`, `outcomes.py`, `trajectories.py` (`build`, `export`),
`evals.py` (`per_specialist_scores`, `regression_check`, `confirm_regressions`,
`DEFAULT_TOLERANCE`), `ace.py` (delta-based context evolution).

### Data model
Reuses `outcomes.jsonl` + `routing_outcomes` (both exist). One addition:

```jsonc
// exemplars.jsonl  (NEW — few-shot selection, not training data)
{ "specialist": "plutus", "task_embedding": [...], "exemplar_run_id": "...",
  "objective_score": 8.4,        // from evals.objective_score — verified, not judged
  "promoted": true, "promoted_by": "gate:margin+reproduction" }
```

### APIs
```python
# olympus/adapt.py  (NEW)
def select_exemplars(specialist: str, task: str, k: int = 3) -> list[dict]
def propose_context_delta(specialist: str) -> dict     # via ace.py
def gate_delta(delta: dict) -> dict   # evals margin + reproduction, then promote/revert
def loop_report() -> dict             # what was proposed / promoted / reverted
```

### Milestones
- **M1 Prototype (mo 24–30).** Exemplar selection: retrieve high-`objective_score`
  past runs as few-shot examples. Purely retrieval — no training, no weights.
- **M2 MVP (mo 30–38).** Context deltas proposed by `ace`, admitted **only**
  through the existing margin + reproduction gate (`OLYMPUS_GATE_MARGIN`,
  `OLYMPUS_GATE_CONFIRM`) against the anchor eval below.
- **M3 Production (mo 38–48).** Small auxiliary models — router/classifier only,
  where 10³–10⁴ labeled samples suffice — trained offline, gated identically,
  promoted only on reproduced improvement. **Never the answering model.**

### The anchor eval (mandatory precondition, from §8.1 as amended by F10)
Before *any* E6 promotion path goes live: a **frozen, human-authored, versioned**
eval set, held separately from `benchmarks.json` and never machine-edited.
F10's contradiction (frozen vs. refreshed) is resolved by **versioning rather than
mutation**: `anchor_v1` is immutable forever; growth means adding `anchor_v2` and
reporting *both*, so drift is visible instead of erased. Sized to available human
authoring capacity, not to an ideal — an honest small anchor beats an aspirational
large one that never gets written.

### Research required
Contextual bandits and exemplar selection are established. **Not attempted:**
weight-level RL on the answering model (F2/F3).

### Risks
| Risk | Mitigation |
|---|---|
| Overfitting to the eval (F3 in miniature) | Immutable versioned anchor; report all versions |
| Gate cost explodes (F8) | Deltas batched; gate runs on a schedule, not per proposal; hard per-cycle API budget |
| Reward hacking through the loop | Objective-verified scores only (`objective_score`), never judge-only, for exemplar promotion |
| Silent reversal of `rlscaffold`'s refusal (F1) | Explicit rule: `rlscaffold` stays advisory; any change to that is an ADR, not a roadmap item |

### Success metrics
- Measurable per-specialist gain on the **anchor** eval (not the tuned one),
  reproduced in an independent confirmation trial.
- Routing accuracy improvement over the `learned_routing` heuristic baseline.
- **0** promotions that fail to reproduce (the gate's whole purpose).
- Loop cost per cycle within a stated USD budget (F8 rule).

---

## 2. Sequencing summary

| Phase | Months | Engines | Ships |
|---|---|---|---|
| P0 | 0–6 | E1 M1–M2, E3 M1 | Effect lint in CI; WAL for side effects |
| P1 | 6–18 | E1 M3, E2 M1–M3, E3 M2–M3 | Spec-checked approval spine; resumable runs |
| P2 | 18–30 | E4 M1–M3, E5 M1–M2 | Statistical audit guarantee; contradiction detection |
| P3 | 30–48 | E5 M3, E6 M1–M3 | Consolidated knowledge; gated adaptive loop |

**Kill criteria (pre-committed, per the F5 sunk-cost lesson).** E2 stops at M1 if
no real defect is found. E5 stops at M2 if `typed_coverage()` < 20%. E6 does not
start until the anchor eval exists and E1+E3 are in production.

---

## 3. Cross-cutting risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| **Provider ships equivalent governance** (F14 — the dominant strategic risk, absent from NORTH_STAR) | The moat is a layer over someone else's models | Position as *complementary*: verifiable audit + capability confinement + measured gates **across** providers. E1/E3/E4 are provider-agnostic by construction |
| **Organization capacity** (F13) | This is a multi-year plan for a very small team | Each engine ships value alone; strict phase gating; nothing depends on a research breakthrough |
| **Gate cost/flakiness** (F8) | Already this repo's weakest component | Every engine states a gate budget; E1/E2 add **zero** API-calling gates |
| **Maintenance surface** (F16) | ~230 modules, one maintainer | Every engine **extends** an existing module; only 5 new files total (`effects`, `durable`, `audit`, `kb`, `adapt`) |
| **No demand evidence** (F15) | Roadmap optimizes a moat, not a user | P0/P1 deliverables (crash-safe runs, CI safety lint) are user-visible reliability wins — validate demand before P2/P3 |
