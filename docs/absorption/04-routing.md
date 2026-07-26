# Absorption 04 — Routing Intelligence

**Colibri domain:** MoE sigmoid/noaux_tc routing & the gate-weight subtlety (§6.3 FASE A, §5.6),
expert top-p (`TOPP`, §6.3), `CACHE_ROUTE` cache-aware routing with agreement telemetry (§6.3,
§19.3, `docs/CACHE_ROUTE.md`), the `EXPERT_BUDGET` quarantine story (§6.3, §26.4), the Expert
Atlas research program (§19.1), route-coupling/copula prefetch tables (§7.5 COUPLE, §19.2),
and LOOKA routing-predictability measurement (§7.5).
**Olympus target:** the specialist/model/tool routing layer — `olympus/config.py`
(`ModelPool.for_specialist`, `capability_score`, `_role_map`), `olympus/learned_routing.py`,
`olympus/bandit_routing.py`, `olympus/routing_outcomes.py`, `olympus/toolselect.py`,
`olympus/specialists.py`, `olympus/dytopo.py`, plus `docs/LEARNED_ROUTING.md` and the
Calibration-Record thesis of `docs/MOAT_ANALYSIS.md`.

## Domain thesis

Colibri's router is the one place in the engine where a *heuristic* is allowed to touch
*answers*, and everything around it exists to keep that power honest: the selection score may
be biased for systemic goals (noaux_tc load balancing, cache residency) but the **mixing weight
is always the unbiased quality estimate**; every lossy routing mode ships with agreement
telemetry (swap%, overlap, KL) so the loss is visible, not vibes; a routing feature that
measured badly (`EXPERT_BUDGET`) was quarantined with a written eulogy instead of deleted; and
the claim "expert 17.42 is a chemistry specialist" had to survive four controlled confounds, a
replication gate that killed 587 fake specialists, and leave-one-prompt-out validation before
it was allowed to feed anything. Olympus already has the right skeleton — an evidence-gated
learned selector (`learned_routing.py`), a deterministic bandit (`bandit_routing.py`), a
passive outcome ledger (`routing_outcomes.py`) — but it lacks Colibri's four routing crown
jewels: a **cost/warmth substitution tier that is rank-banded and telemetered** (CACHE_ROUTE),
a **confound-controlled Specialist Atlas** (what each specialist/model/tool is *measured* to be
good at, replacing the hand-typed `_CAPABILITIES` keyword scores), **co-invocation coupling
tables** that make prefetch a measured bet instead of a guess, and a **formal quarantine
registry** so failed routing experiments become institutional memory. Each of these is an
accumulating asset — exactly the `MOAT_ANALYSIS.md` Asset 1/2 shape — because atlas entries,
agreement telemetry, and coupling tables are integrals over this deployment's real traffic.

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| Sigmoid/noaux_tc routing & gate-weight subtlety | biased score selects, unbiased sigmoid logit mixes; router weights F32 always (§6.3, §5.6) | **absorb-principle** | `config.py` (ModelPool), `learned_routing.py`, `routing_outcomes.py` (doctrine + one schema field) |
| Expert top-p (`TOPP`) | per-position adaptive expert count by probability mass, −30–40% disk at measured quality cost (§6.3) | **redesign** | `toolselect.py`, Athena planner (`orchestrator`), `dytopo.py` |
| `CACHE_ROUTE` + agreement telemetry | keep true top-J, fill from resident set within rank M / mass P, `ROUTE_ALPHA` down-weight, swap%/overlap/KL telemetry (§6.3, §19.3) | **redesign** | **new `olympus/routesub.py`**, `routing_outcomes.py`, `config.py` |
| `EXPERT_BUDGET` quarantine | complete feature shipped OFF behind double opt-in with an empirical eulogy in `main()` (§6.3, §26.4) | **new-subsystem** | **new `olympus/quarantine.py`**, `DEFERRED.md`, CI |
| Expert Atlas | probe battery, 4+1 controlled confounds, replication gate, LOPO 96.7% (§19.1) | **new-subsystem** | **new `olympus/atlas.py`**, `evals.py`, `specialists.py`, `benchmarks.json` |
| Route coupling / copula prefetch tables | offline co-activation copula screening → `.coli_pairs` → prefetch ring, +3.6..+9.4 pp recall (§7.5, §19.2) | **new-subsystem** | **new `olympus/coupling.py`**, `trace.py`, `heartbeat.py`, orchestrator |
| LOOKA predictability measurement | zero-behavior-change 4-predictor routing-predictability report at exit (§7.5) | **absorb-principle** | `olympus/coupling.py` (`predictability_report`), CLI `olympus routing-predict` |

---

## R1. Sigmoid/noaux_tc routing and the gate-weight subtlety (§6.3 FASE A, §5.6)

**1. What Colibri does.** GLM-5.2's router computes sigmoid logits per expert; the *selection*
score is `logit + router_bias` (the noaux_tc auxiliary-loss-free load-balancing bias), top-K is
taken over the biased score, but the **gate weight applied to each selected expert's output is
the raw sigmoid logit, not the biased score** (§6.3). Separately, the mixed-precision policy
(§5.6) keeps `mlp.gate.weight` and `e_score_correction_bias` at F32 *always* — routing is
explicitly classified as too sensitive to quantize, even while the experts it selects run at
int4.

**2. Why it exists.** The bias exists to balance load system-wide without an auxiliary loss;
mixing with the biased score would let a *systemic* objective (balance) corrupt a *local*
objective (answer quality). F32 routers exist because a tiny routing error compounds into a
completely different expert set — placement decides speed, routing decides answers.

