# Matched Olympus-versus-Kronos Evaluation

**Verdict: INSUFFICIENT EVIDENCE. Promotion decision: none possible.**
**And on the question this environment *can* answer, the Olympus-native model
loses to a gradient-boosted tree on every metric tested.**

- **Last updated:** 2026-07-28
- **Module:** `native/matched.py`
- **Runner:** `python scripts/matched_evaluation.py --bars 2000 --json report.json`
- **Tests:** `tests/test_trading_native_matched.py` (39)
- **Contract fingerprint:** `b987b1eea361d134ac3e4234c6f9657f`
- **Companions:** `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` (the ledger),
  `docs/KRONOS_TEARDOWN.md`, `docs/OLYMPUS_NATIVE_MODEL_ARCHITECTURE.md`

> **No number in this document compares Olympus with Kronos.** The Kronos arm
> did not run, for a reason established by measurement and recorded in §1. Five
> of the six required arms did run, and what they show is reported in full —
> including that the native model is the worst of them.

---

## 1. Why the Kronos arm did not run

Three things are needed to run Kronos with its genuine official checkpoint.
Each was attempted and each is reported separately, because they fail for
different reasons and a single "unavailable" would hide which one.

| Requirement | Result | Detail |
|---|---|---|
| Upstream source | **obtainable** | `github.com/shiyu-coder/Kronos` clones. MIT licensed. It contains **no weights**: `model/kronos.py` loads them through `huggingface_hub.PyTorchModelHubMixin`. Not vendored into Olympus, so `kronos_runtime._import_upstream_kronos()` raises `DependencyMissing` as designed. |
| Official weights | **unreachable** | `GET https://huggingface.co/NeoQuasar/Kronos-small/resolve/main/config.json` → `curl: (56) CONNECT tunnel failed, response 403`. The environment's egress gateway denies `huggingface.co` by policy; `$HTTPS_PROXY/__agentproxy/status` records the denial. Pinned revision `901c26c1332695a2a8f243eb2f37243a37bea320`. |
| Weight licence | **unverified** | The *source* is MIT — verified by reading the cloned `LICENSE`. The *weights* are distributed separately on the model hub under terms stated on a page this environment cannot reach. **Blocker B4 stands, and is now narrower than it was**: the code licence is known, the weight licence is not. |

**What was not done, and why.** The Kronos architecture could be instantiated
from the MIT source with random weights. That would produce a full set of
numbers and every one of them would be meaningless: a randomly initialised
transformer is not Kronos, and a comparison against one would be worse than no
comparison, because it would look like a comparison. The arm is therefore
carried as `UNAVAILABLE` with its blockers attached.

The ensemble arm did not run either, for the arithmetic reason that an ensemble
needs both members.

---

## 2. What ran

| # | Required system | Status | Arm |
|---|---|---|---|
| 1 | Persistence baseline | ran | `persistence` |
| 2 | Simple statistical baseline | ran | `autoregression` (order selected on validation) |
| 3 | Best simple ML baseline | ran | `gradient-boosted trees` (depth/estimators selected on validation) |
| 4 | Kronos, genuine official checkpoint | **did not run** | B2, B4 |
| 5 | Olympus-native model | ran | two input classes, shared weights |
| 6 | Olympus + Kronos ensemble | **did not run** | B2, B4 |

**Five of six.** `MatchedReport.missing_arms` carries the other two into every
rendering of the report.

---

## 3. The fairness contract

All twelve quantities Phase 5 requires to be identical are fingerprinted into
one record, and `compare_arms` **raises** rather than warns when two arms carry
different fingerprints.

