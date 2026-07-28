# Controlled Self-Evolution

How Olympus learns, and what it is structurally prevented from doing with what
it learns.

- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Scope:** `olympus/trading/{knowledge,outcomes,drift,hypotheses,lab,proposals,capabilities,champion,rollback,evolution,governance,kernel,storekeys}.py`
- **Tests:** 409 across 13 files; 8,466 lines of new module code; the thirteen completion gates are
  demonstrated end to end in `tests/test_trading_self_evolution.py`

> **What this is not.** Nothing here has been exercised against real market
> data, a real broker, or the genuine Kronos checkpoint — those remain blocked
> by the environment's egress policy (`docs/TRADING_EXTERNAL_VALIDATION.md`
> §1). The learning loop is complete and tested as machinery. It has not yet
> learned anything about a real market, and this document does not claim it has.

---

## 1. The shape of it

```
      ┌──────────────── autonomous ────────────────┐   ┌── human only ──┐

 outcomes ──► drift ──► hypotheses ──► lab ──► proposals ──► capabilities
 (score)     (detect)   (ask)        (test)   (write up)    (promote)
     │           │                                              │
     └───────────┴──► knowledge (what was learned) ──────────────┘
                              │
                    evolution (the record) ──► rollback (undo)

                    governance  ·  kernel        ← the boundary
```

Every arrow left of the divide is something Olympus does by itself. Every arrow
crossing it requires a named operator carrying a token. `governance.py` is the
function that answers which is which; `kernel.py` is the proof that the
machinery on the left has no route to the right.

---

## 2. Modules

| Module | Lines | What it does |
|---|---|---|
| `governance.py` | 287 | The autonomous / human-only / prohibited action split, as a call that raises |
| `kernel.py` | 584 | The eleven protected components, content sealing, and the AST audit behind gate 13 |
| `knowledge.py` | 973 | Versioned beliefs with provenance, confidence, contradiction tracking, correction, expiry |
| `outcomes.py` | 849 | Decision records, outcomes, thirteen-axis scoring, systematic weakness detection |
| `drift.py` | 918 | PSI/KS drift detection and the five-rung deterioration ladder |
| `hypotheses.py` | 703 | Structured research proposals that cannot exist without a failure condition |
| `lab.py` | 769 | The research sandbox and seven experiment kinds |
| `proposals.py` | 568 | Feature and code-change proposals; no method applies one |
| `capabilities.py` | 654 | Ten-state capability lifecycle with evidence-gated, one-rung promotion |
| `champion.py` | 616 | Matched champion/challenger comparison with a parsimony tie-break |
| `rollback.py` | 622 | Signed deployments, nine rollback triggers, post-rollback reconciliation |
| `evolution.py` | 776 | The cycle, the append-only evolution ledger, the fifteen improvement metrics |
| `storekeys.py` | 147 | Composite keys over `olympus.store`, which only accepts simple names |

---

## 3. The design decisions worth defending

### Stopping is autonomous; starting is not

This asymmetry appears in four places and is the same argument each time.
Olympus may engage a kill switch, demote a deteriorating model, suspend a
capability and roll back a deployment — all without asking. It may not clear a
switch, reinstate a model, promote a capability or deploy a version.

The reason is that the failure modes are not symmetric. An unnecessary
demotion costs some missed trades. A degrading model left running because
nobody was awake costs real money, and requiring human approval to stop would
guarantee exactly that. Rollback is autonomous for a narrower reason: its
destination is always a version a human already deployed, so it can only ever
retreat to previously approved state.

### Repetition is not evidence

`knowledge.corroborate()` raises the confidence of an unvalidated external
claim only up to `MAX_UNVALIDATED_CONFIDENCE` (0.5), however many sources agree,
and sources sharing an `origin` are counted once. Ten articles quoting one press
release are one press release.

A knowledge base that let frequency stand in for truth would be a machine for
laundering consensus into fact — and in a trading system, market consensus is
often the thing worth betting against.

### Knowledge is never authority

`assert_safe_for()` refuses to let any record reach risk configuration,
credentials, permissions or execution settings unless it is an
operator-authored rule. This holds for a *validated*, maximally-confident,
thoroughly corroborated record. The strongest available evidence for widening a
limit is still not permission to widen it.

`MODEL_OUTPUT` is classified external. A language model summarising a document
is not a witness to it, and treating its output as internal because it ran on
our hardware is precisely the mistake the classification prevents.

### An unmeasured axis is not a passing one

`outcomes.Grade.UNKNOWN` exists so that a decision whose stop placement cannot
be judged reports "unknown" rather than "fine". `OutcomeEvaluation.unknown_axes`
is surfaced everywhere the evaluation is rendered, because a report showing six
good axes out of six — two of which were unmeasurable — is a misleading report.

The same principle runs through `drift.assess` (an unmeasured hard threshold
flags rather than passes), `rollback.unmeasured_triggers` (a rollback decision
on partial data knows it was partial), and `evolution.Explanation`
(`improved_performance` is `None` until there is post-change measurement).

### It performed well historically is not a licence

