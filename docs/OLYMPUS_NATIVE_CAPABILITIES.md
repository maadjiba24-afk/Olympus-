# Capabilities Beyond Kronos

**Nine capabilities, each reported against the same eight facts — and none of
them production-eligible.**

- **Last updated:** 2026-07-28
- **Modules:** `native/{capability,timeframes,crossasset,microstructure,events,portfolio_context,specialists,scenarios,explain}.py`
- **Tests:** `tests/test_trading_native_capabilities.py` (108),
  `tests/test_trading_native_robustness.py` (70)
- **Register:** `native.capability.native_capabilities()` — this document is
  prose over that object, and `CapabilityRegistry.table()` prints the summary
- **Companions:** `docs/OLYMPUS_NATIVE_MODEL_ARCHITECTURE.md` (the model),
  `docs/OLYMPUS_MARKET_STATE_SCHEMA.md` (the inputs),
  `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` (the ledger)

> **A capability is not complete merely because an interface exists.** Every
> capability below has an implementation that runs and an adversarial test
> suite that exercises its failure modes. None has been measured on real data,
> because none can be: no market-data provider is reachable from this
> environment. That is two of the seven facts, and it is why the right-hand
> column of the table says `NO` nine times.

---

## 0. The register

```
capability                        impl          data        tests       hist      real    paper   prod
------------------------------------------------------------------------------------------------------
multi_timeframe            implemented  not_ingested  adversarial  synthetic   blocked     none     NO
cross_asset                implemented   unreachable  adversarial  synthetic   blocked     none     NO
order_book_liquidity       implemented  not_ingested  adversarial  synthetic   blocked     none     NO
event_awareness            implemented  not_ingested  adversarial  synthetic   blocked     none     NO
portfolio_aware            implemented      internal  adversarial  synthetic      none     none     NO
regime_specialists         implemented     derivable  adversarial  synthetic   blocked     none     NO
scenario_generation        implemented     derivable  adversarial  synthetic   blocked     none     NO
explainability             implemented     derivable  adversarial  synthetic      none     none     NO
robustness                 implemented     derivable  adversarial  synthetic   blocked     none     NO

production eligible: NONE
research usable:     all nine
```

### The eligibility rule

`CapabilityStatus.production_eligible` is a **computed property**. There is no
constructor argument that sets it, and `test_production_eligible_is_computed_
and_cannot_be_supplied` constructs the attempt and asserts the `TypeError`. The
rule is published as data in `capability.ELIGIBILITY_RULE` so a reader can check
it without reading a conditional:

| Fact | Required for production | Required for research |
|---|---|---|
| implementation | `IMPLEMENTED` | `IMPLEMENTED` |
| unit tests | `ADVERSARIAL` | `ADVERSARIAL` |
| historical evaluation | `HISTORICAL` or `LIVE` | — |
| real-data evaluation | `HISTORICAL` or `LIVE` | — |
| paper-trading evaluation | `LIVE` | — |
| limitations | at least one stated | at least one stated |

Two of those deserve a word.

**Paper trading is not optional.** A capability that is implemented,
adversarially tested and measured over a historical corpus is still ineligible.
The failure modes that only appear against a live venue — partial fills, a
quote that moves between the decision and the order, a feed that stops mid-bar
— are exactly the ones a historical replay cannot show.

**A capability must state a limitation.** "None known" is a statement and
somebody has to type it. `CapabilityStatus.__post_init__` refuses an empty
`limitations` tuple outright.

### Enabling one

```python
registry = native_capabilities()
registry.enable("order_book_liquidity", mode="production")
# CapabilityRefused: this capability's evidence does not support enabling it
#   missing=['historical evaluation is synthetic',
#            'real-data evaluation is blocked',
#            'paper-trading evaluation is none']

registry.enable("order_book_liquidity", mode="research")   # fine
```

There is a `force=True`, and it demands a written reason before it will do
anything — an override nobody can find is an override that will be forgotten.
Forced enables are recorded in `registry.overrides` and appear in
`registry.report()`.

