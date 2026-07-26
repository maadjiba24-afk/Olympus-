# Colibri Absorption Programme — Final Report

## 1. Document control

| Field | Value |
|---|---|
| **Title** | Colibri Absorption Programme — Final Report |
| **Status** | **AUTHORITATIVE AND FINAL.** Supersedes every prior absorption document on any point of conflict. |
| **Repository commit** | `429f770` (state reviewed); this report commits on top |
| **Branch** | `claude/colibri-deep-analysis-gpit35` |
| **Report date** | 2026-07-26 |
| **Report owner** | Absorption programme closure review |
| **Source period** | Initial Colibri analysis → Phase 5 completion |
| **Superseded documents** | None deleted. All prior reports remain the historical record of what was known *at their time*; where a later, test-backed finding contradicts an earlier claim, §11 records both and this report states the resolved truth. |
| **Decision authority** | Repository evidence only — code, tests, gates, registries. No claim here rests on a document alone where code could be read. |

**Reading rule.** Where documents conflict, the precedence applied is: (1) later
independently verified evidence, (2) later corrected reports, (3) test-backed
findings, (4) earlier design intent only where no implementation evidence
exists. Conflicts are recorded in §11, never silently resolved.

---

## 2. Executive summary

**Why it started.** Olympus is a Python multi-agent council running on frontier
LLM APIs. Colibri is a pure-C single-file inference engine that runs a 744B MoE
model on ~25 GB of RAM. The two share no runtime, no language, and no problem
domain. The programme's premise was that Colibri's *engineering discipline* —
measured tiers rather than intuition, refusal over silent degradation,
reject-never-repair on persistent artifacts, negative results preserved as
first-class artifacts, provably non-interfering instrumentation — was worth
more than any of its mechanisms, and that its mechanisms were worth studying as
*analogies* rather than as code to port.

**What was actually absorbed.** Thirteen new Olympus modules
(11 during Waves 1–2, 2 during Phase 5) and substantial extensions to roughly
twenty existing ones. Not one line of Colibri code was copied; not one Colibri
mechanism was ported verbatim. The translation that carried the programme was
economic, not structural: **Colibri's scarce resource is disk bandwidth;
Olympus's is token spend.** Expert pinning became learned context and skill
heat. KV-prefix reuse became prompt-cache stability. The token-exact oracle
became golden-eval regression gates. Where the analogy failed, the idea was
rejected (§8) rather than forced.

**What was redesigned rather than copied.** Every absorbed capability was
re-derived from Olympus's own constraints. Nine are documented in §7 as genuine
inventions with no Colibri counterpart — most importantly the *evidence-gate
pattern* itself, in which a capability may be fully designed, built, tested and
still refuse to activate until measured evidence exists.

**What was rejected.** Five substantive Colibri ideas, each with a recorded
reason and an Olympus alternative (§8). The largest is network speculation
(Synthesis ruling R5): a draft/verify scheme that saves disk reads in Colibri
*spends two billed API calls* in Olympus, inverting the economics.

**What remains evidence-gated.** Four capabilities — speculation, predictive
prefetch, local inference tier, provider mirroring. Their designs are complete,
their gates are written, their floors are measured, and all four measure
**NO-GO** on unchanged floors. They are deferred, **not unfinished**: the
absorption work for each is done, and what is missing is operational data that
only a deployed instance with traffic produces.

**Does Olympus still depend on Colibri?** No — verified, not asserted. An AST
scan of every module in `olympus/` finds **zero** imports, identifiers, or
non-comment tokens naming Colibri (§16). The ten mentions that exist are design
provenance in comments and docstrings. Colibri appears in no requirement file,
no build path, and no roadmap document.

**Verdict: COLIBRI ABSORPTION COMPLETE.** See §18 for the criteria and §14 for
what that does — and does not — say about production readiness.

---

## 3. Original objectives

Recovered from `00-SYNTHESIS.md` (rulings R1–R11, budgets B1–B4), the twelve
domain analyses `01-`…`12-`, and `13-review-gaps.md`.

### Architectural

| Objective | Outcome |
|---|---|
| Absorb every *meaningful* Colibri capability, not every capability | **Achieved.** 12 domains analysed; 26 capabilities dispositioned (§5). |
| Never port verbatim — redesign from Olympus's constraints | **Achieved.** Zero copied code; §7 lists nine native inventions. |
| Bound the blast radius: ≤14 new modules (B1) | **Achieved.** 13 of 14 spent; 1 reserved. Three planned modules (`localtier`, `draftverify`, `coalesce`) were never created because their gates failed — the cap was not consumed by speculative code. |
| One evidence store, not many (R1) | **Achieved.** All routing decisions read one authoritative store (Wave-2 A1). |
| One quarantine registry (R2) | **Achieved.** `experiments.py` + `experiments.json`, 21 entries, CI-enforced. |
| One admission owner (R3) | **Achieved.** `usage.slot`. |

### Operational

| Objective | Outcome |
|---|---|
| No new always-on cadence; no default-on spend | **Achieved.** Wave-2 A16; every adaptive capability ships off (§5 activation column). |
| Refusal over silent degradation (R6), universally | **Achieved.** Admission refuses rather than downgrading; the model/effort parameter does not exist on the admission path (source-scanned). |
| Rollback for every capability | **Achieved.** Wave-2 A17; every flag has a deactivation trigger in `experiments.json`. |

### Performance

| Objective | Outcome |
|---|---|
| Measured tiers, never vibes | **Achieved**, and enforced against itself — two performance claims were later found overstated and corrected (§11). |
| Instrumentation provably non-interfering | **Achieved.** `scripts/noninterference_gate.py`, exit 0, in CI. |
| Journal append inside a stated bound | **Partially achieved — bound is depth-qualified.** Measured 1.70 µs/turn of depth scaling after the D1 fix; holds to ~3000 turns of session depth, not unconditionally (§11 F5-1). |

### Security

| Objective | Outcome |
|---|---|
| Reject-never-repair for persistent artifacts | **Achieved.** `ingestgate` (2,080 seeded mutations); `sessionlog` quarantine-and-stop. |
| Sanitize-and-continue for ephemeral payloads | **Achieved.** `openai_compat` typed provider-failure refusal (Phase-4 B-F3). |
| Untrusted-model-mirror threat model translated | **Achieved.** Byte-count-exact validation generalised in `ingestgate`. |

### Product

| Objective | Outcome |
|---|---|
| Dual-dialect API surface | **Achieved.** Both `/v1` dialects through one generation path (W3-A1). |
| No user-visible regression from any absorption | **Achieved.** Every capability default-off or default-additive. |

