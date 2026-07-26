# Colibri Absorption — Master Synthesis & Final Architecture

**Program:** absorb every meaningful capability, principle, and engineering
technique from [Colibri](https://github.com/JustVugg/colibri) (fully
reverse-engineered in `docs/colibri-deep-analysis.md`) into Olympus — as native,
first-principles subsystems, not ports. Colibri runs a 744B MoE on 25 GB of RAM
by treating weights as a learned, measured, tiered streaming problem; Olympus is
a governed multi-agent council on frontier APIs. The absorption target is the
*shape of the thinking*: measured tiers instead of vibes, lossless speculation,
learned placement, refusal over silent degradation, provably non-interfering
telemetry, reject-never-repair at trust boundaries, and negative results kept as
first-class artifacts.

**How this program was built (and how to read it):**

- `01`–`12-*.md` — twelve domain designs, each giving the mandated 12-point
  rubric per Colibri capability (what it does / why / how / strengths /
  weaknesses / security / scalability / performance / maintainability / redesign
  / final architecture / why superior).
- The corpus was then **adversarially reviewed** against the full Colibri
  inventory and against Olympus's binding constraints (`docs/ROADMAP.md` §0).
  In house tradition (`NORTH_STAR.md` kept its own rejection), the review is
  preserved in §5 and **this document is the decision layer above it**: every
  contradiction is resolved here with a binding ruling, every coverage gap is
  closed in `13-review-gaps.md`, and the global budgets the individual documents
  each dodged are imposed in §3.
- Where this synthesis and a domain file disagree, **this file wins.** The
  domain files remain unedited as the analysis record.

---

## 1. The unified architecture

Olympus after absorption is still recognizably Olympus — Zeus routes, Athena
plans dependency graphs, Aletheia verifies, specialists execute, everything
passes the security gate and the measurement culture. What changes is that six
load-bearing ideas from Colibri become **platform invariants**:

**I1 — Everything scarce is tiered, and every tier is measured.**
Models form a price/quality ladder with *measured* grade cards
(`modelgrade`), replacing the hand-typed capability table. Context forms an
explicit T0–T3 hierarchy (pinned prompt / working context / warm store / cold
store) with one budget arbiter (`ctxbudget`) instead of five uncoordinated
budgets. Placement across tiers is driven by learned heat with LFRU + hysteresis
(`ctxheat`), exactly as Colibri pins experts — but per-user, content-free, and
benchmark-gated before any pin-set change ships.