**Nothing is enabled by default.** `test_a_capability_whose_dependency_is_absent
_is_not_silently_enabled` walks the whole register and asserts
`is_enabled(name) is False` for every entry.

---

## 1. Multi-timeframe intelligence

`native/timeframes.py`

Eight declared scales — tick, 1m, 5m, 15m, 1h, 4h, 1d, 1w — with every bar
carrying a state: `CLOSED`, `PARTIAL`, `DELAYED` or `ABSENT`.

The leak this exists to prevent is specific. A four-hour bar two hours into its
interval **has** a `close` attribute, and reading it is a two-hour look-ahead
that no shape check catches. So `TimeframeObservation.last_close` is gated on
the *state*, not on the bar's existence, and `assert_no_partial_leak`
re-derives every state from the bars and the clock so it still works when the
gating is the buggy part.

Three further decisions:

- **A missing feed becomes `ABSENT`, not a shorter view.** A consumer iterating
  the view sees the same keys every time; a dropped feed changes a state rather
  than a shape.
- **A non-closed base bar is refused outright.** There is nothing to forecast
  from.
- **Features are `None` per scale, never carried forward.** A daily trend that
  silently fell back to the hourly scale would report a daily trend that was
  never computed.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** Ladder, states, alignment, features, independent leak check. |
| Dataset availability | **Not ingested.** One timeframe (1h) of synthetic bars exists. |
| Unit tests | **Adversarial.** All four states; a doctored observation; a partial base; a delayed daily scale. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **Blocked** — B1. |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** Only one timeframe of synthetic data exists, so the
ladder has been exercised on constructed series and never on a real multi-scale
feed. Tick is declared and permanently unavailable — it is a stream, not a bar.
And **no model consumes more than the base scale yet**: the ladder produces
features and the multi-task model does not read them.

---

## 2. Cross-asset intelligence

`native/crossasset.py`, `native/dataset.py`

A declared relationship graph over seven edge kinds — sector peer, index
constituent, spot/derivative, related crypto, rates/equity, commodity producer,
currency/macro — with every edge carrying `since` and `until`.

The intervals are the whole point. An index constituent added in March is not a
constituent in February, and a graph without intervals lets February's features
use March's membership. That is the same survivorship error `dataset.Universe`
prevents one level up, and here it is prevented the same way: `edges_at(when)`
reads the graph as of the instant.

Three enforcements in `build_cross_asset_context`, all tested:

1. the edge set is read as of the instant, so a relationship declared later
   cannot inform an earlier forecast;
2. the pairing is causal, delegated to `dataset.align_cross_asset` rather than
   reimplemented;
3. bars after `as_of` are dropped from the target *and* every reference before
   anything is computed — a caller holding a full series is the normal case,
   and filtering is this function's job rather than its precondition.

`assert_no_future_reference` re-reads the raw series independently and is the
check that still works when the filtering is the bug.

**Relationships are declared, not estimated.** An estimated edge uses the
future to find itself. The cost is that the capability is only as good as its
reference data, which is stated as a limitation rather than papered over.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** Graph, intervals, causal features, staleness, independent check. |
| Dataset availability | **Unreachable.** No reference-data source. |
| Unit tests | **Adversarial.** Late edges; closed edges; future bars; missing references; self-relations. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **Blocked** — B1. |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** Every graph tested has been constructed by hand.
Relationships are declared rather than estimated. There is one instrument's
worth of reachable bars and it is synthetic — so a *cross*-asset capability has
never had two real assets to cross.

---

## 3. Order-book and liquidity intelligence

`native/microstructure.py`

Book snapshots, spread, depth with its measurement band, book and trade
imbalance, aggressor flow, fill probability, slippage, square-root market
impact, liquidity deterioration — and `assess_tradability`, which is the gate.

> **A forecast must not automatically become tradable merely because the
> expected return is positive.**

Six conditions, all of which must hold:

| Condition | What it stops |
|---|---|
| `expected_return_nonzero` | trading on no signal |
| `edge_clears_costs` | trading at 1.0× break-even, which is not a reason to trade |
| `fill_probable` | counting an order that would never fill |
| `size_fits_book` | an order larger than a quarter of visible depth |
| `impact_smaller_than_edge` | paying more to enter than the move is worth |
| `book_not_deteriorating` | trading into a widening or thinning book |