### Evidence and validation

| Objective | Outcome |
|---|---|
| Negative results preserved as artifacts | **Achieved.** 8 `accepted_debt` entries carry refuted or bounded claims, none deleted. |
| Independent adversarial audit before proceeding | **Achieved** for Wave 1 (4 auditors, +151 tests). **Intentionally changed** for Wave 2: no separate independent audit was commissioned; Phase-4's four validator suites (Stages A–D, 238 tests) served that role across Waves 1–2 together. This is a real deviation from the Wave-1 pattern and is recorded rather than presented as equivalent. |
| "Code exists ≠ complete" | **Achieved**, and enforced against the programme itself — Wave 2's first verdict was NOT COMPLETE (§11). |

---

## 4. Programme timeline

### Stage 0 — Colibri analysis and synthesis
**Purpose:** build a complete inventory before designing anything.
**Delivered:** `docs/colibri-deep-analysis.md` (28 sections); twelve domain
absorption analyses (`01-`…`12-`, ~45 KB each) each carrying a 12-point rubric
per capability; `00-SYNTHESIS.md` with rulings R1–R11 and budgets B1–B4;
`13-review-gaps.md` closing seven gaps an adversarial re-read of the inventory
found (G1–G7).
**Key finding:** the domain analyses independently proposed overlapping
machinery. The synthesis collapsed it — one evidence store, one quarantine
registry, one admission owner, one coupling/prefetch stack — before any code
was written.
**Gate:** design accepted with a hard 14-module cap.

### Stage 1 — Wave 1 (8 capabilities)
**Purpose:** build the measurement substrate.
**Delivered:** `sessionlog`, `ctxbudget`, `modelgate`, `coupling` (4 new
modules) plus replay fixtures, the non-interference gate, cache telemetry, and
the tool-call recovery ladder in existing owners.
**Result:** 3848 → 3849 tests; +199 Wave-1 tests; all gates green.
**Notable discipline:** the module-admission test *rejected* four proposed new
modules, forcing them into existing owners.

### Stage 2 — Wave 1 independent adversarial audit
**Purpose:** the programme's own rule — "code exists + tests pass" is not
completion.
**Method:** four independent auditors, each on a distinct capability cluster,
**forbidden from patching source**. +151 adversarial tests.
**Findings:** 2 blockers + 1 false claim, all resolved (§10).
**Gate:** PASS — Wave 2 unblocked.

### Stage 3 — Wave 2 (10 capabilities)
**Purpose:** the policy layer on top of the substrate.
**Delivered:** `modelgrade`, `ctxheat`, `routesub`, `experiments`, `ingestgate`,
`watchdog`, `streamguard` (7 new modules → 11 of 14) plus admission, liveness
and config-skew work in existing owners.
**Critical event:** the first verdict was **NOT COMPLETE** — ten capabilities
were built, tested and reversible, but five acceptance gates speak about the
*live* system and the capabilities shipped **unwired**. An integration wave
(PR11–15) followed; the revised verdict is COMPLETE with one named gap.
**Result:** 4686 tests; 17/17 gates.
**Named gap (A3):** `ctxheat`'s promotion signal has no honest source — retrieval
runs before the answer exists. It was **not faked**; a test asserts recall-only
heat can never be promoted.

### Stage 4 — Wave 3 evidence review and implementation
**Purpose:** five candidates, each requiring its own evidence floor first.
**Result:** floors measured by running the code. **4 NO-GO, 1 CONDITIONAL GO.**
One capability built (the Anthropic-compatible surface, 49 tests); four
deferred untouched. No floor lowered; no candidate partially built.
**Structural finding:** four of five gates need data only Phase 5 produces — so
the declared Phase 3→5 order cannot be satisfied as written. Recorded rather
than worked around.

### Stage 5 — Phase 4 offline validation
**Purpose:** attack the system that Waves 1–3 built.
**Delivered:** four validator suites — integration (43), security (114),
reliability (62), performance (21) — plus `scripts/perf_validation.py`.
**Findings:** **12 defects**, including two HIGH: cross-principal memory leakage
on `/v1` (every API key shared one namespace) and a disk fault that turned one
logical call into four billed POSTs. Ten fixed with regression tests; two
re-characterised with measured bounds.
**Verdict:** CONDITIONAL GO.

### Stage 6 — Phase 5 staging and shadow foundations
**Purpose:** build the deployment and safety substrate; collect evidence.
**Delivered:** staging profile (fail-closed boot, `/readyz`, SIGTERM drain,
build reporting), shadow mode, the single side-effect boundary (all 130 tools
classified, default-deny), retention and legacy-namespace procedures, the
backup/restore drill, recovery validation, and the Wave-3 gate re-run.
**Notable:** the client-compatibility campaign that Phase 4 had recorded as
*blocked* was **executed** — 25/25, both dialects, real SDKs over real HTTP.
**Baseline event:** the phase opened with a **red** suite, which surfaced an
overstated performance claim (§11 F5-1).
**Verdict:** CONDITIONAL GO FOR CONTINUED STAGING.

### Stage 7 — Closure review (this document)
**Purpose:** evaluate the absorption programme *independently of* Olympus
production readiness.
**Method:** every capability re-verified against code, not against its report;
Colibri independence verified by AST scan; contradictions enumerated.

---

## 5. Final capability matrix

Classifications are the six permitted values. Activation is the **shipped
default**, verified by executing each predicate (not read from a document).

