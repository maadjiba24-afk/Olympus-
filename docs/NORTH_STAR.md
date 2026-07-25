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

**Status:** V2 (first deep pass). V1 is preserved in §2 so the critique is legible.

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
