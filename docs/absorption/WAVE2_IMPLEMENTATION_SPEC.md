# Wave 2 Implementation Specification — Colibri Absorption

**Authority chain:** production code → `docs/ROADMAP.md` →
`docs/absorption/00-SYNTHESIS.md` → `WAVE1_IMPLEMENTATION_SPEC.md` →
`WAVE1_COMPLETION_REPORT.md` → `WAVE1_INDEPENDENT_AUDIT.md` → `13-review-gaps.md`
→ domain docs 01–12.
**Entry gate:** `WAVE1_INDEPENDENT_AUDIT.md` §6 = **PASS** (2 blockers fixed, 1
false claim re-declared, no critical Wave-1 invariant unverified).
**Baseline:** `3999 passed, 26 skipped, 0 failures`; capabilities, threat-model,
non-interference, compileall, env-docs gates green.
**Scope:** the ten synthesis-approved Wave-2 capabilities — evidence-consuming
policy over the Wave-1 measurement substrate. **Explicitly NOT in Wave 2:**
speculative execution, predictive prefetch activation, local-model production
routing (all Wave 3, evidence-gated).

---

## 0. Implementation map & module budget

**Wave-1 evidence stores this wave consumes:** `modelgate/results.jsonl` +
`freeze.json` (drift), `ctx_calibration.json` (token ratios), `usage/*.json`
(cache/cost), `sessions/*.journal.jsonl` (state), `repair_stats.json` (tool
decay), `coupling` predictability report, `traces/*.jsonl` (decisions).
**Pre-existing seams:** `usage.slot()` (the one concurrency primitive),
`learned_routing.choose(members, specialist, heuristic_pick)` /
`bandit_routing.choose(...)` (advisory-over-heuristic routing signature),
`routing_outcomes` rows, `doctor._*_checks()` concatenation, `health._COMPONENTS`
name registry, `evals.regression_check`, `calibration.wilson_interval`.

**Module admission (2-of-4 test applied per capability):**

| Cap | Home | New? | Admission rationale |
|---|---|---|---|
| C1 model qualification | `olympus/modelgrade.py` | **NEW** | owns grade-card state ✓; enforces "no unqualified model on a protected cell" ✓; independent lifecycle ✓ |
| C2 context/skill heat | `olympus/ctxheat.py` | **NEW** | owns heat ledger ✓; content-minimisation + benchmark-gated pins ✓; independent lifecycle ✓ |
| C3 routing substitution | `olympus/routesub.py` | **NEW** | owns substitution telemetry ✓; enforces quality-band + verifier floor ✓; coupling routing policy into `orchestrator` would be harmful ✓ |
| C4 experiments registry | `olympus/experiments.py` | **NEW** | owns registry state ✓; "no unregistered experimental feature" ✓; independent lifecycle ✓ |
| C5 ingestion gate | `olympus/ingestgate.py` | **NEW** | owns artifact-kind registry + refusal evidence ✓; the trust boundary ✓; independent failure boundary ✓ |
| C6 watchdog | `olympus/watchdog.py` | **NEW** | owns progress leases ✓; "progress-free spend is stopped" ✓; independent failure boundary ✓ |
| C7 admission policy | extend `usage.py` + `gateway.py`/`web.py` | no | synthesis ruling R3 — one slot primitive, policy on top |
| C8 degenerate-stream defense | `olympus/streamguard.py` | **NEW** | pure detectors + pathology evidence ✓; "no partial tool call from a pathological stream" ✓; embedding scanners in `llm.py` would bloat the provider client ✓ |
| C9 optimization liveness | extend `doctor.py` (+ `usage`, `routesub`, `ctxheat` readers) | no | pure reads of existing telemetry (gap G6) |
| C10 config-skew diagnostics | extend `doctor.py` / `health.py` / `config.py` | no | gap G7; registration of startup-scoped settings |

**Budget accounting (synthesis B1, hard cap 14).** Wave 1 used 4 (`sessionlog`,
`ctxbudget`, `modelgate`, `coupling`). Wave 2 uses **7** → **11 of 14**. Wave 3's
approved candidates need exactly 3 (`draftverify`, `localtier`, `coalesce`; the
Anthropic handler lives inside `openai_server.py`, mirror routing inside
`coalesce`). **Wave 3 therefore has ZERO headroom** — any additional module must
retire an existing one. Recorded as a standing risk.

