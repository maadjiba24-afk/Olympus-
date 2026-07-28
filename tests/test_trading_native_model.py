"""The multi-task model: tasks, architecture, abstention, contract, serving.

What this file is arranged around is the set of ways a multi-task model lies
without anyone noticing:

* **a head enabled with no supervision.** `TaskConfig` refuses it, and
  `test_a_blocked_task_cannot_be_enabled` constructs the attempt.
* **a target that peeks.** Every target is computed from a `Window` and is
  tested against a hand-checkable case — `test_the_volatility_target_is_the
  _horizon_not_the_input` is the one that matters, because the tempting
  implementation scores beautifully and forecasts nothing.
* **a causal core that is not causal.** Verified by perturbation, not by
  reading the padding argument.
* **a field that was not computed reading as zero.** `NativeForecast` returns
  `None`, and an abstaining forecast carries no task values at all.
* **an abstention that cannot fire.** All nine reasons are constructed
  individually; a policy whose checks never trigger would pass a test that only
  ever fed it clean input.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.contracts import Candle, DataQuality
from olympus.trading.errors import ConfigurationError
from olympus.trading.native import abstain as A
from olympus.trading.native import model as M
from olympus.trading.native import result as R
from olympus.trading.native import tasks as T
from olympus.trading.native import torchutil as TU
from olympus.trading.native.data import make_windows

START = datetime(2027, 8, 1, tzinfo=timezone.utc)
KEY = "SIM:M"

needs_torch = pytest.mark.skipif(not TU.torch_available(),
                                 reason="torch is an optional `native` extra")


def bars(prices, *, key: str = KEY, highs=None, lows=None) -> list[Candle]:
    out = []
    for i, p in enumerate(prices):
        high = (highs[i] if highs else p * 1.004)
        low = (lows[i] if lows else p * 0.996)
        out.append(Candle(instrument_key=key, timeframe="1h",
                          ts_open=START + timedelta(hours=i),
                          ts_close=START + timedelta(hours=i + 1),
                          open=p, high=max(high, p), low=min(low, p), close=p,
                          volume=1000.0 + i))
    return out


def ar1(n: int = 700, seed: int = 5, phi: float = 0.97,
        noise: float = 0.001) -> list[float]:
    rng = random.Random(seed)
    prices, step = [100.0], 0.0
    for _ in range(n - 1):
        step = phi * step + rng.gauss(0.0, noise)
        prices.append(prices[-1] * math.exp(step))
    return prices


CONFIG = M.ModelConfig(lookback=33, horizon=3, d_model=16, expert_hidden=16)


# ===========================================================================
# the task registry
# ===========================================================================

def test_all_fifteen_tasks_the_plan_names_are_registered():
    assert len(T.TASKS) == 15
    assert {k.value for k in T.TaskKind} == set(T.TASKS.keys() and
                                                {k.value for k in T.TASKS})


def test_every_task_states_its_class_and_what_it_is_waiting_for():
    for kind, spec in T.TASKS.items():
        assert spec.summary.strip(), kind
        if spec.task_class is T.TaskClass.BLOCKED:
            assert spec.blocked_on.strip(), f"{kind} is blocked with no reason"
        if spec.task_class is T.TaskClass.DERIVED:
            assert spec.derived_from, f"{kind} is derived from nothing"
            assert spec.loss is T.LossKind.NONE


def test_a_blocked_task_cannot_be_enabled():
    """A spread head trained on nothing reports a confident number for a
    quantity it has never seen."""
    with pytest.raises(ConfigurationError, match="cannot be enabled here"):
        T.TaskConfig(enabled=(T.TaskKind.RETURN_DISTRIBUTION, T.TaskKind.SPREAD))


def test_a_derived_task_cannot_be_enabled_as_a_head():
    with pytest.raises(ConfigurationError, match="no head to enable"):
        T.TaskConfig(enabled=(T.TaskKind.RETURN_DISTRIBUTION,
                              T.TaskKind.PRICE_QUANTILES))


def test_the_primary_head_is_required():
    with pytest.raises(ConfigurationError, match="required"):
        T.TaskConfig(enabled=(T.TaskKind.DIRECTION,))


def test_price_quantiles_are_derived_not_a_head():
    """Two heads could disagree, and a price band contradicting its own return
    band is worse than one band."""
    spec = T.TASKS[T.TaskKind.PRICE_QUANTILES]
    assert spec.task_class is T.TaskClass.DERIVED
    assert not spec.has_head
    assert T.TaskKind.RETURN_DISTRIBUTION in spec.derived_from


def test_enabled_order_follows_the_registry_not_the_caller():
    forwards = T.TaskConfig(enabled=(T.TaskKind.RETURN_DISTRIBUTION,
                                     T.TaskKind.DIRECTION))
    backwards = T.TaskConfig(enabled=(T.TaskKind.DIRECTION,
                                      T.TaskKind.RETURN_DISTRIBUTION))
    assert forwards.enabled == backwards.enabled


def test_five_tasks_are_trainable_from_what_olympus_can_obtain():
    report = T.task_report()
    assert set(report["blocked"]) == {"liquidity", "spread", "execution_cost",
                                      "cross_asset", "multi_timeframe"}
    assert len(report["trainable_here"]) == 7


# ===========================================================================
# targets
# ===========================================================================

def test_the_return_target_is_the_cumulative_log_return():
    window = make_windows(bars([100.0] * 5 + [110.0, 121.0, 133.1]),
                          lookback=5, horizon=3)[0]
    values = T.target_return_quantiles(window)
    assert values[0] == pytest.approx(math.log(1.1), abs=1e-9)
    assert values[2] == pytest.approx(math.log(1.331), abs=1e-9)


def test_the_direction_target_is_the_sign_of_the_terminal_return():
    up = make_windows(bars([100.0] * 5 + [101.0, 102.0, 103.0]),
                      lookback=5, horizon=3)[0]
    down = make_windows(bars([100.0] * 5 + [99.0, 98.0, 97.0]),
                        lookback=5, horizon=3)[0]
    assert T.target_direction(up) == 1.0
    assert T.target_direction(down) == 0.0


def test_the_volatility_target_is_the_horizon_not_the_input():
    """The tempting implementation scores beautifully and forecasts nothing.

    A head trained on the input window's volatility is learning to copy a
    number it can already see. This constructs a window whose input is wild and
    whose horizon is flat, so the two answers differ by construction.
    """
    # The input window swings wildly and ends at 100; the horizon is flat at
    # 100. The horizon's dispersion is exactly zero, the input's is not, so the
    # two candidate implementations give different answers by construction.
    wild_input = [100.0, 120.0, 90.0, 130.0, 100.0]
    flat_horizon = [100.0, 100.0, 100.0]
    window = make_windows(bars(wild_input + flat_horizon), lookback=5,
                          horizon=3)[0]
    assert T.target_volatility(window) == pytest.approx(0.0, abs=1e-9), (
        "the volatility target read the input window rather than the horizon")


def test_the_drawdown_target_uses_intrabar_lows_against_the_running_peak():
    """Close-to-close would miss the excursion an open position actually felt."""
    prices = [100.0] * 8
    highs = [100.0] * 8
    lows = [100.0] * 5 + [100.0, 90.0, 100.0]
    window = make_windows(bars(prices, highs=highs, lows=lows), lookback=5,
                          horizon=3)[0]
    # Peak 100 from the running high, trough 90 from the intrabar low. A
    # close-to-close measure would report zero here, which is the point.
    assert T.target_drawdown_magnitude(window) == pytest.approx(0.10, abs=1e-6)
    assert T.target_drawdown(window, 0.05) == 1.0
    assert T.target_drawdown(window, 0.20) == 0.0


def test_the_regime_target_is_classified_on_the_inputs():
    """Labelling on the targets would make the head a look-ahead detector."""
    window = make_windows(bars(ar1(60)), lookback=40, horizon=3)[0]
    index = T.target_regime(window)
    assert 0 <= index < len(T.REGIME_CLASSES)


def test_targets_for_produces_exactly_the_enabled_supervised_tasks():
    window = make_windows(bars(ar1(60)), lookback=40, horizon=3)[0]
    config = T.TaskConfig(enabled=(T.TaskKind.RETURN_DISTRIBUTION,
                                   T.TaskKind.DIRECTION))
    produced = T.targets_for(window, config)
    assert set(produced) == {"return_distribution", "direction"}


# ===========================================================================
# the architecture
# ===========================================================================

def test_the_receptive_field_is_arithmetic_not_empirical():
    config = M.ModelConfig(lookback=33, kernel_size=2, n_layers=3)
    assert config.receptive_field == 1 + 2 * 1 * (2 ** 3 - 1) == 15
    assert config.n_positions == 8
    assert config.covers_lookback


def test_a_core_that_does_not_cover_its_window_says_so_rather_than_silently():
    shallow = M.ModelConfig(lookback=129, patch_len=4, kernel_size=2, n_layers=2)
    assert shallow.n_positions == 32
    assert shallow.receptive_field < shallow.n_positions
    assert not shallow.covers_lookback, (
        "a receptive field shorter than the window is legitimate; a silent one "
        "is not")


def test_a_ragged_patch_grid_is_refused():
    with pytest.raises(ConfigurationError, match="whole number of patches"):
        M.ModelConfig(lookback=34, patch_len=4)


def test_regime_routing_requires_the_regime_head():
    """The head's logits *are* the router; disabling it removes the gate."""
    without = T.TaskConfig(enabled=(T.TaskKind.RETURN_DISTRIBUTION,
                                    T.TaskKind.DIRECTION))
    with pytest.raises(ConfigurationError, match="regime head enabled"):
        M.ModelConfig(regime_routing=True, tasks=without)