**3. How it works internally.** One batch matmul → sigmoid → add bias → plain top-K → gate
weight = unbiased logit → optional `norm_topk` renorm → `routed_scaling_factor`. `n_group != 1`
configs are refused outright rather than half-supported.

**4. Strengths.** The score/weight split is a clean two-channel design: you can bias *choice*
arbitrarily hard while the *credit assignment* stays honest. Refusing unsupported router
configs beats silently mis-routing.

**5. Weaknesses & trade-offs.** The subtlety is invisible — one line of code carries the whole
invariant, and nothing in the engine *audits* that the two channels stay separate; it survives
by test fixtures and lore. The bias itself is model-trained and static; Colibri cannot adjust
it. And the doctrine is implicit: nowhere is "selection may be biased, weighting may not" a
named, checkable property.

**6. Security implications.** In Colibri: minimal (weights are data-validated elsewhere). In
Olympus the analog is sharper: any signal that biases routing (cost, warmth, load) is an input
an attacker or a misconfiguration could push on — if biased scores leaked into *quality
accounting*, an adversary who makes a cheap model look busy could degrade the recorded
evidence base itself. Keeping the ledger unbiased is a security property of the moat.

**7. Scalability implications.** The two-channel design is what lets load balancing scale to
256 experts without quality collapse. Olympus's version scales the same way: cost/warmth
pressure can spread work across pool members (`_role_map` already breaks ties toward
least-used) without ever contaminating the Wilson-LB evidence in `learned_routing.py`.

**8. Performance implications.** Zero-cost in both systems — it is a discipline about which
number goes where, not a computation.

**9. Maintainability implications.** In Colibri, one comment. Olympus can do better: make the
invariant *typed* — the outcome ledger records the model that ran, plus **why** it was chosen
(`decided_by`), so analysis can always separate biased-choice effects from quality.

**10. How Olympus should redesign it.** Adopt as a written doctrine with two enforcement
points. (a) **Quality accounting is bias-blind:** `routing_outcomes.record_run` rows gain a
`decided_by` field (`"pin" | "learned" | "bandit" | "substitute" | "heuristic"`), and
`learned_routing._aggregate` keeps aggregating success by `(specialist, model)` regardless of
`decided_by` — but the R3 agreement telemetry can now slice by it. (b) **Routing decisions run
at full precision:** the analog of the F32 router is that the *routing decision path itself*
(Zeus's route/answer choice, Athena's plan, `capability_score`/atlas lookup) never runs on a
degraded substrate. Concretely: Zeus and Athena keep `role="reasoning"` on the strongest pool
member (`Specialist.role` in `specialists.py` already encodes this); the selectors stay
deterministic stdlib code (never an LLM call, never sampled); and no cost-optimization mode
may dial Athena's `effort` below the specialist it plans for. Refusing the unsupported
(`n_group != 1`) maps to what `ModelPool.of` already does in sovereign mode: fail closed
(`NoLocalModelError`) rather than half-route.

**11. Final Olympus architecture.**
- `olympus/routing_outcomes.py`: add `decided_by: str` to the row schema (default
  `"heuristic"`; written by `ModelPool.for_specialist` through the existing telemetry hook).
  One new field, no new namespace, same 2000-row cap semantics.
- `olympus/config.py`: no scoring change; `for_specialist` reports its decision source so the
  orchestrator's `record_run` call can log it.
- Doctrine sentence added to `docs/LEARNED_ROUTING.md`: *"Anything may bias which member is
  chosen; nothing may bias what the ledger records about how it performed."*
- No env vars; this rubric is deliberately behavior-neutral (Colibri's byte-identical-when-off
  standard).

**12. Why the Olympus approach is superior.** Colibri inherits its bias from the model and
cannot inspect it; Olympus *generates* its routing bias (cost, warmth, sovereignty) and can
therefore label every decision with its cause, making the score/weight separation auditable
per-row instead of trusted per-comment — and feeding R3's agreement telemetry for free.

---

## R2. Expert top-p — adaptive selection width (§6.3 `TOPP`)

**1. What Colibri does.** Instead of always running the fixed top-8 experts, `TOPP` keeps, per
position, only the experts covering probability mass P — fewer experts on easy tokens, the
full set on hard ones. Measured: −30–40% disk traffic at a slight, honestly stated quality
cost. `TOPK` is the blunter fixed-reduction cousin.

**2. Why it exists.** Fixed K spends the same I/O on every token; routing mass is wildly
uneven, so a mass-based cut converts confidence into savings where confidence is real.

**3. How it works internally.** After sigmoid scoring, experts are taken in rank order until
cumulative normalized mass ≥ P (§6.3 FASE A override). One env var, per-position adaptive.

**4. Strengths.** Adaptivity is per-*decision*, not per-run; the savings concentrate exactly
where the router is confident; it composes with every other tier mechanism.

**5. Weaknesses & trade-offs.** It is lossy and — crucially — it *hides* experts from any
downstream observer: the Expert Atlas found `TOPP` suppressed 38% of distinct experts, which
would have fabricated specialization claims (§19.1). The quality cost is a single global
average; no per-domain accounting of *where* the loss lands.

**6. Security implications.** None direct in Colibri. In Olympus, adaptive width interacts
with capability separation: narrowing a council fan-out must never be the reason a verifying
step (Aletheia) is skipped — width adaptation applies to *specialist breadth*, never to the
verification spine.

