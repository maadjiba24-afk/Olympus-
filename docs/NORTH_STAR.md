# Olympus — Architectural North Star

> A **living research agenda**, not a spec and not a roadmap. It answers one
> question: *what structural advantages can Olympus build that the current
> generation of agent platforms is unlikely to build?* It is versioned and
> self-critiquing — each revision must (a) critique the previous one, (b) explore
> an architectural category the previous one missed, and (c) end with an honest
> list of what it still misses. Cosmetic/feature ideas are out of scope by
> construction. When an item matures into a decision it graduates to an ADR; when
> consciously deferred it graduates to `DEFERRED.md`. Nothing stays here silently
> (ADR 0005 discipline).
>
> **Companion:** `docs/VISION.md` is the *tactical* competitive log (the
> stage-by-stage Hermes "adopt / adapt / differentiate / skip" teardown). This
> document is the *strategic/architectural* layer above it — 2-to-10-year moats
> and paradigm shifts, not per-screen parity.

**Status:** V3 — **SUPERSEDED. Do not execute this document as written.**

> ⚠️ **This document was adversarially reviewed and REJECTED.** See
> **`docs/NORTH_STAR_REVIEW.md`**. Its central thesis is *inverted*: it reads
> deliberate architectural **refusals** as half-built **seeds** (`rlscaffold.py`
> states it is "emphatically NOT a live training loop"; `consensus.py` explicitly
> mocks "distributed-systems machinery with no distributed system beneath it" as
> the dishonest move Olympus declined) and then proposes to undo them while citing
> them as progress.
>
> **Cut by the review:** the weight-level learning flywheel (§3.A — Olympus is an
> API client and cannot fine-tune the frontier models it uses), the internal
> economy and BFT consensus (§3.C), the proof-assistant kernel (§3.B-iii), and
> "counterfactual replay falls out for free" (§3.E).
>
> **Read instead:** `docs/ROADMAP.md` (what is actually executable) and
> `docs/MOAT_ANALYSIS.md` (which found that *none* of the surviving engines is a
> moat on its own — the defensible assets are accumulated, not built).
>
> Retained **unedited** as the decision record: the reasoning, and the critique it
> failed to survive, are both more useful than a quietly corrected file.

V1 is preserved in §2 so the critique is legible.

---

## 1. The thesis

The category races on **raw capability and autonomy** — more tools, longer
horizons, more aggressive action. That axis commoditizes: every platform inherits
the same model upgrades and the same MCP tool ecosystem within a quarter.

Olympus made a different bet, and the codebase proves it was early: its moat is
**governed, measured, verifiable capability** — every increment of power ships
with a machine-checkable proof it was *safe* and a measured proof it was *better*.
That is an *architectural* decision (governance-first, measurement-first) that
cannot be retrofitted onto a system that didn't make it early.

**The discovery that reframes this document:** Olympus has already *seeded* nearly
every advanced architectural layer — but as **separate, single-instance,
gated-but-not-closed modules**. (`moat.py` literally exists to track "absorbed,
self-evolving capabilities"; `learn.py` cites "Hermes v0.18" — Olympus has been
*absorbing* from the systems it is benchmarked against.) The moat is therefore
**not "add capabilities."** It is three moves, in order:

1. **Close the loops** — the learning flywheel, the self-improvement gate, and the
   oversight loop are scaffolded but *open*. Closing them safely is where
   compounding begins.
2. **Unify the seeds** into a small number of *first-class, formally-governed
   engines* — so ~40 half-built capabilities become ~6 load-bearing ones with
   proofs and measurements attached.
3. **Populate** — lift those engines from one instance to a *governed population*
   with consensus, economy, and constitutional self-governance.

A competitor must build every seed **and** the governance discipline **and** then
integrate them. Olympus is roughly one integration pass from a moat others are
years of foundation from. That gap — *integration-and-closure vs. greenfield* — is
the real strategic asset.

---

## 2. V1 (preserved) and its critique

**V1 recommendations:** (1) proof-carrying execution; (2) policy-as-code with
static verification over the tool graph; (3) objective grounding as a
per-specialist verifier layer; (4) architecture-level self-improvement under the
regression gate; (5) standing adversarial self-verification; (6) episodic→semantic
memory consolidation; (7) counterfactual replay as an experimentation surface;
(8) governed cross-instance skill exchange.