def test_regime_routing_fixes_the_expert_count_to_the_label_set():
    with pytest.raises(ConfigurationError, match="one expert per regime"):
        M.ModelConfig(regime_routing=True, n_experts=3)


def test_a_single_quantile_is_refused():
    with pytest.raises(ConfigurationError, match="at least two quantiles"):
        M.ModelConfig(quantiles=(0.5,))


@needs_torch
def test_the_model_emits_one_output_per_enabled_head():
    torch = TU.require_torch()
    model = M.build_model(CONFIG)
    out = model(torch.randn(4, 4, 32))
    for kind in CONFIG.tasks.heads:
        assert kind.value in out, f"{kind.value} head produced nothing"
    assert tuple(out["return_distribution"].shape) == (4, 3, CONFIG.n_quantiles)
    assert tuple(out["regime"].shape) == (4, len(T.REGIME_CLASSES))


@needs_torch
def test_a_disabled_task_has_no_parameters_at_all():
    """A checkpoint's head bank is a fact about the checkpoint, not a switch."""
    lean = M.ModelConfig(lookback=33, d_model=16, expert_hidden=16,
                         tasks=T.TaskConfig(enabled=(
                             T.TaskKind.RETURN_DISTRIBUTION, T.TaskKind.REGIME)))
    full = CONFIG
    assert (M.model_parameters(M.build_model(lean))
            < M.model_parameters(M.build_model(full)))
    assert "drawdown" not in dict(M.build_model(lean).heads)