A positive expected return is the first and weakest of the six. It is what
makes the question worth asking, not what answers it.

**Unknown depth is treated as not fitting.** An unknown book is not an infinite
one, and defaulting the other way is how a size that could never have filled
gets marked tradable. When depth is unknown, `impact_bps` returns a half-spread
*floor* and says so in `assumptions`, rather than assuming a depth that would
make the impact look small — the estimate stops varying with size, which is
exactly why it is reported as a lower bound.

**Deterioration is relative.** A 40bp spread is normal for one instrument and a
crisis for another; an absolute threshold would be wrong for every instrument
except the one it was set on. So the comparison is against the book's own
trailing median, and a book with no history is *unjudged* rather than fine.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** All of the above, plus `Estimate`, which pairs every number with whether anything was fitted to produce it. |
| Dataset availability | **Not ingested.** |
| Unit tests | **Adversarial.** Crossed books; depth without a band; unknown depth; oversized orders; unfillable limits; widening and thinning books. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **Blocked** — B1. |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** **Nothing here is calibrated.** Fill probability,
slippage and impact all run on declared defaults and report
`calibrated=False`; the numbers have the right shape and the wrong scale. The
impact coefficient *could* be fitted from `outcomes.py`'s recorded fills — that
is the one calibration this system could perform today, and it has not been
done. `TradabilityAssessment.uncalibrated_inputs` names which numbers in any
given assessment are guesses.

---

## 4. Event-aware intelligence

`native/events.py`

Seven event kinds, split into those whose timing is genuinely known ahead
(economic announcements, earnings, central-bank decisions, scheduled market
events) and those that arrive when they arrive (exchange incidents, regulatory
announcements, corporate disclosures). An unscheduled event is invisible before
its `known_at`: a model conditioning on "an exchange incident in two bars"
would be conditioning on the future.

> **External text remains untrusted. Events may affect features and forecasts
> but may never modify: risk limits, broker credentials, permissions, live-mode
> status, safety controls, deployment gates.**

Enforced three ways, in decreasing order of how much they rely on pattern
matching:

1. **No event text becomes a model input at all.** `EventContext` exposes four
   numbers — bars until the next event, whether it is declared high-importance,
   a recent count, and an exchange-incident flag. There is no content field and
   no numeric surprise field, because a surprise without vintages is a
   look-ahead of days. (`schema.event_surprise` is marked `UNREACHABLE` for
   that reason.)
2. **`assert_event_boundary` refuses source that names a forbidden control.**
   Blunt on purpose: a check that tried to distinguish "mentions" from
   "modifies" would be a check with a judgement call in it. `BOUNDARY_EXEMPT`
   has exactly one entry — `events.py`, which declares `FORBIDDEN_TARGETS` and
   therefore must contain every name in it — and `boundary_exemption_is_needed`
   fails if that exemption outlives its reason.
3. **Text is sanitised at construction** by
   `sentiment.sanitise_external_text_report`, and only the sanitised form is
   stored. There is no field holding the original, because a field holding the
   original is a field something will eventually read; a digest ties the record
   to its source without retaining it.

The third is the weakest, and the test suite says so. Sanitisation is a
denylist: it neutralises what it recognises.
`test_text_the_sanitiser_misses_still_cannot_reach_anything` feeds it a payload
it does **not** flag, and asserts that the payload still reaches nothing —
because of (1), not because of (3). A suite that only ever fed the sanitiser
strings it catches would report a defence stronger than the one that exists.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** Calendar, knowability, timing features, boundary check, sanitisation. |
| Dataset availability | **Not ingested.** |
| Unit tests | **Adversarial.** Four injection payloads; one undetected payload; unscheduled events before `known_at`; the boundary against every forbidden target. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **Blocked** — B1. |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** No economic or corporate calendar is reachable. Content
is deliberately not modelled. The text boundary's third layer is a denylist.

---

## 5. Portfolio-aware forecasting

