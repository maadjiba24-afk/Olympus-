# RFC: Adversarial Design Review of `docs/NORTH_STAR.md`

**Status:** Review complete — **REJECT as written.** Major revision required.
**Scope:** `docs/NORTH_STAR.md` V2+V3 (`a73d28e`, `47427e8`).
**Posture:** Adversarial. The goal was to break the document, not improve it.
**Mandate:** NORTH_STAR §10.4 states the document "has not been" red-teamed and
must be treated as "proposer output awaiting verification." This is that review.

**Verdict in one sentence:** the document's central thesis is *inverted* — it
reads deliberate, documented architectural **refusals** as half-built **seeds**,
and then proposes to undo the refusals while citing them as evidence that the
work is nearly done.

---

## 1. FATAL findings

### F1 — The "seeds" thesis is inverted. The seeds are refusals. (kills §1, §3)

The document's reframing claim — *"Olympus has already seeded nearly every
advanced architectural layer… roughly one integration pass from a moat"* — was
produced by **skimming module docstrings**. Reading the modules destroys it.

`olympus/rlscaffold.py`, cited as the seed for the 5-year learning flywheel,
says in its own docstring:

> "Offline RL preference-data + reward-model **SCAFFOLD** — gated, default-off,
> human-in-the-loop, and **emphatically NOT a live training loop**… **No live
> training.** Nothing here updates a language model's weights, calls a model or a
> training API… **No autonomous behavior change.** This module has NO path that
> writes to routing, config, prompts, or any decision… **Every dangerous property
> of RLHF is designed OUT.**"

It is a **linear** Bradley–Terry reward model fit by plain-Python gradient
descent, with *no ML dependency*, whose scores are explicitly "EVIDENCE a human
reads." A test pins that no decision module imports it.

`olympus/consensus.py`, cited as the seed for cross-instance BFT, says:

> "Ruflo ships Raft/Byzantine consensus that, by default, runs over an in-process
> event emitter — **distributed-systems machinery with no distributed system
> beneath it.** Olympus takes the **honest slice**… This module is the pure tally
> core… **No clock, no rng, no I/O.**"

It is a **vote-counting function**. It has no nodes, no network, no protocol.

Same pattern for the economic "seeds": `payrail` — *"SANDBOX/TEST MODE ONLY.
Moves no real money"*; `mandate` — *"creation + verification ONLY (no live rail)."*

**Why this is fatal.** NORTH_STAR §3.A proposes exactly the live training loop
`rlscaffold` was built to refuse, and §3.C proposes exactly the
distributed-consensus theater `consensus` was built to refuse — and both cite the
refusing module as the *foundation to build on*. The document is not a roadmap
extending Olympus's architecture; in its two deepest sections it is a **proposal
to reverse Olympus's documented safety decisions, disguised as completing them.**

**Methodological root cause:** the survey read docstring *first lines* and
pattern-matched on module *names*. No module was read, no test consulted, no
implementation verified. A "seed" inventory built this way has negative value —
it produces confident claims in the wrong direction.

**Required remedy:** every seed citation must (a) quote the module's stated
intent, and (b) explicitly declare whether the proposal *extends* or *reverses*
it. Any reversal must be argued on its own merits against the original decision
record — never counted as existing progress.

### F2 — Olympus does not own model weights. The learning flywheel is not buildable. (kills §3.A)

`backend.py` dispatches to `anthropic` clients, Bedrock Converse, and
OpenAI-compatible endpoints via `(provider, model, api_key, base_url)`. Olympus is
an **API client**. You cannot fine-tune or DPO Claude, GPT, or Kimi through their
inference APIs. The document's core loop — *"gated fine-tune/DPO → regression
harness → promote-or-revert"* — requires a capability Olympus **structurally does
not have**, and this is never once stated as an assumption.

The only escapes both destroy the thesis:
- **Switch to open-weight models you can train.** You abandon frontier capability
  — the thing users actually come for — to gain a training loop. The document
  never acknowledges this trade, let alone argues it.
- **Train only small auxiliary models** (routers, classifiers). Legitimate — and
  `bandit_routing`/`learned_routing` already do the honest version — but that is a
  *routing optimization*, not "the model is no longer a fixed dependency." It does
  not support a 5-year moat claim.

### F3 — The data-moat argument is backwards by six orders of magnitude. (kills §3.A "why hard to copy")

The claim: *"The data is the moat… every trajectory carries provenance and a
verified label no scraped dataset has. You cannot buy it."*