**7. Scalability implications.** This is the mechanism that keeps cost sublinear in council
size. Olympus's council is 13 specialists and Athena can in principle dispatch many per run;
a mass-based width policy is what keeps a future 30-specialist registry (via `agentreg`) from
meaning 30× cost.

**8. Performance implications.** In Olympus units, "disk bandwidth" is **token spend +
latency**: every specialist not dispatched is a full agent-loop's tokens saved, and every tool
schema not sent is prompt tokens saved per round (`toolselect.py`'s own docstring already
makes this argument).

**9. Maintainability implications.** A single mass knob is easier to reason about than N
per-surface caps — Olympus currently has fixed caps in three places (`toolselect` max 16,
`dytopo` `_MAX_OUT_DEGREE 3`, Athena's plan width) with no shared policy.

**10. How Olympus should redesign it.** Adopt mass-based adaptive width at the two places that
already rank by relevance, and *record the suppression* so the atlas confound (R5) is
controllable. (a) `toolselect.select` gains a mass mode: instead of "keep top
`OLYMPUS_TOOL_SELECT_MAX`", keep the ranked tail until cumulative lexical-score mass ≥
`OLYMPUS_TOOL_SELECT_MASS` (default off — the fixed cap remains default; the mass mode is the
measured experiment). Essentials/protected tools stay exempt, exactly as today. (b) Athena's
dispatch: the planner already decides fan-out; add a *width report* — the plan records which
candidate specialists were considered-but-not-dispatched (the analog of `TOPP`-hidden
experts), so R5's atlas and R7's predictability measurement can distinguish "never relevant"
from "suppressed by width policy". (c) Consciously **skip** per-position adaptivity inside a
single agent loop (Colibri adapts per token; Olympus's unit of routing is a step, and a
sub-step mass policy would be unmeasurable noise). Every mode change goes through the golden
evals (`olympus eval`) before/after — the `TOPP` quality-cost honesty, ported.

**11. Final Olympus architecture.**
- `olympus/toolselect.py`: `OLYMPUS_TOOL_SELECT_MASS` (float 0<P≤1, unset = current behavior);
  selection stays deterministic, drop-only, post-security — the three guarantees in its
  docstring are unchanged.
- Orchestrator/Athena: plan rows gain `considered: [...]` alongside dispatched specialists;
  flows into `trace.py` and is readable by `atlas.py` and `coupling.py`.
- `olympus/dytopo.py`: unchanged (its caps are safety bounds, not quality knobs — different
  jobs, keep them separate).
- Measurement: a `gate_prompt`-style before/after eval is mandatory to flip the default,
  matching the README's "nothing ships without a before/after benchmark".

**12. Why the Olympus approach is superior.** Colibri's `TOPP` saves I/O but silently blinds
its own research instrumentation (the 38% confound). Olympus's version records what it
suppresses at the moment of suppression, so the width policy and the atlas can never lie to
each other — the weakness becomes the design.

---

## R3. CACHE_ROUTE — substitution within a measured rank, with agreement telemetry (§6.3, §19.3)

**1. What Colibri does.** `CACHE_ROUTE` (from arXiv:2412.00099 "max-rank") is the only
mechanism allowed to *change which experts run* for speed: the true top-J experts are always
kept, the remaining K−J slots prefer already-resident experts within rank M (or probability
mass P), `ROUTE_ALPHA` down-weights substituted experts' gate contribution, and **full
agreement telemetry** — swap%, top-K overlap, KL divergence between chosen and true routing —
makes the loss permanently visible. Explicitly contrasted with PILOT prefetch, which never
changes routing (§6.3, §7.5).

**2. Why it exists.** In a disk-streaming MoE, a resident 9th-ranked expert can be worth more
than a cold 5th-ranked one — but only if the substitution is bounded (rank band), hedged
(down-weight), and measured (telemetry).

**3. How it works internally.** FASE A computes true routing; the substitution pass rescans
the ranked list preferring residency inside the band; per-token counters accumulate swap% /
overlap / KL and print with the run report.

**4. Strengths.** The three-part contract — *protect the head, substitute only inside a band,
telemeter the divergence* — is the correct general shape for any lossy router. It is opt-in,
reversible, and its cost is a number, not an anecdote.

**5. Weaknesses & trade-offs.** The band M/P is set by hand, not derived from measured quality
equivalence; KL is measured against the model's own routing distribution, not against outcome
quality (a swap could be KL-large and quality-neutral, or the reverse); telemetry aggregates
per run, so nobody can later ask "did substituted decisions fail more?"; `ROUTE_ALPHA` is a
heuristic hedge with no feedback loop.

**6. Security implications.** For Olympus this is the highest-stakes rubric: a substitution
tier is a mechanism that deliberately routes work to a *worse-ranked* member. It must sit
**after** the sovereign filter (`ModelPool.of` filters members before any selection — a
substitution can never resurrect a remote model in sovereign mode) and must never substitute
across a data-class boundary (`local_only()` pools substitute only among local members). The
security gate and capability separation in `specialists.py` are untouched — substitution picks
*models*, never tools or capabilities.

**7. Scalability implications.** This is Olympus's cost lever as pools grow: with 3–5 members
of overlapping quality, banded substitution converts the Calibration Record into direct spend
reduction ("Haiku is within the band of Sonnet for Chronos's scheduling tasks — measured, not
assumed"). Per-cell evidence requirements mean the mechanism scales exactly as fast as the
ledger does — an accumulation asset, per `MOAT_ANALYSIS.md` Asset 2.

**8. Performance implications.** Colibri's "resident" maps to two Olympus warmth/cost signals:
**price tier** (live `price_per_mtok`, already used for tie-breaks in `_role_map`) and
**warmth** (a member with a warm HTTP session / recent prompt-cache hits has lower TTFT).
Substitution within a measured-equivalent band is pure savings; outside it, it is quality
loss, which is why the band must come from data.

**9. Maintainability implications.** Done wrong, this becomes a fourth selector fighting three
existing ones. Done right, it is one more layer in the *already documented* precedence chain
(`docs/LEARNED_ROUTING.md` "Selection precedence"), with the same contract as
`learned_routing.choose` / `bandit_routing.choose`: same signature, returns None to abstain,
never raises.

**10. How Olympus should redesign it.** Build `olympus/routesub.py` as the third pluggable
selector with a **measured band instead of a hand-set rank**: a challenger may substitute for
the quality-first pick only when *both* cells are known (≥ `MIN_CELL_SAMPLES`, reusing
`learned_routing` machinery) and the challenger's Wilson lower bound is within
`OLYMPUS_ROUTE_BAND` (default 0.05) of the incumbent's — i.e., substitution is only permitted
where the ledger says the models are statistically interchangeable for this specialist. Among
band-equivalent members, prefer cheaper (`price_per_mtok`), then warmer (a small in-process
warmth map: last-use timestamp per member, the analog of residency). The true top-J protection
maps to a **protected-role set**: `verify` (Aletheia) and any specialist whose
`Specialist.effort == "high"` and role is `reasoning` for orchestration (Zeus/Athena) are
never substituted — the head of the routing is sacred, exactly as in max-rank. `ROUTE_ALPHA`
translates not as down-weighting the answer (Olympus can't mix outputs) but as **raising
scrutiny**: substituted runs are flagged so Aletheia's verification sampling and `liveeval`
prefer them — the hedge becomes extra verification instead of a smaller gate weight.
**Agreement telemetry is mandatory and per-decision**: every substitution writes the row with
`decided_by="substitute"`, `incumbent_model=<quality-first pick>`; `olympus routing-stats`
grows a swap section — swap% overall and per specialist, and the outcome-rate delta between
substituted and non-substituted rows for the same `(specialist, task_type)` — the KL analog,
but computed against *outcomes*, which is strictly more meaningful than distributional
divergence (weakness 5 fixed).

**11. Final Olympus architecture.**
- **New `olympus/routesub.py`** (~150 lines, stdlib only): `enabled()`
  (`OLYMPUS_ROUTE_SUBSTITUTE=1`, default OFF; forced off under `OLYMPUS_REPLAY` like both
  existing selectors), `choose(members, specialist, quality_pick)` (same seam), band from
  `OLYMPUS_ROUTE_BAND`, warmth map fed by `backend.py` call completions, protected roles from
  `config.specialist_role` + a `PROTECTED_ROLES = {"verify"}` constant.
- `olympus/config.py` `for_specialist`: precedence becomes
  `pin > (bandit | learned) > substitution > heuristic` — substitution runs on the winner of
  the quality layers, so it substitutes within *their* rank, never around them. Sovereign
  filtering still precedes everything (it shapes `self.members`).
- `olympus/routing_outcomes.py`: `decided_by` + `incumbent_model` fields (R1);
  `stats()` gains the swap/outcome-delta aggregation.
- `olympus/aletheia`-side: the orchestrator passes `substituted=True` through to verification
  sampling weight (integration point, one flag).
- CLI: `olympus routing-stats` prints the swap section; env docs in
  `docs/LEARNED_ROUTING.md`.

**12. Why the Olympus approach is superior.** Colibri's band is a config guess and its KL is
outcome-blind; Olympus's band is *derived from the same evidence ledger the selector already
trusts*, its hedge is real extra verification rather than a scalar, and its agreement
telemetry joins to outcomes per-decision — so lossy routing is not only visible, it is
continuously re-priced, and it tightens itself as the Calibration Record accumulates.

---

## R4. The EXPERT_BUDGET quarantine — negative results as infrastructure (§6.3, §26.4)

**1. What Colibri does.** `EXPERT_BUDGET` (a per-layer distinct-expert cap) is fully
implemented, measured catastrophic (#303: hellaswag 30% vs 90%, MTP acceptance 0%, *slower*
than baseline — "empty operating window"), and **quarantined at startup**: it refuses to run
without `EXPERT_BUDGET_EXPERIMENTAL=1`, and the eulogy lives in `main()` where the next
maintainer will read it. §3.1 names this a philosophy: "negative results are preserved as
opt-in flags with written eulogies rather than deleted."

**2. Why it exists.** A deleted failure gets re-invented; a silently-available failure gets
re-enabled by a curious user and blamed on the engine. Quarantine preserves the code, the
measurement, and the warning as one artifact.

**3. How it works internally.** A startup check gates the env var behind the second
`_EXPERIMENTAL` var and prints the measured reason; the feature code itself is untouched.

**4. Strengths.** Institutional memory at zero marginal cost; honest ("we built it, it
failed, here's the number"); reversible if the operating window ever opens (the #328-class
"inverts if TC grouped GEMM lands" pattern in §10.4 shows windows do reopen).

**5. Weaknesses & trade-offs.** It is ad-hoc — one flag, one comment, no registry; nothing
re-tests the eulogy when conditions change; nothing stops quarantined code from bit-rotting
until the double opt-in crashes; CI doesn't verify the quarantine actually holds.

**6. Security implications.** A quarantined *routing* feature is a latent behavior change; in
Olympus, anything that alters routing or capability must not be reachable by prompt injection
or config drift. A formal registry lets the security gate and tests enumerate every
quarantined switch — an auditable surface instead of scattered flags.

**7. Scalability implications.** Olympus's experiment surface is already larger than
Colibri's (learned vs bandit selectors, `OLYMPUS_DYTOPO`, `OLYMPUS_SWARM`, the R2/R3/R6
proposals here, Prometheus's `gate_prompt` rollbacks). Without a registry, the number of
half-remembered dead flags grows with every experiment; with one, experiment count scales
without lore-debt.

**8. Performance implications.** None at runtime (a dict lookup at flag-check time). The
payoff is engineer-time: never re-running a measured-dead experiment.

**9. Maintainability implications.** This is *the* small-team capability. Olympus already has
the culture pieces — `DEFERRED.md`, `docs/DECISION_LOG.md`, `sleeptime.py`'s quarantine
namespace for rejected rewrites, `gate_prompt` auto-rollback — but no single mechanism that
says "this feature exists, is off, and here is the benchmark that killed it."

**10. How Olympus should redesign it.** Build a formal registry, **beyond Colibri** in three
ways: entries are structured data (not comments), each carries a machine-checkable *retest
condition*, and CI enforces the quarantine. `olympus/quarantine.py` holds a static
`QUARANTINED: dict[str, Entry]` where `Entry = {flag, eulogy, evidence (benchmark ids /
docs link), quarantined_on, retest_when}`. `quarantine.gate(name)` implements the double
opt-in: the feature's own flag **and** `OLYMPUS_EXPERIMENTAL_<NAME>=1`, else it logs the
eulogy once and returns False. Every failed routing experiment from this absorption program
lands here instead of being deleted: e.g., if R2's mass-mode toolselect regresses the golden
evals, it ships quarantined with its eval delta, exactly like `EXPERT_BUDGET` shipped with
its hellaswag numbers. The retest hook rides the heartbeat: when `retest_when` names a
condition (`"pool has ≥3 members"`, `"gate_status().met"`), the weekly self-audit
(Prometheus) surfaces "quarantined feature X's window may have opened" as a proposal — never
auto-enables. CI gets one test: every `QUARANTINED` entry's flag, when set *without* the
experimental override, produces no behavior change (the drift-gated capability-count pattern
from the README, applied to negative results).

**11. Final Olympus architecture.**
- **New `olympus/quarantine.py`** (~80 lines): registry + `gate(name)` + `status()` for
  `olympus doctor` / `olympus routing-stats`.
- `DEFERRED.md`: each registry entry links to its section — the registry is the index, the
  doc is the narrative.
- `olympus/heartbeat.py`: weekly audit calls `quarantine.retest_candidates()`; Prometheus
  files `propose_upgrade` notes for opened windows.
- Env convention: `OLYMPUS_EXPERIMENTAL_<NAME>` (mirrors Colibri's
  `EXPERT_BUDGET_EXPERIMENTAL=1` double opt-in).
- Tests: `tests/test_quarantine.py` asserts no-behavior-change-when-gated for every entry.

**12. Why the Olympus approach is superior.** Colibri's quarantine is a convention enforced
by one maintainer's discipline; Olympus's is a registry enforced by CI, indexed for auditors,
and wired to the heartbeat so eulogies are *re-examined* when the world changes — negative
results become a governed, compounding asset instead of folklore in `main()`.

---

## R5. The Expert Atlas → the Specialist Atlas (§19.1)

**1. What Colibri does.** A probe battery (10 topic categories × 3 prompts) maps all 19,456
experts' topic affinities. `sweep.sh` controls four measured confounds — `TOPP` hides 38% of
distinct experts; speculative drafts count unemitted routing; `.coli_usage` accumulates
across runs; CUDA tier nondeterminism — plus a fifth in analysis: autocorrelation (one prompt
= one observation), fixed by a **replication gate across independent prompts that removed
587 fake specialists**. Validation: leave-one-prompt-out classification 29/30 = 96.7% vs 10%
chance. Findings: routing follows task over language; only 7.9% of experts are strong
specialists. The atlas feeds the dashboard and the site galaxy (§14.2, §16).

**2. Why it exists.** Routing intelligence is only as good as the map of what each routee is
actually good at — and naive measurement fabricates specialists out of confounds.

**3. How it works internally.** Controlled engine runs with confound-suppressing env
(`TOPP` off, drafts off, fresh usage files, deterministic tier), `ROUTE_TRACE` dumps,
offline analysis with replication + LOPO gates, published `experts.json`.

**4. Strengths.** It is a *research program*, not a script: confounds are enumerated and
individually measured; claims must replicate; validation is out-of-sample; the output is
consumed by real surfaces. The 7.9% finding is exactly the kind of honest result a
measurement-first culture produces.

**5. Weaknesses & trade-offs.** The probe set is small and hand-curated (10 topics — the
atlas can only see the axes someone thought to probe); it is a one-shot offline campaign, so
it drifts as usage patterns change; probes cost full inference runs; labels are topical
affinity only, not *quality* (a frequently-routed expert isn't necessarily a good one —
Colibri cannot measure per-expert quality at all).

**6. Security implications.** Probe traffic must be unmistakably synthetic: Olympus already
has the flag (`OLYMPUS_ROUTING_SYNTHETIC=1` excludes rows from the Phase B gate,
`docs/LEARNED_ROUTING.md`) — atlas probes MUST set it, or the atlas would feed its own probe
results into the adoption gate it is supposed to be independent of (the `.coli_usage`
confound, ported). Atlas output modifies routing *descriptions*, which Zeus reads — so atlas
writes go through the same review discipline as prompt changes (`gate_prompt`), never raw.

**7. Scalability implications.** The atlas is what lets the registry grow: today 13
specialists are routed by hand-written `description` fields (`specialists.py`) and models by
hand-typed `_CAPABILITIES` keyword scores (`config.py:301` — "a hint, not a precise
leaderboard", per absorption 01). At 30+ file-defined agents (`agentreg`) and 5-member pools,
hand-maintained routing knowledge stops scaling; measured affinities are the only version
that keeps up.

**8. Performance implications.** Probes cost real API tokens — the binding Gate-cost rule
(`ROADMAP.md` §0 rule 2) applies. Mitigation: the atlas has two feeds — a **cheap passive
feed** (the routing-outcomes ledger + `trace.py`, free, always on) and a **bounded active
probe feed** (`OLYMPUS_ATLAS_BUDGET` tokens per training round, run inside the existing
`olympus train` / heartbeat cadence, on the operator's own credentials like the background
audit). Colibri had only the active feed; Olympus's passive feed is **beyond Colibri** —
Colibri cannot observe outcomes, Olympus records them natively.

**9. Maintainability implications.** One schema, three consumers (Zeus routing, R3 bands,
`olympus scores`) beats three ad-hoc capability maps. The replication gate is cheap to keep
honest: it is a filter in analysis code, testable with fixture ledgers.

**10. How Olympus should redesign it.** Build `olympus/atlas.py` as the confound-controlled
aggregation layer over data that already exists, plus a bounded probe generator. The five
Colibri confounds translate one-for-one and each gets an explicit control: (1) *TOPP hides
experts* → toolselect/width suppression hides candidates: R2's `considered` records make
suppression visible, and probe runs disable trimming (`OLYMPUS_TOOL_SELECT=0`); (2)
*speculative drafts count unemitted routing* → retries, fallback-chain attempts
(`fallbacks_for`), and abandoned plan branches must not count as routing observations: atlas
reads only *completed, outcome-labeled* rows; (3) *`.coli_usage` accumulation* → the ledger
mixes epochs (prompts and pools change over time): atlas windows by time and by
prompt-version (`gate_prompt` bumps a version stamp); (4) *tier nondeterminism* → provider
nondeterminism: affinity claims must hold across provider/model, or be labeled per-model;
(5) *autocorrelation* → one conversation = one observation: replication requires ≥N distinct
`run_id`s across ≥2 distinct users (the ledger already stores both), the direct port of the
gate that killed 587 fake specialists. Validation is LOPO's port: **leave-one-task-type-out**
— an atlas claim ("Plutus excels at `finance/xl` tasks on model M") must predict held-out
outcome rows better than the keyword heuristic baseline, or it is not published. Published
atlas entries then feed three consumers: (a) `Specialist.description` routing hints for Zeus
get a measured "verified strengths" appendix (regenerated, gated, never hand-edited); (b)
R3's substitution bands read atlas cells instead of raw ledger cells once atlas quality
exceeds them; (c) `olympus scores` displays measured affinity alongside benchmark score.

**11. Final Olympus architecture.**
- **New `olympus/atlas.py`**: `build(window=None) -> Atlas` (pure aggregation over
  `routing_outcomes._all_rows()` + `trace.py` step graphs, with the five confound filters);
  `replication_gate(claims)`; `lopo_validate(atlas)`; `probe_plan(budget)` (generates probe
  tasks per specialist × task_type × length_bucket cell below the replication threshold —
  probes target the *sparsest* cells, Colibri-style deliberate coverage); persisted as
  `memory/atlas.json` with a `_provenance` history (the `quality_baseline.json` pattern).
- Env/CLI: `OLYMPUS_ATLAS=1` to let consumers read it (default off until LOPO passes);
  `OLYMPUS_ATLAS_BUDGET` (tokens per round, default conservative); `olympus atlas`
  (build + report), `olympus atlas --validate` (LOPO report).
- Integration: heartbeat training round calls `atlas.probe_plan` under budget with
  `OLYMPUS_ROUTING_SYNTHETIC=1`; Prometheus's audit reads `atlas.json` to pick the weakest
  cells (sharpening the existing "train the weakest first" loop with per-cell resolution).
- Research spike (bounded): whether outcome-labeled passive data alone reaches replication
  thresholds on a single-user deployment, or probes are load-bearing — 2 weeks against a
  fixture ledger + one live deployment, before `probe_plan` is built at all.

**12. Why the Olympus approach is superior.** Colibri's atlas measures *affinity* (where
routing mass goes) because it cannot see quality; Olympus's measures *competence* (where
outcomes are good), joins it to cost, and refreshes continuously from free passive data with
probes only where the ledger is sparse. And it inherits the exact confound discipline that
made Colibri's atlas trustworthy — the replication gate is the difference between a
Specialist Atlas and a wall of confirmation bias.

---

## R6. Route coupling — co-invocation tables and prefetch (§7.5 COUPLE, §19.2)

**1. What Colibri does.** `route_coupling_report.py` screens cross-layer expert
co-activation with copula/Fréchet-bound dependence analysis (median lift 1.8×, p99 40×);
`route_pairs.py` productionizes it into the `.coli_pairs` table (`COLIPAIRS 1 <n>` header,
§4.5) that the engine consumes via `COUPLE=`, feeding prefetch hints 1–2 layers ahead
through the same lock-free ring as PILOT. Measured +3.6..+9.4 pp prefetch recall over
marginal heat, and the tables *transfer across workloads*. Crucially, COUPLE (like PILOT)
**never changes routing** — it is purely warmth-side (§6.3's explicit contrast).

**2. Why it exists.** Marginal popularity ("this expert is hot") is a weaker predictor than
conditional structure ("when L7.42 fires, L8.190 fires at 40× base rate"); exploiting the
dependence turns idle I/O time into hit rate.

**3. How it works internally.** Offline: ROUTE_TRACE dumps → dependence screening with
proper statistical bounds → equal-budget prefetch simulation (coupled vs marginal scoring,
train/test transfer) → text table. Online: table lookup on each routed expert emits hints
for the next layers into the prefetch ring.

**4. Strengths.** Offline-heavy/online-cheap split; the *simulation before deployment*
step (equal-budget A/B on recall) means the mechanism was proven on paper before touching
the engine; strict advisory-only contract makes it unable to change answers.

**5. Weaknesses & trade-offs.** Pairs only (no higher-order structure); the table is static
between regenerations; recall gains are modest and workload-dependent; and in Colibri the
prefetch currency (disk bandwidth) is nearly free, which hides the question Olympus must
answer first: *is the prefetched thing cheap enough to speculate on?*

**6. Security implications.** Prefetch must be side-effect-free. In Olympus that is a hard
constraint: pre-warming may touch **reads with no egress consequence** (local memory recall,
skill file loads, context assembly, codegraph queries) and **connection warmth** (HTTP
session establishment to an already-authorized provider), but never tool execution, never a
speculative *model call carrying user content* (that would send data ahead of a decision —
in sovereign mode potentially off-box), and never anything the security gate would screen.
The PILOT eviction-guard lesson (#441/#490: speculation may not evict warm residents)
translates: prefetched context may never displace the *actual* current step's context
budget (`ace.py` trimming must treat prefetched blocks as lowest priority).

**7. Scalability implications.** Coupling tables are per-deployment accumulation — another
Asset-3-shaped artifact (labs can't see which specialists this customer co-invokes). Table
size is trivially bounded (13×13 specialists, ~130 tools → top-N pairs).

**8. Performance implications.** The Olympus win is **latency, not tokens**: while
specialist A runs (seconds to minutes of API latency), the orchestrator can assemble
specialist B's context — memory recall, skill index reads, `toolselect` pre-ranking,
provider session warmth — off the critical path, so B's dispatch is context-ready the
moment A completes. In Athena's dependency graphs the *serial chains* are exactly where
this pays: dependent steps currently begin cold after their input arrives. Token cost of
prefetch as designed: zero (all local reads + connection warmth). Anything token-costing
(e.g., speculative prompt-cache priming) is out of scope here and belongs to the memory
hierarchy domain (see tensions).

**9. Maintainability implications.** One table format, one generator, one consumer seam in
the orchestrator. The offline analysis is a heartbeat job, not a service.

**10. How Olympus should redesign it.** Two stages, Colibri's own order: **measure first
(R7), simulate second, ship third.** `olympus/coupling.py` mines `trace.py` run graphs and
the routing-outcomes ledger for co-invocation structure at three grains: specialist→
specialist within a run (Athena chains), specialist→tool (which loadout items actually fire
per specialist × task_type — sharpening `toolselect` ranking with measured priors, beyond
lexical score), and skill co-loads. Dependence scoring ports the discipline, not the math:
lift with a minimum-support floor and a train/test transfer check (the copula machinery is
overkill for a 13-node graph — *consciously simplified*, and said so). Output:
`memory/route_pairs.json` (versioned header, the `.coli_pairs` convention). The consumer is
an orchestrator hook `prefetch_for(next_candidates)` that runs during a step's API wait:
warms provider sessions, pre-runs `recall_memory`/skill-index reads for the top coupled
candidates, and pre-computes `toolselect` rankings. **Advisory-only is contractual**: the
hook returns warmth, never a routing opinion — `for_specialist` never reads it (the
PILOT/CACHE_ROUTE separation, kept). Equal-budget simulation before shipping: replay
recorded traces (`replaystore.py`) and score "was the next-dispatched specialist in the
prefetch set?" — coupled vs marginal-frequency baseline, the exact §19.2 experiment shape.

**11. Final Olympus architecture.**
- **New `olympus/coupling.py`**: `mine(window) -> PairTable`, `save/load`
  (`memory/route_pairs.json`), `prefetch_candidates(current, table, k=2)`,
  `simulate(traces, table)` (the equal-budget recall A/B), plus R7's
  `predictability_report()`.
- Orchestrator: one call site in the dispatch loop, guarded by `OLYMPUS_COUPLE=1`
  (default OFF until the simulation shows recall over the marginal baseline —
  quarantine-ready if it doesn't, per R4).
- `olympus/heartbeat.py`: regenerates the table on the training cadence
  (`OLYMPUS_TRAIN_EVERY` piggyback).
- Warmth side-channels: `backend.py` session reuse (already keeps HTTP sessions — the
  prefetch just touches them early); `skills.py` index preload; `memory` recall executed
  concurrently via the existing async paths.
- Explicitly rejected (with reason): speculative *dispatch* of the coupled-next specialist
  before its input exists — that is Colibri's rejected GPU-staging experiment (§10.4: "real
  signal, net loss from contention"), and in Olympus it costs real tokens on a guess.

**12. Why the Olympus approach is superior.** Colibri prefetches bytes; Olympus prefetches
*readiness* — and because its prefetch currency (local reads + connection warmth) is
genuinely free, it keeps all of COUPLE's latency upside with none of the bandwidth
contention that sank Colibri's own GPU-staging variant. The table doubles as routing
science: the same co-invocation structure that drives prefetch tells Athena's planner which
specialist chains actually occur, feeding plan quality — a consumer Colibri never had.

---

## R7. LOOKA — routing-predictability measurement before mechanism (§7.5)

**1. What Colibri does.** `LOOKA` is a pure measurement mode: it runs four routing
predictors side-by-side against actual routing and prints a predictability report at exit —
zero behavior change. It is why Colibri *knew* PILOT's router-state prediction (71.6%
recall) beat previous-token heuristics (41.3%) before building the prefetch machinery, and
why the GPU-staging variant could be rejected on contention rather than on signal (§10.4).

**2. Why it exists.** Every prefetch/speculation mechanism is bounded by routing
predictability; measuring the ceiling first prevents building mechanisms against noise.

**3. How it works internally.** Predictor shadow-evaluation inside the decode loop with
counters, report at exit; instrumentation follows the byte-identical-when-off doctrine
(§18's DISK-CLASS private-clock pattern).

**4. Strengths.** It is the cheapest possible de-risking: a report, not a feature. It ranks
predictor families on *your* workload before any is productionized.

**5. Weaknesses & trade-offs.** Exit-report-only (no accumulation across runs); predictor
set is fixed in C; nothing re-runs it as workloads drift.

**6. Security implications.** None — read-only shadow evaluation. The Olympus port must keep
it that way: predictors read the trace, never influence it (the `routing_outcomes.py` Phase
A "passive sensor" contract, verbatim).

**7. Scalability implications.** Trivial; it is offline analysis over the ledger.

**8. Performance implications.** Zero online cost in Olympus (unlike Colibri, which shadows
in the hot loop, Olympus can compute predictability entirely offline from `trace.py` +
routing-outcomes rows, because the ledger already records the ground truth).

**9. Maintainability implications.** One report function keeps every future routing
experiment honest — and gives R4's quarantine registry its evidence format.

**10. How Olympus should redesign it.** `coupling.predictability_report()`: over a trace
window, evaluate a small fixed predictor ladder for "which specialist/model handles the
next step": (P1) marginal frequency, (P2) previous-specialist conditional (the coupling
table), (P3) task-type keyword heuristic (what Zeus/Athena effectively do), (P4) Athena's
own declared plan (does the executed graph match the planned graph? — plan adherence is
Olympus-specific and **beyond Colibri**: it measures whether the planner itself is the best
predictor, which decides whether prefetch should follow the *plan* or the *statistics*).
Report top-1/top-2 recall per predictor, per task_type, printed by `olympus routing-predict`.
Doctrine: **no routing mechanism from this document ships unless this report shows its
predictor family beating the marginal baseline** — R6's `OLYMPUS_COUPLE` default flips only
on P2 > P1 by a stated margin; a failed margin sends the feature to R4's registry with the
report as its eulogy.

**11. Final Olympus architecture.** Part of `olympus/coupling.py` (no separate module — the
predictors and the pair table share all their data plumbing); CLI `olympus routing-predict`;
heartbeat attaches the report to the weekly self-audit so drift in predictability is seen,
fixing Colibri's exit-report-only weakness with accumulation (the report history lives next
to `atlas.json` with the same `_provenance` pattern).

**12. Why the Olympus approach is superior.** Colibri measures predictability per-run and
forgets; Olympus measures it from a persistent ledger, tracks it over time, adds the
plan-adherence predictor no inference engine could have, and hard-wires the result into the
shipping decision for every other rubric in this domain — LOOKA upgraded from a diagnostic
to a gate.

---

## Open questions & research spikes

1. **Passive-only atlas sufficiency (R5, bounded: 2 weeks).** Can a single-tenant
   deployment's outcome ledger alone clear the replication gate (≥N run_ids, ≥2 users) for
   enough cells to be useful, or are budgeted probes load-bearing from day one? Decides
   whether `atlas.probe_plan` is built in v1 or deferred. Run against a fixture ledger plus
   one real deployment's anonymized rows.
2. **Substitution-band width (R3).** Is a Wilson-LB delta of 0.05 the right default
   equivalence band, and should it be per-task_type? Needs the R3 telemetry running for one
   accumulation cycle before any default is claimed; ship at a conservative 0.02 with the
   band itself listed in `olympus routing-stats`.
3. **Ledger capacity (R1/R3/R5).** `routing_outcomes._MAX = 2000` rows/user was sized for a
   selector, not for atlas + swap-delta analysis. Options: raise the cap, or add a compacted
   per-cell aggregate namespace that survives row eviction. The aggregate must preserve the
   replication gate's distinct-run_id counting — a design note, not a spike.
4. **Prefetch ownership tension (R6 vs absorption 02).** Connection/context warmth is
   claimed here; prompt-cache and memory-tier warming likely belong to the memory-hierarchy
   domain. The synthesizer must assign a single owner for "warmth" or the two domains will
   ship duplicate hooks. Proposal: coupling.py *decides who to warm*, the memory hierarchy
   *implements what warm means*.
5. **Selector proliferation (R3).** `for_specialist` will hold four layers (pin >
   learned/bandit > substitute > heuristic). At what point does the precedence chain itself
   need a golden regression test asserting byte-identical decisions on a fixture ledger?
   (Answer: at R3 merge — cheap to write now, painful to retrofit.)
6. **DyTopo interaction (R6).** If `OLYMPUS_DYTOPO`/`OLYMPUS_SWARM` topologies activate,
   co-invocation structure changes regime; coupling tables should be keyed by topology mode
   or they will blend incompatible distributions. Deferred until DyTopo leaves opt-in.