`native/portfolio_context.py`

> **The forecasting model must not directly modify the portfolio or place
> orders.**

Structural, not aspirational, three ways:

1. **This module takes a snapshot, not a manager.** `PortfolioView.from_manager`
   copies values out and does not retain the manager, so there is no code path
   from a forecaster to `apply_fill`. Asserted by
   `test_the_view_copies_out_and_retains_no_manager`, which checks no attribute
   on the frozen record holds one.
2. **Every type here is frozen.** A context object cannot be mutated into a
   position change even by a caller who wanted to.
3. **`assert_read_only` refuses source that names a mutating method**, over the
   same names `kernel.FORBIDDEN_KERNEL_CALLS` protects.
   `test_no_forecasting_module_calls_a_portfolio_mutator` runs it over every
   file in `native/`. `READ_ONLY_EXEMPT` has one entry — this module, which
   declares `MUTATING_METHODS` — and `read_only_exemption_is_needed` fails if it
   outlives its reason.

`PortfolioSignal` is evidence and nothing else: exposure, concentration,
correlated exposure, hedges, drawdown contribution, liquidity requirement, and
a list of cautions written for a reader. **No method on it produces a size.**
Sizing stays in `risk.py`, which is in the safety kernel and is not reachable
from here.

An unestimated correlation returns `None`, never zero — an unestimated
correlation is not an estimate of independence — and `portfolio_signal` emits
a caution saying the diversification effect is *unknown rather than zero*.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** |
| Dataset availability | **Internal.** The one capability whose inputs Olympus actually owns. |
| Unit tests | **Adversarial.** Read-only enforcement over every native module; derived notional and P&L; missing correlations; zero equity. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **None.** |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** Correlations are supplied, not estimated — a portfolio
view is a snapshot and correlation needs a return history. Marginal risk is the
cheap version: the sign of the change in gross exposure, not a covariance-aware
contribution. And the positions are real while the prices marking them come
from synthetic bars, so every exposure figure is real arithmetic over unreal
prices.

---

## 6. Regime-specialist architecture

`native/specialists.py`

Eight specialists — trend, mean reversion, high volatility, low volatility,
illiquid, event risk, crash, recovery — plus a generalist that is registered
automatically if the caller does not register it, because a router with no
fallback is a router that must always pick a specialist.

> **Routing decisions must be recorded and evaluated. A general model should
> remain available as a fallback.**

The router falls back for **four distinct reasons**, each recorded verbatim on
the decision:

1. no specialist is registered for the kinds that scored;
2. a specialist is registered but not fitted, or fitted on too few windows —
   a specialist with a hundred parameters and nine training windows memorised
   nine windows;
3. no specialist outscored the generalist;
4. the top two specialists are within the margin — the router is guessing, and
   the generalist is the honest answer.

Scoring is **rule-based rather than learned**, and that is a considered trade. A
learned router would need its own training data and would make "why was this
window routed here" unanswerable. The cost is that the rules encode somebody's
judgement about what a regime looks like, and that judgement has not been
validated against outcomes — `RoutingLog.evaluate` is what would do it.

`RoutingLog` carries two things worth more than the individual decisions:

- **`degenerate`** — true when one destination takes ≥95% of at least ten
  decisions. A router that always picks the same destination is not routing,
  and the decisions look perfectly reasonable one at a time.
- **`evaluate(errors)`** compares each destination against the **whole set's**
  error, not against the generalist's. The windows the generalist saw are the
  ones no specialist wanted, and comparing a specialist's easy windows with the
  generalist's hard ones flatters the specialist automatically.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** Router, four fallbacks, log, evaluation, degeneracy check. |
| Dataset availability | **Derivable.** |
| Unit tests | **Adversarial.** Each fallback reason individually; degeneracy; evaluation arity; unregistered kinds. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **Blocked** — B1. |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** **No specialist has been trained.** The router, the
fallback logic and the record are complete; the specialists themselves are
registered slots. The routing rules are unvalidated. And the regime classifier
needs a long warm-up, so at the lookbacks used here it reports `UNKNOWN` and the
generalist answers everything — which is the correct behaviour and also means
the specialisation has never been exercised on data.