| Field | Value |
|---|---|
| instruments | `SIM:TREND`, `SIM:CHOP` |
| window | 2027-02-02T09:00Z → 2027-03-25T13:00Z |
| prediction timestamps | 682, hashed — overlap is not enough, the instants must be the same set |
| horizon / lookback | 3 bars / 33 bars |
| input availability | only bars closing at or before the prediction instant; a non-final bar cannot enter a window |
| data quality | OHLC coherence enforced at construction; no gaps, duplicates or non-final bars |
| splits | embargoed three-way temporal split, 60/20/20, embargo 3 bars |
| costs | 4.0 bp round trip, charged on every non-zero position |
| risk limits | max position 1, leverage 1.0 |
| strategy | long when the predicted horizon return exceeds 1.5× the round-trip cost, short below the negative of that, flat otherwise; an abstention is flat; one unit; held for the horizon |
| execution | fill at the anchor close, no slippage model, no partial fills |

### Model selection, and the size of the search

Every arm with hyperparameters got the same courtesy: a **declared, small
search, scored on the validation split only**, with the number of
configurations recorded.

| Arm | Configurations tried | Selected on validation MAE |
|---|---|---|
| persistence | 1 | 0.007667 |
| autoregression | 3 | 0.006651 |
| gradient-boosted trees | 3 | 0.006441 |
| olympus-native | 3 | 0.021527 (`d_model=24`) |

This is not decoration. **The first version of this evaluation gave Olympus one
under-trained configuration** and measured a +1.4σ location bias that a larger
model does not have on other data. Under-training one arm is as unfair as
over-searching another, and it is much easier to do by accident. The search is
declared in `scripts/matched_evaluation.py` as `SEARCH_SPACE` and
`OLYMPUS_SEARCH`, and it is small on purpose.

---

## 4. Forecast metrics

682 prediction instants, two instruments.

| arm | n | answered | MAE | RMSE | dir acc | quantile loss | coverage | calib err |
|---|---|---|---|---|---|---|---|---|
| persistence | 682 | 682 | 0.008711 | 0.011857 | 0.4457 | 0.003215 | 0.8328 | +3.3 |
| autoregression | 682 | 682 | 0.007038 | 0.008892 | 0.6466 | 0.002470 | 0.7683 | +3.2 |
| **gradient-boosted trees** | 682 | 682 | **0.006589** | **0.008418** | **0.6613** | **0.002334** | 0.7727 | **+2.7** |
| olympus-native (candles only) | 682 | 679 | 0.025636 | 0.028413 | 0.4477 | 0.008821 | 0.4713 | +32.9 |
| olympus-native (derived) | 682 | 679 | 0.025636 | 0.028413 | 0.4477 | 0.008821 | 0.4713 | +32.9 |

| arm | Brier | vol err | regime acc | drawdown qual | abs prec | abs recall | p50 ms | mem kb | reliability |
|---|---|---|---|---|---|---|---|---|---|
| persistence | – | – | – | – | – | – | 0.01 | – | 1.000 |
| autoregression | – | – | – | – | – | – | 0.02 | – | 1.000 |
| gradient-boosted trees | – | – | – | – | – | – | 0.30 | – | 1.000 |
| olympus-native (candles only) | – | – | – | – | 0.667 | 0.009 | 35.98 | 765340 | 1.000 |
| olympus-native (derived) | 0.2290 | 0.00086 | 0.000 | – | 0.667 | 0.009 | 36.44 | 765340 | 1.000 |

**A dash is a metric the arm does not produce, not a zero.** Filling those in
would make a persistence rule look perfectly calibrated on a head it does not
have.

Three readings:

- **Olympus is roughly 4× worse than the worst baseline on MAE**, and its
  directional accuracy (0.448) is below a coin toss while the two fitted
  baselines are near 0.65.
- **Its coverage is 0.47 against a nominal 0.80** — 33 points off, an order of
  magnitude worse than any baseline's ~3.
- **Its regime accuracy is 0.000.** The regime head, on this data, never agrees
  with the label. That is not a near miss; it is a head producing something
  unrelated to what it is scored against.
- Latency: Olympus is ~120× slower than the trees and ~3600× slower than
  persistence. On four cores with no GPU, at 36 ms per forecast.

---

## 5. Trading metrics