**I2 — Speculation is legal only when provably lossless.**
Colibri's Leviathan discipline (drafts verified; rejection resamples with the
draft banned) becomes the council law: cheap-model drafts are verified by the
strong model, and **rejection regenerates *without the draft in sight*** (the
anti-anchoring invariant, domain 03) so speculation is invisible in output
quality. Acceptance telemetry auto-disables speculation per task cell, as
Colibri's grammar drafts disable themselves under 50% acceptance. Prefetching
never touches the network before a committed plan step (§4, ruling R5) and may
only fill a disjoint staging budget — never evict genuinely hot state (Colibri's
eviction-guard lesson #441/#490, structurally discharged).

**I3 — State is append-only, sealed, and warm-resumable.**
The `sessionlog` journal (per-session, seq-written-last, per-record hashes,
torn-tail truncation) replaces O(conversation) rewrites — Colibri's `.coli_kv`
crash-safety with its three admitted gaps (fsync policy, record seals, format
rigidity) closed. Prefix stability becomes a designed property (stable prompt
layout → provider prompt-cache hits, the KV-prefix-reuse analog) with cache-hit
telemetry proving it works per provider — or reporting, honestly, that it
doesn't (G6 liveness checks).

**I4 — Every trust boundary is reject-never-repair; every ephemeral input is
sanitize-and-continue.**
The single best doctrine sentence the program produced (domain 10): *anything
that persists is validated reject-never-repair; anything ephemeral is
sanitized-and-continued.* `ingestgate` turns Colibri's process-fatal `exit(1)`
into per-artifact, signed, fail-closed refusals across everything Olympus
ingests (skillpacks, plugins, MCP envelopes, memory imports, model endpoints).
Hostile-input parsers get property-based fuzzing with golden malformation
corpora (G5).

**I5 — Observability must be provably non-interfering, and must name the knob.**
Colibri proves byte-identity-when-off by construction (DISK-CLASS private
clocks); Olympus proves it *per commit* with the replay-diff gate (record a
fixture run with observability off, replay with everything on, assert zero
decision diffs — domain 09). Diagnostics end in plain-language verdicts naming
the knob to turn, and Colibri's phase economics become per-request council
economics: routing / planning / specialist / verification / tool time and spend.

**I6 — Negative results, quarantines, and honesty are artifacts, not folklore.**
One experiments registry (ruling R2) holds every quarantined feature with its
empirical eulogy and a scheduled retest — Colibri's `EXPERT_BUDGET` pattern
industrialized. Demos are replays of real runs, labeled. Generated docs get
drift gates like the capability counts already have. Skips carry reasons
(see G3).

### The absorbed capability map (all 12 domains + gaps)

| # | Domain file | Colibri source | Center of gravity of the redesign |
|---|---|---|---|
| 01 | `01-execution-tier.md` | engine core, quantization, GPU/CPU, oracles | Measured model ladder (`modelgrade`), model-swap golden gates + provider-drift tripwire (`modelgate`), local-model qualification campaign (`localtier`), council precision policy (router-F32 rule ⇒ Zeus/Athena on strongest member; verify-floor rule ⇒ Aletheia never below measured floor) |
| 02 | `02-memory-hierarchy.md` | expert tiers, LFRU, AUTOPIN, RSS guard | Explicit T0–T3 context tiers, `ctxheat` learning ledger (ids-and-counters only), `ctxbudget` refusal-over-truncation planner with output reserve + invoice-calibrated estimator |
| 03 | `03-speculation.md` | MTP/grammar/n-gram drafts, Leviathan | `draftverify` draft-cheap/verify-strong with anti-anchoring rejection, contracts-as-draft-accelerators, per-cell acceptance auto-disable |
| 04 | `04-routing.md` | MoE routing, CACHE_ROUTE, expert atlas | `routesub` warmth/cost-aware substitution *within measured quality bands* with always-on agreement telemetry (swap%, KL analog), confound-controlled Specialist Atlas methodology |
| 05 | `05-state-persistence.md` | KV cache, slots, .coli_kv | `sessionlog` sealed append-only journals, prefix-stability discipline, compression-at-the-source (distilled state, not raw transcripts), resume bleed-guards |
| 06 | `06-gateway-api.md` | openai_server, admission, tool-call recovery | Anthropic-compatible serving of the council, unified admission control (ruling R3), end-to-end cancellation, `streamguard`, typed tool-call recovery ladder with repair-rate-as-decay-detector |
| 07 | `07-io-concurrency.md` | PIPE, io_uring, batch-union, mirror | `coalesce` singleflight dedup with trust-boundary scoping and `fresh=True` for Aletheia (epistemic independence), deterministic provider-mirror routing with health probes |
| 08 | `08-prefetch.md` | SPEC/PILOT/COUPLE/LOOKA ladder | Predictability-first sequencing (`olympus predictability` as go/no-go gate), coupling tables from trajectories, staging-only prefetch with $0 default budget |
| 09 | `09-observability.md` | PROF verdicts, DISK-CLASS, Brain view | `phases` per-request economics, verdict engine, replay-diff non-interference CI gate, council heat view folded into existing dashboard |
| 10 | `10-security-integrity.md` | #413 validation, SEC-6/7/8, supply chain | `ingestgate` fail-closed artifact registry, store seals, egress data-classes, incident-response-as-code |
| 11 | `11-ops-reliability.md` | resource_plan, doctor, supervisor | `opsplan` configuration-with-reasons (setdefault-only), `watchdog` progress-based stall detection with spend as progress currency, process forensics, refusal-over-degradation |
| 12 | `12-engineering-culture.md` | quarantine, generated docs, honest demos | `experiments` registry (single, see R2), docs drift gates extended to env vars, release behavioral verification, demo honesty rules |
| 13 | `13-review-gaps.md` | DSA, sampling armor, tokenizer fidelity, fuzz, no-op detection, config skew, o200k | Relevance-budgeted context selection, degenerate-stream defense, estimator calibration, property-testing discipline, optimization liveness checks, restart-required detection, one recorded skip |

---

## 2. Binding rulings (contradiction resolutions)

The review found eleven cross-file conflicts. Rulings, each binding on the
domain files:

**R1 — One measured-evidence store.** `modelgrade.py` is the *single* measured
capability ladder. Domain 04's separate `atlas.py` live store is **cut**; the
Specialist Atlas survives as a *methodology and report* (confound-controlled
probe campaigns whose results land in the same evidence store and in
`docs/`-published atlas reports). `learned_routing`/`bandit_routing` become
consumers of one ladder, resolving 01's own "three selectors is two too many."
The Calibration Record stays observation-only (read-only, per its charter);
`modelgrade` keeps its own ledger — double-bookkeeping accepted deliberately,
because one file is an immutable observation log and the other is an
evidence-shaped operational store.

**R2 — One quarantine registry.** `experiments.py` (domain 12) wins the name and
the mechanism; domain 04's `quarantine.py` is folded into it (its Entry schema
becomes the Experiment schema's eulogy+retest fields; Prometheus runs the
scheduled retests). Env namespace: `OLYMPUS_EXPERIMENTAL_<NAME>`, one wire-up
into the capabilities drift gate.

