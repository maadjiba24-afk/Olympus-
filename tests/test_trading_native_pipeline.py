"""Training pipeline, serving, evaluation, model card and originality checks.

The claims under test are the ones a reader has to be able to trust without
rerunning anything:

* **reproducibility is computed, not asserted.** There is no argument that sets
  `RunRecord.reproducible` to True; five facts do, and `test_a_run_missing_any
  _of_the_five_is_not_reproducible` removes them one at a time.
* **the same seed gives the same weights.** And a different seed gives
  different ones — a fit that ignored its seed would pass the first half alone.
* **a feature that never ran is not reported as supported.** Mixed precision
  and distributed training are compiled and inert here, and the run record says
  so by name.
* **abstained rows are not scored as forecasts.** Scoring an absent prediction
  as zero would credit the model with persistence's accuracy on every window it
  declined.
* **the model card cannot state something false.** It runs its own originality
  audit before it renders, and a real-data claim is refused outright.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle
from olympus.trading.errors import ConfigurationError
from olympus.trading.native import abstain as A
from olympus.trading.native import baselines as B
from olympus.trading.native import dataset as D
from olympus.trading.native import evaluation as E
from olympus.trading.native import model as M
from olympus.trading.native import modelcard as MC
from olympus.trading.native import originality as O
from olympus.trading.native import pipeline as P
from olympus.trading.native import serve as SV
from olympus.trading.native import torchutil as TU
from olympus.trading.native.data import make_windows

START = datetime(2027, 9, 1, tzinfo=timezone.utc)
KEY = "SIM:P"
LOOKBACK, HORIZON = 33, 3

needs_torch = pytest.mark.skipif(not TU.torch_available(),
                                 reason="torch is an optional `native` extra")


def ar1(n: int = 700, seed: int = 5) -> list[float]:
    rng = random.Random(seed)
    prices, step = [100.0], 0.0
    for _ in range(n - 1):
        step = 0.97 * step + rng.gauss(0.0, 0.001)
        prices.append(prices[-1] * math.exp(step))
    return prices


def bars(prices, *, key: str = KEY) -> list[Candle]:
    return [Candle(instrument_key=key, timeframe="1h",
                   ts_open=START + timedelta(hours=i),
                   ts_close=START + timedelta(hours=i + 1),
                   open=p, high=p * 1.004, low=p * 0.996, close=p,
                   volume=1000.0 + i * (1 + 0.3 * math.sin(i / 40)))
            for i, p in enumerate(prices)]


CONFIG = M.ModelConfig(lookback=LOOKBACK, horizon=HORIZON, d_model=16,
                       expert_hidden=16)


@pytest.fixture(scope="module")
def split():
    windows = make_windows(bars(ar1()), lookback=LOOKBACK, horizon=HORIZON)
    return D.three_way_split(windows, train_fraction=0.6,
                             validation_fraction=0.2)


@pytest.fixture(scope="module")
def trained(split):
    """One trained model, reused. Training is seconds, not milliseconds."""
    trainer = P.Trainer(CONFIG, P.TrainingConfig(seed=0, epochs=12, patience=6),
                        clock=FixedClock(START + timedelta(days=90)))
    record = trainer.fit(split.train, validation=split.validation,
                         dataset_hash="d" * 64,
                         dataset_manifest_hash="m" * 64)
    context = SV.build_serving_context(trainer.model, split.train, CONFIG)
    checkpoint = trainer.to_checkpoint(
        record, checkpoint_id="ck", train_start=START,
        train_end=START + timedelta(days=25), feature_names=("log_return",))
    signature = trainer.sign(checkpoint)
    return {"trainer": trainer, "record": P.finalise(record, checkpoint,
                                                     signature),
            "checkpoint": checkpoint, "context": context,
            "signature": signature}


# ===========================================================================
# capabilities and honesty about them
# ===========================================================================

def test_capabilities_are_detected_never_assumed():
    detected = P.capabilities()
    assert set(detected) >= {"torch", "cuda", "amp", "distributed",
                             "world_size", "cpu_count"}
    assert detected["cpu_count"] >= 1
    if not detected["cuda"]:
        assert not detected["amp"], (
            "mixed precision was reported available with no CUDA device")


@needs_torch
def test_a_requested_feature_that_is_unavailable_is_not_reported_as_used(split):
    """Requesting mixed precision on a machine without it must not record it."""
    trainer = P.Trainer(CONFIG, P.TrainingConfig(seed=0, epochs=2,
                                                 mixed_precision=True,
                                                 distributed=True),
                        clock=FixedClock(START + timedelta(days=90)))
    record = trainer.fit(split.train[:120], dataset_hash="d" * 64)
    available = P.capabilities()
    assert record.training_config["mixed_precision"] is True
    assert record.features_used["mixed_precision"] is available["amp"]
    assert record.features_used["distributed"] is available["distributed"]


@needs_torch
def test_untested_paths_are_named_rather_than_implied(trained):
    """An autocast context that has only ever been a no-op is a no-op that has
    been tested, not a mixed-precision trainer."""
    paths = trained["record"].untested_paths
    assert any("mixed_precision" in p for p in paths)
    assert any("distributed" in p for p in paths)
    for path in paths:
        assert ":" in path, "an untested path must state why it was not run"


# ===========================================================================
# reproducibility
# ===========================================================================

@needs_torch
def test_a_complete_run_is_reproducible_and_says_which_five_facts(trained):
    record = trained["record"]
    assert record.reproducible
    assert record.gaps == ()
    report = record.report()
    for line in ("dataset:", "code:", "config:", "seed:", "environment:"):
        assert line in report


@needs_torch
@pytest.mark.parametrize("field", ["dataset_hash", "code_version",
                                   "config_fingerprint", "environment"])
def test_a_run_missing_any_of_the_five_is_not_reproducible(trained, field):
    """There is no argument that sets this to True; five facts do."""
    payload = trained["record"].to_dict()
    base = trained["record"]
    replacement = {"dataset_hash": base.dataset_hash,
                   "code_version": base.code_version,
                   "config_fingerprint": base.config_fingerprint,
                   "environment": base.environment}
    replacement[field] = "" if field != "environment" else {}
    broken = P.RunRecord(
        run_id=base.run_id, started_at=base.started_at,
        finished_at=base.finished_at, seed=base.seed,
        training_config=base.training_config, **replacement)
    assert not broken.reproducible
    assert any(field.split("_")[0] in gap for gap in broken.gaps)
    del payload


def test_a_run_that_never_recorded_its_seed_is_not_reproducible():
    record = P.RunRecord(
        run_id="r", started_at=START, finished_at=START, dataset_hash="d",
        code_version="c", config_fingerprint="f", environment={"python": "3"},
        seed=0, training_config={})           # no "seed" key
    assert not record.reproducible
    assert any("seed" in gap for gap in record.gaps)


@needs_torch
def test_the_same_config_and_seed_produce_identical_weights(split):
    from olympus.trading.native.torchutil import state_dict_to_json
    subset = split.train[:150]

    def run(seed: int):
        trainer = P.Trainer(CONFIG, P.TrainingConfig(seed=seed, epochs=3),
                            clock=FixedClock(START + timedelta(days=90)))
        trainer.fit(subset, dataset_hash="d" * 64)
        return json.dumps(state_dict_to_json(trainer.model), sort_keys=True)

    assert run(0) == run(0)
    assert run(0) != run(1), (
        "changing the seed changed nothing, so the seed is not being used")


@needs_torch
def test_the_config_fingerprint_changes_with_anything_that_changes_numbers():
    base = P.TrainingConfig(seed=0, epochs=10)
    assert base.fingerprint() == P.TrainingConfig(seed=0, epochs=10).fingerprint()
    assert base.fingerprint() != P.TrainingConfig(seed=1, epochs=10).fingerprint()
    assert base.fingerprint() != P.TrainingConfig(seed=0, epochs=11).fingerprint()


# ===========================================================================
# the training loop
# ===========================================================================

@needs_torch
def test_gradient_accumulation_reaches_the_effective_batch(split):
    config = P.TrainingConfig(seed=0, epochs=2, batch_size=16, accumulate=4)
    assert config.effective_batch == 64
    trainer = P.Trainer(CONFIG, config,
                        clock=FixedClock(START + timedelta(days=90)))
    record = trainer.fit(split.train[:120], dataset_hash="d" * 64)
    assert record.training_config["effective_batch"] == 64
    assert record.epochs and record.epochs[-1].train_loss > 0


@needs_torch
def test_per_task_losses_are_recorded_so_a_dead_head_is_visible(trained):
    """A total loss hides a head that stopped learning in epoch three."""
    record = trained["record"]
    per_task = record.epochs[-1].per_task
    for kind in CONFIG.tasks.heads:
        if kind.value == "regime":
            assert "regime" in per_task
        else:
            assert kind.value in per_task, f"{kind.value} reported no loss"
    assert record.dead_heads() == (), f"dead heads: {record.dead_heads()}"


@needs_torch
def test_early_stopping_records_which_split_chose_the_weights(split):
    """Selection on validation makes that split part of training; the record
    says so rather than leaving it to be reconstructed."""
    trainer = P.Trainer(CONFIG, P.TrainingConfig(seed=0, epochs=40, patience=2),
                        clock=FixedClock(START + timedelta(days=90)))
    record = trainer.fit(split.train[:200], validation=split.validation[:80],
                         dataset_hash="d" * 64)
    assert record.selection_split == "validation"
    assert record.best_epoch is not None
    if record.stopped_early:
        assert len(record.epochs) < 40


@needs_torch
def test_a_run_can_resume_from_a_previous_record(split):
    trainer = P.Trainer(CONFIG, P.TrainingConfig(seed=0, epochs=3),
                        clock=FixedClock(START + timedelta(days=90)))
    first = trainer.fit(split.train[:120], dataset_hash="d" * 64)
    assert len(first.epochs) == 3

    continuation = P.Trainer(CONFIG, P.TrainingConfig(seed=0, epochs=6),
                             clock=FixedClock(START + timedelta(days=90)))
    second = continuation.fit(split.train[:120], dataset_hash="d" * 64,
                              resume_from=first)
    assert len(second.epochs) == 6
    assert [e.epoch for e in second.epochs] == list(range(6))


@needs_torch
def test_training_events_reach_an_audit_trail(split):
    class Recorder:
        def __init__(self):
            self.events = []

        def record(self, event_type, payload=None, **kwargs):
            self.events.append((event_type, dict(payload or {})))

    audit = Recorder()
    trainer = P.Trainer(CONFIG, P.TrainingConfig(seed=0, epochs=2),
                        clock=FixedClock(START + timedelta(days=90)),
                        audit=audit)
    trainer.fit(split.train[:120], dataset_hash="d" * 64)
    kinds = {payload.get("pipeline_event") for _, payload in audit.events}
    assert {"run_started", "run_finished"} <= kinds


@needs_torch
def test_a_broken_audit_sink_does_not_abort_a_run(split):
    class Broken:
        def record(self, *args, **kwargs):
            raise RuntimeError("sink is down")

    trainer = P.Trainer(CONFIG, P.TrainingConfig(seed=0, epochs=2),
                        clock=FixedClock(START + timedelta(days=90)),
                        audit=Broken())
    record = trainer.fit(split.train[:120], dataset_hash="d" * 64)
    assert record.epochs


def test_an_invalid_training_configuration_is_refused():
    for bad in ({"epochs": 0}, {"batch_size": 0}, {"accumulate": 0},
                {"learning_rate": 0.0}, {"patience": -1}, {"grad_clip": -1.0}):
        with pytest.raises(ConfigurationError):
            P.TrainingConfig(**bad)


# ===========================================================================
# checkpoints and signing
# ===========================================================================

@needs_torch
def test_the_checkpoint_carries_a_manifest_and_hashes_its_contents(trained):
    checkpoint = trained["checkpoint"]
    assert checkpoint.manifest.data_hash
    assert checkpoint.manifest.n_train_rows > 0
    assert checkpoint.content_hash
    assert checkpoint.fresh
    assert checkpoint.manifest.config["architecture_version"]


@needs_torch
def test_a_signature_verifies_and_a_tampered_hash_does_not(trained):
    checkpoint, signature = trained["checkpoint"], trained["signature"]
    if not signature.get("signature"):
        pytest.skip(f"signing unavailable: {signature.get('reason')}")
    assert P.verify_artifact(checkpoint.content_hash, signature)
    assert not P.verify_artifact("0" * 64, signature)


def test_an_unsigned_artifact_says_why_rather_than_failing_the_run():
    """A run that aborted because crypto was absent would push people toward
    not signing at all."""
    signature = P.sign_artifact("payload")
    assert "signature" in signature
    if not signature["signature"]:
        assert signature.get("reason")


# ===========================================================================
# serving
# ===========================================================================

@needs_torch
def test_a_checkpoint_whose_hash_does_not_recompute_cannot_be_served(trained):
    from olympus.trading.native.checkpoint import NativeCheckpoint
    checkpoint = trained["checkpoint"]
    payload = checkpoint.to_dict()
    payload["parameters"] = {**payload["parameters"], "kind": "olympus-multitask",
                             "tampered": True}
    tampered = NativeCheckpoint.from_dict(payload)
    # The rebuilt object hashes its own (changed) contents, so a predictor
    # constructed from it is consistent — what must fail is serving the
    # *original* declared hash against the tampered content.
    assert tampered.content_hash != checkpoint.content_hash

    decision = A.AbstentionPolicy().evaluate(
        checkpoint_verified=False, checkpoint_hash=checkpoint.content_hash,
        recomputed_hash=tampered.content_hash)
    assert decision.primary is A.AbstentionReason.UNVERIFIED_CHECKPOINT


@needs_torch
def test_a_foreign_checkpoint_kind_is_refused_at_construction(trained):
    from olympus.trading.native.checkpoint import NativeCheckpoint
    payload = trained["checkpoint"].to_dict()
    payload["parameters"] = {"kind": "somebody-elses-model", "state": {}}
    with pytest.raises(ConfigurationError, match="not produced by the "
                                                 "multi-task model"):
        SV.MultiTaskPredictor(NativeCheckpoint.from_dict(payload))


@needs_torch
def test_a_forecast_carries_every_task_the_checkpoint_has(trained, split):
    predictor = SV.MultiTaskPredictor(
        trained["checkpoint"], context=trained["context"], market="sim",
        thresholds=A.AbstentionThresholds(max_dispersion_ratio=1e6))
    window = split.test[0]
    forecast = predictor.forecast(window.inputs,
                                  as_of=window.inputs[-1].ts_close)
    assert not forecast.abstained, forecast.abstention_reasons
    for task in ("return_distribution", "price_quantiles", "direction",
                 "regime", "volatility", "drawdown", "abstention", "anomaly",
                 "ood"):
        assert forecast.has(task), task
    assert len(forecast.expected_returns) == HORIZON
    assert forecast.anchor_close == window.inputs[-1].close
    assert 0.0 <= forecast.uncertainty <= 1.0
    assert forecast.inference_ms is not None and forecast.inference_ms > 0
    assert forecast.regime_probabilities
    assert sum(forecast.regime_probabilities.values()) == pytest.approx(1.0,
                                                                        abs=1e-5)


@needs_torch
def test_a_short_window_declines_before_any_tensor_is_allocated(trained, split):
    predictor = SV.MultiTaskPredictor(trained["checkpoint"],
                                      context=trained["context"])
    forecast = predictor.forecast(split.test[0].inputs[:5],
                                  as_of=split.test[0].inputs[-1].ts_close)
    assert forecast.abstained
    assert forecast.tasks_present == ()


@needs_torch
def test_asking_for_an_untrained_horizon_declines(trained, split):
    predictor = SV.MultiTaskPredictor(trained["checkpoint"],
                                      context=trained["context"])
    forecast = predictor.forecast(split.test[0].inputs, horizon=HORIZON + 2,
                                  as_of=split.test[0].inputs[-1].ts_close)
    assert forecast.abstained
    assert "unsupported_horizon" in forecast.abstention_reasons


@needs_torch
def test_a_stale_window_declines(trained, split):
    predictor = SV.MultiTaskPredictor(trained["checkpoint"],
                                      context=trained["context"])
    window = split.test[0]
    late = window.inputs[-1].ts_close + timedelta(hours=20)
    forecast = predictor.forecast(window.inputs, as_of=late)
    assert forecast.abstained
    assert "stale_input" in forecast.abstention_reasons


@needs_torch
def test_the_serving_context_is_built_from_the_training_windows_only(trained):
    """Building it on validation would make the OOD detector's notion of
    'normal' include data the model was not fitted on."""
    context = trained["context"]
    low, high = context.volatility_range
    assert high > low > 0
    assert context.reconstruction_range[1] > 0
    assert context.typical_terminal_spread > 0
    assert context.median_window_volatility > 0
    assert context.trained_instruments == (KEY,)
    assert sum(context.regime_support.values()) > 0


@needs_torch
def test_the_dispersion_reference_scales_with_the_volatility_regime(trained):
    context = trained["context"]
    calm = context.expected_dispersion(context.median_window_volatility / 2)
    typical = context.expected_dispersion(context.median_window_volatility)
    wild = context.expected_dispersion(context.median_window_volatility * 2)
    assert calm < typical < wild, (
        "the expected interval width must widen with the volatility regime, or "
        "the check reduces to an absolute threshold")


@needs_torch
def test_inference_latency_and_memory_are_measured_not_estimated(trained, split):
    predictor = SV.MultiTaskPredictor(trained["checkpoint"],
                                      context=trained["context"])
    for window in split.test[:20]:
        predictor.forecast(window.inputs, as_of=window.inputs[-1].ts_close)
    stats = predictor.stats()
    assert stats.calls == 20
    assert stats.p50_ms > 0 and stats.max_ms >= stats.p95_ms >= stats.p50_ms
    memory, source = SV.peak_memory_kb()
    assert (memory is None) == (not source.startswith("getrusage")), (
        "memory must be a real reading or an explicit None with a reason")


def test_calibration_cannot_be_fitted_on_the_training_split():
    with pytest.raises(ConfigurationError, match="always meaningless"):
        SV.fit_calibration([{"p10": -1.0, "p90": 1.0}], [0.0],
                           quantiles=(0.1, 0.9), fitted_on="train")


def test_calibration_measures_coverage_and_proposes_a_correction():
    rows = [{"p10": -1.0, "p90": 1.0} for _ in range(100)]
    actuals = [0.0] * 100                       # every observation inside
    info = SV.fit_calibration(rows, actuals, quantiles=(0.1, 0.9))
    assert info.observed_coverage == 1.0
    assert info.nominal_coverage == pytest.approx(0.8)
    assert info.coverage_error == pytest.approx(20.0)
    assert info.scale_correction < 1.0, "over-wide intervals should narrow"
    assert info.calibrated and info.fitted_on == "validation"


# ===========================================================================
# evaluation
# ===========================================================================

def observation(**overrides) -> E.Observation:
    base = dict(instrument_key=KEY, timeframe="1h", horizon=HORIZON,
                as_of=START, predicted=0.01, actual=0.02,
                quantiles={"p10": -0.01, "p50": 0.01, "p90": 0.03})
    base.update(overrides)
    return E.Observation(**base)


def test_abstained_rows_are_not_scored_as_forecasts():
    """Scoring an absent prediction as zero credits the model with
    persistence's accuracy on every window it declined."""
    answered = [observation(predicted=0.02, actual=0.02) for _ in range(10)]
    declined = [observation(predicted=0.0, actual=0.5, abstained=True)
                for _ in range(10)]
    metrics = E.metrics_for(answered + declined)
    assert metrics["n"] == 20
    assert metrics["n_scored"] == 10
    assert metrics["mae"] == pytest.approx(0.0, abs=1e-12), (
        "a declined row leaked into the accuracy metrics")
    assert metrics["abstention_quality"]["rate"] == pytest.approx(0.5)