Computed over **non-overlapping** periods: 228 per arm, with 454 overlapping
windows dropped.

> This is the correction that matters most in this section. Stride-1 windows
> share all but one input bar and their horizons cover the same future returns.
> The first version of this harness treated them as independent trading periods
> and reported a **Sharpe of +38** for the autoregression. Nothing raised, and
> the arms were still ranked correctly — which is exactly why the error is
> worth naming. `matched.non_overlapping` is the fix and
> `test_overlapping_windows_are_dropped_before_a_sharpe_is_computed` asserts
> the inflation it removes.

| arm | total | annualised | Sharpe | Sortino | max DD | Calmar | profit factor | turnover | exposure | trades | win % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| persistence | +0.00000 | 0.0000 | – | – | 0.00000 | – | – | 0.000 | 0.000 | 0 | – |
| autoregression | +0.90371 | +11.57 | +20.00 | +46.31 | 0.03668 | +315 | 2.940 | 1.127 | 0.934 | 213 | 0.620 |
| **gradient-boosted trees** | **+1.05192** | **+13.47** | **+23.46** | **+59.90** | **0.02840** | **+474** | **3.501** | 1.110 | 0.947 | 216 | **0.644** |
| olympus-native (candles only) | −0.36188 | −4.63 | −7.39 | −9.52 | 0.48840 | −9.49 | 0.685 | 0.022 | 0.991 | 226 | 0.438 |
| olympus-native (derived) | −0.36188 | −4.63 | −7.39 | −9.52 | 0.48840 | −9.49 | 0.685 | 0.022 | 0.991 | 226 | 0.438 |

| arm | avg win | avg loss | tail (worst 5%) | fees |
|---|---|---|---|---|
| persistence | – | – | – | 0.0000 |
| autoregression | +0.010376 | −0.005752 | −0.016515 | 0.0852 |
| gradient-boosted trees | +0.010594 | −0.005463 | −0.014723 | 0.0864 |
| olympus-native | +0.007963 | −0.009090 | −0.028476 | 0.0904 |

**These Sharpe ratios are still not credible as market numbers, and the reason
is the generator, not the harness.** `SIM:TREND` is built with an AR(1) step of
φ = 0.90, so the next three-bar return is strongly predictable from the last
one *by construction*. An autoregression should score extremely well on it,
and does. The trading table ranks the arms; it says nothing about markets.

Persistence never trades: its predicted return is always below the cost
threshold, so it takes no position and earns exactly zero. That is the correct
behaviour of the rule, not a bug, and it is why its Sharpe is `–` rather than 0.

---

## 6. Statistical analysis

**63 tests in the family, Holm-adjusted at α = 0.05.** `Comparison.significant`
reads the *adjusted* p-value and additionally requires the bootstrap interval to
exclude zero — a p-value near threshold with an interval straddling zero is
what a small sample manufactures.

Olympus against every baseline, all metrics (positive gain = Olympus better):

| vs | metric | n | gain | CI | p | p adj | significant |
|---|---|---|---|---|---|---|---|
| persistence | MAE | 679 | −0.016952 | [−0.017914, −0.015978] | 0.0000 | 0.0000 | **YES (loss)** |
| persistence | quantile loss | 679 | −0.005615 | [−0.005979, −0.005246] | 0.0000 | 0.0000 | **YES (loss)** |
| persistence | net return | 682 | −0.001689 | [−0.002537, −0.000776] | 0.0000 | 0.0000 | **YES (loss)** |
| autoregression | MAE | 679 | −0.018609 | [−0.019576, −0.017629] | 0.0000 | 0.0000 | **YES (loss)** |
| autoregression | quantile loss | 679 | −0.006354 | [−0.006745, −0.005967] | 0.0000 | 0.0000 | **YES (loss)** |
| autoregression | net return | 682 | −0.005991 | [−0.007261, −0.004741] | 0.0000 | 0.0000 | **YES (loss)** |
| gradient-boosted trees | MAE | 679 | −0.019061 | [−0.020018, −0.018096] | 0.0000 | 0.0000 | **YES (loss)** |
| gradient-boosted trees | quantile loss | 679 | −0.006492 | [−0.006885, −0.006113] | 0.0000 | 0.0000 | **YES (loss)** |
| gradient-boosted trees | net return | 682 | −0.006410 | [−0.007665, −0.005182] | 0.0000 | 0.0000 | **YES (loss)** |