| # | Capability | Colibri purpose | Olympus equivalent | Classification | Location | Verification | Evidence | Activation (default) | Residual limitation | Future owner |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **Execution tiers** | measured storage tiers (RAM→disk), never guessed | measured *model* tiers: qualification cards gate role eligibility | ABSORBED_AND_REDESIGNED | `modelgrade.py` | 51 tests + Phase-4 | 0 cards (no campaign) | `enabled()=False` | needs a credentialed campaign | Local Model Qualification |
| 2 | **Model qualification** | — (Olympus-native need) | Wilson-bounded per-cell grading with freshness | REPLACED_BY_OLYMPUS_NATIVE_DESIGN | `modelgrade.py` | 51 tests | 0 executed cards | off | fail-open when nothing qualifies (recorded, W2-A2) | Local Model Qualification |
| 3 | **Routing intelligence** | MoE sigmoid routing, `CACHE_ROUTE` | `routesub` substitution inside measured bands, warmth ledger | ABSORBED_AND_REDESIGNED | `routesub.py` | 45 tests | 0 decisions | `mode()="off"` | never reaches the verify role by construction | Adaptive Routing |
| 4 | **Context budgeting** | RAM budget + OOM refusal | token-budget planning + refusal over truncation | ABSORBED_AND_REDESIGNED | `ctxbudget.py` | 51 tests | held-out error measured | `enabled()=False` | scalar chars/token cannot serve mixed content (§11) | Adaptive Routing |
| 5 | **Prompt caching** | KV-prefix reuse | prefix-fingerprint hit/miss telemetry, layout-cliff detection | ABSORBED_AND_REDESIGNED | `usage.py`, `llm.py` | 20 tests | telemetry live | **on** (additive) | provider-reported; no independent verification | Observability Platform |
| 6 | **Memory hierarchy** | 5-tier expert store, LFRU + PIN/AUTOPIN | context & skill *heat* — usefulness, not frequency | ABSORBED_AND_REDESIGNED | `ctxheat.py` | 86 tests | shadow only | `mode()="off"` | **A3: promotion signal has no honest source** | Adaptive Routing |
| 7 | **State persistence** | crash-safe `.coli_kv`, count-written-last | sealed hash-linked session journal | ABSORBED_AND_REDESIGNED | `sessionlog.py` | 27 + 37 audit + 13 cache | live | **on** | `compact()` has no production caller | Distributed Execution |
| 8 | **Session journaling** | append-only KV slots | monotonic seq + prev-hash chain, torn-tail truncation only | ABSORBED_AND_REDESIGNED | `sessionlog.py` | as above | live | **on** | depth-qualified to ~3000 turns (§11) | Distributed Execution |
| 9 | **Replay** | deterministic decode | frozen-fixture replay + divergence diffing | ABSORBED_AND_REDESIGNED | `replaystore.py` | 15 tests + gate | live | **on** | base64/split-field secrets slip the screen (registered) | Observability Platform |
| 10 | **Non-interference** | byte-identical-when-off instrumentation | CI gate proving observe-plugins cannot alter a run | ABSORBED | `scripts/noninterference_gate.py` | gate exit 0 | live | **on** (CI) | parallel-dispatch ordering limitation (registered) | Observability Platform |
| 11 | **Provider drift detection** | untrusted-model-mirror validation | behavioural drift tripwire with reproduce-before-believe | ABSORBED_AND_REDESIGNED | `modelgate.py` | 17 + 39 audit | cadence off | cadence off | missing-domain limitation (registered) | Adaptive Routing |
| 12 | **Tool-call recovery** | malformed-tensor salvage ladder | 4-rung repair ladder with an execution precondition (I-T1) | ABSORBED_AND_REDESIGNED | `toolcall_repair.py` | 42 tests | live | **on** (salvage rung off) | `validate=off` is the one escape hatch (registered) | Core Execution |
| 13 | **API compatibility** | dual OpenAI+Anthropic gateway | both dialects through **one** generation path | ABSORBED_AND_REDESIGNED | `web.py`, `openai_server.py` | 49 + 24 campaign | **25/25 real-SDK over HTTP** | **on** | tool-use round-trip untested at client layer | SDK Ecosystem |
| 14 | **Cancellation** | generation abort | end-to-end propagation incl. mid-stream disconnect | ABSORBED | `orchestrator.py`, `web.py` | campaign + Phase-4 | verified over the wire | **on** | — | Core Execution |
| 15 | **Admission control** | bounded-FIFO scheduler, 429s | one admission owner; refusal, never downgrade | ABSORBED_AND_REDESIGNED | `usage.py`, `web.py` | 28 tests | live | flag-gated | — | Enterprise Tenancy |
| 16 | **Concurrency controls** | PIPE worker pool | `MAX_CONCURRENT_CALLS` + measured ledger ceiling | ABSORBED_AND_REDESIGNED | `usage.py` | contention curve | measured 1→16 threads | **on** | ledger lock caps throughput ~2000/s | Distributed Execution |
| 17 | **Speculation** | lossless draft/verify, Leviathan sampling | council draft/verify — **designed, gate written, never built** | DEFERRED_PENDING_EVIDENCE | none (`draftverify` absent) | gate re-run | 0 cards | **not built** | needs qualified draft + verifier per cell | Adaptive Routing |
| 18 | **Predictive prefetch** | `PILOT` router-state prediction (71.6% recall) | coupling-driven local pre-work — **designed, gate written, never built** | DEFERRED_PENDING_EVIDENCE | `coupling.py` (report only) | gate re-run | n=1, **synthetic** | **not built** | needs n≥200 real runs, recall@2≥0.6, CI≤0.1 | Adaptive Routing |
| 19 | **Provider mirroring** | redundant expert sources | mirror routing — **designed, gate written, never built** | DEFERRED_PENDING_EVIDENCE | none (`coalesce` absent) | gate re-run | 0 decisions | **not built** | needs a measured unavailability rate | Adaptive Routing |
| 20 | **Local inference** | the entire premise (local 744B MoE) | local tier qualification — **designed, gate written, never built** | DEFERRED_PENDING_EVIDENCE | none (`localtier` absent) | gate re-run | 0 cards | **not built** | needs a local runtime + campaign | Local Model Qualification |
| 21 | **Observability** | `PROF` verdicts naming the knob | decision log, OTLP export, liveness verdicts, config skew | ABSORBED_AND_REDESIGNED | `trace.py`, `otel.py`, `doctor.py` | 61 tests | live | **on** | +1.5–2.1 ms/run, accepted with a bound | Observability Platform |
| 22 | **Evidence retention** | — (Olympus-native need) | `RETAIN_DAYS` over 5 ledgers + forensics | REPLACED_BY_OLYMPUS_NATIVE_DESIGN | `memory.sweep_evidence` | 6 tests | live | **on**, 30 days | — | Enterprise Tenancy |
| 23 | **Backup and recovery** | — (Olympus-native need) | signed archive + verified restore into a clean tree | REPLACED_BY_OLYMPUS_NATIVE_DESIGN | `backup.py` | 18 drills | **executed** | on demand | off-host delivery untested | Deployment Platform |
| 24 | **Privacy and deletion** | — (Olympus-native need) | complete derived-data deletion, verified; legal hold | REPLACED_BY_OLYMPUS_NATIVE_DESIGN | `retention.py` | 26 tests | executed | mechanism on, **policy unset** | policy is an operator decision | Enterprise Tenancy |
| 25 | **Principal isolation** | — (Olympus-native need) | per-API-key derived principals | REPLACED_BY_OLYMPUS_NATIVE_DESIGN | `web._v1_principal` | Phase-4 + campaign | verified over the wire, across restore, under concurrency | **on** | legacy `api-v1` needs an operator procedure | Enterprise Tenancy |
| 26 | **Spend controls** | RAM budget refusal | daily budget + pre-flight worst-case estimate | ABSORBED_AND_REDESIGNED | `usage.py`, `modelgate.py` | 90/90 under 6 threads | live | **on** | ≤16 concurrent calls/host | Billing & Usage |