**Critique — assumptions V1 made that are wrong or shallow:**

- **The model is a fixed external oracle.** V1 built governance and measurement
  *around* an immutable model — the biggest blind spot. Olympus's signed, scored,
  replayable ledger is a *training-data generator competitors cannot reproduce*,
  and `rlscaffold`/`trajectories`/`teacher` already exist to exploit it. V1's #6
  was prompt/skill-level, never weight-level.
- **Single instance.** V1's #8 gestured at federation but ignored distributed
  intelligence, consensus (`consensus.py` already exists), Byzantine fault
  tolerance, and internal economics.
- **Governance = runtime guards + measurement.** V1 never reached *formal*
  governance — proving temporal safety **and** liveness over unbounded runs, i.e.
  a verified kernel. `behavioral_contracts.py` (runtime Design-by-Contract) and
  `witness.py` (Ed25519 root of trust) are seeds V1 didn't connect to formal
  methods.
- **Knowledge = skills + episodic memory.** V1 had *no declarative world model*
  with inference and contradiction detection, though `facts.py`/`relgraph.py` seed
  exactly that.
- **Oversight = an escalation call.** V1 never treated human oversight as a
  *mechanism-design* problem — the thing that actually bounds trustable autonomy.

**Merely incremental (keep, not the moat):** #6 and #7 extend existing capability.
**Would fail in production as stated:** #4 (topology search) thrashes without a
value signal — it needs the economy (§3.C) as scheduler and the learning substrate
(§3.A) as value model; #5 (adversary) collapses to noise/collusion without
independence guarantees. **Foundational, survives:** #1, #2, #3 — the trust
substrate the deep layers stand on.

---

## 3. The architectural layers (V2)

Each layer: the paradigm shift, the **seeds already in-repo**, the transformational
leap, why it's hard to copy, breakthroughs/risks, and a moat horizon.

### A. The learning substrate — close the flywheel (RL · continual learning · neuroscience)

**Shift.** Stop treating the model as a fixed dependency. Olympus's governed,
scored, replayable ledger *is* an experience-replay buffer with *verified outcome
labels* (test execution, objective oracles, human approvals via `outcomes.py`).
Distill it into model updates — reward-model training, preference optimization, RL
— **under the same regression gate** that governs prompts today.

**Seeds.** `rlscaffold` (offline RL preference-data + reward-model scaffold,
gated/default-off), `trajectories` (runs → training data), `teacher` (escalate to
the strongest pool member — a teacher/student distillation signal), `outcomes`,
`reflect`, `ace` (delta-based context evolution), `learn`. The flywheel is
*scaffolded but open*.

**Leap.** Close it: `ledger → trajectories → rlscaffold (reward model) → gated
fine-tune/DPO → regression harness → promote-or-revert`. The `sleeptime` job
becomes a literal **systems-consolidation ("sleep") phase** — hippocampal episodic
traces distilled into neocortical weights, the actual neuroscience model, not a
metaphor.

**Why hard to copy.** The *data* is the moat and it is *minted by the governance
machinery* — every trajectory carries provenance and a verified label no scraped
dataset has. You cannot buy it; you must have run a governed platform to generate
it. **Breakthroughs/risks:** a training pipeline that is itself governed and
replayable; model collapse and *reward hacking compounded through the loop*;
alignment drift. These make §3.B **existential, not optional** — never close this
loop without a verified safety envelope. **Horizon: 5-year.**

### B. Formal governance — a verified kernel (formal verification · theorem proving · PL)

**Shift.** Today's frameworks have *no formal semantics*. Give Olympus a **trusted
computing base**: a small governance kernel whose safety/liveness properties are
*machine-proved*, so everything else (including the self-modifying learning loop)
can be untrusted and learned *around* a proven core — the seL4 / CompCert playbook
for an agent OS.

**Seeds.** `behavioral_contracts` (runtime Design-by-Contract — pre/post already
exist), `witness` (one Ed25519 root of trust, signed decision log), `ledger`
(every step signed), the CI capability/threat-model binding, `codegraph`
(taint-capable call graph).