Every component's evaluation expires. `DeteriorationThresholds.evaluation_ttl`
defaults to 14 days, and a component past it is flagged **even if every metric
looks excellent**, because a metric computed six weeks ago is a claim about six
weeks ago. A component that was never evaluated is flagged too.

### A hypothesis with no failure condition is a preference

`ResearchProposal` refuses to construct without non-empty `failure_criteria`,
and refuses when success and failure criteria overlap after normalisation — if
no result could distinguish them, the proposal is unfalsifiable. The experiment
design is pre-registered on the same object, because specifying the design after
seeing the data is how a research process talks itself into what it already
believed.

`ProposalStatus.REFUTED` is terminal under the same id, and there is no path
that deletes it. A system that could tidy away its failed hypotheses could
present a research record that never fails.

### Isolation by absence, not by inspection

`ResearchSandbox` does not check what an experiment does and decide whether to
permit it. It holds no reference to live credentials, order submission,
production risk configuration, secrets, live-mode activation or operator
tokens — `request("live_broker_credentials")` raises because there is nothing to
return. Enforcement by absence survives an experiment that is trying to get
around it; enforcement by inspection does not.

The sandbox also fails closed on unlisted resources, so granting a new
capability is a deliberate edit to `ALLOWED_RESOURCES` rather than an accident
elsewhere. Every request is recorded, granted or not: an attempt to reach
production is itself a finding, and `lab.ResearchLab.run` writes the ERRORED
result *before* re-raising so the attempt survives whether or not the caller
handles the exception.

### The simpler model wins a tie

`champion.compare()` breaks a statistical dead heat in favour of the *simpler*
contender — including when the simpler one is the challenger, which is the only
branch in the system that lets Olympus remove complexity. A framework that
treated "indistinguishable" as "keep the incumbent" would ratchet complexity
upward forever.

It also refuses a challenger that wins on average but is materially worse in any
regime with a usable sample. An average that hides the regime where the new
model loses money is not an improvement.

And it **raises** rather than reporting when the two arms were not measured
under the same dataset, cost model and risk limits. Comparing a challenger
measured on cheap costs against a champion measured on realistic ones is the
most common way research produces an improvement that does not exist.

### Learning more is not improving

`evolution.measure_improvement()` scores fifteen metrics and starts at
`UNPROVEN`. Three of the fifteen — capabilities added, unsafe proposals
rejected, deteriorating models restricted — are **excluded from the verdict**.
They measure the guard rails working, not the trading getting better, and
including them would let Olympus look like it was improving by generating more
bad ideas to reject.

---

## 4. The safety kernel

Eleven components, fourteen modules, three independent mechanisms.

| Component | Modules |
|---|---|
| risk_policy_enforcement | `risk` |
| kill_switches | `killswitch` |
| credential_vault | `olympus.vault` |
| permission_system | `kernel`, `capabilities` |
| capability_boundaries | `lab` |
| order_authorisation | `execution`, `oms`, `brokers` |
| live_mode_gates | `modes` |
| audit_ledger | `audit`, `olympus.ledger` |
| model_isolation | `registry` |
| human_approval | `governance` |
| deployment_signing | `rollback` |

**Declaration** — `SAFETY_KERNEL` names each component, its modules, and why it
is protected. The rationale field is required so a proposed exemption can be
reviewed on its merits.

**Sealing** — `seal()` hashes the source of all fourteen modules; `verify_seal()`
names which one changed. Comment changes break the seal deliberately: a comment
explaining why a limit exists is part of the limit.

**Structural denial** — `audit_evolution_modules()` parses every self-evolution
module and fails if any imports `execution`, `brokers`, `olympus.vault` or
`modes`, or calls any of ~20 kernel mutation entry points. This runs in CI. It
is why gate 13 is a fact about the codebase rather than a claim about behaviour.

Olympus may **recommend** a kernel change: `propose_kernel_change()` returns a
document. There is no `apply_kernel_change()`, and
`test_no_module_can_apply_a_kernel_change` parses the whole package to keep it
that way.

**What is not claimed:** this defends against the system's own machinery, not
against a human with a shell. Anyone who can edit the source can edit
`kernel.py`. The guarantee is that no code path Olympus can reach autonomously
leads to a kernel change, and that any out-of-band change is detected by the
seal.

---

## 5. The thirteen completion gates

| # | Gate | Demonstrated by |
|---|---|---|
| 1 | Record forecasts, compare with outcomes | `test_gate_1_forecasts_are_compared_with_outcomes` |
| 2 | Detect deterioration | `test_gate_2_deterioration_is_detected_and_acted_on` |
| 3 | Generate structured hypotheses | `test_gate_3_hypotheses_are_structured_and_falsifiable` |
| 4 | Isolated experiments, no production access | `test_gate_4_experiments_cannot_reach_production` |
| 5 | Champion vs challenger | `test_gate_5_champion_and_challenger_are_compared_on_matched_terms` |
| 6 | Reject what does not beat baselines | `test_gate_6_a_result_that_does_not_beat_the_baseline_is_rejected` |
| 7 | Preserve provenance and confidence | `test_gate_7_provenance_and_confidence_are_preserved` |
| 8 | Deprecate outdated knowledge | `test_gate_8_outdated_knowledge_is_deprecated_without_being_erased` |
| 9 | Code proposals, never deployed | `test_gate_9_code_proposals_are_produced_and_never_deployed` |
| 10 | Promotion only through authorised gates | `test_gate_10_capabilities_are_promoted_only_through_authorised_gates` |
| 11 | Roll back safely | `test_gate_11_a_deployed_component_rolls_back_safely` |
| 12 | Complete audit trail | `test_gate_12_every_evolutionary_change_is_explainable` |
| 13 | Kernel unreachable from self-evolution | `test_gate_13_no_self_evolution_mechanism_can_alter_the_safety_kernel` |