The frontier labs have hundreds of millions of interactions **with verified
outcomes** — code that compiled, tests that passed, human preference labels — and
industrial RLHF/RLAIF pipelines to consume them. A single-operator governed
platform produces perhaps 10³–10⁴ trajectories. Preference-tuning at that scale
does not produce a better model; it produces a model **overfit to your own eval
distribution** — which §8.1 then has to invent an escrowed anchor eval to detect.

The document proposes to compete with Anthropic and OpenAI **on their home turf,
at ~10⁻⁶ of their data scale, with none of the training infrastructure**, and
calls it a moat. Provenance quality does not close a six-order-of-magnitude volume
gap for gradient-based learning.

### F4 — The dependency DAG is rooted in an admitted open research problem. (kills §6)

§6: *"Do **B and E first**… A closed learning loop without B is the single most
dangerous thing in this document."* Everything depends on B (the verified
governance kernel). B's load-bearing safety property, per §8.3, is that *"the
governance kernel is OUTSIDE the learning loop's reach… immutable to the
self-improvement process by construction."* Then §10.2 concedes:

> "§8.3's 'the constitutional core is immutable to the learning loop' is *stated
> as an axiom*. Whether a self-improving system can be given a genuinely immutable
> value core it cannot learn to route around is an **open alignment research
> problem**."

So: the root of the dependency graph is an unsolved research problem, which means
**the plan is not executable in the stated order** — not "risky," *unexecutable*.
A roadmap whose first step is "solve an open problem in alignment" is not a
roadmap. Burying this in §10 while §6 presents a confident execution order is the
document's most serious rhetorical failure.

---

## 2. SEVERE findings

### F5 — Formal verification of a daily-changing Python codebase is fantasy. (guts §3.B)

- **Cost class.** seL4: ~20 person-years for ~10k lines of C against a *frozen*
  spec. Olympus is ~230 Python modules under daily change by an agent that
  modifies itself.
- **No semantics.** Python has no formal semantics. There is no Lean/Coq path to a
  verified kernel without rewriting the TCB in a verifiable language — a rewrite
  the document never proposes, budgets, or admits.
- **Proof maintenance is the unpriced killer.** Verification cost is not one-time;
  it scales with *change rate*. A self-improving system is a maximal-change-rate
  system. The document never mentions proof maintenance at all.
- **TLA+ verifies your model, not your code.** The spec-vs-implementation gap gets
  one clause ("the spec-vs-implementation gap") and is then ignored, when it is the
  entire question of whether the exercise delivers safety or theater.

**B-iii (proof-assistant kernel) should be deleted, not scheduled.** B-i (TLA+ on
*one* protocol) survives only as a bounded experiment (see §4).

### F6 — The internal economy is a manufactured problem. Delete it. (kills §3.C-i, §8.4)

Markets allocate scarce resources among **self-interested agents holding private
information**. Olympus's "agents" are subroutines the operator wrote. They have no
private information, no genuine preferences, and no interests to align. There is
nothing for a market to discover that a scheduler and a cost budget cannot express
directly.

Worse, the proposal is self-harming: introducing an internal economy **creates**
the collusion, wealth-concentration, and Goodharting pathologies that §8.4 then
needs demurrage, progressive taxation, Sybil resistance, and circuit breakers to
suppress. **The document invents a disease in §3.C and sells the cure in §8.4.**
Every line of that mechanism design is work that exists only because of an
unnecessary architectural choice.

The stated justification — *"the market is the search"* for topology
self-improvement — also fails independently: it depends on cheap counterfactual
evaluation, which F9 shows does not exist.

### F7 — BFT is cargo-culted from blockchain. (kills §3.C-ii)

BFT assumes **mutually distrusting parties with independent failure modes**. All
Olympus instances are one operator's, running identical code and (per §3.A) the
same learned weights. Failures are **perfectly correlated** — the exact case BFT
provides no protection against.