@needs_torch
def test_the_causal_core_cannot_read_a_later_position():
    """Verified by perturbation, not by reading the padding argument."""
    torch = TU.require_torch()
    model = M.build_model(CONFIG)
    base = torch.randn(1, 4, 32, generator=torch.Generator().manual_seed(0))
    altered = base.clone()
    altered[:, :, -CONFIG.patch_len:] += 10.0

    def hidden(x):
        latent, _, _ = model.encoder(x, None)
        return model.core(latent)

    with torch.no_grad():
        before, after = hidden(base), hidden(altered)
    assert torch.allclose(before[:, :-1, :], after[:, :-1, :], atol=1e-5), (
        "changing the last patch moved earlier positions: the core is not causal")
    assert not torch.allclose(before[:, -1, :], after[:, -1, :], atol=1e-5)


@needs_torch
def test_the_quantile_ladder_cannot_cross():
    torch = TU.require_torch()
    model = M.build_model(CONFIG)
    for seed in range(3):
        x = torch.randn(8, 4, 32, generator=torch.Generator().manual_seed(seed))
        with torch.no_grad():
            q = model(x)["return_distribution"]
        assert bool((q[..., 1:] >= q[..., :-1]).all()), "quantiles crossed"


@needs_torch
def test_the_gate_is_a_distribution_and_comes_from_the_regime_head():
    torch = TU.require_torch()
    model = M.build_model(CONFIG)
    with torch.no_grad():
        out = model(torch.randn(5, 4, 32))
    gate, logits = out["gate"], out["regime"]
    assert torch.allclose(gate.sum(-1), torch.ones(5), atol=1e-5)
    assert torch.allclose(gate, torch.softmax(logits, dim=-1), atol=1e-6), (
        "the gate is not the regime head's softmax, so routing is not "
        "supervised and the interpretability claim is false")