---

## 7. Causal scenario generation

`native/scenarios.py`

Six scenarios per forecast — bullish, bearish, high volatility, liquidity
stress, regime transition, event shock — with probabilities that sum to exactly
one. A set that does not sum to one has an unstated outcome in it, and
`ScenarioSet` refuses to be constructed.

> **Scenarios must be probabilistic forecasts, not statements of certainty.**

The split between the two kinds of probability is the design:

- **Unconditional (bullish, bearish)** take their probabilities from where the
  model put its own mass — the share of its predicted band that sits above the
  median. A confident model produces a lopsided set and a hedged one produces a
  balanced set *automatically*, which makes the split a property of the forecast
  rather than of this function.
- **Conditional (high volatility, liquidity stress, event shock)** take declared
  probabilities, because a return distribution contains no information about
  whether the exchange will halt. Each says so in its `probability_basis`.

The unconditional mass is whatever the conditional probabilities leave, so the
set always sums to one without any scenario being invented to close the gap. A
zero-probability conditional is dropped rather than carried: a scenario with
probability zero is not a scenario, and keeping it invites reading the list
length as a count of possibilities.

**Every scenario must state what would falsify it.** One that cannot be
falsified is a story, not a forecast, and `Scenario.__post_init__` refuses an
empty `invalidated_by`.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** |
| Dataset availability | **Derivable.** |
| Unit tests | **Adversarial.** Non-unit sets; inverted bands; unfalsifiable scenarios; horizon disagreement; conditional crowding. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **Blocked** — B1. |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** The three conditional probabilities are declared.
Scenarios are bands, not simulated paths — producing paths would need a
generative model this system does not have. And **no scenario set has been
scored against what actually happened**, so the probabilities are untested.

---

## 8. Explainability

`native/explain.py`

Twenty-six reason codes in a **closed set**, each with a declared category
(driver, uncertainty, data limitation, risk factor) and a declared meaning. A
code missing from `CODE_META` cannot be emitted — `Reason` looks it up and
raises.

> **Do not use free-form explanation as proof that a prediction is correct.**

Two mechanisms:

- **`evidence_only` is what a machine consumes.** Codes, measurements, dominant
  timeframes, cross-asset references, regime, specialist, falsifiers. No prose.
- **`narrative()` is assembled from the codes.** It cannot contain a claim the
  codes do not carry, because it is built from them — and it ends with the
  sentence *"These are the conditions the forecast rests on. They are not
  evidence that it is correct."* It is deliberately dull: a narrative that read
  well would invite being quoted as the reason, and the reason is the code list.

Every reason carries `measured` and, where there is one, `threshold`. "Volatility
is elevated" is a sentence; "volatility ratio 3.2 against a threshold of 2.0" is
a claim someone can check and disagree with.

`dominant_uncertainty` returns `None` unless the emitter ranked its reasons.
"The main source of uncertainty" with nothing behind the ranking is exactly the
kind of unfounded claim this module exists to prevent.

`explain_forecast` guards every branch, because the point of an explanation is
to be produced *especially* when things are missing — a forecast made with three
of eight timeframes and no order book is exactly the one whose limitations a
reader needs. With no context at all it still produces an explanation, whose
single reason says that nothing about the forecast can be explained.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** |
| Dataset availability | **Derivable.** |
| Unit tests | **Adversarial.** Closed code set; empty explanations; missing falsifiers; unranked uncertainty; every wiring branch. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **None.** |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** Reason codes are emitted by **rules over the available
context, not by attribution over the model's own computation** — they say what
conditions held, not which input moved the output. No faithfulness check exists:
nothing verifies that a stated driver actually drove the number. Gradient- or
perturbation-based attribution is implementable and has not been built.

---

## 9. Adversarial and robustness testing

`tests/test_trading_native_robustness.py`, over
`native/{abstain,serve,timeframes,microstructure,events}.py`

Thirteen conditions, one section each. The standard throughout is that **the
system must degrade explicitly**: producing a number is not passing. A test
passes when the degradation is visible in the record — an abstention with a
reason, a mask with a `False` in it, a state that is not `CLOSED`, a gap in a
report, a refusal at a constructor.