§8.5 half-notices this, then proposes a counter that is worse than the disease:
*"deliberate heterogeneity — instances run different model versions / different
frozen policies."* This **destroys deterministic replay** (the document's own
strongest asset, §3.E), **destroys eval comparability** (you can no longer compare
scores across the fleet — the very problem that consumed multiple PRs in this
codebase's history), and multiplies infrastructure cost by the number of variants.

If you own all the nodes, you need **replication, checkpointing, and staged
rollout** — none of which require consensus. The BFT framing adds cost and removes
nothing.

### F8 — The measurement gate is the unpriced bottleneck, and it is already the weakest component. (cross-cuts everything)

The document says "under the same regression gate" as though gating were free. In
this very repository, that gate:
- takes **~14 minutes** per run and costs real API money per invocation;
- is flaky enough that **multiple PRs were consumed** diagnosing baseline drift and
  a refusal-floor false-positive;
- required a **confirmation pass** ("noise rarely strikes twice") to be usable at
  all;
- and currently gates only **prompts and skills** — the cheapest possible objects.

NORTH_STAR proposes to gate, with the same machinery: weight updates, topology
deltas, policy amendments, fleet-wide skill imports, and market decisions. The
measurement substrate is **the most expensive and least reliable component in the
system**, and its cost scales *superlinearly* with the number and complexity of
things gated. It is the true bottleneck of the entire program and it is priced at
zero in §9.3.

### F9 — "Counterfactual replay falls out for free" is false. (kills §3.E claim, undermines §3.C-i)

§3.E: *"Give governed state real ACID + MVCC + time-travel queries (V1 #7 falls
out for free)."*

Deterministic replay works by **replaying frozen recorded model outputs**. Change
a policy, a prompt, or a skill, and the model's output changes — the recording is
**invalid from the first divergent token onward**. The document's mitigation
("re-execute only the divergent subtree") misunderstands LLM systems: divergence
propagates immediately and totally, so "the divergent subtree" is in practice
*everything after the change*.

Therefore counterfactual replay is **re-run everything, at full cost, with
sampling noise** — not a cheap experimentation surface. This independently
undermines C-i (market-scheduled topology search), which assumed cheap
counterfactual evaluation as its fitness function.

### F10 — The anchor eval reintroduces the human bottleneck at the point of maximum leverage, and is self-contradictory. (weakens §8.1)

The escrowed anchor must be simultaneously: **frozen** and never model-touched;
**large and diverse** enough to detect distributional collapse; and **refreshed**
as capabilities grow — but refreshing *is* touching, and an anchor that is never
refreshed becomes irrelevant precisely as the system improves.

Who authors it? Human authorship at the scale required to detect collapse across
ten specialist domains is exactly the human cost the self-improvement program
exists to avoid. The collapse counter reintroduces the bottleneck the flywheel was
built to remove.

### F11 — Neuro-symbolic soundness as designed is vacuous. (guts §9.1, §3.D-ii)

§9.1 concedes: *"facts the LLM never emits into the grammar are invisible to the
checker; the grammar's coverage is the ceiling on soundness."*

That is not a footnote risk — it is the **refutation**. You can only check what
was emitted into the typed grammar, i.e. the claims the model already
structured — the easy case. Hallucinations that never enter the grammar pass
through entirely unchecked. So the mechanism does not deliver "the durable answer
to hallucination"; it delivers *soundness-within-the-typed-subset*, which is a far
weaker and much less interesting claim than §3.D advertises.

Additionally: constrained decoding measurably degrades generation quality, and
"typed knowledge graph with inference" is only cheap if it stays in a decidable
fragment — a constraint the document never commits to.

### F12 — Capability-effect typing is the best idea here and is still oversold. (tempers §9.2)

Static effect *inference* over Python — `getattr`, dynamic dispatch, decorators,
C extensions, `eval` — is undecidable in general, and this repo's own `codegraph`
already documents precision limits. So the checker validates **declarations**, and
nothing validates the declarations against reality. That makes it a **linting
convention with a type-system vocabulary**, not a proof. Calling it a "typing rule"
that lets CI "prove it for *all* compositions instead of testing a sample"
(§9.2) is simply false.

Worse, the case Olympus most cares about is **LLM-generated skills**, whose "code"
is a natural-language prompt whose effects are a *runtime property of model
behavior*. Those are not statically typeable even in principle. The proposal is
strongest exactly where the problem is easiest, and absent where it is hardest.

---

## 3. STRATEGIC failures and blind spots

### F13 — No organization, no budget, no staffing model.

The document proposes a research program spanning formal methods, RL/continual
learning, distributed systems, mechanism design, and knowledge representation —
four to six specialist teams — for what is, by all repository evidence, a
**solo-operator project**. §9.3's cost model quotes "eng-months" without ever
naming an engineer, a budget, a funding source, or a hiring plan. **A 10-year moat
program with no organization behind it is fiction**, and the cost model's
"verdict" column is therefore unfalsifiable.

### F14 — The single largest strategic risk is entirely absent: the model provider is the competitor.

Olympus is a governance layer over **someone else's models**, reached through
their APIs, subject to their pricing and terms. Anthropic, OpenAI, and Google are
actively shipping sandboxing, permissioning, audit logging, agent SDKs, and
managed agent runtimes. If a provider ships equivalent governance natively — which
is squarely on their roadmaps — **the entire moat evaporates**, and no amount of
BFT or effect typing compensates.