def test_a_model_that_declined_everything_reports_no_accuracy():
    metrics = E.metrics_for([observation(abstained=True) for _ in range(10)])
    assert metrics["n_scored"] == 0
    assert metrics["mae"] is None
    assert metrics["quantile_loss"] is None
    assert metrics["abstention_quality"]["rate"] == 1.0


def test_mape_declines_rather_than_dividing_by_a_near_zero():
    assert E.mape([observation(actual=1e-9)]) is None
    assert E.mape([observation(actual=0.5, predicted=0.4)]) == pytest.approx(0.2)


def test_the_calibration_error_is_signed():
    covered = [observation(actual=0.01) for _ in range(100)]
    assert E.calibration_error(covered) == pytest.approx(20.0)
    missed = [observation(actual=5.0) for _ in range(100)]
    assert E.calibration_error(missed) == pytest.approx(-80.0)


def test_drawdown_quality_reports_lift_not_accuracy():
    """A threshold breached 5% of the time is 95% 'accurate' from a head that
    always says no."""
    rows = [observation(drawdown_probability=0.05, actual_drawdown=0.0)
            for _ in range(95)]
    rows += [observation(drawdown_probability=0.05, actual_drawdown=0.5)
             for _ in range(5)]
    quality = E.drawdown_quality(rows, threshold=0.02)
    assert quality["base_rate"] == pytest.approx(0.05)
    assert "skill_score" in quality
    assert abs(quality["skill_score"]) < 0.2, (
        "a base-rate forecast should show near-zero skill")