**Totals: 3 ABSORBED · 14 ABSORBED_AND_REDESIGNED · 5
REPLACED_BY_OLYMPUS_NATIVE_DESIGN · 4 DEFERRED_PENDING_EVIDENCE · 0 REJECTED ·
0 OUT_OF_SCOPE · 0 INCOMPLETE.**

Rejected *ideas* (as opposed to capabilities) are in §8 — they never became
capabilities, which is why the matrix shows zero REJECTED rows.

### Activation evidence required by each deferred capability

| # | Capability | Exact evidence floor | Current | Procedure |
|---|---|---|---|---|
| 17 | Speculation | a qualified draft model **and** a qualified verifier per cell, plus a per-cell A/B showing acceptance pays for the second call | 0 cards | run a credentialed `modelgrade` campaign; then A/B per cell |
| 18 | Prefetch | recall@2 ≥ 0.6, Wilson CI half-width ≤ 0.1, **n ≥ 200**, provenance ≠ synthetic | n=1, synthetic | accumulate real traffic; `coupling.predictability_report(days=30)` |
| 19 | Mirroring | a measured provider-unavailability rate worth mitigating + deterministic selection | 0 decisions | observe provider health over a meaningful window |
| 20 | Local tier | qualification cards per cell (n ≥ MIN_N, Wilson ≥ floor, fresh) | 0 cards | install a local runtime; run the campaign against it |

---

## 6. Olympus architecture, before and after

**Before.** A council orchestrator over frontier APIs. Zeus routed, Athena
planned a dependency graph, Aletheia verified; 13 specialists, 130 tools, 131
CLI commands. State was a whole-file conversation snapshot. Routing was
heuristic. Instrumentation was ad hoc. There was no evidence layer, no
quarantine registry, no admission owner, and no way to prove a run had not been
perturbed by observation.

**After.** The same council, on a substrate that did not previously exist.

| Plane | Before | After |
|---|---|---|
| **Execution model** | direct dispatch | unchanged core, plus admission (one owner), progress leases, degenerate-stream defence |
| **Request lifecycle** | ask → route → plan → run → verify → reply | same, with journaling, tracing, budget observation and a shadow boundary at the tool seam |
| **Provider abstraction** | per-provider adapters | adapters + a typed provider-failure class, drift fingerprinting, and a qualification layer that can gate role eligibility |
| **Routing** | heuristic | pin > bandit/learned > qualification guard > substitution > heuristic, through **one** authoritative evidence store |
| **State persistence** | snapshot rewrite per turn; a corrupt snapshot loaded as `[]` | snapshot **plus** a sealed hash-linked journal; corruption quarantines and reads stop at the verified boundary |
| **Replay** | none | frozen fixtures, divergence diffing, a CI gate |
| **Context management** | `chars // 4` | calibrated per-class estimation with refusal over truncation (default off, honestly bounded) |
| **Caching** | invisible | prefix-fingerprint hit/miss telemetry with cliff detection |
| **Evidence storage** | none | five append-only ledgers + forensics, all retention-swept |
| **Verification** | Aletheia | Aletheia + a verification floor that substitution structurally cannot cross |
| **Tool execution** | direct dispatch | dispatch through **one** chokepoint carrying a five-band side-effect classification, default-deny |
| **Failure recovery** | exception handling | a 4-rung repair ladder with an execution precondition; typed refusals; accounting that never escalates into a provider retry |
| **API surfaces** | one dialect | two dialects through **one** generation path, both verified with real SDKs over real HTTP |
| **Authentication** | shared token | per-API-key derived principals |
| **Principal isolation** | one `/v1` namespace for all keys | one namespace per key, holding under concurrency and across restore |
| **Cost control** | daily budget | daily budget + pre-flight worst-case estimate + a measured concurrency ceiling |
| **Staging** | none | a fail-closed profile with readiness, drain and build reporting |
| **Shadow execution** | none | a named mode with a proven containment boundary |
| **Retention** | traces and usage only | + evidence ledgers, forensics, and a conversation-policy surface that refuses to invent a policy |
| **Backup/recovery** | archive only | archive + **verified restore**, isolation preserved |
| **Observability** | metrics | metrics + decision log + OTLP + liveness verdicts + config skew, provably non-interfering |

**The final architecture is not a modified Colibri system.** Colibri is a
single-process C engine whose scarce resource is disk bandwidth and whose unit
of work is a token. Olympus is a distributed-capable Python platform whose
scarce resource is token spend and whose unit of work is a verified answer
produced by a council of specialists using tools under approval boundaries.
What transferred was a *way of deciding* — measure before you tier, refuse
before you degrade, preserve the negative result — not a structure.

---

## 7. Olympus-native inventions and redesigns

Nine designs with no Colibri counterpart, or where Colibri's mechanism was
insufficient and the replacement is materially different.

**1. The evidence-gate pattern.** Colibri quarantines a *tuning constant*
(`EXPERT_BUDGET`) when it fails to pay for itself. Olympus generalised this into
a programme rule: a capability may be fully designed, built, tested and
*refuse to activate* until measured evidence exists. Four capabilities sit in
that state today and are not defects. Verified by `experiments.py`'s CI-enforced
registry and by the Wave-3 gate re-runner, which holds floors as constants and
has no mechanism to lower one.

**2. Usefulness-not-frequency heat.** Colibri pins experts by access frequency.
Frequency is the wrong signal when the item is context: a document retrieved 100
times that never helped is worth less than one retrieved 5 times that the
verifier accepted 5 times. `ctxheat` scores on external verifier acceptance,
caps self-reported usefulness below one acceptance, and saturates frequency so
no retrieval count overtakes a single acceptance. Verified by 86 tests.

**3. Content-minimisation by signature.** The heat API *cannot accept content* —
there is no `content`/`text`/`snippet` parameter to drop silently. A caller
cannot attempt to persist user text. Verified by asserting `TypeError` on six
plausible parameter names.

