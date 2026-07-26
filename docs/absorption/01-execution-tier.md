# Absorption 01 — The Model Execution Tier

**Colibri domain:** the core engine philosophy (§3), quantization formats & mixed-precision
policy (§5 — fmt taxonomy, IDOT, E8 lattice, CFSE), CPU SIMD/GPU backends & determinism
doctrine (§10, §11), token-exact oracle validation (§3.1, §23), conversion pipelines (§5.5).
**Olympus target:** the provider/model execution layer — `olympus/backend.py`,
`olympus/providers.py`, `olympus/llm.py`, `olympus/config.py` (ModelPool), `olympus/modelpin.py`,
`olympus/evals.py`, plus `docs/SOVEREIGNTY.md` and `docs/CALIBRATION_RECORD.md`.

## Domain thesis

Colibri's execution tier is a masterclass in one translation: **placement decides speed,
never answers** — every byte-saving trick (int4, int3-g64, E8 lattice, rANS) ships with a
token-exact oracle or an honestly-measured quality delta, and every accelerator fails soft
while every data corruption fails hard. Olympus's analog of a quantization bit-width is a
**model-price tier**: routing Aletheia's verification to Haiku instead of Opus is exactly
"quantizing the attention projections" — and Colibri measured that specific mistake at +12%
perplexity and *forced exact kernels there forever*. Olympus already has the skeleton
(`ModelPool._role_map`, `capability_score`, `_fallback_chain`, sovereign fail-closed pool
filtering, `replaygate` byte-identical replay) but its capability ladder is **vibes**
(`config.py` `_CAPABILITIES`: hand-typed keyword scores, "a hint, not a precise
leaderboard"). The absorption below converts that ladder into a measured, accumulated asset —
which is precisely the Calibration Record moat thesis (`docs/MOAT_ANALYSIS.md` Asset 1/2)
applied to the execution layer — and gives the sovereignty tier the first-class,
qualification-gated local-engine membership that `docs/SOVEREIGNTY.md` currently only sketches
as "point `OLYMPUS_BASE_URL` at Ollama."

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| Single-file engine philosophy | one TU, zero deps, fail-soft accel / fail-hard data, measured comments (§3.1) | **absorb-principle** | `backend.py`, `llm.py`, `security.py` (doctrine, not code) |
| Quantization format taxonomy + mixed-precision policy | 7 `QT.fmt` layouts; converter `classify()` per tensor class; MTP-must-be-int8 (§5.1, §5.6) | **redesign** | `config.py` (ModelPool), **new `olympus/modelgrade.py`** |
| IDOT activation quantization & dispatch policy | int8 activations on SIMD ladders; exact kernels forced on attention; `I4S` batch thresholds (§5.2–5.3) | **redesign** | `backend.py`, `config.py`, `llm.py` (`effort`) |
| E8 lattice + CFSE entropy codec | 3.06 bpw rotated codebook; rANS with whiteness proof + integrity seals (§5.1 fmt 6, §5.4) | **absorb-principle** (codec itself: skip) | `ace.py`, `config.py` history budgets, `replaystore.py` |
| Token-exact oracle + quant ablation harness | tiny true-architecture fixture, TF 32/32; engine-free A/B "the delta IS the cost" (§3.1, §5.7, §23) | **new-subsystem** | **new `olympus/modelgate.py`**, `evals.py`, `replaygate.py`, `quality_baseline.json` |
| CPU SIMD / GPU backends & determinism doctrine | per-backend kernels, bitwise-exact parallel router, fail-at-startup on silent-CPU, no-atomics (§10, §11) | **redesign** | `backend.py`, `replaygate.py`, `compare.py`, `openai_compat.py` |
| Conversion pipelines | resumable shard-at-a-time converters, parameter manifest, repair surgery, size-verified downloads (§5.5) | **new-subsystem** | **new `olympus/localtier.py`**, `providers.py`, `doctor.py`, `heartbeat.py` |

---

## R1. The single-file engine philosophy (§3.1)

**1. What Colibri does.** The entire 744B-model engine is one C translation unit plus
header modules; the default build has zero external libraries. Optimizations carry measured
justifications in comments; negative results are preserved as quarantined flags with written
eulogies (`EXPERT_BUDGET`, §26.4). Accelerator failures degrade silently to correct-but-slower
paths; corrupt model containers `exit(1)` ("reject, never repair"). All model inputs are
treated as hostile (untrusted-mirror threat model, §3.1, §4.1).

**2. Why it exists.** A single maintainer cannot afford build systems, dependency rot, or
optimizations whose value is unverifiable. The philosophy converts smallness into rigor.

**3. How it works internally.** Header-only modules with no engine dependencies
(`quant.h` is standalone); `qt_resolve_fmt` as a byte-count-exact security boundary (#413);
issue numbers as design provenance in comments.

**4. Strengths.** Reviewability (one file = one audit surface); the fail-soft/fail-hard split
is exactly right (speed lies are recoverable, data lies are not); negative results preserved
means no one re-derives a dead end.

**5. Weaknesses & trade-offs.** A 6,751-line TU is a bus-factor-of-one artifact; "single file"
is a *discipline* that C forces and Python does not; comment-borne measurement decays when the
hardware under it changes; the philosophy scales to one engine, not to a 230-module platform
(exactly the F1/F16 "debt, not inventory" finding in `docs/ROADMAP.md` §0).

**6. Security implications.** The untrusted-input doctrine is the transferable gold: Colibri
validates *model containers* the way Olympus's security gate validates *commands*. Olympus's
equivalent hostile inputs are provider responses, model lists (`providers.fetch_models`
returns `[]` on any error — good), and local-engine outputs; R7 extends this to local model
onboarding.

**7. Scalability implications.** Zero-dep single-file scales *down* (any box) not *out* —
which matches Olympus's "three pure-Python dependencies, runs anywhere" positioning
(README §Headless-first). The principle to keep: optional capability = optional dependency,
lazily imported (`bedrock_converse`, `moa` already do this in `backend.py`).

**8. Performance implications.** Measurement-driven comments are Colibri's real perf engine:
nothing ships un-benchmarked. Olympus's analog already exists as culture (`gate_prompt`
auto-rollback, README §Self-improving) but not yet at the *execution layer* — model/tier
changes today are not gated (fixed by R5).

**9. Maintainability implications.** "Truth lives only at `getenv()` sites" (§24) is directly
adoptable: Olympus's `config.py` is already the single `OLYMPUS_*` choke point; the drift-gated
capability counts in CI (README) are the same idea. Adopt Colibri's *eulogy rule*: a removed
or quarantined feature keeps its measured reason in `DEFERRED.md`, never silent deletion.

**10. How Olympus should redesign it.** Adopt as written doctrine, not code: (a) **one
dispatch seam** — `backend.py` stays the only place provider identity is known (it already is;
enforce with a CI grep that no other module imports `anthropic`/`openai_compat` for
completions); (b) **fail-soft for speed, fail-hard for data** — a failed cheap-tier call may
fall back to a stronger member (`_with_failover`), but a *semantic* outcome (refusal,
`ReplayDivergence`, schema violation) must surface unchanged — `_should_failover` already
encodes exactly this split; document it as the invariant it is; (c) **eulogy rule** into
`DEFERRED.md` process.

**11. Final Olympus architecture.** No new module. Additions: a short
`docs/EXECUTION_DOCTRINE.md` section (or a header block in `backend.py`) stating the three
invariants above; a CI test `test_backend_is_sole_dispatch_seam` (AST scan, same machinery as
`capabilities.py` drift gates). Env: none.

**12. Why the Olympus approach is superior.** Colibri enforces the philosophy by C's pain;
Olympus enforces it by CI — the discipline survives contributors who never read the doctrine.
And where Colibri's fail-hard is `exit(1)`, Olympus's is a *typed refusal* (`EgressBlocked`,
`NoLocalModelError`) that the council can route around deliberately, with the refusal recorded
in the signed trace.

---

## R2. Quantization format taxonomy + mixed-precision policy (§5.1, §5.6)

**1. What Colibri does.** Seven weight formats from F32 to 3.06-bpw E8 lattice (§5.1), and a
converter `classify()` that assigns precision *per tensor class* (§5.6): router weights F32
always; norms/biases F32; embed/head int8; routed experts int4; the MTP head **int8 mandatory**
because per-row int4 zeroed half its embedding and collapsed draft acceptance to 0–4% (#8).

**2. Why it exists.** 370 GB of experts cannot all be F32; but not all tensors tolerate the
same loss. Precision is spent where sensitivity is measured, saved where it is not.

**3. How it works internally.** Byte-count-exact format resolution (`qt_resolve_fmt`, never
metadata); group size derived from `.qs` array size; ablation-selected assignments
(int4-g128 recovers ~63% of the int4 loss for +0.25 bpw, §5.7).

**4. Strengths.** The policy is a *table with reasons*, each row a measured incident or
ablation; the floor of each format is known ("int2 craters"); sensitivity classes are
architectural (router ≠ expert), not guessed per tensor.

**5. Weaknesses & trade-offs.** The policy is frozen at conversion time — a workload that
stresses the quantized class pays forever until re-conversion; sensitivity was measured on
generic benchmarks (hellaswag/arc/mmlu), not the user's workload; the taxonomy is
per-*tensor*, with no per-*request* precision (Colibri cannot say "this question deserves
F32"). Olympus must fix all three: its "conversion" is a routing decision, re-decidable per
request, and its sensitivity data can come from the actual workload (Calibration Record).

**6. Security implications.** Colibri's insight that *format resolution is a security
boundary* (#413: ambiguous byte counts → OOB reads) translates: model-tier resolution must
never be inferable from untrusted input. A visitor's BYOK request must not be able to
"claim" the operator's premium tier (`backend._fallback_chain` already refuses BYOK
failover); a prompt must never steer its own tier upward (tier selection reads only
role + measured grades, never message text).

**7. Scalability implications.** Colibri's bit-widths are static; Olympus's price ladder is
*live* — `providers.fetch_pricing` (OpenRouter $/Mtok) and `usage.estimate_cost` already
exist. A graded ladder scales to any pool size because grades attach to `(provider, model)`
pairs, not to a hand-maintained table; new models enter ungraded and are treated at the
`_DEFAULT_CAP` floor until measured (the honest ladder).

**8. Performance implications.** The MoE lesson transfers directly: ~95% of Colibri's bytes
are experts at int4; ~95% of Olympus's tokens are specialist work-steps that a mid-tier model
handles. The measured shape to reproduce: cheap tiers for bulk expert work, full precision on
the routing/verification spine. Expected win (to be measured, not promised): the majority of
council token-spend moves one price tier down with the delta gated by R5's oracle.

**9. Maintainability implications.** `_CAPABILITIES` in `config.py` is the exact failure mode
Colibri avoided: a hand-typed table (`"gpt-4o": {"reasoning": 8, ...}`) that silently rots as
model families evolve. Replacing rot-prone constants with measured records that carry their
provenance (`quality_baseline.json` already has a `_provenance` history — the pattern exists).

**10. How Olympus should redesign it.** Build the **precision policy for the council**, one
table with reasons, mirroring `classify()`:

| Olympus "tensor class" | Precision (tier) | Colibri analog & reason |
|---|---|---|
| Zeus routing / Athena planning | strongest configured member, always | router `mlp.gate.weight` F32 — "routing sensitivity"; a wrong plan corrupts every downstream step |
| Aletheia verification | never below the `verify`-graded floor | attention forced exact (+12% perplexity when IDOT'd, §5.2); verification is the answer-bearing path |
| Specialist work steps | graded tier per role, per measured grade | routed experts int4 — biggest token pool, most tolerant |
| Skill distillation / Metis synthesis | mid tier minimum | MTP-int8 lesson (#8): the *self-improvement* head is precision-critical — a cheap model writing the skill library poisons every future run; benchmark gate (README §Cross-model learning) is the existing backstop |
| Sub-agent scratch work (`subagents.py`, `treesearch` expansion) | cheapest graded member | int2/int3 territory — bounded blast radius, verified downstream |

**11. Final Olympus architecture.** **New module `olympus/modelgrade.py`** — the measured
capability ladder. Data model: `GradeCard {provider, model, role, score, n, ci_lo, ci_hi,
source: "measured"|"heuristic", asof, eval_hash}` persisted in
`MEMORY_DIR/model_grades.jsonl` (append-only, hash-chained per the `ledger`/`calibration`
pattern). API: `grade(model, role) -> float` — returns the Wilson-lower-bound measured score
when `n >= _MIN_SAMPLES` (the `outcomes._MIN_SAMPLES = 5` precedent), else falls through to
`config.capability_score` **byte-for-byte** (the `learned_routing.py` never-worse-than-heuristic
contract, reused verbatim). Sources feeding it: `evals.py` judge scores per member,
`routing_outcomes.py` success records, `compare.py` blind-pick tallies, R5 oracle runs.
Integration: `ModelPool._role_map` consults `modelgrade.grade` behind
`OLYMPUS_MEASURED_GRADES=1` (default off; replay-mode disabled, same three activation gates as
`learned_routing`). Env: `OLYMPUS_MEASURED_GRADES`, `OLYMPUS_GRADE_MIN_SAMPLES` (default 5).
CLI: `olympus grades` (ladder, lowest-confidence first, mirrors `olympus scores`).

**12. Why the Olympus approach is superior.** Colibri's precision policy is frozen at
convert time and measured on someone else's benchmark; Olympus's is re-decided per request and
measured on *this deployment's* workload — and every grade entry is simultaneously a
Calibration Record datapoint (`docs/MOAT_ANALYSIS.md` Asset 1), so the ladder is not just
config, it is the accumulating moat. Colibri cannot A/B its own quantization in production;
Olympus can (blind `compare.py` runs are already the pattern).

---

## R3. IDOT activation quantization & the dispatch policy (§5.2–5.3)

**1. What Colibri does.** Beyond quantizing weights, it quantizes *activations* to int8
(`qrow_i8`) so integer dot-product SIMD (VNNI/SDOT/SMMLA) applies — with a full dispatch
ladder (`matmul_qt_ex`) choosing Metal→CUDA→grouped→IDOT→exact-f32 per call, gated by batch
thresholds (`I4S`) measured per microarchitecture. Crucially, **attention projections force
exact kernels**: IDOT there cost a measured +0.117 nats/token.

**2. Why it exists.** Weight precision alone leaves compute on the table; but the extra
lossy step is only safe where measurement says so.

**3. How it works internally.** Per-row absmax scaling with Python/C math mirrored exactly
(`np.rint` ↔ `lrintf`); bit-exact integer accumulation demanded by tests; the dispatch is a
priority list with per-format, per-hardware, per-batch-size gates.

**4. Strengths.** Two-axis precision (weights × activations) with each axis independently
measured; the "exact path" carve-out is a *hard* rule, not a preference; opt-in flags for
wins that are hardware-conditional (`XEXP` +11.6% on 48 cores, negative on 24).

**5. Weaknesses & trade-offs.** The ladder is combinatorially fragile (7 formats × 8 SIMD
families × batch thresholds); thresholds measured on specific chips (Xeon 8370C) silently
mis-tune elsewhere; the doctrine costs honesty overhead — every rung needs its own oracle
test. Olympus's translation must collapse the combinatorics: its "dispatch" axis is one
dimension (the `effort` parameter + tier choice), not eight.

**6. Security implications.** Activation quantization is invisible to the caller — the
Olympus analog (silently lowering `effort` or `max_tokens` on a path) must never apply to
security-relevant calls: the security gate's screening calls, Aegis, and Aletheia run at
full effort always (the attention-projection rule). Enforce in code, not convention.

**7. Scalability implications.** The batched-threshold insight (`I4S`: int4-IDOT pays at S=1
only on VNNI-class hardware) maps to batching council calls: cheap tiers pay off on
high-volume fan-out (Athena's parallel branches, heartbeat scans) and may *lose* on single
interactive turns where latency dominates — tier policy should see call multiplicity.

**8. Performance implications.** Olympus's activations are *tokens in context*: the analog of
int8 activations is compressed history (`OLYMPUS_HISTORY_TOKEN_BUDGET`, ACE compaction) and
`effort` ∈ {low, high} on `backend.complete_text`. The measured-threshold discipline says:
each (role × effort × compaction) combination gets a measured delta before it becomes a
default — no "surely low effort is fine for summaries" without a number.

**9. Maintainability implications.** Colibri keeps hardware-conditional wins opt-in with the
measurement in the flag's documentation. Olympus mirror: workload-conditional defaults
(e.g. low effort on heartbeat scans) ship as `OLYMPUS_*` flags documented with their measured
delta in `docs/CALIBRATION_RECORD.md`-style provenance, and `setdefault` semantics (user env
always wins — Colibri's `env_for()` rule, §13).

**10. How Olympus should redesign it.** An **effort policy table** in `config.py`:
`effort_for(role, call_kind)` — `verify`/security/screening → always `"high"`;
bulk scan/summarize/heartbeat → `"low"` when `OLYMPUS_EFFORT_POLICY=measured` and a measured
delta exists for that (role, effort) cell in `modelgrade`; otherwise `"high"`. The dispatch
priority mirror of `matmul_qt_ex` already exists as `backend._dispatch_text`'s provider
ladder — leave it; add only the effort seam.

**11. Final Olympus architecture.** No new module. `config.effort_for()` +
`OLYMPUS_EFFORT_POLICY` (`off`|`measured`, default `off` — byte-identical when off, the
PROF=0 rule). `backend.complete_text`/`run_agent_counted` accept the resolved effort as today;
call sites in `orchestrator.py`/`heartbeat.py` route through `effort_for`. Every downgraded
call records `effort` into the trace so `liveeval` scorers can attribute regressions to it.

**12. Why the Olympus approach is superior.** Colibri's activation quantization is global
per-kernel; Olympus's is per-*call-site* with the exact-path set enforced by type of work,
and every downgrade is attributable in the signed trace — Colibri can tell you IDOT cost
+0.117 nats on a benchmark; Olympus can tell you low-effort scanning cost 2 missed
opportunities *last month, on your account*.

---

## R4. E8 lattice + CFSE entropy codec — compression with proofs (§5.1 fmt 6, §5.4)

**1. What Colibri does.** fmt 6 packs weights at 3.0625 bpw via a pre-rotated E8/IQ3 codebook
(QuaRot-family, deterministic sign-PRNG "the constants are the spec"); CFSE is an order-0
rANS codec over int4 nibbles achieving ~1.37× lossless shrink, justified by a *whiteness
proof* (conditional-entropy gain +0.000 — order-0 is provably sufficient), with a raw-fallback
mode byte whenever compression doesn't strictly win, and integrity seals (~2⁻⁴⁶ silent-
corruption probability). CFSE sits in-tree fuzz-hardened but not yet wired in (§26.15).

**2. Why it exists.** Below int4, naive rounding craters; rotation + lattice codebooks buy
the last bits. CFSE buys 27% of disk back losslessly.

**3. How it works internally.** §5.1/§5.4; decoder is safety-first because "weights corrupt
silently"; the packer mandates an in-memory round-trip before writing anything.

**4. Strengths.** *Measure entropy before building the codec* (the whiteness study); *raw
fallback whenever compression doesn't strictly win*; *integrity seals sized to the threat*
(quantified corruption probability); *frozen spec constants pinned by tests*.

**5. Weaknesses & trade-offs.** Enormous engineering for the last ~0.4 bpw; CPU-only,
unintegrated; the codec is useless outside its exact container. The honest Olympus verdict:
**skip the artifact, absorb four principles.** Olympus has no weight bytes to compress — its
scarce byte is the *context token*, and its lossy compressor already exists (`ace.py`
history compaction, `OLYMPUS_HISTORY_CONTEXT_FRACTION`, in-run compaction via
`OLYMPUS_INRUN_COMPACT`).

**6. Security implications.** "Weights corrupt silently" ↔ *compacted context corrupts
silently*: a summarization step that drops the user's constraint produces confidently wrong
answers with no error. CFSE's answer — integrity seals + round-trip verification — maps to:
compaction outputs carry a provenance marker in the trace, and `replaystore.request_hash`
already makes any silent context change a `ReplayDivergence` (the seal). Keep compaction
*outside* the replayed decision path or hash its inputs — never both unhashed.

**7. Scalability implications.** The whiteness lesson: before building a cleverer compactor
(embedding-based dedup, learned salience), *measure whether the simple one is already at the
entropy floor* for real conversations. A bounded study (see spikes) beats a subsystem.

**8. Performance implications.** The strictly-wins rule, adopted: compaction (and any prompt-
shrinking trick) must show a measured token-cost win *and* a non-regressing golden-eval delta
(R5), else the raw path ships. `config.HISTORY_*` budgets become measured knobs, not vibes.

**9. Maintainability implications.** "The constants are the spec, pinned by tests":
Olympus's compaction prompt and history-budget defaults should be pinned by golden tests the
way `quality_baseline.json` pins judge scores, so a well-meaning prompt edit that quietly
degrades recall trips CI.

**10. How Olympus should redesign it.** No codec. Three changes: (a) a **strict-win gate**
for compaction defaults — any change to `ace.py` prompts or `HISTORY_*` defaults runs the R5
golden set before/after (reusing `gate_prompt` mechanics); (b) **compaction provenance** — a
`compacted: true` marker + source-hash on compacted history blocks in the trace; (c) the
**entropy study** as a bounded spike (below), not a feature.

**11. Final Olympus architecture.** Extends `ace.py` (marker emission), `evals.py`/R5
`modelgate.py` (the strict-win gate as a callable `gate_compaction()`), `trace.py` (marker
schema). Env: none new (the gate rides `OLYMPUS_MEASURED_GRADES` machinery). Explicitly
rejected: any lossy compression of stored memory (`facts`, skill library) — memory is
Colibri's "data", and data fails hard, never lossy.

**12. Why the Olympus approach is superior.** Colibri compresses a static artifact once,
offline, with a mathematical integrity proof; Olympus compresses a *living* stream per
conversation — so its integrity mechanism is behavioral (golden-eval non-regression +
replay-hash divergence detection) rather than checksum-shaped, which is the only kind of
seal that can catch *semantic* corruption. And by rejecting compression of durable memory
outright, Olympus keeps a fail-hard data class Colibri needed a 2⁻⁴⁶ seal to protect.

---

## R5. Token-exact oracle validation + the ablation harness (§3.1, §5.7, §23)

**1. What Colibri does.** Correctness is defined as **token-exact equality against a
HuggingFace oracle** on a tiny true-architecture fixture (TF=1, 32/32 teacher-forcing — the
canonical gate); integer kernels must be bit-exact vs plain-C references; measured *quality
claims ship as regression tests* (int3-beats-int4-on-outliers, §23). The ablation harness
(`quant_ablation.py`, §5.7) isolates deltas engine-free: quantize→dequantize with the exact
production math, score both sides with one harness — "the delta IS the quantization cost" —
with guards against silent coverage gaps (`--min-coverage 95%` hard-fails) and OOD prefix
hazards (#108: a missing `[gMASK]<sop>` *flips the sign* of measured deltas).

**2. Why it exists.** "Placement decides speed, never answers" is only credible if answer
identity is machine-checked; and a quantization decision made on a mis-measured delta is
worse than no decision.

**3. How it works internally.** §3.1/§23: random-weight fixtures preserving real
architecture shapes; oracle version hard-gates (transformers ≥5.11, #281 — an old oracle was
*silently wrong*); CI floors on the tiny model; the CPU/CUDA agreement gate (≥70%).

**4. Strengths.** The gate is cheap (tiny fixture), canonical (one number: 32/32), and
version-pinned; quality claims become executable; delta isolation prevents the classic
"benchmark moved because three things changed" failure.

**5. Weaknesses & trade-offs.** Token-exactness is only *defined* because Colibri owns the
weights and the RNG; it holds only under serial validation configs (threaded FP reassociation
breaks it, §10.3 — honestly caveated). Olympus **cannot have token-exact**: frontier APIs are
nondeterministic, versioned behind aliases, and change under Olympus's feet. The absorbed
principle must be re-based: *exactness where Olympus owns the computation* (the orchestration
decision path — `replaygate` already proves byte-identical replay of decisions against frozen
responses) and *statistical golden gates where it does not* (model outputs).

**6. Security implications.** The oracle-mismatch guard (#76: detect tiny-oracle vs real-model
mismatch by max token id) translates to model-identity attestation: record the provider's
reported model/version/fingerprint from response metadata into the trace, so "the eval passed"
is always attributable to a specific model identity — and a silent provider-side swap is
*detectable*, which is also Calibration Record evidence integrity.

**7. Scalability implications.** Golden-gating every model call is unaffordable (ROADMAP §0
Gate-cost rule F8). Colibri's answer scales: the *tiny fixture* — a frozen golden set of
~20–40 items per role (small enough to run on demand), distinct from the full `evals.py`
benchmark sweep, with a stated budget per run (env-capped dollars via `usage.estimate_cost`).

**8. Performance implications.** The gate's job is to make tier-downgrades *safe to attempt*:
without it every "route Plutus to DeepSeek" experiment risks silent regression; with it the
experiment is reversible (`gate_prompt`'s apply-else-rollback shape, applied to routing).

**9. Maintainability implications.** Judge drift is Olympus's transformers-5.11 hazard: the
LLM judge (`config.JUDGE_MODEL`) is itself a model that changes. Pin the judge per golden-set
version; when the judge model changes, re-baseline (record both old/new scores on the frozen
set — the `--fp8` "compute the reference after the round-trip" move: measure with the
instrument you'll actually use).

**10. How Olympus should redesign it.** **New module `olympus/modelgate.py`** — the
golden-oracle regression gate for the execution tier. Three triggers: (a) **swap gate** — a
pool member's model string changes, a new member joins, or a role reassignment is proposed
(by `learned_routing`/`bandit_routing`/`modelgrade`): run the role's golden set on
incumbent + candidate, admit only if candidate's Wilson lower bound ≥ incumbent's − ε;
(b) **drift tripwire** — heartbeat-cadence re-run of each active member's golden set,
alarming on a drop beyond the baseline's noise band (the `replaygate.self_check` escalation
pattern: memory report + Telegram + GitHub issue, verbatim reuse); (c) **on-demand** —
`olympus modelgate <member>`. Deltas are isolated Colibri-style: same prompts, same judge,
same harness, one variable (the model).

**11. Final Olympus architecture.** `olympus/modelgate.py`; data:
`MEMORY_DIR/golden/<role>.json` (frozen items, versioned, judge-model-pinned) +
results appended to `model_grades.jsonl` (R2) and mirrored into `quality_baseline.json`
provenance. API: `gate_swap(incumbent, candidate, role) -> Verdict`,
`self_check() -> list[Alert]` (heartbeat), `gate_compaction()` (R4). Env:
`OLYMPUS_MODEL_GATE=1` (default off), `OLYMPUS_MODEL_GATE_BUDGET_USD` (per-run cap, F8 rule),
`OLYMPUS_MODEL_GATE_EVERY` (heartbeat cadence, default 7 days like `REPLAY_GATE_EVERY`).
Integration: `ModelPool` consults gate verdicts before applying measured-grade role changes;
`heartbeat.py` schedules `self_check`; failures write calibration entries.

**12. Why the Olympus approach is superior.** Colibri's oracle proves its *own code* didn't
change the answer; it is blind to upstream model changes (it owns frozen weights, so there are
none). Olympus's version watches the axis Colibri never needed to: the *provider* changing
underneath a pinned name — the failure mode API clients actually die of. And every gate run
deposits provider-neutral comparative evidence, feeding Asset 2 (`docs/MOAT_ANALYSIS.md`):
the safety mechanism and the moat are the same write.

---

## R6. CPU SIMD / GPU backends & the determinism doctrine (§10, §11)

**1. What Colibri does.** Eight SIMD families behind one dispatch with portable scalar
fallbacks; four GPU linkage models behind one C ABI with a single-source vendor-compat rule;
**fallback discipline**: every GPU entry point has a CPU fallback, per-tensor failure latching,
but an *explicit* GPU request with no runtime fails at startup — because silently-CPU "GPU
benchmarks" once got published (#121). Determinism doctrine: fixed-order reductions, no
atomics, a parallel Metal router memcmp-proven bitwise-identical to serial CPU routing (§10.3,
§26.14), reassociation policy explicit per call-site, honest caveats where threading breaks
argmax stability.

**2. Why it exists.** Heterogeneous backends must be speed choices, not answer choices —
the fidelity doctrine applied to hardware.

**3. How it works internally.** §10–11: `backend_gpu_compat.h` (~37 mappings, never `#ifdef
__HIP__` in kernels); `COLI_GPU_FAIL_AFTER=N` fault injection to test fallback lifecycles;
`IDOT_KERNEL` banner reporting which ladder rung engaged.

**4. Strengths.** The #121 rule (explicit requests fail loud, defaults fail soft) is the
best two-line policy in the codebase; fault-injection for fallback paths means the rarely-
exercised path is tested; the bitwise-router proof makes "same decision on any backend" a
test, not a hope.

**5. Weaknesses & trade-offs.** True bitwise determinism costs real performance (no atomics,
serial-order reductions) and still dissolves under threaded FP — Colibri honestly scopes the
claim to serial validation configs. Olympus's providers are *inherently* nondeterministic and
heterogeneous (Anthropic vs OpenAI-compat differ in schema support — see
`llm._UNSUPPORTED_SCHEMA_KEYS`, verified live). Chasing token determinism cross-provider is
the "free counterfactual replay" trap ROADMAP §0/F9 already cut. The doctrine must be split:
**decision-determinism** (owned, provable) vs **output-fidelity** (statistical, gated).

**6. Security implications.** Silent substitution is the execution tier's spoofing attack:
a benchmark or comparison that quietly ran on a different member is corrupted evidence.
Olympus already has the primitive — `backend.complete_text_once` exists precisely because
blind compare must never fail over. Generalize: any *measurement* call path (evals, compare,
modelgate, calibration) uses `_once` semantics; only *service* paths fail over. Sovereign mode
is the same rule at the egress layer: fail closed (`NoLocalModelError`), never downgrade.

**7. Scalability implications.** Colibri's per-backend kernel matrix (7 formats × 4 backends,
with honest gaps: fmt 5/6 CPU-only) maps to Olympus's provider-capability matrix: structured
outputs, server-side tools, MCP, effort — each supported unevenly (`backend.py` already
routes non-Claude Bedrock to Converse with degraded tool loops, documented inline). Absorb
the compat-header rule: capability differences live in ONE place per provider
(`openai_compat.py`, `bedrock_converse.py`, `claude_code.py`) — never `if provider ==`
scattered in specialists. This already mostly holds; make it a stated invariant with the R1
CI test.

**8. Performance implications.** The `IDOT_KERNEL` banner ↔ Olympus should always be able to
say *which member actually served a call* — `usage.record` keys by model already; ensure
failover switches are visible in the trace (which member was attempted, which answered), so
"fast because it silently fell back to Haiku" is impossible to miss — the #121 lesson in
telemetry form.

**9. Maintainability implications.** Fault injection transfers directly and cheaply: an
`OLYMPUS_FAIL_AFTER=N` test hook in `backend._with_failover` (raise a synthetic
`ConnectionError` after N calls) lets tests exercise the failover chain, BYOK-refusal, and
sovereign fail-closed paths deterministically — today those paths are tested by mocking;
a first-class hook keeps them tested against the real dispatch code.

**10. How Olympus should redesign it.** (a) **Split the doctrine explicitly**:
decision-determinism is `replaygate`'s byte-identical replay of the orchestration path
(already built, keep as the canonical gate); output-fidelity is R5's statistical golden gate.
Document that Olympus makes Colibri's claim only where it owns the computation. (b) **The
#121 rule everywhere**: explicit member selection (`/model` pin via `modelpin.py`,
`OLYMPUS_SPECIALIST_MODELS`, measurement paths) never silently substitutes — pins currently
"fail open to normal selection" when stale (`modelpin.resolve`); keep that for *service* but
make measurement paths refuse. (c) **Fault-injection hook** as above. (d) Fixed-order
tie-breaking in `ModelPool._role_map` (it already sorts deterministically; pin with a test the
way Metal's router is memcmp-pinned).

**11. Final Olympus architecture.** No new module. `backend.py`: `OLYMPUS_FAIL_AFTER` test
hook + trace-visible failover records; `modelgate.py`/`compare.py`/`evals.py` audited onto
`complete_text_once`; a `test_role_map_deterministic` tie-break pin;
`docs/EXECUTION_DOCTRINE.md` section "Determinism: what we prove, what we gate."

**12. Why the Olympus approach is superior.** Colibri spends kernel-level rigor to make
hardware invisible and still must caveat threading; Olympus refuses the unwinnable fight
(token identity across providers), proves the stronger claim it *can* make — the entire
decision path replays byte-identically (`replaygate`) — and covers the residual with measured
statistical gates. That is the same doctrine, re-based on what an API client actually owns.

---

## R7. Conversion pipelines → the local sovereignty tier (§5.5)

**1. What Colibri does.** `convert_fp8_to_int4.py` turns a 756 GB FP8 release into a 372 GB
int4 container one shard at a time, with operational armor from real incidents: a `[PLAN]`
line at second 1, a **parameter manifest that refuses resume-with-different-flags** (#355: a
mismatched resume silently overwrote 137/141 finished shards), outdir locks, atomic progress,
checkpointed multi-stream downloads ("NO byte is lost however the connection dies"),
revision pinning "for supply-chain integrity", and in-place repair surgery
(`repair_mtp_int8.py`). Honest limitations: size-only verification, hardcoded personal paths.

**2. Why it exists.** The model a user can download is not the model the engine can run;
conversion is the bridge, and at 372 GB every operational failure is hours lost.

**3. How it works internally.** §5.5. Python converters mirror C math exactly so the
"token identical" claim survives conversion.

**4. Strengths.** Every armor plate is a memorialized incident; the manifest rule and atomic
progress are the difference between a tool and infrastructure.

**5. Weaknesses & trade-offs.** Colibri converts *weights*; Olympus never touches weights (a
ROADMAP §0 hard rule — no weight-level loops). But Olympus has the same *shape* of problem in
sovereignty mode: getting a local engine (Ollama, vLLM, llama.cpp — or Colibri itself) from
"running on localhost" to "a trusted, graded pool member." Today `docs/SOVEREIGNTY.md` says
"export OLYMPUS_BASE_URL and go" — there is no qualification, no measured grade (the
`_CAPABILITIES` table keys on frontier names; `qwen`/`llama` get generic 6–7s, and an
unrecognized local model silently gets `_DEFAULT_CAP` 5s), and no integrity check that the
local endpoint serves the model it claims. That gap is this rubric.

**6. Security implications.** Colibri's untrusted-mirror threat model, ported: a local
endpoint is *inside* the sovereign allowlist and therefore the highest-trust network position
in the system — it deserves the most scrutiny, not the least. Qualification must verify:
(a) endpoint identity (`/models` response recorded + pinned, drift alarmed — the `COLIKV1`
model-fingerprint pattern); (b) egress posture (the member's host resolves within
`OLYMPUS_EGRESS_ALLOWLIST`/loopback — reuse `security.assert_egress_allowed` and
`providers.local_provider_hosts()`); (c) capability honesty — measured, not claimed. Model
*downloads* remain the user's job (the Tauri shell's reasoning, §15: hundreds of GB "must
remain an external, user-selected resource") — Olympus qualifies endpoints, it does not
fetch weights; anything else drags supply-chain risk (size-only verification! hardcoded
paths!) into a security product.

**7. Scalability implications.** A qualification harness that works for one Ollama box works
unchanged for a rack of vLLM (`10.0.5.20` CIDR case in SOVEREIGNTY.md) because it attaches to
`(base_url, model)` pool members, which is already the pool's identity scheme
(`backend._fingerprint`).

**8. Performance implications.** Local models are Olympus's slowest, cheapest "format" —
the int2/int3 end of the ladder, where Colibri found "craters" and measured *exactly where*.
Qualification must measure per-role floors so sovereign routing can refuse work honestly:
a local 7B failing the `verify` golden floor means Aletheia in sovereign mode degrades
*loudly* (a stated verification-confidence downgrade in output), never silently. Fail-closed
sovereignty already exists (`NoLocalModelError`); this adds fail-*honest* quality.

**9. Maintainability implications.** The manifest rule transfers to Olympus's long jobs:
qualification campaigns and training rounds (`OLYMPUS_TRAIN_EVERY`) should refuse to resume
under changed parameters and write progress atomically — `proclock.py` and the
`fetch_benchmarks.py` atomic-write lesson (a truncated file blocks re-download forever) are
the existing in-house patterns to reuse.

**10. How Olympus should redesign it.** **New module `olympus/localtier.py`** — local-member
qualification and lifecycle, Colibri's `diag_harness.py` 5-phase campaign (§19.4) reshaped for
an API client: phase 1 *reachability* (endpoint up, `/models` lists the claimed model);
phase 2 *fingerprint* (record model id/digest where the server exposes one — Ollama does;
pin it); phase 3 *smoke* (short generations, repetition/garbage detection — the
`diag_harness` smoke phase); phase 4 *golden floors* (R5 golden sets per role → measured
GradeCards into `modelgrade`, source `"measured"`); phase 5 *admission* (member enters the
pool tagged `tier: "local"`, with per-role floors; roles whose floor failed are excluded for
this member the way sovereign mode excludes remote members — same eligibility-filter seam in
`ModelPool.of`).

**11. Final Olympus architecture.** `olympus/localtier.py`; data:
`MEMORY_DIR/local_members.jsonl` (qualification manifests: params, fingerprint, phase
results, atomic append; refuses re-qualification resume with changed params). CLI:
`olympus localtier qualify <base_url> <model>`, `olympus localtier status`. Env:
`OLYMPUS_LOCAL_QUALIFY=strict|warn|off` (default `warn`: unqualified local members serve but
are excluded from `verify` and flagged in `olympus status`; `strict` = sovereign-grade
fail-closed). Integration: `doctor.py` gains local-tier checks (Colibri's `coli doctor` CUDA
matrix analog: requested × reachable × qualified); `heartbeat.py` re-runs fingerprint checks
(a swapped GGUF behind the same model name is #281's silently-wrong-oracle, locally);
sovereign mode composes — `OLYMPUS_SOVEREIGN=1` + `strict` is the full "prove nothing left
the box *and* prove what stayed answers well enough."

**12. Why the Olympus approach is superior.** Colibri's pipeline earns trust in an artifact
once, at conversion time, on the maintainer's machine; Olympus earns trust in a *live
endpoint* continuously, on the customer's machine, with the evidence written into the
Calibration Record. And it is "beyond Colibri" in the direction that matters for the moat:
Colibri can tell you its int4 container scores 62.5% on hellaswag; a qualified Olympus local
tier can tell you whether *your* sovereign deployment's model is good enough for *your*
workload's verification step — per role, with confidence bounds, refusing where it is not.

---

## Beyond Colibri (capabilities Colibri lacks that this domain needs)

- **Provider-drift tripwire** (R5b): Colibri owns frozen weights and never needed it; an API
  client's models mutate behind stable names. `modelgate.self_check` is the new subsystem.
- **Per-request precision** (R2/R3): Colibri's precision is frozen per tensor at convert time;
  Olympus re-decides tier and effort per call, per data-class (`X-Olympus-Data-Class` already
  routes sensitivity — extend the same taxonomy to tier floors).
- **Measurement-grade call semantics** (R6): a formal `_once` (no-substitution) contract for
  every evidence-producing call path, generalizing `backend.complete_text_once`.

## Open questions & research spikes

1. **Golden-set size vs gate power** (R5, bounded: 1 week + ≤ $50 API): how many items per
   role give a Wilson-bound gate that detects a 1-tier quality drop at ~90% power? Colibri's
   32/32 works because exactness is binary; judge-scored items need a measured noise band
   first (run the frozen set 5× on one pinned model; the variance IS the band).
2. **Compaction entropy study** (R4, bounded: 3 days, offline over existing traces): measure
   golden-eval delta of current ACE compaction vs raw history at equal token budgets. If the
   delta is ~0 (the whiteness result), freeze the compactor and stop investing; if not, the
   gap sizes any future work.
3. **Judge re-baselining protocol** (R5): when `OLYMPUS_JUDGE_MODEL` must change, is
   double-scoring the frozen set (old + new judge) sufficient to splice baselines, or do
   grade histories need a discontinuity marker? Decide before the first judge swap, not after.
4. **Local fingerprinting coverage** (R7, bounded: 2 days): survey what identity Ollama /
   vLLM / llama.cpp-server / Colibri's own gateway actually expose (digests, build info) and
   define the minimal fingerprint record; where nothing is exposed, is a fixed-prompt
   greedy-sample transcript a stable-enough behavioral fingerprint across server restarts?
   (Colibri's own §10.3 warning: threaded FP breaks greedy stability — test before trusting.)
5. **Tension to resolve with the synthesizer:** `modelgrade` (this doc) vs the existing
   `learned_routing`/`bandit_routing` pair — three selectors over overlapping evidence is two
   too many. Proposal: `modelgrade` becomes the *ladder store* both consume; `learned_routing`
   keeps the conservative-exploiter policy; `bandit_routing` remains the opt-in explorer.
   Someone must own that merge before any of the three gains new powers.