def test_decision_value_suppresses_a_move_smaller_than_the_round_trip():
    from olympus.trading.native.benchmark import CostModel, FREE
    tiny = [observation(predicted=1e-5, actual=1e-5) for _ in range(20)]
    dear = E.decision_value(tiny, costs=CostModel(spread_bps=50.0))
    free = E.decision_value([observation(predicted=0.02, actual=0.02)
                             for _ in range(20)], costs=FREE)
    assert dear["trades"] == 0 and dear["net_return"] == 0.0
    assert free["trades"] == 20 and free["net_return"] > 0


def test_failure_is_distinct_from_abstention():
    """A system that conflated them would report its crashes as caution."""
    rows = [observation(failed=True), observation(abstained=True),
            observation()]
    assert E.failure_rate(rows) == pytest.approx(1 / 3)
    assert E.abstention_quality(rows)["rate"] == pytest.approx(1 / 3)


def test_all_eight_strata_are_cut():
    rows = [observation(as_of=START + timedelta(days=i),
                        window_volatility=0.001 * (i + 1),
                        window_volume=100.0 * (i + 1),
                        regime="ranging" if i % 2 else "trending_up",
                        seen_instrument=i % 3 != 0)
            for i in range(30)]
    cuts = E.cut_strata(rows)
    assert set(cuts) == {"instrument", "timeframe", "horizon", "regime",
                         "volatility_environment", "liquidity_environment",
                         "calendar_period", "seen_instrument"}
    assert set(cuts["seen_instrument"]) == {"seen", "unseen"}
    assert set(cuts["volatility_environment"]) == {"low", "medium", "high"}