**4. Reject-never-repair with a single permitted mutation.** Colibri's
validation exits the process. Olympus quarantines by copy, stops reads at the
verified boundary, and permits **exactly one** mutation: truncating a torn final
line. Everything earlier is corruption wherever it sits.

**5. The execution precondition (I-T1).** The tool-repair ladder validates
*before* a handler ever runs, so a repaired call cannot execute on the strength
of having been repaired.

**6. Provably non-interfering observability.** Not a convention but a CI gate
that runs a fixture with and without a hostile observe-plugin and diffs the
decisions. Colibri asserts byte-identity by discipline; Olympus proves it per
commit.

**7. Per-key principal derivation.** A domain-separated SHA-256 prefix of the
presented credential: one-way (it lands in on-disk paths), stable across
restarts, and useless against any other system hashing the same key. Verified
over a real socket with two real SDK clients.

**8. The single side-effect boundary.** 40 modules can egress, but only two
sites execute a council tool and both resolve through `tools.resolve_handler`.
One wrapper covers both API dialects plus plugin and MCP handlers. Five bands,
all 130 tools classified, **default-deny** — a tool added next month is refused
until classified. Verified through ten adversarial bypass routes.

**9. Policy-refusing retention.** `OLYMPUS_CONVERSATION_RETAIN_DAYS` has **no
default**, and unset is distinct from 0. A default would invent a legal
position; 0 would silently delete user content. Unset is a *reported* state that
exits non-zero and names the block. The sharpest test asserts that with no
policy, a non-dry-run sweep deletes nothing.

---

## 8. Rejected Colibri ideas

| Idea | Why rejected | Evidence | Olympus alternative | Reconsideration |
|---|---|---|---|---|
| **Network speculation** (draft/verify across providers) | Inverted economics. Colibri's draft/verify saves *disk reads*; across a network it **spends a second billed API call**. Speculation only pays when the draft is nearly free. | Synthesis ruling **R5** | Council draft/verify *within* a qualified local tier — gated, unbuilt (§9) | **Allowed**, but only with a qualified local draft model where the draft is genuinely cheap |
| **Self-re-exec on config skew** | Colibri re-executes itself to apply corrected settings. A council mid-run holds journals, leases and approvals; re-exec would strand them. | `config.py:1400` | Make the skew *legible*: `doctor.config_skew`, 8 skew classes, each naming an operator action | **No** — the alternative is strictly better here |
| **Process-fatal validation** (`exit(1)`) | One hostile artifact would take down a multi-tenant server. | `ingestgate.py:286` | Typed refusal, quarantine by copy, reads stop at the boundary | **No** |
| **Frequency-based pinning** (LFRU) | Frequency ≠ usefulness for context (see §7.2). | `ctxheat.py` design | Verifier-acceptance heat with a self-report cap | **No** |
| **Colibri's 25%+4 budget constant** | Tuned to Colibri's disk economics; carrying it over would be vibes, not measurement. | `ctxheat.py:183` | Value-density selection (score per token), constants marked PROVISIONAL pending calibration | **Allowed** once calibration data exists |

---

## 9. Deferred capabilities

All four have **complete designs and complete gates**. None is unfinished
absorption.

| | Speculation | Predictive prefetch | Provider mirroring | Local inference tier |
|---|---|---|---|---|
| **Current state** | designed; gate written; **not built** | `coupling.py` reports only; **prefetch not built** | designed; gate written; **not built** | designed; gate written; **not built** |
| **Why deferred** | no qualified draft or verify model exists | no operational coupling data | no measured unavailability rate | no local runtime, no campaign |
| **Sample count** | 0 cards | **n=1, provenance synthetic** | 0 decisions | 0 cards |
| **Evidence floor** | qualified draft **and** verifier per cell + per-cell A/B | recall@2 ≥ 0.6, CI ≤ 0.1, n ≥ 200 | a measured unavailability rate | cards per cell (n ≥ MIN_N, Wilson ≥ floor, fresh) |
| **Qualification procedure** | credentialed `modelgrade` campaign, then A/B | 30-day `predictability_report` on real traffic | provider-health observation window | install runtime, run campaign |
| **Risk if activated early** | pays for a second call that does not land | egress before a decision (R5 boundary) | doubled spend for an unmeasured benefit | unqualified model on a protected cell |
| **Activation authority** | human operator, after gate pass | human operator | human operator | human operator |
| **Rollback** | flag off; capability unbuilt | flag off; capability unbuilt | flag off; capability unbuilt | flag off; capability unbuilt |

**On prefetch's n=1.** It moved from 0 to 1 during Phase 5. That is **not**
progress: the run came from this repository's own test executions, provenance
`synthetic`. A floor of n ≥ 200 exists to characterise *real traffic*; 200
synthetic runs would satisfy it numerically and prove nothing. Reported NO-GO on
provenance as well as count.

---

## 10. Independent audit findings

### Wave-1 independent audit (4 auditors, forbidden from patching source)

**B1 — Replay secret screen missed modern credential formats.** MEDIUM-HIGH.
Invariant: no secret leaves in a committable fixture. The v1 screen
`sk-[A-Za-z0-9]{8,}` never engaged on `sk-proj-…` (only 4 alnum chars precede
the dash), so a **live-format key planted in a frozen response exported into a
committable fixture with the manifest reporting a clean scrub.** Fixed with a
dash-tolerant screen plus AWS/GitHub/Slack/Google patterns. Regression test
pins each format. **Residual:** base64 and split-field secrets still slip —
registered as `replay-secret-screen-residual`.

**B2 — Drift-gate budget cap overrun.** HIGH (denial-of-wallet). Trailing-max
cost projection allowed a corpus of `[0.1, 0.1, 5.0]` to spend **$5.20 under a
$1 cap**. Fixed with a pre-flight worst-case estimate before each item.
*Correction during the fix:* the first attempt used `config.MAX_TOKENS` (16k),
so conservative it blocked half the corpus and broke 12 tests; corrected to a
realistic short-answer bound. **Residual:** none.

**I-C4 — false performance claim.** See §11.

### Phase-4 validators (four suites, 238 tests)

**B-F2 — cross-principal memory leakage.** **HIGH.** Invariant: per-user
namespacing. `/v1` built every council with the literal `user="api-v1"`; the
credential fed only the auth compare. Key holder B could read what holder A
saved through `recall_memory`, `search_sessions`, `read_document`,
`list_documents`, `list_todos`. Authentication distinguished callers; the memory
scope threw that distinction away. Fixed with `_v1_principal()`. Regression
tests assert distinctness, one-wayness, stability and path-safety, plus
isolation at the memory layer. **Residual:** legacy `api-v1` data needs an
operator procedure (§12).