`test_every_gate_has_a_demonstration` fails if a gate loses its test.

**All thirteen pass** — against internal components and simulated data. Gates
1, 2, 5, 6 and 12 will produce *different results* once real data and a real
checkpoint are available; what is demonstrated now is that the machinery
records, detects, compares, rejects and explains correctly, not that any
particular model is good.

---

## 6. The seven questions

`EvolutionLedger.explain(event_id)` answers each directly:

| Question | Field |
|---|---|
| Why did Olympus change? | `why` (from `observed` / `inferred` / `proposed`) |
| What evidence justified it? | `evidence` |
| Who authorised it? | `authorised_by` — empty for autonomous acts, by design |
| Which version was active? | `active_version` |
| Can the previous version be restored? | `restorable` + `rollback_target` |
| Did the change improve performance? | `improved_performance` — `None` until measured |
| Did the change create new risks? | `new_risks` — regressions **and** unmeasured dimensions |

Metric direction is read from `IMPROVEMENT_METRICS`, never assumed. A falling
drawdown is an improvement and a falling Sharpe is not; a comparison treating
every metric as higher-is-better would report the wrong sign for half of them.

---

## 7. Defects found while building this

| Defect | Where | Fix |
|---|---|---|
| `store._safe()` rewrites `\|` in keys, so every composite-key prefix scan silently matched nothing — five registries could write records they could never list | `knowledge`, `outcomes`, `champion`, `proposals`, `evolution` | New `storekeys.KeyedStore`: logical key stored inside the payload, physical name is a readable slug plus a digest |
| `validate()` set `epistemic=CONFIRMED_FACT`, which the constructor forbade for external sources — external claims could never be validated at all | `knowledge.py` | Constructor now forbids `CONFIRMED_FACT` for external content *only while unvalidated*; `OPERATOR_RULE` stays forbidden always |
| `Explanation` treated every metric as higher-is-better, so a drawdown falling from 0.4 to 0.1 read as a regression | `evolution.py` | Direction read from `IMPROVEMENT_METRICS`; metrics with no declared direction are reported as not judged |
| `latest_performance()` used `max()` on timestamps alone, returning the *earlier* of two entries recorded in the same clock tick — a stale metric reported as current | `capabilities.py` | Tie-break on insertion order |
| Broker adapters were not in the safety kernel, so a generated patch could have targeted the file holding credentials and the testnet host allow-list | `kernel.py` | `olympus.trading.brokers` added to `order_authorisation` |

The first is the one worth noting: it was invisible to every happy-path test —
writes succeeded, `get()` round-tripped through the same mangling — and only
surfaced because the knowledge tests asserted on *listing* rather than
retrieval.

---

## 8. Known limits

1. **No real evidence has been processed.** Every number in every test is
   synthetic. The loop is proven to record, detect and reject correctly; it has
   not been proven to detect anything true about a market.
2. **Nothing runs on a schedule.** `EvaluationSchedule` says what is due;
   `EvolutionCycle.run()` performs one turn. No process calls either. Auto-trips,
   drift jobs and evolution cycles all require a caller.
3. **Drift statistics are deliberately simple.** PSI and two-sample KS, in pure
   stdlib. More powerful tests exist; these were chosen because an operator can
   check the number by hand, which matters more here than statistical power.
4. **Complexity is author-declared.** `Contender.complexity` is an integer
   someone types. The parsimony tie-break is only as honest as that number, and
   nothing validates it.
5. **The sandbox is a capability boundary, not a process boundary.** An
   experiment runs in-process. It cannot obtain production resources through the
   sandbox, and `kernel.audit_evolution_modules()` proves the lab module has no
   import route to them — but arbitrary code executing in the same interpreter is
   not contained by either mechanism. Running untrusted experiment bodies would
   need OS-level isolation that is not built.
6. **`ImprovementReport` compares two periods handed to it.** It does not select
   the periods, and choosing favourable ones would produce a favourable verdict.
   Period selection is an operator decision and is not audited.

---

## 9. Verify

```bash
python -m pytest tests/test_trading_kernel.py -q          # gate 13, structural
python -m pytest tests/test_trading_self_evolution.py -q   # all thirteen gates
python -m pytest tests/test_trading_*.py -q                # 2427 passing
python -m pytest -q                                        # 7590 passed, 30 skipped
python -m olympus capabilities --check
```