**R3 — One admission owner.** Mechanics live in an *extended* `usage.slot`
(domain 07's approach — no new module); domain 06's policy layer (priority
classes, per-user round-robin fairness, 429 + `Retry-After`, queue-wait
headers/ledger) is implemented *on top of* that single slot primitive in the
gateway. One env set: `OLYMPUS_MAX_QUEUE`, `OLYMPUS_QUEUE_TIMEOUT` (one default,
300 s), `OLYMPUS_SLOT_RESERVE`.

**R4 — One prompt-cache telemetry schema.** `usage.py` records provider-mirrored
fields (`cache_read`, `cache_creation`, uncached) — domain 05's schema wins
because it mirrors what providers actually report; domain 07's `cached_in`
column is dropped. One CI test (`tests/test_prefix_stability.py`); both
`olympus usage` and `olympus status` read the same source. `ctxbudget` enforces
layout stability; `usage` proves it worked (and G6 flags it when it provably
doesn't). No provider TTLs are designed from memory (05 §5's hardcoded TTLs are
struck; 07's "do not design from remembered provider capabilities" rule is
binding — measure per provider).

**R5 — Network speculation is ruled out.** Domain 08's rejection is binding:
speculative *fetches* are egress-before-decision (a beacon and an injection
amplifier) and additionally violate the sovereignty posture. Domain 07's
`coalesce.prefetch` of step-text-named URLs is **cut**; `coalesce` keeps
singleflight dedup and *local* pre-work only (embeddings, file reads, connection
warm-up). Domain 03's instrument-first spike remains the only path to ever
revisit, and only on offline evidence.

**R6 — Refusal-over-degradation is universal.** Domain 06's "interactive
degrades to fast mode before refusing" is amended to comply with 11 O7: a
degraded answer is legal only when the degradation is *disclosed in the reply*
or the user has explicitly opted into a degrade-first preference. Silent
quality downgrades are banned platform-wide — the same rule that binds model
failover (01), context planning (02), and admission shedding.

**R7 — One route-auth taxonomy.** Domain 10's `egress.DataClass` (C0/C1/C2) is
the classification vocabulary; domain 06's route manifest becomes the per-route
*assignment* of those classes. One CI check enforces the manifest; the
duplicate checker script is dropped.

**R8 — One attach-mode spec.** `OLYMPUS_ATTACH=auto|never|always` (domain 11's
surface) with instance identity from `identity.py` (domain 12's check). One doc,
one env var, one health-payload field.

**R9 — One definition of warmth.** Warmth is an *observation*, recorded once
(`memory/warmth.json`, generalizing 07's mirror-health map) with one writer and
many readers (routing substitution in 04, local pre-work in 08, mirror choice in
07). "T0 warmth" is renamed **prefix stability** everywhere to kill the
overload; provider keep-warm priming follows 08's posture — measured for a week
on the target instance before any `OLYMPUS_WARMUP_EVERY` cadence ships (11's
open question resolved in 08's favor).

**R10 — One coupling/prefetch stack.** `coupling.py` per domain 08's binding
sequencing: `predictability` report first (offline, over existing trajectories,
recall floors as go/no-go), then coupling tables (`memory/coupling.json` — 04's
duplicate file name dropped), then a single orchestrator prefetch hook gated by
`OLYMPUS_PREFETCH`. The LOOKA analog lives in the same module with one CLI
(`olympus predictability`). Domain 04 R6/R7's parallel spec is folded in.

**R11 — Scope trims from the quality flags.** `councilmap` folds into
`phases.py` + existing dashboard surfaces (no new module). `demo.py` is cut to
honesty *rules* in `RELEASING.md` plus a `scripts/` replay bundler. 06's
council model aliases and queue-position ETAs are deferred until a second
external API consumer exists (06's own multi-tenancy logic, applied
consistently). 02's inherited `0.25` hysteresis constant ships behind a
calibration TODO tied to real swap telemetry, not as a blessed default —
ships-with-vibes is exactly what this program condemns. 04's atlas replication
gate gains a single-user mode (replication across *sessions/days* instead of
users) so the flagship methodology isn't inert on the primary deployment shape.
09's verdict thresholds must come from the measured baseline distribution
(percentile-derived, revisited on a cadence), not hardcoded regimes.

---

## 3. Global budgets (what no single document could impose)

The review's sharpest structural finding: twelve locally-frugal documents
collectively proposed ~28 new modules and a dozen new heartbeat job families —
"an unbounded-headcount plan wearing per-file fig leaves." Bindings:

**B1 — Module budget: net +14, hard cap.** After the rulings above, the new
modules are: `modelgrade`, `modelgate`, `localtier`, `ctxheat`, `ctxbudget`,
`draftverify`, `routesub`, `coupling`, `sessionlog`, `coalesce`, `phases`,
`ingestgate`, `watchdog`, `experiments` — **14**, with `opsplan` folded into the
existing `doctor.py`/`config.py` pair, `streamguard`+cancellation+auth
implemented inside the existing gateway modules (`gateway.py`,
`openai_server.py`), and the Anthropic-compatible surface as a handler inside
`openai_server.py` rather than a parallel server. Anything beyond 14 must retire
or absorb an existing module to enter.

**B2 — One measurement ledger, one budget.** Every gate, tripwire, retest, and
probe cadence proposed anywhere in this corpus (model-drift tripwires, atlas
probes, compaction-recall probes, quarantine retests, dossier cadences)
registers in a single heartbeat cadence table with a global
`OLYMPUS_MEASURE_BUDGET_USD` per day. The heartbeat is, per the ROADMAP, the
system's flakiest substrate and true bottleneck: new job families displace or
share slots; none self-adds. The measurement budget rule (ROADMAP F8) is thereby
enforced *globally*, not per-feature.

**B3 — Sequencing subordinate to the ROADMAP.** This program is not a parallel
roadmap. It lands in waves, each shippable alone, each traceable to the
engines the ROADMAP retained:

- **Wave 0 (doctrine, no code):** the platform invariants I1–I6 as documented
  rules + CI greps (execution doctrine, refusal-over-degradation, durable-vs-
  ephemeral boundary, demo honesty, skip-with-reason). Cost ≈ docs.
- **Wave 1 (measurement substrate — the review's "strongest ideas" cluster):**
  `sessionlog`; `ctxbudget` + usage cache telemetry (R4) + estimator calibration
  (G4); `toolcall_repair` extension (no new module); `phases` + the replay-diff
  non-interference CI gate; `predictability` offline report; `modelgate` drift
  tripwire with its dollar budget. Everything here measures before it steers.
- **Wave 2 (evidence-consuming policy):** `modelgrade` ladder (R1);
  `ctxheat` autopin (benchmark-gated); `routesub` substitution bands with
  agreement telemetry; `experiments` registry (R2); `ingestgate`; `watchdog`;
  admission policy on `usage.slot` (R3); G2 stream defense; G6/G7 doctor
  sections.
- **Wave 3 (conditional on Wave-1/2 evidence):** coupling-driven prefetch (only
  if predictability floors pass); `draftverify` speculation cells (only where
  the per-cell A/B pays); `localtier` qualification campaign + sovereignty
  integration; the Anthropic-compatible handler; provider-mirror routing in
  `coalesce`.

**B4 — The corpus polices itself.** The absorption docs adopt the drift-gate
reflex they praise: line-level code citations in 01–13 are treated as unaudited
until a mechanical citation check exists (accepted-debt entry in the experiments
registry with a scheduled spike), and rubric-depth compression flagged in
docs 12 and 10 §9 is recorded there as well rather than silently forgiven.

---

## 4. Why the absorbed Olympus surpasses Colibri

Point by point on the dimensions that matter:

- **Performance.** Colibri optimizes one process's bytes; Olympus optimizes the
  council's *dollars and seconds* with the same discipline — batch-union becomes
  singleflight coalescing across parallel plan branches, KV-prefix reuse becomes
  measured prompt-cache stability, expert pinning becomes learned context/skill
  heat — and every optimization carries a liveness signal (G6) so an inert one
  cannot silently pretend.
- **Reliability.** Colibri's crash-safety ends at count-written-last; Olympus
  adds per-record seals, explicit fsync policy, progress-based (not
  liveness-based) stall detection with spend as a currency, and config/version
  skew made visible instead of self-re-exec magic.
- **Security.** Colibri hardens one input class (model containers) superbly;
  Olympus generalizes reject-never-repair to *every* persistent trust boundary
  behind one fail-closed registry, keeps refusals as signed evidence, scopes
  caches by trust boundary, and guarantees Aletheia's verification reads are
  never satisfied from a cache the claim-maker warmed — an *epistemic* security
  property Colibri has no analog for.
- **Scalability.** Colibri scales down (25 GB) and up (6×5090) on one box;
  Olympus scales across providers, channels, and users — admission control with
  per-user fairness, deterministic provider mirroring, and tiers whose capacity
  is priced in dollars, which (unlike VRAM) scales elastically and is governed
  by budget guards.
- **Extensibility & portability.** Colibri's single-file purity is the right
  call for one engine and one maintainer; Olympus absorbs the *discipline*
  (small module budget, hard cap, retire-to-enter) without the monolith, and
  its execution tier is provider-plural by construction — a new model is a
  catalog row plus a qualification campaign, not a port.
- **Observability.** Colibri names the knob; Olympus names the knob *and*
  machine-proves non-interference per commit, and its telemetry feeds the
  accumulated-asset moat (calibration records, comparative evidence) rather
  than a terminal printout.
- **Developer experience & culture.** Everything Colibri does by heroic
  convention (measurement-justified comments, negative results as opt-ins,
  generated docs, honest demos) Olympus does by *enforced artifact* — drift
  gates, an experiments registry with scheduled retests, benchmark-gated
  prompt/pin changes, and an adversarial-review tradition that this very
  program just exercised on itself.
- **Long-term evolution.** Colibri's moat is craftsmanship; Olympus's is
  accumulation. Every subsystem above is designed to *deposit evidence* —
  grade cards, outcome ledgers, atlas reports, calibration records — so the
  system's advantage compounds with operation, which is precisely the moat
  thesis (`MOAT_ANALYSIS.md`) this program was bound to serve.

## 5. The adversarial review (preserved record)

Verbatim findings of the coherence review over domains 01–12 — kept, per house
tradition, because the critique is as valuable as the designs. Status
annotations show the disposition under §2–§3.

**Coverage gaps** *(all closed in `13-review-gaps.md`)*: DSA sparse attention
(→G1); sampling armor & substrate-tuned defaults (→G2); o200k ahead-of-need
support skip-with-reason (→G3); tokenizer fidelity / estimator ownership (→G4);
fuzz-hardening discipline (→G5); silent no-op substrate detection (→G6);
startup-only settings / config skew (→G7).

**Contradictions** *(all resolved)*: dual quarantine registries (→R2); dual
coupling/prefetch specs (→R10); dual admission designs (→R3); triple
prompt-cache telemetry claims (→R4); three positions on network speculation
(→R5); silent-degrade vs refusal doctrine (→R6); dual route-auth taxonomies
(→R7); colliding attach-mode specs (→R8); four warmth owners (→R9); dual
capability stores (→R1); keep-warm posture conflict (→R9/R11).

**Constraint violations** *(all remediated)*: aggregate ~28-module plan vs
F1/F16 (→B1 cap 14); parallel-roadmap bypass of the rule of construction (→B3
wave sequencing under the ROADMAP); global measurement-budget absence vs F8
(→B2 single ledger + dollar cap); egress-before-decision prefetch (→R5 cut).

**Quality flags** *(dispositions)*: rubric compression in docs 12/10 §9
(recorded as accepted debt, B4); councilmap demo-ware (folded, R11); demo.py
scope creep (cut to rules + script, R11); remembered provider TTLs (struck,
R4); hardcoded verdict thresholds (percentile-derived, R11); speculative API
surfaces without consumers (deferred, R11); vibes-shipped hysteresis constant
(calibration-gated, R11); single-user-inert atlas gate (single-user mode
added, R11); unaudited citation volume (accepted-debt spike, B4).

**Strongest ideas** (the review's top ten, all retained in Waves 1–2):
`sessionlog`'s sealed journal; `modelgate`'s provider-drift tripwire ("the most
API-client-native absorption — it watches exactly the axis Colibri never
needed"); the toolcall-repair extension with repair-rate-as-decay-detector;
`ctxbudget`'s refusal-over-truncation planner with invoice-calibrated
estimation; predictability-as-shipping-gate; coalescing with epistemic
independence for Aletheia; the replay-diff non-interference gate; `ingestgate`'s
durable-vs-ephemeral doctrine; the anti-anchoring rejection rule; the
progress-based watchdog with spend as a currency.

---

*Program artifacts: `docs/colibri-deep-analysis.md` (source inventory),
`01`–`12-*.md` (domain rubrics), `13-review-gaps.md` (gap closures), this file
(decision layer). Next step when scheduling work: read §3 B3 against
`docs/ROADMAP.md` and open ADRs for Wave-1 items only.*