| # | Condition | How it is caught | Where |
|---|---|---|---|
| 1 | Missing channels | mask with `None` in the slot; `MISSING_CHANNELS` abstention | `schema`, `abstain` |
| 2 | Delayed data | `DELAYED` state, `last_close is None`; `STALE_INPUT` abstention | `timeframes`, `abstain` |
| 3 | Corrupt candles | refused at `Candle.__post_init__`; conflicting duplicates flagged separately from identical ones | `contracts`, `dataset` |
| 4 | Sudden volatility | `EXCESSIVE_DISPERSION` against an empirical, regime-scaled reference | `abstain`, `serve` |
| 5 | Exchange outages | `ABSENT` state; `EXCHANGE_INCIDENT` event; gap report; liquidity deterioration | `timeframes`, `events`, `dataset` |
| 6 | Regime changes | `UNSUPPORTED_REGIME` abstention; route follows the regime; transition is a scenario, not a switch | `abstain`, `specialists` |
| 7 | New instruments | `INSTRUMENT_OUT_OF_DISTRIBUTION`, but only when the model carries an instrument-specific component | `abstain`, `crossasset` |
| 8 | Abnormal spreads | tradability blocked on two conditions at once; judged against the instrument's own history | `microstructure` |
| 9 | Flash crashes | **accepted as real data**, routed to the crash specialist, outside the trained volatility range | `contracts`, `specialists`, `serve` |
| 10 | Extreme gaps | counted and published in the quality report | `dataset` |
| 11 | Manipulated external text | sanitised and flagged; undetected payloads still reach nothing | `events`, `sentiment` |
| 12 | Dependency failures | `DependencyMissing`, not `ImportError`; capabilities detected, never assumed | `torchutil`, `pipeline` |
| 13 | Model-serving restarts | identical forecasts after reload; tampered checkpoints refuse to construct | `serve`, `checkpoint` |

Two of these are worth pulling out because they are the opposite of the others.

**A flash crash must not be rejected as corrupt.** A genuine 30% move has to
survive validation, or the system blinds itself exactly when it matters. So
condition 9 asserts the crash *passes* every check that condition 3 asserts a
corrupt candle *fails*.

**A relative check must not become an absolute one.** Condition 8 asserts that a
40bp spread is a crisis against a 2bp history and *not* a crisis against a 40bp
history. A check that fired on both would look robust and be useless.

| Fact | Status |
|---|---|
| Implementation | **Implemented.** |
| Dataset availability | **Derivable.** |
| Unit tests | **Adversarial** — this is the suite. |
| Historical evaluation | **Synthetic.** |
| Real-data evaluation | **Blocked** — B1. |
| Paper trading | **None.** |
| Production eligibility | **No.** |

**Known limitations.** Every adversarial condition is *constructed*: a flash
crash here is a synthetic series with a step in it, not one that happened. The
tests establish that the system degrades **safely**, not that it degrades
**well** — safe degradation is refusing to answer, and a system that refused
everything would also pass this suite. That is why the register also carries the
measured abstention rates from Phase 2 rather than treating "it declined" as a
success on its own. No chaos testing against a live venue has been done.

---

## 10. What would change these verdicts

Every `NO` in the table is missing the same two or three facts, and they have
the same root:

| Blocker | Unblocks |
|---|---|
| **B1** — no market-data provider reachable | historical and real-data evaluation for all nine |
| **no paper broker fed by real quotes** | paper-trading evaluation for all nine |
| **no fill history used** | the one calibration reachable today: `ImpactModel.calibrate_from_fills` |
| **no specialist trained** | regime specialisation becoming a capability rather than a router |

The order that matters: B1 first, because seven of the nine capabilities cannot
produce a number worth evaluating without it; then the impact calibration,
because it is the only one that needs nothing external; then specialist
training, which needs enough data *per regime* and therefore needs B1 twice
over.

Until then, the honest statement is the one the register prints:
**production eligible: NONE; research usable: all nine.**