**Nine usable comparisons, nine significant losses, zero wins.** The correction
changes nothing here: the effects are far outside the intervals either way.

Among the baselines themselves, all significant: gradient-boosted trees beat
autoregression (MAE gain 0.000450, p_adj 0.0000; net return 0.000419, p_adj
0.0120), and both beat persistence.

### Input advantage

| comparison | metric | gain | p_adj | significant |
|---|---|---|---|---|
| olympus (derived) vs olympus (candles only) | MAE | +0.000000 | 1.0000 | no |
| olympus (derived) vs olympus (candles only) | quantile loss | +0.000000 | 1.0000 | no |
| olympus (derived) vs olympus (candles only) | net return | +0.000000 | 1.0000 | no |

**The input-advantage axis measured exactly zero, and that is a finding.** The
two arms share weights and differ only in whether the derived outputs —
volatility, direction, regime — are permitted. Those outputs do not feed the
point forecast in the current architecture, so permitting them changes no
prediction and no trade. Whatever the multi-task heads are contributing, they
are not contributing to the number the strategy acts on.

---

## 7. Regime, period and instrument analysis

**36 regime-scoped comparisons, 28 significant after correction — every one of
them a loss for Olympus.** There is no regime in which it wins.

| arm | ranging (n=89) | trending down (n=371) | trending up (n=222) |
|---|---|---|---|
| persistence MAE | 0.008278 | 0.009244 | 0.007994 |
| autoregression MAE | 0.006906 | 0.007191 | 0.006836 |
| gradient-boosted trees MAE | 0.006434 | 0.006857 | 0.006204 |
| olympus MAE | 0.025320 | 0.027437 | 0.022776 |
| olympus net return | −0.010390 | −0.451759 | **+0.152834** |

Olympus is net positive in exactly one stratum — trending up — and that is the
one place a systematically positive bias would help. It is a symptom of the
bias in §8, not evidence of skill.

**By period** (contiguous thirds), Olympus is negative in all three
(−0.185, −0.171, −0.007) and the baselines positive in all three. No arm's
ranking depends on the period.

**By instrument**, one genuine stratum finding: **autoregression is net
negative on `SIM:CHOP`** (−0.042, Sharpe −2.66) while gradient-boosted trees
is positive (+0.060, Sharpe +3.84). An average over both instruments hides
that, which is why `champion.compare()` refuses a challenger that wins on
average and loses materially in a stratum with a usable sample.

---

## 8. Error analysis

| arm | mean err | p90 err | max err | **bias** | abstained | failed | fees |
|---|---|---|---|---|---|---|---|
| persistence | 0.008711 | 0.018285 | 0.040994 | +0.001881 | 0 | 0 | – |
| autoregression | 0.007038 | 0.014976 | 0.029002 | +0.000695 | 0 | 0 | 0.2556 |
| gradient-boosted trees | 0.006589 | 0.014320 | 0.027219 | +0.000582 | 0 | 0 | 0.2552 |
| olympus-native | 0.025636 | 0.041110 | 0.070572 | **+0.025038** | 3 | 0 | 0.2716 |

**This is the sharpest number in the document.** Olympus's mean absolute error
is 0.025636 and its bias — mean(predicted − realised) — is **+0.025038**.
Essentially the entire error is a **location** error: the model's predicted
distribution is displaced upward by about +0.025 in log-return space, roughly
2.7× the realised standard deviation of the target.