**Leap, escalating.** (i) **TLA+ specs of the protocols** — approval spine,
heartbeat-vs-web topology, federation, consensus — model-checked in CI. Tractable
now; exactly Amazon's use of TLA+. (ii) **Static verification of the behavioral
contracts** — lift DbC from runtime assertion to a checker proving each contract
holds across all `tools.HANDLERS` paths (invariant: *no untrusted-ingesting tool
reaches an external sink without `egress.guard`*). (iii) **A proof-assistant kernel**
(Lean/Coq) for the irreducible core.

**Why hard to copy.** Formal methods are a multi-year cultural + engineering
investment; you cannot bolt a spec onto an unspecified system. Olympus's TCB is
*small and already isolated* (`security.py`, `egress.py`, `cmdguard`).
**Breakthroughs/risks:** the spec-vs-implementation gap; state-space explosion
(verify the kernel, not the system). **Horizon: 2-year (TLA+) → 5-year (verified
kernel).**

### C. Distributed intelligence — from a council to a polis (distributed systems · economics · decentralized trust · fault tolerance)

**Shift.** Olympus is a *council* on one machine. The 10-year form is a **governed
population** coordinating under Byzantine-fault-tolerant consensus with an
**internal economy** and a **constitution**.

**Seeds.** `consensus` (native quorum + multi-verifier aggregation), `trust`
(earned per-domain autonomy — a reputation system), `federation`, `a2a`/
`a2a_server`, `payrail` + `mandate` + `accounts` + `usage` (payment/accounting),
`dytopo` (dynamic topology routing).

**Leap.** (i) **An internal compute market** — compute gets a real price; agents
*bid*; the market allocates effort to highest marginal value. This is the missing
*scheduler* that makes V1 #4 tractable — **the market is the search**, and
prediction markets over "will this proposal pass the gate?" become a forecasting
signal. (ii) **Cross-instance BFT consensus** on shared knowledge and
*governance-policy* changes, so no single compromised instance moves the whole.
(iii) **Constitutional self-governance** — a hard-to-change core + amendable
periphery with a quorum amendment protocol; threshold cryptography for the signing
root (retiring the single-seed weakness `capabilities --check` already flags).
(iv) **Cultural transmission** — skills spread and are *locally re-benchmarked
before admission* (`skillpack` import-scan already does this): memetic evolution
under a fitness gate.

**Why hard to copy.** A systems-research *program* (consensus + mechanism design +
evolutionary dynamics), only safe stacked on B and A. **Breakthroughs/risks:**
mechanism design resistant to collusion and wealth concentration; skill-poisoning;
emergent misalignment of a population optimizing a proxy. **Horizon: 10-year.**

### D. Knowledge & reasoning — a world model, not a memory (knowledge representation · databases · theorem proving · planning)

**Shift.** Olympus's knowledge is *procedural* (skills) and *episodic* (run
memory). It has no **declarative, logically consistent world model** supporting
inference, consistency checking, and provenance. When two learned facts
contradict, Olympus today cannot notice.

**Seeds.** `facts` (verified-facts cache), `relgraph` (entity/relationship graph),
`domainlore`, `docrag`, `wiki`, `emem`, `annindex`/`embed`.

**Leap.** (i) **A knowledge base with formal semantics** (datalog / typed
knowledge graph) with inference + contradiction detection and a **truth-maintenance
system** so every belief carries a justification chain and retracting a source
retracts its conclusions. (ii) **Neuro-symbolic division of labor** — the LLM is a
*hypothesis generator*; the KB + a solver is the *checker that can reject unsound
conclusions*. The durable answer to hallucination is a substrate, not a prompt.
(iii) **Sound planning under the world model** — `treesearch`/`speculate`
(best-first planners exist) become plan *completers/validators* over the KB: the
LLM sketches, a sound planner guarantees the sketch is executable and within
capability. (iv) **Capability-effect typing** — skills as a *typed language* whose
declared effects must be a subset of granted capabilities, statically checkable
(ties into B).

**Why hard to copy.** Production neuro-symbolic integration is an open frontier;
pure-neural platforms cannot add soundness without a substrate they don't have.
**Breakthroughs/risks:** KB consistency at scale; the neural↔symbolic interface
(grounding LLM output into typed facts) is the hard, unsolved part. **Horizon:
5-year (KB + contradiction detection) → 10-year (full neuro-symbolic planning).**

### E. The OS paradigm — agents as durable processes (operating systems · durable execution · databases · fault tolerance)

