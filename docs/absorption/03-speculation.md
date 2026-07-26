# Absorption 03 — Speculative Execution & Draft/Verify

**Colibri domain:** lossless speculative decoding with three heterogeneous draft sources
(§6.5), Leviathan rejection sampling (§6.5, §19.3), grammar-as-draft-source (§6.6, §26.8),
the MTP head + `SPEC_PIN` kernel pinning (§6.5.2), acceptance guards & auto-disable
(§6.5, §17.2), n-gram prompt lookup (§6.5.3), and the MTP-at-residency economics inversion
(§10.4).
**Olympus target:** draft-cheap/verify-strong orchestration across the model pool —
`olympus/moa.py`, `olympus/consensus.py`, `olympus/contracts.py`, `olympus/speculate.py`,
`olympus/treesearch.py`, `olympus/toolcall_repair.py`, `olympus/evolve.py`,
`olympus/routing_outcomes.py`, `olympus/trace.py`, plus one new module,
**`olympus/draftverify.py`**.

## Domain thesis

Colibri's speculation subsystem is built on one non-negotiable invariant: **speculation is
invisible in outputs**. Leviathan rejection sampling makes the emitted token distribution
*exactly* what the verifier alone would have produced — a wrong draft can only cost speed,
never answers — and every draft source lives or dies by a measured acceptance rate with an
automatic kill switch. Olympus's analog of "draft with a cheap head, verify with the full
model" is **draft with a cheap pool member, verify with the strong one** — and the analog
of Colibri's token-exact losslessness, which is unreachable across opaque frontier APIs,
is a *golden-eval non-inferiority gate*: speculation may only be enabled per task type
where a before/after benchmark proves the drafted pipeline is not worse, and a windowed
acceptance guard auto-disables it the moment live economics stop paying (the `gate_prompt`
culture applied to a routing mechanism). The deepest Colibri lesson here is §10.4's
economics inversion — speculation that wins in one bottleneck regime *loses* in another —
so the Olympus design makes the enable decision a **measured, per-regime, accumulating
record** (which model can safely draft for which task, at what acceptance), feeding the
Calibration Record and cross-provider comparative evidence moats (`docs/MOAT_ANALYSIS.md`
Assets 1–2) rather than a static config flag. Note on naming: `olympus/speculate.py`
already exists and means *plan-level* speculation (uncommitted tree-search branches over
the ledger); this domain adds *answer-level* speculation and keeps the two deliberately
separate — one speculates about **actions** (governed by the approval gate), the other
about **text** (governed by the verifier).

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| Leviathan lossless rejection sampling | greedy = exact match; sampled accept w.p. `p(draft)`, resample with rejected token banned+renormalized (§6.5) | **absorb-principle** | **new `olympus/draftverify.py`**, `evals.py`, `quality_baseline.json` |
| Three-draft-source hierarchy | grammar-forced > MTP head > n-gram, `DRAFT=-1` auto, mux force-disables unsafe sources (§6.5) | **redesign** | `draftverify.py`, `contracts.py`, `recall.py`/`usermem.py` |
| MTP head drafting | chained draft head, own KV window, int8-mandatory (§5.6 #8), 2.2–2.8 tok/forward (§6.5.2) | **redesign** (as cheap-model drafting) | `draftverify.py`, `config.py` (ModelPool), `modelpin.py` |
| Grammar-as-draft-source | grammar never a sampling constraint, only a draft source; wrong grammar costs only rejects (§6.6) | **redesign** | `contracts.py`, `toolcall_repair.py`, `docs/DESIGN_OUTPUT_CONTRACTS.md` |
| n-gram prompt lookup (method E) | bigram continuation from context; zero cost, default OFF (5% acceptance = 3× slower cold) (§6.5.3) | **redesign** (verified answer reuse) | `draftverify.py` draft source backed by `recall.py`/`emem.py` |
| Acceptance guards & auto-disable | #163 soft guard (<10%/24 → pause 256, re-arm); grammar kill (<50%/32); `coli plan` emits `DRAFT=0` with reasons (§6.5, §17.2) | **redesign** | `evolve.py` (bounded auto-tune), `routing_outcomes.py` pattern, `heartbeat.py` |
| `SPEC_PIN` kernel pinning | pin the S=1 kernel family across draft+verify; kernel-family/FP-order divergence collapses acceptance (§6.5.2) | **absorb-principle** | `draftverify.py` context fingerprint, `modelpin.py`, `trace.py` |
| Batch-union verify economics | all drafts verify through one batch forward; accepted drafts = disk reads avoided (§6.3 FASE B, §26.8) | **absorb-principle** | `draftverify.py` (input/output token-price asymmetry) |
| MTP-at-residency inversion | at full residency verify cost ~linear in S; 79%-acceptance head still −5%; inverts with grouped GEMM (§10.4) | **absorb-principle** | `draftverify.py` regime model, `providers.py` pricing, `compare.py` |
| Speculative parallel tool-calls (**beyond Colibri** at this layer; PILOT/§7.5 is the engine-side cousin) | — | **new-subsystem** (bounded spike) | `speculate.py`, `treesearch.py`, `toolselect.py`, `security.py` |

---

## R1. Leviathan lossless verification → the answer-level never-worse guarantee

**1. What Colibri does.** `spec_decode` implements Leviathan et al. rejection sampling
(§6.5): under greedy decoding a draft token is accepted iff it exactly matches the
verifier's argmax; under sampling it is accepted with probability `p_verifier(draft)`, and
on rejection the engine **resamples from the verifier's distribution with the rejected
token banned and renormalized**. The emitted distribution is mathematically identical to
the verifier running alone — "speculation invisible even under sampling."

**2. Why it exists.** Speculation is only free if it cannot change answers. Without the
lossless property every acceptance-rate knob becomes a quality knob, and the fidelity
doctrine (§1, "placement only ever decides speed, never answers") collapses.

**3. How it works internally.** All draft tokens verify through one batch-union forward
(§6.3 FASE B); the rejection-resample math is exact because the engine owns both
distributions. See §6.5 and §19.3.

**4. Strengths.** A provable invariant, not a tested tendency; it converts every
speculation experiment into a pure-performance experiment; it lets three heterogeneous
draft sources coexist behind one verifier with zero quality review per source.

**5. Weaknesses & trade-offs.** The proof requires owning both probability
distributions token-by-token. Olympus cannot have that: frontier APIs expose no
comparable logits, different providers tokenize differently, and the "verifier" for an
Olympus answer is a *judgment* (Aletheia, contracts, benchmarks), not a softmax. Any
straight port of "lossless" would be a false claim — exactly the kind of "free"
guarantee `docs/ROADMAP.md` §0 cut (F9: no free counterfactual replay; same epistemics
here). A second, subtler weakness even in Colibri: losslessness is *distributional*,
not per-run — a rejected draft still consumed latency, and under threading the FP-order
caveat (§10.3) already softens "token-exact" to "under serial validation configs."

**6. Security implications.** The verifier is the trust boundary. In Colibri a hostile
draft source can only slow decoding. Olympus must preserve that: a draft produced by a
cheap (possibly weaker-aligned, possibly local) model must pass through **exactly the same
gates** as a native strong-model answer — the security screen, `contracts.check`
(fail-closed), and Aletheia verification — with no "it's just a draft" shortcut. The
draft path must also never widen egress: in sovereign mode (`docs/SOVEREIGNTY.md`) the
drafter is selected from the already-filtered local-eligible pool, so speculation can
never route restricted data to a remote model that verification would have kept local.

**7. Scalability implications.** Answer-level speculation scales *better* than
token-level: one draft + one verify call replaces one strong generation, no ragged-batch
machinery needed. The bottleneck moves to verifier honesty at volume (see point 5's
false-accept risk), which is why acceptance must be sampled against ground truth
(`liveeval.py` stride sampling) rather than trusted blindly.

**8. Performance implications.** The economics are the input/output token-price
asymmetry (the answer-level image of "verify batches are nearly free on I/O", §6.5):
verifying a draft is mostly *input* tokens (typically 3–5× cheaper than output across
providers, and far faster), while regeneration is output tokens. Expected cost
≈ `C_draft + C_verify + (1−a)·C_strong`; speculation pays when acceptance
`a > (C_draft + C_verify)/C_strong`. With a Haiku-class drafter at ~1/10–1/30 of an
Opus-class strong model and a short-verdict verify call, break-even sits near
**a ≈ 0.3–0.5** — measurable, not guessable (see R7).

**9. Maintainability implications.** One new module with one seam. The danger is claim
drift: the moment someone writes "lossless" in Olympus docs, the drift-gated capability
culture (README cap markers, CI-verified counts) is violated. The guarantee must be
stated as what it is: *benchmark-bounded non-inferiority with live auto-rollback*.

**10. How Olympus should redesign it.** Translate the invariant, not the math:

- **Accept = ship the draft byte-for-byte.** The verifier renders `accept | reject`
  (never "edit"). An accepted draft is emitted unmodified, so the output provably came
  from a path the strong verifier endorsed in full — no half-trusted splices presented
  as strong-model work.
- **Reject = regenerate WITHOUT the draft in context.** This is the Leviathan
  "banned and renormalized" analog and the one place teams get it wrong: if the strong
  model regenerates *given* the rejected draft, it anchors on the weak model's framing
  and the output distribution drifts toward the drafter. Rejection discards the draft
  entirely; the strong model answers from the original task.
- **Enablement is earned per task type.** Before `(task_type, drafter)` may speculate in
  production, a golden-eval A/B (`olympus eval` items for that specialist,
  `quality_baseline.json` + provenance history) must show the drafted pipeline
  non-inferior — the same gate discipline as `gate_prompt` (README: "applied only if a
  before/after benchmark shows no regression, else auto-rolled-back").
- **Live guard on top** (R4): windowed acceptance below threshold auto-disables.

**11. Final Olympus architecture.**

- **New module `olympus/draftverify.py`** (pure decision core + one orchestration
  function, mirroring the `contracts.py` purity discipline):
  - `DraftVerdict(accept: bool, reason: str, verifier_conf: float)`;
  - `SpecOutcome(source: str, accepted: bool, cost_draft: float, cost_verify: float, cost_regen: float, task_type: str, drafter: str, verifier: str, fingerprint: str)`;
  - `speculative_complete(settings, system, messages, *, task_type) -> str` — picks a
    draft source (R2), drafts, verifies, ships-or-regenerates, records the outcome.
- **Verifier** = the strong pool member with an accept/reject rubric; with
  `OLYMPUS_CONSENSUS=on` the verdict folds through `consensus.safest_verdict` (a reject
  from any quorum stands; `LENSES[3]` ADVERSARIAL is always among the lenses so verifier
  sycophancy has a designed antagonist).
- **Env/CLI (Olympus conventions):** `OLYMPUS_DRAFT` = `off` (default) `| auto | on`;
  `OLYMPUS_DRAFT_MODEL` (shorthand matching like `modelpin.py`); byte-identical behavior
  when off — the Colibri "additive-only, byte-identical with PROF=0" doctrine (§18)
  applied to a feature flag.
- **Trace:** every speculation is a `decision_type="speculation"` record in the existing
  Ed25519-signed `Trace` (`trace.py`) — never a separate log
  (`docs/DESIGN_OUTPUT_CONTRACTS.md` Part 1 rule). Replay safety follows the
  `bandit_routing.py` precedent: during replay the *recorded* accept/reject is
  reproduced, never re-decided.
- **Integration:** Zeus marks a route draft-eligible; Athena's DAG steps carry
  `task_type`; Aletheia runs on the final answer exactly as today (speculation sits
  *before* verification, invisible to it); the heartbeat reviews the acceptance ledger.

**12. Why the Olympus approach is superior.** Colibri's guarantee is exact but only
exists because the engine owns the weights. Olympus's version is honest about being
weaker — and then *stronger where it matters operationally*: Colibri cannot tell you
whether speculation helped answer quality (it's axiomatically neutral); Olympus's gate
produces a signed, accumulating record of measured non-inferiority per task type and
model pair, which is a Calibration Record asset a competitor cannot backfill.

---

## R2. Three draft sources, one verifier → the Olympus draft-source ladder

**1. What Colibri does.** §6.5: three heterogeneous draft sources feed one verifier, in
strict priority — (F) grammar-forced spans, (MTP) the model's own chained draft head,
(E) n-gram prompt lookup. `DRAFT=-1` auto-selects; the multiplexer force-disables the
non-ragged-safe sources while keeping grammar drafts.

**2. Why it exists.** Different content is cheap to predict by different means:
structure is *deterministic* (grammar), prose is *model-predictable* (MTP), repetition is
*lookup-predictable* (n-gram). One verifier makes them composable and individually
disposable.

**3. How it works internally.** Draft tokens from whichever source is armed enter the
same verify forward; per-source guards (R4) kill an underperforming source without
touching the others. §6.5.1–6.5.3.

**4. Strengths.** Priority = marginal-cost order (free structure first, cheap model
second, lookup last); per-source accounting; graceful degradation to plain decoding.

**5. Weaknesses & trade-offs.** MTP requires model-specific weights (and the §5.6 #8
int8 lesson shows how fragile a draft head is to quantization); n-gram is measured
harmful in the cold-disk regime (5% acceptance → 3× slower); the mux restriction shows
draft sources interact badly with shared batching. The general lesson: **every draft
source needs its own economics and its own kill switch**, and the set of viable sources
depends on the serving context.

**6. Security implications.** Each Olympus source has a distinct provenance class:
deterministic skeletons are operator-authored (trusted), cheap-model drafts are
model-generated (verify like any output), and *memory-reuse drafts carry stored user
content* — the reuse source must respect per-user memory isolation (README: the working
model "never leaks across people"; `memory.set_user` scoping) and never resurface a
previously-redacted or corrected answer (check against Aletheia's correction lessons
before reuse).

**7. Scalability implications.** The ladder is embarrassingly parallel-safe: sources are
tried in priority order and only one draft per task is verified, so cost is bounded at
one draft + one verify regardless of how many sources exist. Contrast `moa.py`, which
fans out to N members *by design* (cost grows linearly, `MAX_REFERENCE=4`) — the ladder
is the cheap complement, not a replacement, for MoA's quality-seeking fan-out.

**8. Performance implications.** Source hit-rates differ by orders of magnitude:
a deterministic skeleton for a schema-bound task has near-1.0 "acceptance" at zero LLM
cost; a cheap-model draft on well-specified tasks lands wherever the golden evals say;
answer reuse is bimodal (near-1.0 on true repeats, near-0 otherwise) — exactly Colibri's
n-gram profile, hence default-off with the same honesty.

**9. Maintainability implications.** A `DraftSource` protocol (`propose(task) ->
str | None`, `name`) keeps sources one-file-small and individually deletable — the
Colibri pattern of preserving negative results as opt-in flags with eulogies (§3.1)
applies: a source that measures out stays in-tree, quarantined, with its numbers.

**10. How Olympus should redesign it.** Three sources, translated:

1. **`contract_skeleton`** (analog of grammar drafts, highest priority — see R3):
   for schema-bound steps, deterministically pre-fill the structural skeleton and known
   values (from the plan step's inputs); the drafter only fills free fields.
2. **`cheap_model`** (analog of the MTP head): a weaker pool member drafts the full
   answer. The MTP int8-mandatory lesson (§5.6 #8) translates directly: **do not
   over-cheapen the drafter** — the drafter's floor is set by measured acceptance, not
   by price alone; a too-weak drafter makes speculation strictly negative (draft cost +
   verify cost + ~always regenerate).
3. **`verified_reuse`** (analog of n-gram lookup, default OFF): when `recall.py` /
   `emem.py` finds a previously *verified* answer to a near-identical query, propose it
   as the draft. Like Colibri's `DRAFT=n`, opt-in (`OLYMPUS_DRAFT_REUSE=1`) because on
   non-repetitive workloads it only adds retrieval latency and rejected verifies.

The multiplexer lesson translates to gateways: on the multi-user web/Telegram surfaces,
`verified_reuse` is force-disabled across users (isolation), while `contract_skeleton`
remains safe everywhere — the same shape as "grammar drafts remain in mux."

**11. Final Olympus architecture.** `draftverify.py` hosts the `DraftSource` protocol
and the ladder (`SOURCES = (contract_skeleton, cheap_model, verified_reuse)`);
`OLYMPUS_DRAFT_SOURCES` optionally narrows the set (comma list, mirroring
`OLYMPUS_MODELS` style). Per-source acceptance lands in the same `speculation` trace
records with `source` set, so `evolve.py` can health-review each source independently
(the `evolve.record(OK/DEGRADED/FAIL)` pattern `moa.py` already uses).

**12. Why the Olympus approach is superior.** Colibri's sources are frozen into the
engine; Olympus's are pluggable against a measured protocol, and the priority order is
justified by the same marginal-cost logic but *re-derivable from the ledger* when
economics shift (R7) instead of hard-coded.

---

## R3. Grammar-as-draft-source → output contracts as accelerators, never as mutators

**1. What Colibri does.** §6.6: the grammar is **never a sampling constraint** — only a
draft source. Wherever the GBNF PDA admits exactly one legal next byte, the forced span
is injected as draft tokens. A wrong or desynced grammar can only cost rejected drafts;
output remains byte-identical by construction. JSON-Schema→GBNF compiles fail-closed
("never silently looser"); the `jws` optional-whitespace lesson: a compact-only grammar
desyncs at the first stray space and forfeits every later span. Measured: 1.60
tok/forward on conforming NDJSON; 87% acceptance on sloppy spacing.

**2. Why it exists.** Constrained sampling *masks* model failure (the model "produces"
JSON it never actually chose) and couples correctness to grammar correctness. As a draft
source, the grammar accelerates the honest path instead of replacing it — and in a
disk-streaming MoE, forced spans are *disk reads avoided*, an economics play (§26.8).

**3. How it works internally.** Set-of-stacks PDA (≤64 stacks), lazy arming past
`<think>` preambles, desync re-arm, adaptive kill at <50% acceptance over 32 proposals
(§6.6).

**4. Strengths.** The accelerator/correctness decoupling is the single most elegant idea
in the domain: the grammar has *zero authority over content* and therefore zero failure
modes beyond wasted speed. Fail-closed schema compilation keeps "supports JSON Schema"
honest.

**5. Weaknesses & trade-offs.** Colibri's grammar only helps token-sequential decoding;
the span cap (24) bounds gains; and grammar drafting is greedy-only in mux (§27). The
trade-off Olympus must not repeat from the *other* direction: `contracts.py` today is
purely post-hoc (reject after full generation) — all enforcement, no acceleration; the
schema's deterministic content is regenerated by the expensive model every time and paid
for at output-token prices.

**6. Security implications.** Two existing Olympus doctrines already encode the
principle and must be named as load-bearing: (a) `contracts.py` fails **closed** — a
violating output degrades to the typed "treat as missing" marker, never silently
repaired; (b) `toolcall_repair.py` is **refusal-safe by construction** — it reconstructs
a tool call only when the content names a tool actually offered, so a refusal is never
laundered into an action. The absorption adds the inverse guard: a contract skeleton is
draft-side only and can never *insert* content the model didn't endorse into a shipped
answer — the skeleton ships only inside a draft the verifier accepted whole (R1).
A malicious or wrong schema therefore costs rejected drafts, never wrong outputs —
Colibri's exact security posture, one level up.

**7. Scalability implications.** Skeleton pre-fill shrinks the drafter's job on
structured tasks to value-filling, which is precisely where cheap models are strongest —
raising acceptance where volume is highest (agent-to-agent JSON, `a2a.py`, tool
pipelines, `webplan.py` step outputs).

**8. Performance implications.** Three stacked savings: (i) deterministic skeleton
tokens are never LLM-generated at all; (ii) `contracts.check` runs as the **zero-cost
first verifier stage** — a schema-violating draft is rejected *before* any strong-model
verify tokens are spent (the cheapest possible reject); (iii) the `jws` lesson translates
as: the contract-as-verifier must be canonicalization-tolerant (whitespace, key order,
trailing-newline variance) or it will reject semantically perfect drafts and destroy
acceptance — normalize before compare, exactly one `json.loads`-then-structural-check
deep, which `contracts._check_schema` already is.

**9. Maintainability implications.** No new schema language. The existing
`OutputContract` (deliberately shallow, `DESIGN_OUTPUT_CONTRACTS.md` Part 4) is the whole
vocabulary; skeleton generation is a ~50-line pure function over
`json_schema.required` + `properties`. Anything fancier (nested schemas, GBNF) is scope
creep the design doc already forbids ("Do not pre-build it").

**10. How Olympus should redesign it.**
- `contract_skeleton(contract, step_inputs) -> str | None` in `draftverify.py`: emits
  the object skeleton with required keys and any values determined by the plan step's
  inputs; returns `None` when the contract is absent/no-op (the "empty thing costs
  nothing" discipline).
- Pipeline order per drafted step: skeleton → cheap-model fill → **`contracts.check`
  (free reject)** → strong-model verify → ship or regenerate.
- Never constrain the strong path: when speculation is off or rejected, the strong model
  generates exactly as today and `contracts.check` remains the post-hoc gate. The
  contract's two roles (accelerator on the draft side, fail-closed gate on the commit
  side) share one definition and never conflict — the draft side can only lose speed.

**11. Final Olympus architecture.** Home: `contracts.py` (unchanged definition; one new
pure helper `skeleton()` is acceptable there since it stays I/O-free) + `draftverify.py`
(orchestration). Trace: skeleton use is recorded in the `speculation` record's
`source="contract_skeleton"`; contract checks keep their existing `contract` decision
records — two record types, one signed log.

**12. Why the Olympus approach is superior.** Colibri accelerates *syntax* only; Olympus
accelerates syntax *and* the recurring semantic boilerplate of planned steps (known
inputs flow into the skeleton), and its contract doubles as a free verifier stage —
Colibri's grammar can propose but never pre-reject, because rejection there requires the
full forward anyway.

---

## R4. Acceptance guards & auto-disable → the speculation health governor

**1. What Colibri does.** Every draft source carries a measured tripwire: the MTP soft
guard (#163: acceptance <10% over a 24-proposal window → pause 256 tokens → re-arm), the
grammar adaptive kill (<50% over 32 proposals), n-gram default-off with its measured
eulogy, and `coli plan`'s static auto-tune that emits `DRAFT=0` **with reasons** when the
regime is compute-bound (#389) or low-hit disk-bound (#467) (§17.2).

**2. Why it exists.** Speculation has a failure mode invisible to correctness tests: it
silently makes everything slower. Only a live acceptance measurement distinguishes "free
speed" from "pure overhead."

**3. How it works internally.** Windowed counters per source; pause-and-re-arm rather
than permanent kill (workloads shift mid-generation); plan-time static defaults from
hardware probes. §6.5, §17.2.

**4. Strengths.** Self-defending; honest defaults (a measured-harmful source ships OFF);
the *reasoned* auto-tune (`DRAFT=0` because…) preserves operator trust.

**5. Weaknesses & trade-offs.** Colibri's guards are heuristic thresholds tuned by one
maintainer's measurements, and the acceptance signal is purely local (this run, this
window) — nothing accumulates across sessions except `.coli_usage` for pinning. A
threshold that pauses too eagerly forfeits real gains; too lazily, it burns latency. And
Colibri's guards protect *speed only* — they can, because quality is axiomatically safe
(R1); Olympus's guards must protect speed *and* the non-inferiority claim.

**6. Security implications.** The governor's parameters are performance-relevant, not
security-relevant, so they are legal for bounded self-tuning under the `evolve.py`
doctrine ("a parameter may self-adjust ONLY if registered with an explicit [min,max]
range and is not security-relevant"). The enable/disable *state* must be visible in every
trace (replay must know whether a run speculated — the same run-metadata rule as
`OLYMPUS_CONTRACTS` in `DESIGN_OUTPUT_CONTRACTS.md` Part 8.3).

**7. Scalability implications.** Per-`(task_type, drafter, verifier)` cells keep the
ledger small (dozens of cells, not per-request growth beyond the capped outcome list —
the `routing_outcomes.py` append-only capped pattern). Warmup gating (no cold-start
decisions on no data) copies `bandit_routing.MIN_WARMUP`.

**8. Performance implications.** The governor is the difference between "speculation is
a bet" and "speculation is a measured instrument": expected-cost accounting per cell
(R1.8 formula, with real prices from `providers.fetch_pricing`) turns the Colibri
threshold heuristics into explicit break-even math — pause when the windowed acceptance
drops below the *computed* break-even for that cell, not below a magic 10%.

**9. Maintainability implications.** Zero new storage systems: outcomes ride the
`routing_outcomes.py` substrate style (same `store` backend, capped, best-effort writes
that never raise into the caller), health review rides `evolve.py`, periodic review
rides the heartbeat. One dashboard line in `olympus scores` territory:
`olympus speculation` prints the cell table (acceptance, break-even, state), lowest
margin first — the `coli plan` reasoned-verdict culture.

**10. How Olympus should redesign it.** Three layers, mirroring Colibri's
soft-guard / adaptive-kill / plan-time-static split:
1. **Gate (static, measurement-first):** a cell is *eligible* only after the golden-eval
   non-inferiority A/B (R1.10) — Colibri never needed this layer; Olympus's weaker
   losslessness demands it.
2. **Governor (live, windowed):** acceptance over the last `OLYMPUS_DRAFT_WINDOW`
   (default 24 — keep Colibri's number until measured otherwise) below the cell's
   break-even → pause the cell for `OLYMPUS_DRAFT_PAUSE` tasks (default 50), then
   re-arm. Pause, not kill: workloads shift.
3. **Review (periodic):** the heartbeat's evolve review demotes a cell to `off` with a
   written reason when it has been paused ≥ N consecutive windows — the quarantine-with-
   eulogy pattern (§26.4), surfaced in the evolution report, never silently.

**11. Final Olympus architecture.** `draftverify.py` owns the pure governor math
(`break_even(cell) -> float`, `should_pause(window) -> bool`); outcome rows are written
via a `record_outcome()` that mirrors `routing_outcomes.record` (per-user scoping,
synthetic/replay rows flagged and excluded from the gate — the existing anti-Goodhart
guard); `evolve.py` registers `("draftverify", "window")` and `("draftverify",
"pause_tasks")` with hard ranges; env overrides win (`OLYMPUS_DRAFT_MIN_ACCEPT` forces a
floor above computed break-even for cautious operators).

**12. Why the Olympus approach is superior — and the moat tie-in.** Colibri's acceptance
data evaporates at process exit. Olympus's acceptance ledger **accumulates**: per task
type, per drafter, per verifier, across months — which is literally a slice of Asset 2
(`MOAT_ANALYSIS.md`: "which model actually serves this workload best, measured on the
customer's real tasks") that no lab will publish about its own models. "Haiku drafts
safely for Plutus summaries at 71% acceptance but is uneconomic for Hephaestus code" is
a sentence only an accumulated, deployment-local record can utter.

---

## R5. SPEC_PIN → draft/verify context pinning and the speculation fingerprint

**1. What Colibri does.** `SPEC_PIN=1` pins the S=1 kernel family across draft and
verify because kernel-family divergence collapses acceptance; MTP is off by default
under CUDA because CPU/GPU FP-order divergence on cold experts tanks acceptance
(§6.5.2). The same physics appears in §10.3 (threaded FP reassociation breaks argmax
determinism).

**2. Why it exists.** Speculation compares two computations of "the same" distribution.
Any *incidental* difference between how the draft context and the verify context are
produced manifests as rejections that look like model disagreement but are
infrastructure noise — unfixable by better models, invisible without pinning.

**3. How it works internally.** A flag forcing the verify batch through the same kernel
dispatch family the drafter used. §6.5.2.

**4. Strengths.** Names a failure class most systems never diagnose: *acceptance decay
from environment skew, not from prediction quality*.

**5. Weaknesses & trade-offs.** Pinning trades peak verify throughput for acceptance —
the pinned kernel family may be slower at the verify batch size. It's a knob, not a
default, because the right trade depends on the regime (R7 again).

**6. Security implications.** The Olympus analog has a supply-chain edge: an unpinned
provider alias (`model: latest`-style routing) can silently swap the drafter or verifier
mid-deployment, invalidating every accumulated acceptance cell *and* every golden-eval
gate that authorized speculation. Model identity pinning (`modelpin.py`) is therefore
not just a UX feature here — it is what keeps the non-inferiority evidence attached to
the thing actually running.

**7. Scalability implications.** Fingerprinting keeps acceptance cells honest as the
system grows: without it, a prompt-scaffold change quietly resets the meaning of every
historical acceptance number and the ledger silently lies.

**8. Performance implications.** The dominant Olympus skew sources, in measured-impact
order to be confirmed by the spike (R8/Open questions): (a) **context skew** — drafter
given a truncated/differently-retrieved context than the verifier evaluates against
(the analog of kernel-family divergence; drafts get rejected for "missing" facts the
drafter never saw); (b) **scaffold skew** — different system prompts/skills loaded;
(c) **sampling skew** — drafter run at high temperature vs a verifier judging as if
greedy. Each collapses acceptance without any model being wrong.

**9. Maintainability implications.** One helper, zero policy: a fingerprint is computed,
recorded, and compared — it never blocks anything by itself; the governor (R4) reacts to
the acceptance consequences. That keeps the mechanism observational (Colibri's
byte-identical-when-off instrumentation doctrine, §18/§26.5).

**10. How Olympus should redesign it.** **Pin the draft context to the verify context by
construction:** the drafter receives the *same* system scaffold, the same recalled
memory, the same step inputs, and the same effort setting the strong model would have
received for that step — assembled once, used twice. Where full pinning is too expensive
(the whole point is a cheaper call), the *deviation is declared*: the fingerprint
records which components were reduced (e.g. `context: truncated-8k`), so acceptance
stats stratify by deviation instead of averaging infrastructure noise into model skill.

**11. Final Olympus architecture.** `draftverify.fingerprint(settings, scaffold,
context_refs, effort) -> str` — a stable hash over (drafter model id, verifier model id,
prompt-scaffold hash, context digest, effort, contract hash), stored on every
`speculation` trace record and every acceptance-ledger row. `modelpin.py` pins resolve
before fingerprinting so shorthand aliases can't blur cells. A fingerprint change
auto-opens a fresh cell (old evidence is retired, not silently blended) — the same
model-fingerprint-mismatch-→-ignore-file behavior as Colibri's KV persistence header
(§8.2).

**12. Why the Olympus approach is superior.** Colibri pins one binary's kernel dispatch;
Olympus pins the whole epistemic context of the comparison and makes every deviation
first-class data. SPEC_PIN prevents skew; the fingerprint *prices* it.

---

## R6. Batch-union verify economics + speculative tool prefetch (beyond Colibri)

**1. What Colibri does.** All draft tokens verify through the batch-union forward: each
unique expert the batch needs is read from disk once (§6.3 FASE B), so accepted drafts
convert almost directly into disk reads avoided — grammar drafts are "an economics play"
(§26.8). Separately, the PILOT prefetcher (§7.5) speculatively *loads* likely-next
experts under a strict safety invariant: a misprediction must never kill the server, and
speculation may never evict a genuinely warm resident (#441/#490 eviction guard).

**2. Why it exists.** Speculation's payoff is denominated in the bottleneck resource
(disk bandwidth there; strong-model output tokens and wall-clock here), and prefetching
converts idle capacity into hidden latency.

**3. How it works internally.** §6.3 FASE B (unique-expert union), §7.5 (PILOT ring,
generation barrier, eviction guard).

**4. Strengths.** Verify cost sub-linear in draft length; prefetch bounded by hard
safety invariants rather than hope.

**5. Weaknesses & trade-offs.** PILOT-class prefetch is only safe because expert loads
are idempotent, read-only, and invisible to outputs. The Olympus analog — speculatively
executing tool calls a draft predicts the verified plan will need — is only sound under
the same three properties, which most Olympus tools do NOT have (web fetches cost
egress; actions have side effects). The design must be aggressively narrow or it becomes
the "free counterfactual" fallacy ROADMAP §0 already rejected once.

**6. Security implications.** Hard rules, all inherited from machinery that exists:
speculative tool calls are restricted to `treesearch.READ_ONLY` effect class (never
`REVERSIBLE` — reversal is an action too); every speculative call passes the security
gate and sovereign egress check *exactly as a real call* (`security.assert_egress_allowed`);
results are quarantined (`security.wrap_untrusted`) and **never enter any answer or
context unless the committed plan explicitly requests that same call** — the
eviction-guard analog: speculation may not displace or contaminate committed context.
A misprediction is discarded silently (PILOT's non-fatal load doctrine, §7.2).

**7. Scalability implications.** Bounded by the existing `SearchCaps` discipline: a
per-run speculative-prefetch budget (count + wall-clock), first-trip-wins. Unbounded
prefetch would DOS rate limits and pollute the web-fetch cache.

**8. Performance implications.** The win is latency, not cost (the calls are spent
either way when predicted correctly; mispredictions are pure waste — so the budget
defaults tiny). Highest-value targets: `recall.py` memory retrieval and `websearch.py`
lookups predicted by Athena's plan *before* the strong-model step that consumes them —
overlap LLM latency with I/O latency, Colibri's GPU-compute/disk-I/O overlap (§10.3)
translated to API-call/tool-call overlap.

**9. Maintainability implications.** This is the one capability in the domain that
needs a **bounded research spike** before productization (does plan-predicted prefetch
hit often enough to pay?). Everything else in this document is assembly of existing
parts; this one has an open empirical question. Bound: instrument-only first (record
"would-have-prefetched, was-it-used" in traces for 2 weeks of real runs; ship nothing
that executes), decide from the hit rate.

**10. How Olympus should redesign it.** Phase 0 (instrument-only, as above). Phase 1
(only if hit rate clears the budget math): `speculate.py` grows
`prefetch(run_id, predicted_steps)` — reusing its ledger-recording pattern so every
speculative fetch is a signed, uncommitted record, and `commit()`-time matching promotes
a prefetched result into the run iff the committed step is identical (same tool, same
canonicalized args). `OLYMPUS_SPEC_TOOLS=0/1` (default 0), budget
`OLYMPUS_SPEC_TOOLS_MAX` (default 3 calls/run).

**11. Final Olympus architecture.** Homes: `speculate.py` (orchestration + ledger),
`treesearch.py` (effect classification, unchanged), `toolselect.py` (which tools are
prefetch-eligible: a static allowlist of pure read tools), `security.py` (unchanged
gates on the speculative path). No new module.

**12. Why the Olympus approach is superior.** Colibri's prefetch is safe by memory-
management invariants; Olympus's is safe by *governance* invariants that already exist
(effect classes, approval halts, signed speculation records) — and unlike PILOT, every
speculative fetch leaves an auditable trace, so the hit-rate question is answerable from
the ledger rather than from bespoke telemetry.

---

## R7. The MTP-at-residency inversion → regime-aware speculation economics

**1. What Colibri does.** §10.4: on the fully-resident 6×5090 rig, MTP speculation
*loses* — verify positions route to mostly different experts, so per-unique-expert cost
scales ~linearly with S, and even a 79%-acceptance int8 head measured −5%. The same head
is a clear win in the disk-streaming regime. The inversion flips back if tensor-core
grouped GEMM ever makes S=4 ≈ S=1 — an explicitly flagged future direction. Meanwhile
`coli plan` bakes the regime into static defaults (`DRAFT=0` when compute-bound, §17.2).

**2. Why it exists (as a lesson).** Speculation is not a feature with a truth value; it
is an arbitrage between two cost curves, and the curves move when the substrate moves.
A default tuned in one regime is silently wrong in another.

**3. How it works internally.** Measured A/B on real hardware, preserved as a written
experiment (`docs/experiments/glm52-6x5090-2026-07-12.md`) with the rejected variants
and their data (§10.4).

**4. Strengths.** The honesty of *measuring the inversion* instead of assuming
speculation always helps; recording the condition under which the conclusion flips.

**5. Weaknesses & trade-offs.** Colibri's regime detection is plan-time and coarse
(hardware probes). It cannot notice a mid-life regime change (e.g., the operator adds a
GPU) without a re-plan. And the knowledge lives in docs, not in a machine-consulted
model.

**6. Security implications.** Minimal — but one Olympus-specific case is governance-
relevant: in **sovereign mode** the regime flips hard (remote price ≈ 0 because remote
is forbidden; the "strong" local model's scarce resource is GPU wall-clock), so the
speculation decision must be recomputed from the sovereign-filtered pool, never carried
over from the remote-regime ledger cells (fingerprint R5 handles this: pool composition
is part of the cell identity).

**7. Scalability implications.** The regime model must stay tiny — a handful of
measured inputs, not a simulator: per-model $/Mtok in and out (`providers.fetch_pricing`),
measured p50 latency per model (already observable from traces), and the operator's
objective (`latency | cost | balanced`).

**8. Performance implications.** The concrete Olympus inversions to encode, each the
answer-level image of §10.4:
- **Prompt caching** is Olympus's "full residency": with a cached long prefix, the
  strong model's marginal generation cost collapses, break-even acceptance rises, and
  speculation can flip negative on exactly the workloads (long stable scaffolds) where
  it looked best uncached.
- **Batch/latency-insensitive jobs** (heartbeat digests, overnight training rounds):
  latency wins are worthless, so only the cost inequality matters — cells can be
  economic there at lower acceptance than interactive chat.
- **Cheap-strong-model routes:** when Zeus already routed to a cheap model, drafting
  under it is Colibri's n-gram-on-cold-disk — strictly negative; `draftverify` refuses
  cells where `C_strong` is within a factor `OLYMPUS_DRAFT_MIN_RATIO` (default 3) of
  `C_draft + C_verify`.

**9. Maintainability implications.** One pure function (`break_even`, R4) consuming
live pricing + measured latency keeps the regime model out of config files. Pricing
drift is handled by the existing `providers.fetch_pricing` refresh; a price change
re-derives break-evens on next review, no human in the loop, every change logged.

**10. How Olympus should redesign it.** Make regime awareness *the* enablement input:
`OLYMPUS_DRAFT=auto` (the shipped recommendation once the feature graduates) means
"speculate exactly where `gate ∧ governor ∧ regime` all say yes, per cell" — the
`coli plan` reasoned-auto-tune pattern, but continuous instead of plan-time, and with
every verdict explainable (`olympus speculation` prints the inequality with numbers, the
"plain-language verdict naming the knob" culture of §18).

**11. Final Olympus architecture.** `draftverify.regime(cell) -> RegimeVerdict(ok,
reason, break_even)` reading `providers.py` pricing, trace-derived latency medians, and
`OLYMPUS_DRAFT_OBJECTIVE` (`cost|latency|balanced`, default `balanced`). `compare.py`'s
blind-comparison substrate is the graduation path: a cell's drafted-vs-direct outputs
can be sampled into blind comparison, adding human picks to the evidence — directly
feeding Asset 2.

**12. Why the Olympus approach is superior.** Colibri documents its inversion in an
experiment file and encodes it in plan-time defaults; Olympus encodes the *inequality
itself* and re-evaluates it continuously against live prices and measured latencies —
so when a provider cuts output-token prices 40% overnight (a real, recurring event in
the API economy), the speculation posture adjusts by arithmetic, with the reasoning in
the log, before any human notices.

---

## Open questions & research spikes

1. **Verifier false-accept rate (blocking for `OLYMPUS_DRAFT=auto`).** The whole
   never-worse claim rests on the strong verifier rejecting bad drafts. Spike (bounded:
   ~1 week, uses existing `evals.py` items): measure accept/reject decisions against
   golden-eval ground truth per verifier model; publish the false-accept rate into
   `quality_baseline.json` provenance. If a verifier's false-accept exceeds a few
   percent on a task type, that cell never graduates past `off` regardless of
   acceptance economics.
2. **Verify-call design: verdict-only vs verdict+regenerate-in-one.** A single strong
   call that outputs `ACCEPT` or, failing that, continues into a fresh answer would
   halve rejected-path latency — but risks the anchoring contamination R1 forbids
   (the regeneration shares a context window with the draft). Spike: A/B anchored vs
   clean regeneration on golden evals; if anchoring is measurable, the two-call design
   stands permanently and the finding is written down as the eulogy.
3. **Speculative tool prefetch hit rate (Phase 0 of R6).** Instrument-only for two
   weeks of real traces: how often would a plan-predicted read-only call have been
   used verbatim? Ship nothing executable until the number exists.
4. **Does `verified_reuse` ever pay outside support-style workloads?** Colibri's
   n-gram source earned a default-off eulogy; ours needs its own measurement, not an
   inherited one (Olympus's near-duplicate-query rate is an empirical property of each
   deployment — another per-deployment number the acceptance ledger answers).
5. **Contract-skeleton coverage.** Today all shipped specialists have `contract=None`
   (`DESIGN_OUTPUT_CONTRACTS.md` Part 3). The skeleton source is worthless until
   schema-bearing contracts exist on the high-volume JSON paths (Athena plan steps,
   a2a exchanges). Sequencing: contracts first, skeleton drafting second — do not build
   the accelerator before the structure it accelerates.
6. **Interaction with MoA.** `moa.py` spends N drafts to *raise* quality; draftverify
   spends one draft to *cut* cost at equal quality. They must not stack blindly
   (drafting the references of an ensemble multiplies the false-accept surface).
   Proposed rule until measured: `provider=moa` routes are never draft-eligible.