Its prediction *spread* is approximately right. In a separate measurement its
predictions had a standard deviation of 0.0096 against a realised 0.0094 — the
model has learned the scale of the process and put the whole distribution in the
wrong place.

Three consequences follow directly, and they explain the rest of this report:

1. **Coverage 0.47 against nominal 0.80.** The intervals are wide, but they are
   centred +2.7σ away, so the realised values sit near their lower edge.
2. **Directional accuracy 0.448.** A distribution displaced upward predicts "up"
   too often.
3. **Net positive only in the trending-up stratum.** An upward bias pays there
   and costs everywhere else.

This is the same defect Phase 2 localised and named — "too wide *and*
mis-centred, and a scale correction cannot fix a location error" — reproduced
here on different data, with a larger magnitude, after a validation-only
configuration search. `fit_calibration` produces a scale correction and
`serve.py` deliberately applies it about the median rather than shifting it,
which is correct and does not help.

**Abstention is not rescuing it.** Three declines in 682 windows: precision
0.667, recall 0.009. The policy is essentially inactive, so the model answers
almost every window it gets wrong.

---

## 9. Cost analysis

Fees are identical by construction (4.0 bp round trip on every non-zero
position) and the small differences are turnover differences: 0.2552 for the
trees, 0.2556 for autoregression, 0.2716 for Olympus — Olympus takes marginally
more positions because it is almost always above the threshold in one direction.

Slippage is not separated from fees. There is no order book in this evaluation
to separate it with, and reporting a zero would claim a measurement that was
not made.

Costs do not change any ranking here. Olympus's gross error is 4× the
baselines'; a 4 bp round trip is not what is deciding this.

---

## 10. Verdict

### The Phase 5 question

> **INSUFFICIENT EVIDENCE**

Computed, not chosen. `MatchedReport.verdict` returns this whenever the Kronos
arm did not run, whatever the other five arms did.
`test_no_arrangement_of_the_other_arms_produces_a_verdict_without_the_reference`
constructs the most favourable possible arrangement — Olympus available, every
other arm available, Olympus winning every comparison against every one of them
by a wide significant margin — and asserts the verdict is still
`INSUFFICIENT_EVIDENCE`. There is no argument that overrides it, because
`verdict` is a property with no backing field.

### The promotion decision

> **NO PROMOTION DECISION POSSIBLE**

Kronos is not replaced, not routed around, not ensembled with and not confirmed
as champion, because none of those conclusions has been earned.

### The question this environment *can* answer

> **The Olympus-native model does not beat the simple baselines. It loses to all
> three, on every metric tested, at the corrected significance level.**

Nine usable comparisons; nine significant losses; zero wins. Under the phase's
own decision framing this points at **"both are rejected in favour of a simple
baseline"** — but only half of that sentence is supported. Olympus is rejected
in favour of a gradient-boosted tree on this data. Kronos was never measured, so
nothing is concluded about it.

**Truth is more important than protecting the project's ego**, and the truthful
summary is: on generated data, under a matched contract, after giving every arm
the same validation-only configuration search, the 15,929-parameter native model
is the worst of the five arms that ran — worse than a persistence rule, worse
than an order-3 autoregression, and 4× worse than gradient-boosted trees.

### A note on naming

Inside `olympus/trading/native/`, the incumbent arm is called
`EXTERNAL_REFERENCE`, not Kronos. That is gate G2, not evasion: a native module
carrying a competitor's name in an enum value is a native module whose
vocabulary is shaped by that competitor, and
`tests/test_trading_independence.py` refuses it — it caught the first version of
`matched.py` doing exactly that. The identity lives in
`kronos_adapter.matched_reference_label()`, which is the same resolution Phase 2
reached when `evaluation.py` needed to name its opponent.

This document and `scripts/matched_evaluation.py` sit outside the independence
boundary and name it plainly. Nothing is hidden from a reader — only from the
import graph.

---

## 11. Reproducibility