**Universal Wave-2 rules (binding on every capability below):**
- **Default off / shadow-first.** Every policy capability ships disabled; where it
  can only be calibrated in operation it ships in **shadow mode** (records what it
  *would* do, changes nothing). Flag off ⇒ byte-identical behaviour, replay-proven.
- **No uncalibrated inherited constants** (user directive). Any threshold not
  derived from measured data ships only in shadow mode, labelled `PROVISIONAL`
  with its calibration owner named.
- **Flag pairing.** Any flag that alters the decision path is added to BOTH
  `tr.meta` and `orchestrator.replay_run`'s env-reproduction list.
- **Refusal over silent degradation** (ruling R6) — a degraded answer is legal
  only when disclosed in the reply or explicitly opted into.
- **Measurement budget** (B2) — every new cadence registers in the single
  heartbeat table under `OLYMPUS_MEASURE_BUDGET_USD`; none self-adds.
- **Every experimental flag gets an `experiments.py` entry** (C4 enforces in CI).

---

## 1. Capability specifications

### W2-C1 — Unified measured model qualification (`modelgrade.py`)

1. **Problem.** Routing evidence is scattered (hand-typed `config._CAPABILITIES`,
   `routing_outcomes`, `evals` baselines, `modelgate` drift rows, `calibration`
   observations) and no store can answer "is this model qualified for *this kind
   of task*?". A model can be selected today on a human-written claim alone.
2. **Current.** `_CAPABILITIES` is a static table; `learned_routing`/
   `bandit_routing` consume raw outcome counts; `modelgate` gates drift but does
   not qualify.
3. **Proposed.** One authoritative evidence store keyed by **member × task cell**.
   - `TaskCell = (task_class, language, context_band, needs_tools, needs_structured)`.
   - `GradeCard` dimensions: `quality, cost, latency, reliability, structured,
     tools, security`, each with `n`, Wilson `lo/hi`, and `freshness_days`.
   - **States:** `untested → observing → qualified` (+ `restricted`, `frozen`,
     `quarantined`, `retired`).
   - **Promotion requires measured evidence:** `qualified` iff
     `n ≥ MIN_N` **and** Wilson lower bound ≥ the cell's floor **and**
     `freshness ≤ TTL` **and** no `block`/`quarantine` from `modelgate`.
     A `_CAPABILITIES` entry may seed a **prior** but can never yield `qualified`
     (invariant W2-I1.1).
   - **Staleness expires:** past `OLYMPUS_GRADE_TTL_DAYS` (default 30) confidence
     decays and the card falls back to `observing`; `retired` after 2×TTL silent.
   - **Contradictory evidence is preserved, never overwritten:** evidence is an
     append-only JSONL; conflicting observations for one key are retained and a
     `conflict` record names the resolution rule applied (recency-weighted Wilson)
     — resolution is explicit, auditable, reversible.
4. **Invariants.** W2-I1.1 no manual claim promotes. W2-I1.2 every card carries
   the full key (provider, model, revision, commit, tmpl_ver, policy_ver,
   eval_set_ver, cell, date). W2-I1.3 stale evidence loses confidence rather than
   silently persisting. W2-I1.4 read-only w.r.t. other stores.
5. **Schema.** `MEMORY_DIR/modelgrade/evidence.jsonl` (append-only observations),
   `cards.json` (derived, rebuildable — deleting it loses nothing).
6. **Interfaces.** `observe(...)`, `card(member, cell)`, `status(member, cell)
   -> State`, `qualified(member, cell) -> bool`, `rebuild()`, `explain(member,
   cell) -> dict` (why this state — the operator-facing answer).