**C-D1 — accounting escalated into billing.** **HIGH.** An `ENOSPC` on the usage
ledger escaped into `openai_compat`'s retry handler, which treats `OSError` as a
*provider* fault: **one logical call became four billed HTTP POSTs.** Fixed;
wedged-lock and full-disk are captured with distinct contexts.

**A-D1** replay reported a false divergence on every verified run (and
`replaygate` files GitHub issues). **A-D2** a cancelled run's forensics said
`"ok"`. **A-D3** a dropped history slice could leave no durable evidence — which
disproved a Wave-1 claim (§11). **B-F1** `Infinity` in `max_tokens` dropped the
connection. **B-F3** provider-response parse contained "by luck" via a broad
`except Exception` one layer up. **B-F4** `/api/chat` was the one surface with no
loopback fallback. **C-D2** a trace I/O failure destroyed a computed reply.
**C-D3** two threads shared a snapshot temp path. **D-D1** journal write path
scaled with session depth. **E-F1** absorption evidence ledgers grew unbounded.

All ten fixed with regression tests written against the fixed behaviour, not the
symptom.

### Phase-5 findings

**F5-3 — the phase's own unit caused a 100-test regression.** The
client-compatibility harness installed a stub on `web.orchestrator.Olympus` — a
module attribute — and never restored it, so every later test ran against the
stub. Caught by the full suite; fixed with a `restore()` closure; the incident
is recorded in the function's docstring.

**F5-4 — two CI gates caught drift from Phase-5 work.** The env-docs gate caught
`OLYMPUS_BIND_HOST`, a knob introduced in the same commit. The `ctxheat` wiring
guard caught `retention.py` naming ctxheat; rather than widening the allowlist,
the allowance was paired with an AST check that `retention.py` never *imports*
ctxheat — the guard is now stricter than before.

---

## 11. False claims and corrected claims

Preserved for engineering truth, not blame. Each was found by this programme's
own machinery.

**1. "Estimator error 29.4% → 0.1%" — CIRCULAR.** The benchmark calibrated and
measured against the same injected chars-per-token constant, so it measured
memorisation. Honest held-out measurement (train n=200 → held-out n=200 per
class, bootstrap 95% CI, B=2000): english 6.9% [6.2, 7.6], code 15.7%
[14.2, 17.4], cjk 11.5% [10.2, 12.9], cyrillic 9.0% [8.1, 10.0], json 12.7%
[11.4, 14.0], **mixed 43.9% [42.6, 45.2] — worse than naive `chars//4` at
29.2%.** Every class's p90 exceeds the declared ±15%. **Correction:** I-C4 and
gate A7 re-declared distributionally; `OLYMPUS_CTX_BUDGET` stays default-off;
registered as `accepted_debt` with an activation condition. **Cause:** a
benchmark that trains and tests on the same constant is a memorisation test.

**2. "A6 truncation record is flag-independent" — FALSE AS STATED.**
`WAVE1_COMPLETION_REPORT.md` §1 and §71 claim the truncation event is emitted
even flag-off. Phase-4 A-D3 found `_maybe_compact` runs from `_finish`, where
`trace.current()` is always `None` and the trace is already flushed — so the
event was **not durable evidence**, and in practice the record was gated behind
`OLYMPUS_CTX_BUDGET`. **Correction:** an unconditional `errors.capture` now
precedes the flag-gated path. The A6 gate is satisfied **today**, but not for
the reason originally given.

**3. "The D1 journal fix makes `sync` flat in depth" — OVERSTATED.** The claim
appeared in a test name, the perf harness findings, an SLO note and the
readiness report. Direct measurement (medians of syncs taken *at* depth):

| depth | cached | uncached |
|---|---|---|
| 100 | 1.68 ms | 4.36 ms |
| 500 | 1.73 ms | 14.56 ms |
| 1500 | 2.53 ms | 38.31 ms |
| 3000 | 6.61 ms | 82.20 ms |

Depth-scaling coefficient **1.70 µs/turn cached vs 26.84 µs/turn uncached — a
15.8× reduction, not an elimination.** An O(history) term remains and is
inherent to `sync`'s contract. **Correction:** all four sites corrected; the SLO
is now depth-qualified to ~3000 turns. **Cause:** the original test fitted a
slope across the deciles of one growing run — noise-dominated at small n. It
**flaked in a full-suite run**, which is how it was caught.

**4. "Compatibility is SDK-type-verified only; no third-party client can drive a
socket here" — TOO PESSIMISTIC.** `PRODUCTION_READINESS_REPORT.md` recorded G2
as blocked. Both real SDKs are installed and loopback sockets work.
**Correction:** the campaign ran — 25/25, both dialects, real SDKs, real HTTP.
The claim is now *real-SDK-over-HTTP verified in staging*, still **not**
production-client verified. A CI test enforces the ceiling and distinguishes
asserting from mentioning; a second test proves that detection fires on a bare
claim rather than passing vacuously.

**5. Evidence-counting error (W3R-1).** `WAVE3_EVIDENCE_REVIEW.md` publishes
`print(len(modelgrade.cards()), …)` expecting `0`. It now prints **`4`** —
`cards()` returns the whole document and `len()` counts its four keys. The real
count is `counts["cards"]`, still `0`. Verdict unchanged, but a reviewer running
this repo's own published evidence would read it as *"the floor is now
four-fifths met."* **Correction:** the re-run harness measures the right field
with the explanation inline. The original review is left unedited as the
historical record.

**6. Wave 2 declared complete before it was.** The first verdict was **NOT
COMPLETE** — ten capabilities built, tested and reversible, but five acceptance
gates speak about the *live* system and the capabilities shipped **unwired**.
**Correction:** an integration wave (PR11–15) wired every capability; the
revised verdict is COMPLETE with one named gap. The superseded verdict is
retained in the report as the record.

**7. Deployment claims that were only local.** Every Phase-5 staging artifact is
authored and schema-validated, never deployed — no Docker daemon exists here.
No report claims otherwise; recorded in this list because it is the class of
claim most likely to be misread.

---

## 12. Security and privacy outcome