def test_a_thin_stratum_is_marked_rather_than_hidden():
    rows = [observation() for _ in range(5)]
    report = E.evaluate(rows, model_name="m")
    thin = [s for s in report.strata if not s.sufficient]
    assert thin, "a five-observation stratum was reported as a result"
    assert "(thin)" in report.table()


def test_the_liquidity_stratum_labels_itself_a_proxy():
    rows = [observation(window_volume=float(i)) for i in range(40)]
    report = E.evaluate(rows, model_name="m")
    liquidity = report.dimension("liquidity_environment")
    assert liquidity and all("proxy" in s.note for s in liquidity)
    assert any("proxy" in note for note in report.notes)


def test_an_unavailable_external_arm_is_named_as_missing_not_omitted():
    """An evaluation that quietly dropped its hardest opponent would read as
    complete."""
    arm = E.unavailable_arm("some-external-model", "blocker B2: unreachable")
    report = E.evaluate([observation() for _ in range(40)], model_name="m",
                        arms=[arm])
    assert report.missing_arms
    assert report.missing_arms[0]["name"] == "some-external-model"
    assert "blocker B2" in report.missing_arms[0]["unavailable_reason"]
    assert "NOT RUN  some-external-model" in report.table()


def test_the_external_arms_identity_comes_from_its_adapter_not_from_native():
    """A native evaluation module that hardcoded a third-party model's name
    would have that model in its source, which is what
    `tests/test_trading_independence.py` exists to prevent."""
    from olympus.trading import kronos_adapter
    arm = kronos_adapter.native_benchmark_arm()
    assert not arm.available
    assert "blocker B2" in arm.unavailable_reason
    assert "B4" in arm.unavailable_reason
    assert "superiority" in arm.unavailable_reason