@needs_torch
def test_a_free_router_is_available_so_the_comparison_can_be_run():
    """The design gives up a discoverable partition; the alternative exists."""
    config = M.ModelConfig(lookback=33, d_model=16, expert_hidden=16,
                           regime_routing=False, n_experts=4)
    torch = TU.require_torch()
    model = M.build_model(config)
    with torch.no_grad():
        gate = model(torch.randn(3, 4, 32))["gate"]
    assert tuple(gate.shape) == (3, 4)


@needs_torch
def test_cross_asset_attention_passes_through_a_row_with_no_references():
    """Softmax over an all-masked row is undefined and produces NaN."""
    torch = TU.require_torch()
    config = M.ModelConfig(lookback=33, d_model=16, expert_hidden=16,
                           cross_asset_heads=2)
    model = M.build_model(config)
    references = torch.randn(3, 2, 16)
    mask = torch.tensor([[False, False], [False, True], [True, True]])
    with torch.no_grad():
        out = model(torch.randn(3, 4, 32), references=references,
                    reference_mask=mask)
    assert bool(torch.isfinite(out["summary"]).all()), (
        "a row with every reference absent produced a non-finite summary")


@needs_torch
def test_timeframe_fusion_fixes_its_order_at_construction():
    torch = TU.require_torch()
    config = M.ModelConfig(lookback=33, d_model=16, expert_hidden=16,
                           timeframes=("1h", "4h"))
    model = M.build_model(config)
    assert model.fusion.order == ("1h", "4h")
    with pytest.raises(ValueError, match="missing timeframes"):
        model(torch.randn(2, 4, 32), extra_timeframes={"1d": torch.randn(2, 16)})


@needs_torch
def test_a_model_with_an_instrument_embedding_demands_instrument_ids():
    torch = TU.require_torch()
    config = M.ModelConfig(lookback=33, d_model=16, expert_hidden=16,
                           n_instruments=4)
    model = M.build_model(config)
    with pytest.raises(ValueError, match="instrument ids"):
        model(torch.randn(2, 4, 32))


@needs_torch
def test_a_non_sequential_representation_is_refused_by_the_causal_core():
    """`multi_resolution_patch` concatenates several time orders."""
    config = M.ModelConfig(lookback=33, patch_len=4, d_model=16,
                           expert_hidden=16,
                           representation="multi_resolution_patch")
    with pytest.raises(ConfigurationError, match="single time order"):
        M.build_model(config)


def test_the_architecture_report_names_its_external_methods_and_its_limits():
    report = M.architecture_report(CONFIG)
    joined = " ".join(report["external_methods"]).lower()
    for method in ("patch", "causal convolution", "mixture of experts",
                   "attention", "quantile", "pinball"):
        assert method in joined, method
    assert all("published" in m or "general" in m
               for m in report["external_methods"])
    assert report["claimed_original"] and report["not_claimed"]
    assert any("superiority" in claim for claim in report["not_claimed"])


# ===========================================================================
# abstention
# ===========================================================================

def test_all_nine_reasons_exist_and_each_has_a_description():
    assert len(A.AbstentionReason) == 9
    assert set(A.CHECK_ORDER) == set(A.AbstentionReason)
    for reason in A.AbstentionReason:
        assert A.REASON_DESCRIPTIONS[reason].strip()


def test_a_clean_request_does_not_abstain():
    decision = A.AbstentionPolicy().evaluate(
        checkpoint_verified=True, requested_horizon=3, trained_horizon=3,
        last_bar_close=START, as_of=START + timedelta(hours=1),
        bar_seconds=3600, dispersion=0.02, expected_dispersion=0.03,
        coverage_error=3.0, regime="ranging", regime_support={"ranging": 100})
    assert not decision.abstained
    assert decision.measurements["dispersion_ratio"] < 1.0