7. **Threats.** Evidence poisoning (bounds-validated observations, append-only,
   keyed); a compromised provider inflating its own scores (scores derive from
   verifier/eval outcomes, never from the model's self-report).
8. **Failure.** No evidence ⇒ `untested` (never a default `qualified`). Corrupt
   `cards.json` ⇒ rebuilt from evidence; corrupt evidence ⇒ quarantined
   (reject-never-repair), store falls back to `untested`.
9. **Migration/rollback.** New files; `OLYMPUS_MODELGRADE=off` ⇒ inert.
10. **Tests/acceptance.** Promotion refused on manual claim; promotion on
    sufficient measured evidence; staleness demotion; conflict preserved +
    resolution recorded; freeze/quarantine from `modelgate` reflected; full-key
    completeness; corrupt-artifact handling. **Gate W2-A1/A2.**

### W2-C2 — Context & skill heat (`ctxheat.py`)

1–3. **Problem/current/proposed.** Nothing learns which context/skill items are
   *useful*; Colibri's expert-heat principle translated. Heat is scored from
   **measured usefulness, not raw frequency**: retrieval→verifier acceptance,
   reuse, avoided cost/latency, task-completion improvement, recency,
   per-user relevance, confidence, correction rate.
4. **Invariants.** W2-I2.1 **content-minimised** — ids, counters, scores,
   timestamps, provenance only; never raw user content. W2-I2.2 per-user
   isolation; global vs user-specific ledgers separated. W2-I2.3 pins bounded by
   a max pin budget with hysteresis + decay; eviction protection for genuinely hot
   items. W2-I2.4 **no pin-set policy change without a before/after benchmark
   gate** (same discipline as `gate_prompt`). W2-I2.5 rollback to static
   placement is one flag.
5. **Schema.** `MEMORY_DIR/users/<user>/context_heat.json` +
   `MEMORY_DIR/context_heat.json` (global), `{id, kind, hits, useful, decayed,
   last, provenance}`.
6. **Ownership/mode.** **Ships in SHADOW mode** (`OLYMPUS_CTXHEAT=shadow|off|on`,
   default `off`): records heat and *proposes* pin sets; applying requires an
   explicit on + a passing benchmark gate. The hysteresis/decay constants are
   **PROVISIONAL** until calibrated from real swap telemetry (Wave-1 audit
   directive: no inherited constants) — calibration owner named in the module.
7. **Threats.** Heat poisoning (an injected item farming its own promotion) —
   mitigated: only gated-store content is eligible; usefulness requires *verifier
   acceptance*, which the injecting path cannot grant itself; per-user isolation.
8–10. **Failure/migration/rollback/tests.** Missing ledger ⇒ static placement.
   Tests: usefulness≠frequency, poisoning resistance, isolation, hysteresis,
   decay, budget cap, benchmark-gate refusal, shadow-mode zero-effect.
   **Gate W2-A3.**

### W2-C3 — Routing substitution within measured quality bands (`routesub.py`)

1–3. Cost/warmth-aware substitution of a **cheaper or warmer** qualified
   candidate, only inside a measured band — Colibri's CACHE_ROUTE translated with
   its agreement telemetry.
4. **Invariants (all must hold to substitute).** Both candidates `qualified` for
   the cell; the cheaper stays above the cell's **minimum quality floor**; the
   **verifier floor is preserved**; security classification permits; context/tool
   requirements supported; evidence fresh; confidence ≥ threshold.
   **W2-I3.1 Aletheia is NEVER substituted below its measured verification
   floor** (hard rule, test-enforced). **W2-I3.2 no silent quality degradation** —
   any user-visible degradation is disclosed.
5. **Recorded per decision (always):** original preferred route, substituted
   route, reason, estimated savings, actual cost, latency, verifier outcome,
   disagreement, fallback activation, user-visible degradation.
6. **Mode.** `OLYMPUS_ROUTESUB=off|shadow|on`, default **off**; shadow records
   the counterfactual without changing routing. Integrates at the existing
   `choose(members, specialist, heuristic_pick)` seam alongside
   `learned_routing`/`bandit_routing`, consuming `modelgrade` (one ladder, R1).
7–10. Threats: cost-driven quality erosion (floors + disclosure); evidence
   staleness (freshness check). Tests: every precondition individually blocks
   substitution; Aletheia floor; agreement/disagreement telemetry; shadow-mode
   zero-effect; rollback. **Gates W2-A4/A5/A11.**

### W2-C4 — Experiments & quarantine registry (`experiments.py`)

1–3. One registry for experimental capabilities, failed optimizations,
   quarantined models, disabled routes, rejected algorithms, security incidents,
   negative benchmark results, scheduled retests, accepted debt (synthesis R2 —
   `quarantine.py` folded in here).
4. **Entry fields (all required):** `id, owner, hypothesis, status, evidence,
   reason, activation_condition, deactivation_trigger, cost, security_impact,
   created, last_tested, next_review, outcome`.
5. **Invariants.** W2-I4.1 **no experimental feature bypasses the registry** — a
   CI test enumerates experimental flags (`OLYMPUS_*EXPERIMENTAL*` + a declared
   list) and fails on any without an entry. W2-I4.2 **no feature stays enabled
   after its evidence expires** — `experiments.active(id)` returns False past
   `next_review` until re-tested, and features consult it.
6. **Schema.** `olympus/experiments.json` (committed registry) +
   `MEMORY_DIR/experiments/state.jsonl` (retest outcomes).
7–10. Tests: missing-entry CI failure; expiry auto-disable; retest updates;
   seeded with the real Wave-1 negatives (I-C4 re-declaration, base64 secret
   residual, ctxheat PROVISIONAL constants, `EXPERT_BUDGET`-style quarantines).
   **Gate W2-A6.**

### W2-C5 — Persistent artifact ingestion gate (`ingestgate.py`)

1–3. One authoritative trust boundary for persistent external artifacts
   (Colibri's reject-never-repair, generalised — domain-10's doctrine sentence).
4. **Covered kinds (registry, fail-closed):** plugins, skills, MCP payloads,
   memory imports, session imports, model endpoints, configuration bundles,
   replay fixtures, evaluation corpora, generated policy files, tool schemas,
   provider metadata. **An unregistered kind is REFUSED**, not passed.
5. **Pipeline (persistent):** canonicalize → validate → verify provenance →
   verify integrity → schema-validate → size limit → version policy → classify
   data → **reject on failure with signed refusal evidence**. Never silently
   repair a persisted artifact.
   **Ephemeral provider responses** may be sanitized only within explicitly
   enumerated safe rules and **must still pass the authoritative schema before
   execution** (the C8/tool-ladder boundary).
6. **Invariants.** W2-I5.1 unregistered kind ⇒ refusal. W2-I5.2 persistent
   artifacts are never repaired. W2-I5.3 every refusal produces retained,
   signed evidence. W2-I5.4 malformed input cannot bypass validation.
7. **Tests.** Property-based sweeps (seeded stdlib; `hypothesis` unavailable) +
   a **permanent malformed-input corpus** under `tests/fixtures/ingest_corpus/`
   that grows with every real-world malformation. **Gates W2-A7/A8.**

### W2-C6 — Progress-based watchdog (`watchdog.py`)

1–3. Stall detection by **useful progress**, not liveness (domain-11's "spend as
   a progress currency").
4. **Progress signals:** completed plan nodes, validated tool results, verified
   response sections, accepted specialist outputs, increasing replay state,
   bounded token generation, spend that produces measurable advancement.
   **W2-I6.1 model-generated text alone is NEVER proof of progress.**
5. **Detects:** deadlocks, retry loops, repeated identical calls, stalled
   streams, runaway tool recursion, denial-of-wallet patterns, provider hangs,
   verifier loops, progress-free spend, queue starvation.
6. **Actions:** warn → cancel → preserve forensic state → refuse unsafe
   continuation → release admission slots → quarantine repeated failure patterns
   (into `experiments`) → expose the exact reason **and the operator action**.
7. **Mode.** `OLYMPUS_WATCHDOG=off|observe|enforce`, default **observe** once
   validated (ships `off`). False-positive rate is an explicit acceptance gate.
8–10. Tests: each detection class; forensics retained; slot release; no
   false-positive on a slow-but-progressing run. **Gate W2-A9.**

### W2-C7 — Unified admission policy (extend `usage.slot` + gateway)

1–3. Extend the **existing** primitive (ruling R3) — no parallel system.
4. **Adds:** global / provider / model / tenant capacity, user fairness,
   priority classes, queue limits, queue timeout, cancellation, **reserved safety
   capacity**, denial-of-wallet protection, retry guidance, structured overload
   responses (429 + `Retry-After` + queue-wait headers).
5. **Invariants.** W2-I7.1 **no silent quality downgrade to admit traffic** —
   degraded mode legal only if opted into or disclosed (R6). W2-I7.2 fairness:
   no user starves another under saturation. W2-I7.3 reserved capacity is never
   consumed by best-effort traffic.
6. **Tests.** Concurrency fairness under saturation; priority ordering; timeout;
   cancellation releases; reserved-capacity protection; structured overload
   shape. **Gates W2-A10/A11.**

### W2-C8 — Degenerate-stream defense (`streamguard.py`)

1–3. Colibri's sampling armor (#369) translated to an API client — the failure
   class Olympus actually meets (gap G2).
4. **Detects:** repeated token loops, empty progress, malformed event sequences,
   invalid tool deltas, impossible usage counters, never-ending whitespace,
   repeated partial JSON, invalid Unicode sequences, provider event-order
   violations, output exceeding declared bounds.
5. **Must:** terminate safely; preserve evidence; **never execute a partial tool
   call**; return a structured failure; contribute to provider drift/reliability
   evidence (feeds `modelgrade` + `modelgate`).
6. **Invariants.** W2-I8.1 no partial tool call executes from a tripped stream.
   W2-I8.2 detectors are pure and side-effect-free. W2-I8.3 a tripped stream is
   an explicit failure, never a silent truncated answer.
7–10. Tests: one per pathology; boundary/chunk-split safety; false-positive
   guard on legitimate slow streams. **Gate W2-A12.**

### W2-C9 — Optimization liveness (extend `doctor.py`)

1–3. Gap G6: every optimization must prove it is **active and beneficial**.
4. **Per optimization** (prompt caching, context heat, routing substitution,
   coalescing, provider mirroring, compression, local preparation, and — when
   they exist — speculation and prefetching) expose: `configured, eligible,
   activated, hits, misses, acceptance_rate, savings, overhead, net_benefit,
   inactivity_reason`.
5. **Invariant W2-I9.1: a configured-but-inactive optimization is reported as
   INACTIVE, never as success.** W2-I9.2 liveness reads existing telemetry only
   (no new measurement load).
6. **Tests.** Each optimization's verdict on synthetic telemetry: active /
   inert / no-signal / net-negative. **Gate W2-A13.**

### W2-C10 — Configuration-skew diagnostics (extend `doctor`/`health`/`config`)

1–3. Gap G7: detect runtime config differing from documented defaults,
   deployment config, replay-fixture config, expected process env, persisted
   schema expectations, the active feature registry, and generated capability
   docs.
4. **Reports:** startup-only settings requiring restart, unknown settings,
   deprecated settings, conflicting settings, undocumented settings, settings
   ignored by the current runtime, unsafe combinations, version skew between
   processes.
5. **Invariant W2-I10.1: never silently self-correct critical configuration
   drift** — report with the exact operator action. W2-I10.2 startup-scoped
   settings are *registered* in `config.py` (enumerable, not folklore) and
   surfaced via a running-process fingerprint in `health`.
6. **Tests.** Each skew class detected; restart-required fingerprint; unsafe
   combination flagged; no auto-correction. **Gate W2-A14.**

---

## 2. Wave-2 threat-model additions

| Threat | Surface | Mitigation | Gate |
|---|---|---|---|
| Evidence-store poisoning | `modelgrade`, `ctxheat` | bounds-validated, append-only, keyed; usefulness requires verifier acceptance | A1, A3 |
| Promotion by manual claim | `modelgrade` | W2-I1.1 measured-evidence-only promotion | A2 |
| Quality erosion via substitution | `routesub` | floors + verifier floor + disclosure | A4, A5, A11 |
| Aletheia downgrade | `routesub` | W2-I3.1 hard rule, test-enforced | A5 |
| Malicious persistent artifact | `ingestgate` | reject-never-repair, fail-closed kinds, signed refusals | A7, A8 |
| Prompt/tool injection via ephemeral content | `ingestgate` + tool ladder | sanitize-within-rules, authoritative schema before execution | A8 |
| Denial-of-wallet (runaway spend) | `watchdog`, admission | progress-free spend detection; reserved capacity; DoW patterns | A9, A16 |
| Queue starvation / unfair admission | admission | fairness + priority + reserved capacity | A10 |
| Pathological provider stream | `streamguard` | terminate safely, no partial tool execution, evidence to drift | A12 |
| Silent inactive optimization | liveness | inactive ≠ success | A13 |
| Config drift / version skew | config-skew | report, never auto-correct | A14 |
| Stale experimental feature left on | `experiments` | expiry auto-disable | A6 |

---

## 3. Wave-2 acceptance matrix (the 17 completion requirements)

| # | Gate | Threshold | Evidence source |
|---|---|---|---|
| A1 | One authoritative evidence store | all routing decisions read `modelgrade`; no second ladder | routesub/learned_routing tests |
| A2 | No unqualified model on a protected task | `qualified()` required; manual claim never promotes | modelgrade tests |
| A3 | Heat changes benchmark-gated + reversible | pin-set change refused without passing gate; flag rollback | ctxheat tests |
| A4 | Substitutions inside measured bands | every precondition individually blocks | routesub tests |
| A5 | Aletheia never below verification floor | hard-rule test | routesub tests |
| A6 | Every experimental feature registered | CI enumeration test | experiments tests |
| A7 | All persistent artifacts pass the gate | unregistered kind refused | ingestgate tests |
| A8 | Malformed artifacts cannot bypass validation | property sweeps + malformed corpus | ingestgate tests |
| A9 | Progress-free spend detected and stopped | watchdog trips; slots released; forensics kept | watchdog tests |
| A10 | Admission fairness under concurrency | no starvation; priority honoured | admission tests |
| A11 | No silent quality downgrade | disclosure or opt-in required everywhere | routesub + admission tests |
| A12 | Degenerate streams terminate safely | each pathology; no partial tool call | streamguard tests |
| A13 | Every enabled optimization has a liveness verdict | verdict present + correct | doctor liveness tests |
| A14 | Config skew observable and actionable | each skew class + no auto-correct | config-skew tests |
| A15 | Full suite + security gates pass | 0 failures; capabilities/threat-model/non-interference/env-docs green | CI |
| A16 | Cost inside the global measurement budget | no new always-on cadence; budget respected | heartbeat/budget tests |
| A17 | Rollback to Wave-1 behaviour tested | every capability flag off ⇒ Wave-1 behaviour, replay-verified | rollback tests |

---

## 4. PR decomposition (dependency-ordered)

| Unit | Content | Depends on |
|---|---|---|
| W2-PR1 | this spec (no behaviour change) | — |
| W2-PR2 | `modelgrade` (C1) — the shared ladder | — |
| W2-PR3 | `experiments` registry (C4) + Wave-1 negatives seeded | — |
| W2-PR4 | `ingestgate` (C5) + malformed corpus + property sweeps | — |
| W2-PR5 | `watchdog` (C6) | C4 (quarantine sink) |
| W2-PR6 | `streamguard` (C8) | — |
| W2-PR7 | `ctxheat` (C2, shadow) | C4 |
| W2-PR8 | `routesub` (C3, shadow) | C1 |
| W2-PR9 | admission policy (C7) on `usage.slot` | C6 (slot release) |
| W2-PR10 | liveness (C9) + config-skew (C10) doctor/health | C1,C2,C3 |
| W2-PR11 | rollback suite (A17) + completion report | all |

## 5. Explicit exclusions (Wave 2)

No speculation (`draftverify`), no prefetch activation, no local-inference
routing (`localtier`), no provider mirroring (`coalesce`), no Anthropic-
compatible handler — all Wave 3, each gated on Wave-1/2 evidence. No new CLI
commands (flags on existing commands only). No pricing-table unification. No
`hypothesis` dependency (seeded stdlib property sweeps).