def test_an_unavailable_arm_without_a_reason_is_refused():
    with pytest.raises(ConfigurationError, match="must say why"):
        E.unavailable_arm("x", "  ")


def test_an_unavailable_arm_must_state_why():
    with pytest.raises(ConfigurationError, match="must say why"):
        E.ComparisonArm(name="x", kind="external", available=False)


def test_a_comparison_intersects_window_sets_rather_than_zipping():
    ours = [observation(as_of=START + timedelta(hours=i), predicted=0.02,
                        actual=0.02) for i in range(40)]
    theirs = [observation(as_of=START + timedelta(hours=i), predicted=0.0,
                          actual=0.02) for i in range(20)]
    comparison = E.compare_arms(
        ours, E.ComparisonArm(name="persistence", kind="persistence",
                              observations=theirs))
    assert comparison.common == 20
    assert comparison.mae_gain > 0


def test_a_verdict_needs_a_significant_gain_and_no_decision_loss():
    """A model that scores better and loses money has not improved anything."""
    better = E.ArmComparison(opponent="p", common=100, mae_gain=0.1,
                             quantile_loss_gain=0.1, net_return_gain=0.01,
                             significant=True, p_value=0.0)
    loses_money = E.ArmComparison(opponent="p", common=100, mae_gain=0.1,
                                  quantile_loss_gain=0.1,
                                  net_return_gain=-0.01, significant=True,
                                  p_value=0.0)
    not_significant = E.ArmComparison(opponent="p", common=100, mae_gain=0.1,
                                      quantile_loss_gain=0.1,
                                      net_return_gain=0.01, significant=False,
                                      p_value=0.4)
    assert better.better
    assert not loses_money.better
    assert not not_significant.better