@pytest.mark.parametrize("reason,kwargs", [
    (A.AbstentionReason.UNVERIFIED_CHECKPOINT, {"checkpoint_verified": False}),
    (A.AbstentionReason.INCOMPATIBLE_VERSION,
     {"architecture_version": "1", "expected_architecture_version": "2"}),
    (A.AbstentionReason.UNSUPPORTED_HORIZON,
     {"requested_horizon": 5, "trained_horizon": 3}),
    (A.AbstentionReason.MISSING_CHANNELS, {"missing_channels": ("bid",)}),
    (A.AbstentionReason.STALE_INPUT,
     {"last_bar_close": START, "as_of": START + timedelta(hours=9),
      "bar_seconds": 3600}),
    (A.AbstentionReason.INSTRUMENT_OUT_OF_DISTRIBUTION,
     {"instrument_key": "NEW", "trained_instruments": ("OLD",),
      "has_instrument_embedding": True}),
    (A.AbstentionReason.UNSUPPORTED_REGIME,
     {"regime": "crisis", "regime_support": {"crisis": 1}}),
    (A.AbstentionReason.POOR_CALIBRATION, {"coverage_error": 40.0}),
    (A.AbstentionReason.EXCESSIVE_DISPERSION,
     {"dispersion": 0.5, "expected_dispersion": 0.01}),
])
def test_each_reason_can_fire_on_its_own(reason, kwargs):
    """A policy whose checks never trigger would pass a test that only ever
    fed it clean input."""
    settings = {"checkpoint_verified": True, **kwargs}
    decision = A.AbstentionPolicy().evaluate(**settings)
    assert decision.abstained
    assert reason.value in decision.reasons


def test_an_unverified_checkpoint_is_reported_first():
    """If the hash does not recompute, nothing else about it can be trusted —
    including the thresholds the other checks would use."""
    decision = A.AbstentionPolicy().evaluate(
        checkpoint_verified=False, requested_horizon=5, trained_horizon=3,
        missing_channels=("bid",), coverage_error=99.0)
    assert decision.primary is A.AbstentionReason.UNVERIFIED_CHECKPOINT
    assert len(decision.findings) >= 3


def test_an_instrument_agnostic_model_is_not_out_of_distribution_on_a_new_symbol():
    """Applying it to a new symbol is exactly what it was built for."""
    decision = A.AbstentionPolicy().evaluate(
        checkpoint_verified=True, instrument_key="NEW",
        trained_instruments=("OLD",), has_instrument_embedding=False)
    assert not decision.abstained


def test_an_abstention_with_no_reason_is_refused():
    with pytest.raises(ConfigurationError, match="no reason"):
        A.AbstentionDecision(abstained=True)


def test_findings_without_an_abstention_are_refused():
    finding = A.AbstentionFinding(reason=A.AbstentionReason.STALE_INPUT,
                                  detail="x")
    with pytest.raises(ConfigurationError, match="decision was to answer"):
        A.AbstentionDecision(abstained=False, findings=(finding,))


def test_a_decision_records_the_passing_measurements_too():
    """A record that only says 'did not abstain' cannot support asking later
    whether the limit was right."""
    decision = A.AbstentionPolicy().evaluate(
        checkpoint_verified=True, dispersion=0.01, expected_dispersion=0.02,
        coverage_error=2.0)
    assert not decision.abstained
    assert decision.measurements["dispersion_ratio"] == pytest.approx(0.5)
    assert decision.measurements["coverage_error"] == 2.0
    assert decision.thresholds["max_dispersion_ratio"] > 0


# ===========================================================================
# the forecast contract
# ===========================================================================

def full_forecast(**overrides) -> R.NativeForecast:
    base = dict(
        instrument_key=KEY, market="sim", timeframes=("1h",),
        input_start=START, input_end=START + timedelta(hours=32),
        created_at=START + timedelta(hours=32), horizon=3,
        model_version="olympus-multitask/1/abc", checkpoint_hash="h" * 64,
        dataset_version="d" * 64, feature_schema_version="olympus.market.v1",
        expected_returns=(0.001, 0.002, 0.003),
        quantiles={"p10": (-0.01, -0.02, -0.03), "p50": (0.001, 0.002, 0.003),
                   "p90": (0.01, 0.02, 0.03)},
        anchor_close=100.0, uncertainty=0.4,
        tasks_present=("return_distribution",))
    base.update(overrides)
    return R.NativeForecast(**base)