A strategy document about durable advantage that never analyzes
**provider-as-competitor** or **platform dependency risk** has omitted the
dominant term.

### F15 — Zero user, market, or demand evidence.

There is no user, workload, adoption number, revenue, or customer problem anywhere
in the document. Nobody asked for a polis. The document optimizes an abstract moat
against an unspecified competitor for an unnamed user. §10.1's "the correct
continuation is code, not V4" is self-serving humility that dodges the only
question that matters: **is any of this more valuable than making the existing
product reliable for the people using it today?**

### F16 — ~230 modules with one maintainer is a liability, not an asset.

The document treats "~40 half-built capabilities" as strategic inventory. In
maintenance terms it is **technical debt with a marketing story**. Integration cost
is superlinear in module count (pairwise interface interactions), and this
codebase already carries a documented Windows degradation, a signing-posture
weakness, a drifting eval baseline, and 300+ environment-gated skipped tests.
*"Olympus is roughly one integration pass from a moat"* is the most dangerous
sentence in the document.

### F17 — The governance-capture counter is incoherent with the amendment protocol.

§3.C-iii proposes *"a hard-to-change core + amendable periphery with a quorum
amendment protocol."* §8.3 requires the core be *"immutable to the self-improvement
process by construction."* **The boundary between core and periphery is where all
the difficulty lives, and it is never specified.** If the boundary is itself
amendable, capture is trivial (amend the boundary, then amend the core). If it is
not amendable, the policy is frozen permanently and cannot adapt to new threat
classes — which contradicts the entire "governance evolves" premise.

Two further gaps in the same section: there is **no threat model for the operator
as adversary** (the person who can rotate the signing seed), and **supply-chain
compromise of the signing root** is addressed only by a single unbudgeted mention
of threshold cryptography.

---

## 4. What survives, and what to do instead

Only after breaking everything. **Four items survive; the rest should be deleted
or demoted from the roadmap.**

**DELETE outright:**
- §3.C in its entirety — internal economy (F6), BFT consensus (F7), the polis.
  It solves no problem Olympus has and manufactures problems it does not have.
- §3.B-iii — the proof-assistant kernel (F5). Not affordable, not maintainable, no
  path for Python.
- §3.A's weight-training premise (F2, F3). Not buildable via API; not competitive
  at Olympus's data scale.
- The "one integration pass from a moat" framing (F1, F16).

**KEEP, with honest scope:**
1. **Capability-effect declarations as a CI lint** (from §9.2) — genuinely the
   highest-ROI item. Ship it as *declared* effects checked against the existing
   capability manifest, with an explicit statement that it is a convention, not a
   proof, and that LLM-generated skill effects remain dynamic-only. Cheap,
   compounding, honest.
2. **TLA+ on exactly one protocol** (the approval spine) as a **bounded, falsifiable
   experiment** — timeboxed, with a pre-committed decision rule: if it finds no bug
   a review would have caught, stop; do not proceed to a verification program.
3. **Durable execution** (§3.E minus the false counterfactual claim, F9) — real,
   ordinary, valuable systems engineering. Crash-safety and resumable runs help
   users today.
4. **Statistical audit sampling** (§F-i) — bounded-cost oversight with a real
   guarantee, buildable now on the existing signed ledger.

**REFRAME the learning story honestly.** What Olympus *can* do via API, at its
actual data scale: optimize **context, retrieval, exemplar selection, and
routing** — and train **small auxiliary models** (routers/classifiers) where
10³–10⁴ labeled samples genuinely suffice. `bandit_routing` and `learned_routing`
already do the honest version. That is a real, defensible improvement loop. It is
not "the model is no longer a fixed dependency," and the document should stop
claiming it is.

**REFRAME the moat.** The defensible position is not "the next AI operating
system." It is narrower and more credible: **governance-and-measurement discipline
as the product** — verifiable audit trails, capability confinement, and measured
regression gates, layered over frontier APIs, sold into deployments where
compliance and auditability are the binding constraint. That moat is real, is
already substantially built, and — critically — is *complementary* to the
providers rather than in a losing race against them (F14).

**Fix the methodology.** The document's failure was procedural before it was
technical: it inventoried architecture by reading names and first lines. Any
future revision must read the module and its tests before citing it, and must
explicitly flag when a proposal **reverses** a documented decision rather than
extending it. Applied honestly, that single rule would have prevented F1, F2, F6,
and F7 — i.e. most of this review.