def test_a_report_flags_strata_where_decisions_lost_money():
    good = [observation(as_of=START, predicted=0.02, actual=0.02,
                        regime="ranging") for _ in range(40)]
    bad = [observation(as_of=START, predicted=0.02, actual=-0.02,
                       regime="crisis") for _ in range(40)]
    report = E.evaluate(good + bad, model_name="m")
    weak = {s.value for s in report.weak_strata}
    assert "crisis" in weak
    assert "ranging" not in weak


@needs_torch
def test_the_full_evaluation_runs_end_to_end(trained, split):
    predictor = SV.MultiTaskPredictor(
        trained["checkpoint"], context=trained["context"], market="sim",
        thresholds=A.AbstentionThresholds(max_dispersion_ratio=1e6))
    windows = split.test
    forecasts = [predictor.forecast(w.inputs, as_of=w.inputs[-1].ts_close)
                 for w in windows]
    observations = E.observations_from_forecasts(forecasts, windows)
    assert len(observations) == len(windows)
    assert all(o.window_volatility is not None for o in observations), (
        "window volatility must come from the window, so the stratum survives "
        "an abstention")

    baseline = B.build_baseline("persistence", horizon=HORIZON)
    baseline.fit(split.train)
    arm = []
    for window in windows:
        prediction = baseline.predict_from_bars(window.inputs)
        arm.append(E.Observation(
            instrument_key=KEY, timeframe="1h", horizon=HORIZON,
            as_of=window.as_of,
            predicted=prediction.log_return_quantiles[prediction.median_label][-1],
            actual=window.target_log_returns()[-1],
            quantiles={k: v[-1]
                       for k, v in prediction.log_return_quantiles.items()}))

    report = E.evaluate(
        observations, model_name="olympus-multitask",
        arms=[E.ComparisonArm(name="persistence", kind="persistence",
                              observations=arm),
              E.unavailable_arm("external", "blocker B2: unreachable")])
    assert report.verdict in {"better", "worse", "indistinguishable",
                              "insufficient"}
    assert report.overall["n"] == len(windows)
    assert report.comparisons and report.comparisons[0].opponent == "persistence"
    assert report.missing_arms
    table = report.table()
    assert "OVERALL" in table and "vs persistence" in table