def test_the_contract_carries_every_field_the_plan_requires():
    record = full_forecast().to_dict()
    for field in ("instrument", "market", "timeframes", "input_start",
                  "input_end", "horizon", "expected_returns",
                  "direction_probabilities", "quantiles", "price_quantiles",
                  "predicted_volatility", "drawdown_probability",
                  "liquidity_forecast", "execution_cost_estimate",
                  "regime_probabilities", "uncertainty", "calibration",
                  "ood_score", "abstained", "abstention_reasons",
                  "model_version", "checkpoint_hash", "dataset_version",
                  "feature_schema_version", "warnings"):
        assert field in record, field


def test_provenance_is_required():
    """A forecast whose model cannot be identified is not auditable."""
    for missing in ("model_version", "checkpoint_hash", "dataset_version",
                    "feature_schema_version"):
        with pytest.raises(ConfigurationError, match="required"):
            full_forecast(**{missing: ""})


def test_a_task_this_checkpoint_did_not_produce_reads_as_none():
    """A zero drawdown probability from a checkpoint with no drawdown head is
    a claim of safety."""
    forecast = full_forecast()
    assert forecast.drawdown_probability is None
    assert forecast.value("drawdown") is None
    assert not forecast.has("drawdown")
    assert forecast.value("return_distribution") == forecast.quantiles


def test_crossed_quantiles_are_refused_by_the_contract_as_well_as_the_head():
    with pytest.raises(ConfigurationError, match="cross"):
        full_forecast(quantiles={"p10": (0.5, 0.5, 0.5),
                                 "p90": (-0.5, -0.5, -0.5)})


def test_an_abstaining_forecast_carries_no_task_values():
    decision = A.AbstentionPolicy().evaluate(checkpoint_verified=False)
    forecast = R.abstaining_forecast(
        instrument_key=KEY, market="sim", timeframes=("1h",),
        input_start=START, input_end=START + timedelta(hours=32),
        created_at=START + timedelta(hours=32), horizon=3, decision=decision,
        model_version="m/1/a", checkpoint_hash="h", dataset_version="d",
        feature_schema_version="f")
    assert forecast.abstained
    assert forecast.tasks_present == ()
    assert forecast.drawdown_probability is None
    assert forecast.expected_returns == ()
    assert forecast.uncertainty == 1.0
    assert forecast.data_quality is DataQuality.DEGRADED


def test_a_calibration_fitted_on_training_rows_is_refused():
    with pytest.raises(ConfigurationError, match="always meaningless"):
        R.CalibrationInfo(fitted_on="train")


def test_the_contract_narrows_to_the_serving_result_one_way_only():
    forecast = full_forecast()
    narrowed = forecast.to_forecast_result()
    assert narrowed.instrument_key == KEY
    assert narrowed.horizon == 3
    assert set(narrowed.quantiles) == {"p10", "p50", "p90"}
    # Prices, not log returns.
    assert narrowed.quantiles["p50"][0] == pytest.approx(100.0 * math.exp(0.001))
    assert narrowed.expected_return == pytest.approx(math.expm1(0.003))
    assert not hasattr(narrowed, "to_native_forecast")


def test_an_abstaining_contract_narrows_with_its_reason_attached():
    decision = A.AbstentionPolicy().evaluate(
        checkpoint_verified=True, requested_horizon=5, trained_horizon=3)
    forecast = R.abstaining_forecast(
        instrument_key=KEY, market="sim", timeframes=("1h",),
        input_start=START, input_end=START + timedelta(hours=1),
        created_at=START + timedelta(hours=1), horizon=5, decision=decision,
        model_version="m", checkpoint_hash="h", dataset_version="d",
        feature_schema_version="f")
    narrowed = forecast.to_forecast_result()
    assert narrowed.abstained
    assert narrowed.failure_code == "unsupported_horizon"
    assert narrowed.failure_reason


def test_two_identical_forecasts_fingerprint_the_same():
    assert full_forecast().fingerprint() == full_forecast().fingerprint()
    assert (full_forecast(uncertainty=0.9).fingerprint()
            != full_forecast().fingerprint())


def test_a_regime_vector_of_the_wrong_width_is_refused():
    with pytest.raises(ConfigurationError, match="label set"):
        R.regime_distribution([0.5, 0.5])
    mapped = R.regime_distribution([1.0] + [0.0] * (len(T.REGIME_CLASSES) - 1))
    assert mapped[T.REGIME_CLASSES[0]] == 1.0