| Area | Final safeguard | Remaining limitation |
|---|---|---|
| **Secret screening** | dash-tolerant screen + AWS/GitHub/Slack/Google patterns | base64 and split-field secrets slip — registered |
| **Replay data** | screened before fixture export; manifest reports the scrub | as above |
| **Journal integrity** | sha256 seal + prev-hash chain; quarantine-and-stop; one permitted mutation | seal key rotation unsolved — registered |
| **Path traversal** | `safe_id` sanitiser; restore refuses traversal entries | `safe_id` is lossy — distinct principals *can* collide (registered; isolation holds, uniqueness never claimed) |
| **Principal isolation** | per-key derived principals | legacy `api-v1` is commingled — operator procedure required |
| **Anonymous access** | all three HTTP surfaces refuse an off-box caller with no credential | — |
| **API keys** | constant-time compare; keys never appear in derived namespaces or error messages | — |
| **Shadow side effects** | one boundary, 5 bands, 130 tools classified, **default-deny** | proven for the tool path, approval spine and explicit guard; a module bypassing all three is untested |
| **Tool execution** | execution precondition before any handler runs | `validate=off` remains the one escape hatch — registered |
| **Malformed provider responses** | typed provider-failure `RuntimeError`; unnameable calls dropped; all-malformed degrades to text | — |
| **Denial-of-wallet** | daily cap + pre-flight worst-case estimate; refusal, never downgrade | ledger throughput ~2000/s caps enforcement rate |
| **Retention** | `RETAIN_DAYS` over traces, usage, payloads and 5 evidence ledgers | **conversation policy unset — deployment blocked for regulated data** |
| **Deletion** | complete derived-data removal, tombstone before unlink, verified from the filesystem | backups predating a deletion still contain it — documented, not enforced |
| **Legacy namespaces** | inspect / export / quarantine / delete; adoption needs a verbatim acknowledgement; module-wide AST scan forbids automatic adoption | operator must act |
| **Backups** | signed archive; tampered archive refused; restore verified by reading data back | off-host delivery untested |

**Remaining limitations, stated plainly.** The conversation-retention policy is
unset, which blocks regulated and multi-user personal-data use — a decision only
an operator can make. Backup expiry is documented, not enforced. Five accepted
residuals remain registered and pinned by tests that assert each is *still only
that*.

---

## 13. Validation outcome

**Latest verified numbers only.** Historical totals are **not** summed — each
wave's figure was true of a different tree.

| | |
|---|---|
| **Full suite (current tree)** | **5141 passed, 30 skipped, 0 failures** (219 s) |
| Wave-1 tip (historical) | 3849 |
| Wave-2 tip (historical) | 4686 |
| Wave-3 tip (historical) | 4735 |
| Phase-4 tip (historical) | 5002 |
| Independent adversarial tests added | +151 (Wave-1 audit) |
| Phase-4 validator tests | 238 across four suites |
| Phase-5 tests | +139 |

| Gate | Result |
|---|---|
| `compileall` | ✅ |
| capabilities drift | ✅ manifest and README match code |
| threat model | ✅ covers all 130 exposed tools |
| non-interference | ✅ exit 0 |
| no-prerelease dependencies | ✅ |
| env-documentation | ✅ derived scan |
| experiments registry | ✅ 21 entries, expiry enforced |

| Campaign / drill | Result |
|---|---|
| Client compatibility (real SDKs over real HTTP) | **25/25** |
| Backup & restore drill | **12 drills**, restore verified by reading data back |
| Restart / failure / recovery | **7 fault classes**, all contained |
| Shadow containment | **10 bypass routes**, all closed |
| Concurrency (isolation, journal density, accounting) | 3 principals isolated; seqs dense; **90/90** increments |

---

## 14. Production-readiness boundary

### Proven

Architecture implemented and wired; local validation at 5141 tests across seven
gates; an independent adversarial audit that found and fixed two blockers and
one false claim; a staging **configuration** that fails closed; shadow
containment through ten bypass routes; **real-SDK compatibility over loopback
HTTP** on both dialects; backup restoration verified by reading data back into a
clean tree; retention and deletion mechanisms with verified completeness;
evidence gates that hold their floors under pressure.

### Not proven

Cloud or any deployment (no Docker daemon, no host); live provider reliability
(no credentials — **zero model calls in the entire programme's validation
work**); real-user traffic; production load; production rollback thresholds
(every one is expressed against a shadow-measured baseline that does not exist);
real-world operational baselines; an approved retention policy; canary safety;
production readiness.

> **Completion of the Colibri absorption programme does not equal production
> approval for Olympus.**
>
> They are different questions. The absorption programme asks *"did Olympus
> acquire what was worth acquiring, and does it now own it?"* — the answer is
> yes. Production readiness asks *"is it safe to serve real users?"* — that
> remains **CONDITIONAL GO FOR CONTINUED STAGING**, and is blocked on
> infrastructure, credentials, traffic and one operator policy decision, none of
> which is absorption work.

---

## 15. Remaining technical debt

| # | Debt | Severity | Impact | Owner programme | Blocks |
|---|---|---|---|---|---|
| D1 | **Conversation-retention policy unset** | HIGH | blocks regulated / multi-user personal data | Enterprise Tenancy | **canary + production** |
| D2 | `ctxheat` promotion signal has no honest source (A3) | MEDIUM | heat accumulates, promotes nothing | Adaptive Routing | activation of ctxheat only |
| D3 | `sessionlog.compact()` has zero production callers | MEDIUM | journal size bounded only by the 64 MB cap | Distributed Execution | nothing today; long sessions eventually |
| D4 | Journal depth-scaling residual (1.70 µs/turn) | LOW | SLO depth-qualified to ~3000 turns | Distributed Execution | nothing today |
| D5 | Usage-ledger throughput ceiling (~2000/s, p99 123 ms at 16 threads) | MEDIUM | caps fan-out at 16 concurrent calls/host | Billing & Usage | production scaling |
| D6 | Observability overhead +1.5–2.1 ms/run | LOW | <0.1% of a real call | Observability Platform | nothing |
| D7 | base64 / split-field secrets slip the replay screen | MEDIUM | a crafted secret could reach a fixture | Observability Platform | nothing today (registered) |
| D8 | `safe_id` is lossy — principals can collide | MEDIUM | two principals could share a namespace | Enterprise Tenancy | **canary** (multi-tenant) |
| D9 | Journal seal key rotation unsolved | MEDIUM | a rotated key cannot re-verify old records | Distributed Execution | nothing today (registered) |
| D10 | Tool-use round-trip untested at the client layer | MEDIUM | compatibility claim has a hole | SDK Ecosystem | **canary** |
| D11 | Backup expiry documented, not enforced | MEDIUM | a deletion is not durable until archives age out | Enterprise Tenancy | production (regulated) |
| D12 | Off-host backup delivery untested | MEDIUM | disaster recovery unverified | Deployment Platform | production |
| D13 | Staging profile never deployed | HIGH | everything in §14 "not proven" | Deployment Platform | **canary + production** |
| D14 | `validate=off` escape hatch on tool arguments | LOW | operator-only; registered | Core Execution | nothing |