# ===========================================================================
# originality
# ===========================================================================

def test_no_native_module_imports_a_kronos_implementation_module():
    assert O.check_no_foreign_imports() == []


def test_no_native_module_references_a_foreign_weight_source():
    findings = O.check_no_foreign_weight_sources()
    assert findings == [], "\n".join(str(f) for f in findings)


def test_every_marker_exemption_is_still_needed():
    """An exemption that has outlived its reason is a standing permission."""
    assert O.check_exemptions_are_still_needed() == []
    assert set(O.MARKER_EXEMPT) <= {p.name for p in O.native_modules()}


@needs_torch
def test_a_native_checkpoint_declares_no_foreign_origin(trained):
    assert O.check_checkpoint_origin(trained["checkpoint"]) == []


def test_a_checkpoint_claiming_a_foreign_origin_is_caught():
    class Fake:
        checkpoint_id = "fake"
        initialised_from = "https://example.invalid/weights.safetensors"
        parameters = {"state": "pytorch_model.bin"}

    findings = O.check_checkpoint_origin(Fake())
    assert len(findings) >= 2
    assert all(f.check == "checkpoint_origin" for f in findings)


def test_a_configuration_naming_a_foreign_checkpoint_is_caught():
    assert O.check_configuration({"model": {"repo": "someone/kronos-small"}})
    assert O.check_configuration({"weights": ["a", {"b": "model.safetensors"}]})
    assert O.check_configuration(M.ModelConfig().to_dict()) == []


def test_the_model_configuration_is_clean():
    assert O.check_configuration(CONFIG.to_dict()) == []