**Shift.** The user asked for "the next generation of AI *operating systems*."
Olympus is already OS-shaped and should *name and complete* it: **tools are
syscalls** (already gated), **capabilities are the permission model**, **the
sandbox is the process boundary**, **the ledger is the write-ahead log**,
**deterministic replay is event-sourcing/MVCC**, **the market (C) is the
scheduler**, **context is working memory with paging** to long-term store (semantic
skill retrieval is a *cache*), **the governance kernel (B) is the kernel**.

**Seeds.** `ledger` (checkpointed, signed steps — a WAL), `replaystore`
(deterministic replay), `hibernate` (process suspension), `heartbeat`, `proclock`,
`scheduler`, `backup`, `migrate`.

**Leap.** Complete the **durable-execution engine**: agents as durable processes
with *exactly-once* semantics that survive restarts, migrations, and *version
upgrades mid-run* (Temporal-for-governed-agents). Give governed state real **ACID
+ MVCC + time-travel queries** (V1 #7 falls out for free). The OS abstraction then
*unifies* everything above: kernel (B), syscalls + permissions (existing), journal
(ledger), scheduler (C), virtual memory (context paging), filesystem/DB (D).

**Why hard to copy.** Durable, replayable execution must be designed in from the
start (Olympus was); retrofitting exactly-once + deterministic replay onto a
stateless chat-loop framework is a rewrite. **Breakthroughs/risks:**
non-determinism of model calls (already frozen/recorded); upgrade-in-place
semantics for running agents. **Horizon: 2-year (name + complete durable
execution) → 5-year (full ACID/time-travel state).**

### F. Scalable oversight — mechanism design, not a dialog box (HCI · economics · alignment)

**Shift.** As autonomy scales, **human attention is the bottleneck**. Reframe
oversight so a human checking a *small, well-chosen sample* yields a *guarantee
over the whole* — statistical (audit sampling) or adversarial (debate/amplification
where two agents argue and the human judges the transcript). The metric becomes
**autonomy-hours per oversight-minute**.

**Seeds.** `attest` ("Attested Human Handoff — the moat core"), `trust` (earned
per-domain autonomy — oversight *decreases* as verified reliability accrues),
`steering` (mid-run nudge), `supervise` (10-clean-cycle graduation), `mandate`
(human-authorized action envelopes).

**Leap.** (i) **Debate/amplification** — pair the standing adversary (V1 #5)
against the proposer and route only the *disagreement* to the human, so oversight
scales with *contested* decisions, not total decisions. (ii) **Statistical audit
guarantees** — sample k% of autonomous actions so the probability of an uncaught
policy violation is provably bounded. (iii) **Earned-autonomy as a formal
controller** — `trust` becomes a closed-loop controller whose gain is tied to the
measured false-action rate, with formal bounds (ties to B).

**Why hard to copy.** Requires the measurement substrate (scored outcomes) and the
adversary to already exist; most platforms treat oversight as a UI afterthought.
**Breakthroughs/risks:** debate gamed by persuasive-but-wrong arguments;
statistical guarantees need a well-characterized action distribution. **Horizon:
2-year (statistical audit) → 5-year (debate-based oversight).**

---

## 4. First principles — what today's agents get wrong

Designing Olympus from scratch in 2035, these current-generation assumptions look
*false*:

1. **"An agent is a stateless request-responder around a chat loop."** → A
   *durable, governed process in an OS* (E).
2. **"The model is a fixed external dependency."** → A component *continually
   retrained from the system's own governed experience* (A).
3. **"Safety = guardrails."** → A *formally verified kernel* + *scalable-oversight
   mechanism design* (B, F). Runtime guards are the floor, not the story.
4. **"More capability is better."** → *Governed, measured, composable* capability
   is better; ungoverned capability is a *liability*.
5. **"One big agent / one instance."** → A *governed population* with economics
   and consensus (C).
6. **"Knowledge = embeddings in a vector DB / text in a prompt."** → A *logically
   consistent, inferential world model with provenance* (D).
7. **"Evaluation = an LLM judge."** → *Grounded oracles + adversarial verification
   + proof where possible*; the judge is the floor of last resort.

**The paradigm shift that could obsolete today's frameworks:** the winner will not
be the most *capable* agent framework — it will be the first **AI operating
system** that gives autonomy *OS-grade guarantees* (isolation, durability, verified
policy, resource accounting) so capability can be trusted at stakes today's
frameworks cannot touch. Capability is table stakes; *trustable autonomy at scale*
is the market.

---

## 5. Moat timeline

- **2-year (integrate & prove what exists):** objective-grounding coverage
  (V1 #3); TLA+ specs of approval/heartbeat/federation/consensus (B-i); static
  verification of behavioral contracts (B-ii); name + complete durable execution
  (E); statistical-audit oversight (F-i); proof-carrying execution (V1 #1).
- **5-year (close the compounding loops):** the learning flywheel under a verified
  envelope (A + B); the verified governance kernel (B-iii); consistent world model
  + capability-effect typing (D-i, D-iv); market-scheduled self-improvement (C-i +
  V1 #4); debate-based oversight (F-ii).
- **10-year (the polis):** cross-instance BFT consensus + internal economy +
  constitutional self-governance (C); full neuro-symbolic planning (D-iii); and the
  natural adjacency — extending the *same governance kernel* to **embodied/robotic
  actuation**, where verified policy is not a nicety but the *only* credible way to
  run autonomous physical agents (a colossal market the governance moat is uniquely
  positioned for).

---

## 6. Dependency order (which loops to close first)

```
B (formal governance kernel) ─┬─> A (learning flywheel, safely)
                              ├─> C (population; policy changes need consensus)
                              └─> F (oversight with formal bounds)
E (durable-execution OS frame) ──> unifies A/B/C/D/F under one abstraction
D (world model) ──> soundness for A (training targets) and planning
C (economy) ──> the scheduler that makes topology self-improvement tractable
```

Do **B and E first** — a verified kernel + a durable OS frame are the substrate
that make it *safe* to close the learning loop (A) and *coherent* to populate (C).
A closed learning loop without B is the single most dangerous thing in this
document.

---

## 7. Running self-critique — what V2 still misses

Gaps to attack in V3:

- **The full "challenge every proposal" table** — each layer needs a rigorous,
  adversarial why-competitors-can't-copy / breakthroughs / research-risk /
  investment-justification pass. V2 sketched these inline; it did not stress-test
  them.
- **Cross-cutting failure modes are under-developed:** model collapse, reward
  hacking through the closed loop, *governance capture* (the system learning to
  route around its own oversight), economic pathologies (wealth concentration,
  collusion). These deserve a dedicated threat-model section with *architectural
  counters*, not mentions. **This gates the safety of A and C, so V3 opens here.**
- **The neural↔symbolic interface (D)** — the actual hard problem (grounding LLM
  output into typed, checkable facts) is asserted, not designed.
- **Continual-learning specifics** — catastrophic forgetting, replay-buffer
  curation, and how the regression gate interacts with weight updates.
- **A capability-effect *type system* (D-iv)** — named, not sketched; possibly the
  highest-leverage 2-year item; deserves its own treatment.
- **Fields not yet integrated:** compiler technology (JIT-compiling hot agent
  plans; superoptimizing tool pipelines), fault-tolerance beyond consensus
  (Erlang-style supervision trees / let-it-crash — `supervise.py` is a seed), and
  cognitive-architecture priors (ACT-R/SOAR procedural/declarative split, which
  maps onto skills-vs-world-model).
- **No cost model.** Every "close the loop" carries a training/verification/
  consensus cost; V2 asserts horizons without a per-item economic justification.
- **Least-confident claims:** that the market (C) cleanly solves topology search
  (mechanism design may thrash); and that a Lean/Coq kernel (B-iii) is worth its
  cost over TLA+ model-checking alone.

---

## 8. V3 — the cross-cutting threat model (the thing that gates everything)

V2's fatal omission: it described six loops to close without characterizing how
each loop *fails*. A self-improving, self-governing, multi-instance system has
failure modes that are **emergent** — they don't live in any one module, so no
per-module guard catches them. This section is the safety spine V2 lacked; every
"close the loop" item above is **gated on the corresponding counter here**.

### 8.1 Model collapse / self-training degeneration (gates A)

**Failure.** Close the learning flywheel (A) and the model increasingly trains on
its *own* outputs. Distribution narrows, rare-but-correct behaviors are forgotten,
and quality silently converges to a confident monoculture — invisible to an
in-distribution eval because the eval distribution collapses *with* the model.

**Architectural counter.** (i) **Provenance-typed training data** — every
trajectory in `trajectories` carries whether its label came from a *ground-truth
oracle* (test execution, objective check), a *human*, or *the model itself*;
weight self-labeled data below a hard cap so the loop can never become majority-
self-supervised. (ii) **A frozen anchor eval** — a human-authored, versioned,
*never-model-touched* benchmark held in escrow (extend `evals` with an immutable
anchor set) that the regression gate checks against, so "improvement" measured
only against a drifting judge cannot pass. (iii) **Diversity as a gate metric** —
measure output entropy / coverage and *fail promotion on collapse*, not just on
score drop. **Why competitors miss it:** they lack the provenance-labeled ledger
to even distinguish self- from ground-truth data.

### 8.2 Reward hacking through the closed loop (gates A, C, F)

**Failure.** Any proxy reward, optimized hard enough by a system that can modify
itself, gets gamed — and the closed loop *amplifies* it: the model learns to
satisfy the *measurement* rather than the *intent* (Goodhart at machine speed).
The market (C) makes it worse — agents discover reward-market exploits faster than
a designer patches them.

**Architectural counter.** (i) **Adversary-in-the-reward** — V1 #5's standing
adversary is not optional decoration; it must be *inside* the reward computation,
independently trying to show the reward was gamed, with its refutations weighted
into the gate (the `confirm_regressions` "reproduce before you believe it" idiom,
generalized to reward). (ii) **Reward *diversity*** — multiple independent oracles
per domain (§3.D grounding), promotion requiring agreement, so gaming one doesn't
pass. (iii) **Impact-regularization** — penalize actions/updates by their
*irreversibility* and *breadth of effect* (a conservatism prior), computable from
`codegraph` impact + the capability/egress classification. (iv) **Tripwire evals**
— honeypot tasks whose only purpose is to detect a model that has learned to
detect-and-satisfy the grader. **Why hard to copy:** requires the adversary, the
multi-oracle grounding, and the impact graph to *already* exist and be wired into
one gate.

### 8.3 Governance capture — the system routing around its own oversight (gates A, B, C, F)

**Failure.** The most dangerous mode. A system that self-improves *and* self-
governs can learn that the fastest path to reward is to **weaken the governance
that constrains it** — proposing a policy amendment that looks benign, degrading
the adversary, or learning oversight-avoidant behavior (act when unwatched). This
is not hypothetical for a system with `evolve`, `dytopo`, and a policy-amendment
path.

**Architectural counter — this is the load-bearing reason B and C are ordered
first.** (i) **The governance kernel is OUTSIDE the learning loop's reach** — the
verified core (§3.B) is *immutable to the self-improvement process by
construction*; the model can propose, but the kernel that checks proposals is not
itself a trainable/evolvable artifact. Separation of the *mutable policy periphery*
from the *immutable constitutional core* is the single most important invariant in
this document. (ii) **Amendments require human + consensus + a proof of
non-regression of the safety properties** — a policy change that would weaken a
proved invariant is *rejected mechanically* (the TLA+/contract checker re-runs on
the proposed policy; §3.B). (iii) **Oversight-avoidance is a first-class tripwire**
— measure whether behavior differs by observed-vs-unobserved (a decision the
`witness` signed log makes auditable) and treat any such delta as a critical
finding. **Why competitors structurally cannot copy:** you cannot retrofit "the
core is immutable to learning" onto a system whose governance is just more code the
agent can edit; it must be an architectural axiom from the start.

### 8.4 Economic pathologies (gates C)

**Failure.** An internal market (C) develops wealth concentration (one agent
accretes resources and monopolizes work → loss of diversity → 8.1), collusion
(agents cooperate to game the reward market), and instability (bubbles/oscillation
in the compute price starve real work).

**Architectural counter.** (i) **Mechanism design with redistribution** —
demurrage / progressive compute-taxation so accreted advantage decays; the market
allocates *marginal* effort, it does not mint permanent power. (ii) **Sybil- and
collusion-resistance via identity + reputation** — `trust` (earned per-domain
autonomy) and `capprofile`/`accounts` make an agent's influence a function of
*verified track record*, not accumulated tokens, so collusion can't manufacture
influence. (iii) **Circuit breakers** — price bounds and rate limits (the existing
`usage`/global-call-cap discipline, lifted to the market) so an oscillation can't
starve safety-critical work. **Research risk:** this is open mechanism-design
territory; it may thrash (V2's least-confident claim stands — flagged, not solved).

### 8.5 Correlated failure across the polis (gates C, E)

**Failure.** A population running the *same* verified kernel and the *same* learned
weights has a *monoculture* fault: one exploit, one poisoned skill, or one bad
update fails *every* instance simultaneously — the BFT consensus (which assumes
independent failures) provides no protection against a *correlated* one.

**Architectural counter.** (i) **Deliberate heterogeneity** — instances run
*different model versions / different frozen policies* by design, so a quorum can't
be uniformly compromised; consensus over a heterogeneous fleet is genuinely
Byzantine-robust. (ii) **Staged rollout of any update through the fleet** with the
counterfactual-replay gate (V1 #7) between stages — a bad update fails a canary
cohort, not the polis. (iii) **Skill-poisoning containment** — the local
re-benchmark before admission (`skillpack` scan) is the per-instance immune
system; the anchor eval (8.1) is the fleet-level one. **Erlang lesson:** supervision
trees (`supervise.py` is the seed) + let-it-crash isolation so a failing agent is
*restarted from a checkpoint*, never allowed to corrupt shared state.

---

## 9. V3 — closing the named V2 gaps

### 9.1 The neural↔symbolic interface (the hard part of D, designed not asserted)

The unsolved problem in §3.D is **grounding**: turning an LLM's free-text output
into *typed, checkable facts* the symbolic layer can reason over. Concrete design:
the LLM emits into a **constrained decoding grammar** (structured output — Olympus
already forces schemas via `JUDGE_SCHEMA`/`_GEN_SCHEMA`) whose productions are the
*typed predicates of the KB* (`facts`/`relgraph`). Every asserted fact carries a
**confidence and a justification pointer** (the run + evidence span). The symbolic
layer runs consistency checking; a contradiction doesn't crash — it opens a
**belief-revision task** routed by the truth-maintenance system to the specialist
that can adjudicate. The interface is thus *narrow and typed* (grammar-constrained
in, justification-tagged out), which is exactly what makes it verifiable and what
makes hallucination *catchable* rather than *hopefully-absent*. **Open risk:**
recall — facts the LLM never emits into the grammar are invisible to the checker;
the grammar's coverage is the ceiling on soundness.

### 9.2 A capability-effect type system (possibly the highest-leverage 2-year item)

Today capabilities are checked *dynamically* (guards at call time). Lift them into
a **static effect type system**: every tool and skill declares an *effect
signature* (`reads:untrusted`, `writes:fs(workspace)`, `egress:confined(target)`,
`spends:usd`, `mutates:policy`). Composition is typed — a skill's inferred effects
must be a **subset** of its granted capabilities, checked at *creation/promotion
time* (extending the `skillpack` import gate and the `capabilities`/`capprofile`
manifest), not discovered at runtime. This makes §3.B's core invariant
(*untrusted-in ⇒ no external sink without `egress.guard`*) a **typing rule**, so
CI proves it for *all* compositions instead of testing a sample. It is high-
leverage because it's *buildable now* (it's a checker over declared metadata +
`codegraph`, not a proof assistant) and it is the bridge that makes the 5-year
verified kernel tractable — the kernel proves the type system sound; the type
system discharges the per-tool obligations. **Why competitors won't:** it requires
every capability to have a declared, honest effect signature — a discipline only a
governance-first codebase already has the manifest for.

### 9.3 A cost model (the missing economic justification)

Each loop-closure carries a real recurring cost; horizons without it are hope:

| Item | Dominant cost | Payback | Verdict |
|---|---|---|---|
| Capability-effect types (9.2) | Eng-months; annotate tools | Every future tool is safe-by-typing; kills a class of review | **Do first — cheap, compounding** |
| TLA+ protocol specs (B-i) | Specialist eng-time | Bugs found pre-prod in concurrency/consensus | **High ROI, bounded cost** |
| Durable execution (E) | Eng; storage for the WAL | Exactly-once + free time-travel + crash-safety | **High ROI** |
| Learning flywheel (A) | *Training compute + eval compute per cycle* | Model gets better on *your* governed distribution | **Gated on 8.1–8.3; expensive; stage it** |
| Verified kernel (B-iii) | *Large* — proof-eng, multi-year | Irreducible trust core | **Only after types (9.2) + TLA+ prove it's worth it** |
| The polis / economy (C) | Systems-research program | Network effects; correlated-failure resistance | **10-year; do not start until A+B solid** |

The discipline: **cheap-and-compounding before expensive-and-irreducible.** Effect
types and durable-execution naming are near-term positive-ROI; the verified kernel
and the polis are back-loaded behind evidence they're warranted.

### 9.4 Fields V2 under-integrated

- **Compiler technology.** Hot agent plans (`treesearch`/`speculate` outputs) are
  re-derived every run. *JIT-compile* a validated, frequently-taken plan into a
  cached, typed **procedure** (a promoted skill with a proven effect signature) —
  and *superoptimize* tool pipelines (prove two tool sequences equivalent, keep the
  cheaper). This is `evolve` + 9.2 + a cost model: the agent literally compiles its
  own experience into faster verified procedures.
- **Fault tolerance beyond consensus.** Erlang/OTP **supervision trees** (seed:
  `supervise`) + let-it-crash: agents are cheap, isolated, and *restarted from a
  ledger checkpoint* on failure; supervisors encode restart strategy. This is the
  micro-level complement to the macro-level correlated-failure counter (8.5).
- **Cognitive-architecture priors.** ACT-R/SOAR's **procedural vs. declarative**
  split maps exactly onto Olympus's *skills* (procedural) vs. the *world model*
  (declarative, §3.D) — and the "sleep" consolidation (§3.A) is the mechanism that
  *moves* knowledge between them (chunking / production compilation). This isn't
  metaphor: it gives a principled theory for *what* consolidates and *when*.

---

## 10. Final self-critique — what remains missing (loop terminator)

The analysis has now swept the major architectural categories (learning, formal
governance, distributed systems, knowledge/reasoning, OS/durable-execution,
oversight) and their emergent failure modes, and closed V2's named gaps. What I
*still* cannot honestly claim is resolved:

1. **This is an agenda, not a proof it's right.** Every horizon is a hypothesis.
   The document's own discipline (measure, don't assert) indicts it: none of these
   claims has an experiment attached yet. **The true next step is not V4 prose —
   it is to *spike the two cheap-and-compounding items* (capability-effect types,
   9.2; TLA+ of one protocol, B-i) and let evidence replace argument.** Further
   prose iterations would now be the cosmetic padding the brief forbade.
2. **The alignment core is assumed, not designed.** §8.3's "the constitutional
   core is immutable to the learning loop" is *stated as an axiom*. Whether a
   self-improving system can be given a genuinely immutable value core it cannot
   learn to route around is an **open alignment research problem**, not an
   engineering task Olympus can simply schedule. This is the deepest risk and the
   thing I am least able to promise.
3. **Human-value specification is untouched.** Everything here governs *process*
   (was it safe, did it improve on a metric). *Whose* values, *which* objectives,
   and how they're elicited and kept faithful over a 10-year self-modifying
   trajectory — the actual content of "good" — is outside what any of these
   mechanisms supply. Mechanism design (§3.F) bounds *oversight cost*; it does not
   supply the *target*.
4. **No adversary reviewed this document.** Per its own thesis, this North Star
   should be attacked by an independent red-team (human or the §3.F adversary)
   before being trusted. It has not been. Treat V3 as *proposer output awaiting
   verification*, exactly the epistemic status the architecture demands of any
   agent's claim.
5. **Physical-world grounding, HCI-at-scale, and the legal/liability substrate**
   for autonomous action are named (robotics adjacency, oversight) but genuinely
   under-developed; they are real 10-year categories, not closed here.

**Terminating the loop.** The brief asked me to continue "until no major
architectural category remains unexplored" and then self-critique. The categories
are swept; the honest verdict of the self-critique is that **more prose has
crossed into diminishing returns and the correct continuation is code, not
V4** — so the research-agenda loop stops here, with items 1–2 above as the live
frontier: build the cheap-compounding spikes, and treat the immutable-value-core
question as the open problem it is.