---

## 16. Why Olympus no longer depends on Colibri

**Architecture independence.** The final architecture (§6) is organised around
token spend, verified answers, specialists, approval boundaries and tenant
isolation — none of which exists in Colibri. Five capabilities are marked
`REPLACED_BY_OLYMPUS_NATIVE_DESIGN` because Colibri had no counterpart at all.

**Code independence — verified, not asserted.** An AST scan of every module in
`olympus/`:

- imports naming Colibri: **0**
- identifiers or attributes naming Colibri: **0**
- non-comment, non-string tokens containing "colibri": **0** across all 10 files
  that mention it

The ten mentions are comments and docstrings recording *why a design is what it
is* (e.g. "Colibri's `exit(1)` would let one hostile artifact take down a
multi-tenant server"). That is provenance, and deleting it would destroy the
reasoning while changing no behaviour.

**Build independence.** Colibri appears in no `requirements.txt`,
`requirements.lock`, or `pyproject.toml` entry.

**Capability ownership.** Every absorbed capability lives in an Olympus module
with an Olympus owner, Olympus tests and an Olympus rollback path. No capability
is defined by reference to Colibri behaviour.

**Testing independence.** Two test files mention Colibri
(`test_routesub.py`, `test_wave1_env_docs.py`) and neither asserts parity with
it. No test compares Olympus output to Colibri output.

**Evidence-system independence.** The evidence stores, the quarantine registry,
the gates and their floors are all Olympus-defined. No floor is set by a Colibri
number; the one Colibri constant considered (25%+4) was explicitly rejected as
tuned to different economics.

**Roadmap independence.** `docs/ROADMAP.md` derives from `docs/NORTH_STAR.md` as
amended by `NORTH_STAR_REVIEW.md` and `MOAT_ANALYSIS.md`. Colibri appears in
none of them.

**Operational independence.** No runtime, deployment, backup or recovery path
references Colibri.

**Future innovation.** The four deferred capabilities are gated on *Olympus*
evidence floors, not on Colibri parity. Nothing in the deferred set requires
consulting Colibri to proceed.

### Final dependency classification

| Dependency type | Status |
|---|---|
| Runtime dependency | **No** — verified by AST scan |
| Build dependency | **No** — absent from all dependency files |
| Design dependency | **No** — design provenance is recorded in comments; no design decision is pending on Colibri |
| Roadmap dependency | **No** — the roadmap predates and excludes it |
| Benchmark reference | **Optional** — retained for comparison if a specific capability is proposed |
| Historical reference | **Yes** — this is Colibri's only remaining role |

---

## 17. Programme closure criteria

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | All identified Colibri capabilities classified | ✅ | §5, 26 capabilities, 6 permitted classifications, none vague |
| 2 | All absorbed capabilities mapped to Olympus | ✅ | §5 location column; every module verified to exist |
| 3 | All rejected capabilities documented | ✅ | §8, 5 rejections with reason, evidence and alternative |
| 4 | All deferred capabilities have evidence gates | ✅ | §9; re-runner holds floors as constants |
| 5 | All critical audit findings resolved or explicitly open | ✅ | §10; both HIGH findings fixed with regression tests; residuals registered |
| 6 | No hidden Colibri runtime dependency | ✅ | §16, AST scan: 0 imports, 0 identifiers, 0 code tokens |
| 7 | No future roadmap dependency on Colibri | ✅ | §16; `ROADMAP.md` derives from `NORTH_STAR.md` |
| 8 | Archive index complete | ✅ | `COLIBRI_ARCHIVE.md` |
| 9 | Final report complete | ✅ | this document |
| 10 | Repository tests and gates green | ✅ | §13: 5141 passed, 0 failures, 7 gates |

**10 of 10 met.**

---

## 18. Final verdict

# COLIBRI ABSORPTION COMPLETE

- **The programme is officially closed.**
- **Colibri is now historical reference material.** Its only remaining roles are
  historical research, regression comparison, benchmark comparison, and analysis
  of a specifically proposed capability.
- **No new Olympus programme may be justified solely by Colibri parity.** A
  proposal must stand on user value, evidence, security, reliability, cost,
  maintainability, scalability, or developer experience.
- **Future Colibri reviews require a specific capability proposal and new
  evidence.** Broad re-absorption requires a new approved programme charter.

Four capabilities remain `DEFERRED_PENDING_EVIDENCE`. That is **not** an
incomplete absorption: their designs are complete, their gates are written and
measured, their floors were never lowered, and the modules they would need were
deliberately never created. Deferral on measured evidence is the programme
working as designed — rule 5 is explicit that a deferred capability lacking live
evidence is not a failed one.

---

## 19. Signed evidence statement

**Directly inspected.** All 34 files under `docs/absorption/`; `docs/ROADMAP.md`,
`NORTH_STAR.md`, `SECURITY_RESIDUALS.md`, `THREAT_MODEL.md`, and the ADR index;
`olympus/experiments.json` (21 entries) and `olympus/capabilities.json`; the
source of all 13 absorption-created modules and the seams they extend
(`tools.resolve_handler`, `actions._execute`, `agent._tool_result`,
`openai_compat.run_agent`, `memory`, `usage`, `web`, `cli`).

**Directly tested.** The full suite (5141 passed, 30 skipped, 0 failures) and
all seven CI gates, executed on the reviewed tree. Default activation state for
every adaptive capability executed rather than read from a document. The Wave-3
evidence gates re-run via `scripts/wave3_gate_rerun.py`. Colibri independence
established by an AST scan over every module in `olympus/`.

**Inferred.** Historical wave test totals are quoted from their completion
reports and were **not** re-executed against their original trees — they are
labelled historical and are not summed. Severity ratings and owner assignments
in §15 are judgements informed by the evidence, not measurements.

**Unavailable.** No deployment target, no Docker daemon, no provider
credentials, no representative traffic, no real users. Consequently: zero real
model calls in any validation work; no operational baseline; no live-traffic
behaviour; no cloud, TLS, proxy or multi-host verification.

**No production claim has been made without evidence.** Where evidence was
unavailable the item is recorded as not proven (§14), and where a prior claim
exceeded its evidence it is corrected in §11 rather than quietly restated.