def test_a_card_that_hides_an_external_method_is_caught():
    findings = O.check_model_card_discloses_methods({"external_methods": []})
    assert findings
    partial = O.check_model_card_discloses_methods(
        {"external_methods": ["patch encoders"]})
    assert any("mixture of experts" in f.detail for f in partial)


def test_every_third_party_import_is_disclosed():
    findings = O.check_dependencies_disclosed(
        {"dependencies": O.DISCLOSED_DEPENDENCIES})
    assert findings == [], "\n".join(str(f) for f in findings)


def test_an_ownership_claim_without_its_basis_is_caught():
    bare = {"summary": "an Olympus-owned model", "dependencies": {}}
    assert O.check_ownership_claims_are_narrow(bare)
    qualified = {"summary": "an Olympus-owned model",
                 "ownership": {"weights": "trained from an Olympus dataset "
                                          "with no foreign weights"}}
    assert O.check_ownership_claims_are_narrow(qualified) == []


def test_a_card_claiming_real_data_weights_without_a_source_is_caught():
    assert O.check_ownership_claims_are_narrow(
        {"weights_trained_on_real_data": True,
         "ownership": {"x": "independently produced"}})


def test_the_whole_audit_is_clean():
    report = O.audit_report()
    assert report["clean"], json.dumps(report["by_check"], indent=2)[:2000]
    assert len(report["checks_run"]) == 8


# ===========================================================================
# the model card
# ===========================================================================

@needs_torch
def test_a_generated_card_states_everything_the_plan_requires(trained):
    card = MC.build_card(
        name="olympus-multitask", version="1",
        created_at=START + timedelta(days=90), model_config=CONFIG,
        run_record=trained["record"], checkpoint=trained["checkpoint"],
        abstention_policy=A.AbstentionPolicy(),
        performance=[MC.PerformanceClaim(
            metric="quantile_loss", value=0.0127,
            evidence=MC.EvidenceClass.SYNTHETIC_DATA,
            dataset="AR(1) generated by this repository", n=96)])
    assert card.strongest_evidence is MC.EvidenceClass.SYNTHETIC_DATA
    assert card.limitations and card.external_methods and card.out_of_scope
    assert card.ownership["weights_trained_on_real_data"] is False
    assert card.reproducibility["reproducible"]

    markdown = card.to_markdown()
    for heading in ("## Summary", "## Out of scope", "## Architecture",
                    "## Tasks", "## Performance", "## Limitations",
                    "## External methods", "## Dependencies disclosed",
                    "## Ownership", "## Abstention", "## Reproducibility"):
        assert heading in markdown, heading
    assert "No novelty is claimed" in markdown
    assert "No claim above rests on real market data" in markdown


def test_a_card_with_no_limitations_is_refused():
    """'None known' is a statement and should be written as one."""
    with pytest.raises(ConfigurationError, match="at least one limitation"):
        MC.ModelCard(name="x", version="1", created_at=START, summary="s",
                     intended_use="u", out_of_scope=("a",), architecture={},
                     tasks={}, training_data={}, training_procedure={},
                     evaluation={}, performance=(), limitations=(),
                     external_methods=("patch",), dependencies={},
                     ownership={}, abstention={})


def test_a_real_data_performance_claim_is_refused():
    """Nothing in this repository has real-data evidence; the template does not
    depend on whoever fills it in."""
    with pytest.raises(ConfigurationError, match="no such evidence exists"):
        MC.build_card(name="x", version="1", created_at=START,
                      model_config=CONFIG,
                      performance=[MC.PerformanceClaim(
                          metric="mae", value=0.1,
                          evidence=MC.EvidenceClass.REAL_DATA,
                          dataset="a real exchange")])


def test_a_data_backed_claim_must_name_its_dataset():
    with pytest.raises(ConfigurationError, match="must name its dataset"):
        MC.PerformanceClaim(metric="mae", value=0.1,
                            evidence=MC.EvidenceClass.SYNTHETIC_DATA)


def test_the_card_lists_the_blocked_tasks_with_their_reasons():
    card = MC.build_card(name="x", version="1", created_at=START,
                         model_config=CONFIG)
    blocked = " ".join(card.limitations)
    for task in ("liquidity", "spread", "execution_cost", "cross_asset",
                 "multi_timeframe"):
        assert task in blocked, task


def test_the_template_names_every_section_the_generator_fills():
    template = MC.card_template()
    card = MC.build_card(name="x", version="1", created_at=START,
                         model_config=CONFIG)
    missing = set(template) - set(card.to_dict())
    assert missing == set(), missing