```bash
# the full evaluation, tables and verdict
python scripts/matched_evaluation.py --bars 2000 --json report.json

# the harness guarantees, including the verdict rule
python -m pytest tests/test_trading_native_matched.py -q

# confirm the Kronos arm is unbuildable here, by measurement
curl -sS -o /dev/null -w "%{http_code}\n" \
  https://huggingface.co/NeoQuasar/Kronos-small/resolve/main/config.json
curl -sS "$HTTPS_PROXY/__agentproxy/status" | python -m json.tool
```

Everything is seeded. `SEED = 20280501`; the series generator, the split, the
model initialisation, the batching and every bootstrap take it.

### Manifest

| Item | Value |
|---|---|
| contract fingerprint | `b987b1eea361d134ac3e4234c6f9657f` |
| prediction timestamps hash | `a42c531903a1485dc1546af22d13fba9` (682 instants) |
| `SIM:TREND` dataset sha256 | `9d56baa45d0329302e65f0c109dc258c52acb743fe52561f49b8e33cf7710bfb` |
| `SIM:CHOP` dataset sha256 | `a60ff0eb1c131be1bd161111c0050a656e1b3643a6fb3dceb741c6c91cc27cda` |
| persistence model hash | `d1578a37a52e652d` |
| autoregression model hash | `19423c3031fbcd05` |
| gradient-boosted trees model hash | `80626b4fac3fb1f1` |
| olympus-native checkpoint hash | `79a282f0fa87beb1` (15,929 parameters) |
| kronos checkpoint hash | *(none — the arm did not run)* |
| statistical family size | 63 tests, Holm-adjusted |
| non-overlapping periods per arm | 228 (454 overlapping windows dropped) |

The full machine-readable report — every arm, every comparison, every metric —
is written by `--json`.

---

## 12. Known limitations

1. **The Kronos arm did not run.** Its source is MIT and obtainable; its weights
   are on a host this environment's gateway denies. No number here compares
   Olympus with Kronos, and the headline verdict says so.
2. **The ensemble arm did not run**, because an ensemble needs both members.
3. **Every price series is generated by this repository.** Nothing here is
   evidence about any market. The trading numbers in particular are inflated by
   a generator built with strong, exploitable autocorrelation.
4. **The full-feature input class was not run.** Order book, aggressor flow and
   events are not ingested (B1), so the input-advantage axis was measured
   between candles-only and derived-features — a narrower question than Phase 5
   poses, and it returned exactly zero because the derived channels do not reach
   the point forecast.
5. **One walk-forward fold, not several.** The split is an embargoed three-way
   temporal split. A rolling-refit evaluation over many folds needs more data
   than two generated series provide.
6. **Execution is a fill at the anchor close with a flat round-trip cost.**
   Unrealistic for every arm equally, which preserves the comparison and
   overstates every arm's returns.
7. **Slippage is not separated from fees**, because there is no order book.
8. **Latency and memory were measured on this machine, under this load**, with
   four cores and no GPU. They rank the arms; they do not predict production
   numbers.
9. **The configuration searches were small** — one to three configurations per
   arm. A larger search might find a native configuration that does better; it
   might also find one that overfits the validation split. The search size is
   recorded so the numbers can be read with it in mind.
10. **`memory_kb` is whole-process resident memory**, not the model's footprint.
    It is comparable between the two Olympus arms and not comparable with the
    baselines, which is why the baselines report a dash.

---

## 13. What would change this

| To answer | Needs |
|---|---|
| the Phase 5 question at all | egress to `huggingface.co`, or the Kronos weights supplied out of band, **and** the weight licence read |
| whether Olympus has architectural merit | fixing the location bias in §8 — the one substantial piece of unblocked modelling work |
| whether any of this transfers | real market data (B1) |
| the full-feature comparison | order-book and event ingestion (B1) |

The location bias is the item to act on. It does not need market data, it does
not need Kronos, and until it is fixed no comparison involving the native model
is measuring its architecture — it is measuring an offset.
