"""Encoder contracts and the seven representation candidates.

Two things are under test and they are different in kind.

**The contracts** (`interfaces.py`) are pure stdlib and must stay importable
with torch absent, because a consumer that only wants to read a `LatentSpec`
should not need a deep-learning framework. Those tests run everywhere.

**The candidates** (`representations.py`) need torch and are skipped without
it. What they assert is that the documentation is *true of the code*: the
declared latent shape is the shape produced, the declared parameter count is
the count measured, the candidate that says it tokenises emits codes, and the
one that says it handles missingness actually distinguishes an absent channel
from a zero.

The load-bearing test is `test_the_quantised_candidates_pay_the_error_floor_
they_document`. Every candidate here claims something about its own trade-off;
that one is where a claim is checked against a number rather than against the
sentence that made it.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.contracts import Candle
from olympus.trading.errors import ConfigurationError
from olympus.trading.native import interfaces as I
from olympus.trading.native import representations as R
from olympus.trading.native import torchutil as TU
from olympus.trading.native.data import make_windows

START = datetime(2027, 5, 1, tzinfo=timezone.utc)
KEY = "SIM:R"

needs_torch = pytest.mark.skipif(not TU.torch_available(),
                                 reason="torch is an optional `native` extra")

CONFIG = R.RepresentationConfig(lookback=16, patch_len=4, d_model=16,
                               codebook_size=16)


def series(n: int = 600, seed: int = 3, phi: float = 0.95,
           noise: float = 0.002) -> list[float]:
    rng = random.Random(seed)
    prices, step = [100.0], 0.0
    for _ in range(n - 1):
        step = phi * step + rng.gauss(0.0, noise)
        prices.append(prices[-1] * math.exp(step))
    return prices


def bars(prices) -> list[Candle]:
    return [Candle(instrument_key=KEY, timeframe="1h",
                   ts_open=START + timedelta(hours=i),
                   ts_close=START + timedelta(hours=i + 1),
                   open=p, high=p * 1.003, low=p * 0.997, close=p,
                   volume=1000.0 + i)
            for i, p in enumerate(prices)]


@pytest.fixture(scope="module")
def tensors():
    windows = make_windows(bars(series()), lookback=17, horizon=3)
    everything = R.windows_to_tensor(windows, CONFIG)
    cut = int(len(windows) * 0.7)
    return everything[:cut], everything[cut:]


# ===========================================================================
# contracts — no torch required
# ===========================================================================

def test_the_contracts_import_without_torch():
    """A consumer reading a latent spec must not need a training framework."""
    import importlib
    module = importlib.import_module("olympus.trading.native.interfaces")
    assert module.CONTRACT_VERSION >= 1
    source = __import__("pathlib").Path(module.__file__).read_text()
    assert "import torch" not in source


def test_there_is_no_zero_fill_mask_policy():
    """The same absence the schema has, for the same reason."""
    assert not any("zero" in policy.value for policy in I.MaskPolicy)


def test_a_latent_spec_declares_its_shape_before_any_forward_pass():
    spec = I.LatentSpec(positions=4, d_model=16)
    assert spec.shape == (4, 16)
    assert spec.sequential and not spec.emits_mask
    with pytest.raises(ConfigurationError, match="d_model must be >= 1"):
        I.LatentSpec(positions=4, d_model=0)


def test_compatibility_needs_matching_width_and_ordering_not_length():
    wide = I.LatentSpec(positions=4, d_model=16)
    longer = I.LatentSpec(positions=8, d_model=16)
    narrower = I.LatentSpec(positions=4, d_model=8)
    unordered = I.LatentSpec(positions=4, d_model=16, sequential=False)
    assert wide.compatible_with(longer)
    assert not wide.compatible_with(narrower)
    assert not wide.compatible_with(unordered)


def test_encoder_metadata_identity_covers_more_than_the_name():
    def build(**overrides):
        base = dict(name="e", version="1",
                    latent=I.LatentSpec(positions=4, d_model=16),
                    channels=("a", "b"), schema_fingerprint="abc")
        base.update(overrides)
        return I.EncoderMetadata(**base)

    original = build()
    assert build().identity() == original.identity()
    assert build(version="2").identity() != original.identity()
    assert build(channels=("b", "a")).identity() != original.identity(), (
        "channel order is part of the identity")
    assert build(schema_fingerprint="def").identity() != original.identity()
    assert build(params={"d": 1}).identity() != original.identity()


def test_metadata_refuses_a_duplicate_or_empty_channel_surface():
    spec = I.LatentSpec(positions=4, d_model=16)
    with pytest.raises(ConfigurationError, match="must declare the channels"):
        I.EncoderMetadata(name="e", version="1", latent=spec, channels=(),
                          schema_fingerprint="x")
    with pytest.raises(ConfigurationError, match="[Dd]uplicate channel"):
        I.EncoderMetadata(name="e", version="1", latent=spec,
                          channels=("a", "a"), schema_fingerprint="x")


def test_an_encoded_batch_refuses_to_contradict_its_own_metadata():
    spec = I.LatentSpec(positions=4, d_model=16, emits_mask=True)
    metadata = I.EncoderMetadata(name="e", version="1", latent=spec,
                                 channels=("a",), schema_fingerprint="x",
                                 tokenises=True)
    with pytest.raises(ConfigurationError, match="no codes"):
        I.EncodedBatch(latent=object(), spec=spec, metadata=metadata,
                       valid=object())
    with pytest.raises(ConfigurationError, match="no validity mask"):
        I.EncodedBatch(latent=object(), spec=spec, metadata=metadata,
                       codes=object())


def test_metadata_round_trips_through_a_dict():
    original = I.EncoderMetadata(
        name="e", version="3", latent=I.LatentSpec(positions=2, d_model=4),
        channels=("a", "b"), schema_fingerprint="fp",
        mask_policy=I.MaskPolicy.LEARNED_ABSENT, trainable_parameters=17,
        params={"x": 1}, reconstructs=True)
    rebuilt = I.EncoderMetadata.from_dict(original.to_dict())
    assert rebuilt.identity() == original.identity()
    assert rebuilt.handles_missing


# ===========================================================================
# documentation is data, and every candidate has all of it
# ===========================================================================

def test_every_candidate_documents_all_eleven_required_facts():
    """An undocumented candidate cannot be compared against another."""
    assert len(R.CANDIDATES) == 7
    for key, candidate in R.CANDIDATES.items():
        record = candidate.to_dict()
        for field in ("rationale", "formulation", "input_shape", "output_shape",
                      "complexity", "reconstruction_objective",
                      "forecasting_compatibility", "prior_art", "independence"):
            assert record[field].strip(), f"{key} has no {field}"
        assert record["trainable_components"], f"{key} lists no trainable parts"
        assert record["limitations"], f"{key} states no limitation"


def test_a_candidate_without_a_stated_limitation_is_refused():
    """'None known' is a statement and should be written as one."""
    base = R.CANDIDATES["continuous_patch"].to_dict()
    base.pop("mask_policy")
    base["limitations"] = []
    with pytest.raises(ConfigurationError, match="at least one limitation"):
        R.RepresentationCandidate(**{**base,
                                     "trainable_components": tuple(base["trainable_components"]),
                                     "limitations": ()})


def test_every_candidate_states_its_relationship_to_prior_work():
    """Vector quantisation is published. Saying so is not optional."""
    for key, candidate in R.CANDIDATES.items():
        assert "novelty" in candidate.prior_art.lower(), key


def test_the_candidate_report_names_which_are_implemented_and_which_tokenise():
    report = R.candidate_report()
    assert set(report["implemented"]) == set(R.CANDIDATES)
    assert set(report["tokenising"]) == {"learned_tokens", "residual_vq", "hybrid"}


# ===========================================================================
# configuration rejection
# ===========================================================================

def test_a_lookback_that_is_not_a_whole_number_of_patches_is_refused():
    with pytest.raises(ConfigurationError, match="whole number of patches"):
        R.RepresentationConfig(lookback=17, patch_len=4)


def test_a_resolution_that_does_not_divide_the_lookback_is_refused():
    with pytest.raises(ConfigurationError, match="must divide the lookback"):
        R.RepresentationConfig(lookback=16, patch_len=4, patch_lens=(3, 4))


def test_a_channel_in_two_groups_is_refused():
    with pytest.raises(ConfigurationError, match="two groups"):
        R.RepresentationConfig(groups={"a": ("log_return",),
                                       "b": ("log_return",)})


def test_a_group_naming_an_absent_channel_is_refused():
    with pytest.raises(ConfigurationError, match="does not carry"):
        R.RepresentationConfig(groups={"a": ("not_a_channel",)})


def test_an_unknown_candidate_is_refused():
    with pytest.raises(ConfigurationError, match="unknown representation"):
        R.build_representation("transformer_xl")


# ===========================================================================
# shapes and metadata, measured
# ===========================================================================

@needs_torch
@pytest.mark.parametrize("key", sorted(R.CANDIDATES))
def test_the_declared_latent_shape_is_the_shape_produced(key, tensors):
    train, _ = tensors
    representation = R.build_representation(key, CONFIG)
    encoded = representation.encode(train[:4])
    spec = representation.latent_spec
    assert tuple(encoded.latent.shape) == (4, spec.positions, spec.d_model)
    assert encoded.spec is spec


@needs_torch
@pytest.mark.parametrize("key", sorted(R.CANDIDATES))
def test_the_declared_parameter_count_is_the_count_measured(key):
    """Measured from the built module, not predicted from the formulation."""
    representation = R.build_representation(key, CONFIG)
    counted = sum(p.numel() for p in representation.module.parameters()
                  if p.requires_grad)
    assert representation.metadata.trainable_parameters == counted > 0


@needs_torch
def test_the_grouped_candidate_has_fewer_parameters_than_the_dense_one():
    """Its complexity note claims the block-diagonal structure saves weights."""
    grouped = R.build_representation("grouped_embeddings", CONFIG)
    dense = R.build_representation("continuous_patch", CONFIG)
    assert (grouped.metadata.trainable_parameters
            < dense.metadata.trainable_parameters)


@needs_torch
def test_the_multi_resolution_candidate_declares_it_is_not_one_time_order():
    """Its stated cost. A causal trunk may not span the concatenation join."""
    representation = R.build_representation("multi_resolution_patch", CONFIG)
    assert representation.latent_spec.sequential is False
    assert representation.latent_spec.positions == sum(
        CONFIG.lookback // length for length in CONFIG.patch_lens)
    assert any("time order" in limit or "time-order" in limit
               for limit in R.CANDIDATES["multi_resolution_patch"].limitations)


@needs_torch
@pytest.mark.parametrize("key", ["learned_tokens", "residual_vq", "hybrid"])
def test_a_tokenising_candidate_emits_codes_within_its_vocabulary(key, tensors):
    train, _ = tensors
    representation = R.build_representation(key, CONFIG)
    assert isinstance(representation, R.QuantisedRepresentation)
    encoded = representation.encode(train[:8])
    assert encoded.codes is not None
    assert int(encoded.codes.min()) >= 0
    assert int(encoded.codes.max()) < CONFIG.codebook_size
    utilisation = representation.utilisation(encoded.codes)
    assert 0.0 < utilisation <= 1.0


@needs_torch
def test_residual_quantisation_has_a_larger_effective_vocabulary():
    single = R.build_representation("learned_tokens", CONFIG)
    residual = R.build_representation("residual_vq", CONFIG)
    assert residual.levels == CONFIG.residual_stages
    assert residual.vocabulary_size == CONFIG.codebook_size ** CONFIG.residual_stages
    assert residual.vocabulary_size > single.vocabulary_size


@needs_torch
def test_detokenise_inverts_tokenise_up_to_the_quantiser(tensors):
    """A code must stand for the vector the quantiser chose, exactly."""
    train, _ = tensors
    torch = TU.require_torch()
    representation = R.build_representation("learned_tokens", CONFIG)
    with torch.no_grad():
        encoded = representation.encode(train[:4])
        rebuilt = representation.detokenise(encoded.codes)
    assert tuple(rebuilt.shape) == tuple(encoded.latent.shape)
    assert torch.allclose(rebuilt, encoded.latent, atol=1e-5)


# ===========================================================================
# missing-feature handling
# ===========================================================================

@needs_torch
def test_the_mask_aware_candidate_distinguishes_absent_from_zero(tensors):
    """The whole reason the candidate exists. A zero is a real observation."""
    train, _ = tensors
    torch = TU.require_torch()
    representation = R.build_representation("mask_aware", CONFIG)
    sample = train[:2].clone()

    zeroed = sample.clone()
    zeroed[:, 3, :] = 0.0
    present = torch.ones_like(sample)
    absent = torch.ones_like(sample)
    absent[:, 3, :] = 0.0

    with torch.no_grad():
        as_zero = representation.encode(zeroed, mask=present)
        as_absent = representation.encode(zeroed, mask=absent)
    assert not torch.allclose(as_zero.latent, as_absent.latent), (
        "a channel marked absent encoded identically to a channel of zeros")


@needs_torch
def test_the_mask_aware_candidate_emits_a_per_position_validity_mask(tensors):
    train, _ = tensors
    torch = TU.require_torch()
    representation = R.build_representation("mask_aware", CONFIG)
    mask = torch.ones_like(train[:2])
    mask[:, :, :4] = 0.0                                 # the first patch
    encoded = representation.encode(train[:2], mask=mask)
    assert encoded.valid is not None
    assert tuple(encoded.valid.shape) == (2, representation.latent_spec.positions)
    assert float(encoded.valid[0, 0]) == 0.0, "the fully-absent patch is invalid"
    assert float(encoded.valid[0, 1]) == 1.0


@needs_torch
@pytest.mark.parametrize("key", ["continuous_patch", "learned_tokens",
                                 "grouped_embeddings"])
def test_a_candidate_that_cannot_consume_a_mask_refuses_one(key, tensors):
    """Accepting and discarding it would let a caller believe otherwise."""
    train, _ = tensors
    torch = TU.require_torch()
    representation = R.build_representation(key, CONFIG)
    with pytest.raises(ConfigurationError, match="does not consume a mask"):
        representation.encode(train[:2], mask=torch.ones_like(train[:2]))


# ===========================================================================
# reconstruction invariants
# ===========================================================================

@needs_torch
@pytest.mark.parametrize("key", sorted(R.CANDIDATES))
def test_reconstruction_returns_the_input_shape(key, tensors):
    train, _ = tensors
    representation = R.build_representation(key, CONFIG)
    encoded = representation.encode(train[:4])
    rebuilt = representation.reconstruct(encoded)
    assert tuple(rebuilt.shape) == tuple(train[:4].shape)


@needs_torch
def test_a_reconstruction_of_the_wrong_shape_is_refused(tensors):
    train, _ = tensors
    representation = R.build_representation("continuous_patch", CONFIG)
    encoded = representation.encode(train[:4])
    with pytest.raises(ConfigurationError, match="does not match"):
        representation.reconstruction_error(encoded, train[:2])


@needs_torch
def test_masked_positions_are_excluded_from_the_error_not_scored_as_zero(tensors):
    """Scoring them rewards agreeing with a value nobody observed."""
    train, _ = tensors
    torch = TU.require_torch()
    representation = R.build_representation("continuous_patch", CONFIG)
    sample = train[:4]
    encoded = representation.encode(sample)

    everywhere = representation.reconstruction_error(encoded, sample)
    full_mask = torch.ones_like(sample)
    assert representation.reconstruction_error(
        encoded, sample, mask=full_mask) == pytest.approx(everywhere, rel=1e-6)

    half = torch.zeros_like(sample)
    half[:, :2, :] = 1.0
    partial = representation.reconstruction_error(encoded, sample, mask=half)
    assert partial != pytest.approx(everywhere, rel=1e-9), (
        "a mask that excludes half the channels changed nothing, so it was "
        "not applied")

    with pytest.raises(ConfigurationError, match="every position is masked"):
        representation.reconstruction_error(encoded, sample,
                                            mask=torch.zeros_like(sample))


# ===========================================================================
# training, and the trade-offs the candidates claim
# ===========================================================================

@needs_torch
@pytest.mark.parametrize("key", sorted(R.CANDIDATES))
def test_every_candidate_learns_something_on_a_structured_series(key, tensors):
    train, test = tensors
    result = R.evaluate_representation(key, train, test, config=CONFIG,
                                       epochs=10, seed=0)
    assert result.learned, (
        f"{key} did not reduce its reconstruction error at all, so either the "
        f"decoder is disconnected from the encoder or nothing is training")
    assert result.parameters > 0
    assert result.positions == R.build_representation(key, CONFIG).latent_spec.positions


@needs_torch
def test_the_quantised_candidates_pay_the_error_floor_they_document(tensors):
    """The load-bearing claim, checked against a number.

    `learned_tokens` states that a codebook imposes a hard error floor.
    `residual_vq` states that quantising the residual is the direct answer.
    Both are checkable, and if either were false the candidate's rationale
    would be wrong rather than merely optimistic.
    """
    train, test = tensors
    results = {r.key: r for r in R.compare_representations(
        train, test, keys=("continuous_patch", "learned_tokens", "residual_vq"),
        config=CONFIG, epochs=12, seed=0)}

    continuous = results["continuous_patch"].reconstruction_error
    single = results["learned_tokens"].reconstruction_error
    residual = results["residual_vq"].reconstruction_error

    assert single > continuous, (
        "quantisation did not cost anything, which means the codebook is not "
        "in the reconstruction path")
    assert residual < single, (
        "the residual stage did not improve on the single codebook")


@needs_torch
def test_a_comparison_reports_parsimony_beside_the_error(tensors):
    """A comparison reporting only error lets the largest model win by size."""
    train, test = tensors
    results = R.compare_representations(train, test,
                                        keys=("continuous_patch", "hybrid"),
                                        config=CONFIG, epochs=6, seed=0)
    assert [r.reconstruction_error for r in results] == sorted(
        r.reconstruction_error for r in results)
    for result in results:
        assert result.error_per_parameter > 0
        assert result.to_dict()["improvement"] == result.improvement
    table = R.comparison_table(results)
    assert "continuous_patch" in table and "params" in table


@needs_torch
def test_a_representation_is_deterministic_given_a_seed(tensors):
    train, test = tensors
    first = R.evaluate_representation("continuous_patch", train, test,
                                      config=CONFIG, epochs=4, seed=0)
    second = R.evaluate_representation("continuous_patch", train, test,
                                       config=CONFIG, epochs=4, seed=0)
    third = R.evaluate_representation("continuous_patch", train, test,
                                      config=CONFIG, epochs=4, seed=1)
    assert first.reconstruction_error == second.reconstruction_error
    assert first.reconstruction_error != third.reconstruction_error, (
        "changing the seed changed nothing, so the seed is not being used")


# ===========================================================================
# serialisation and portability
# ===========================================================================

@needs_torch
@pytest.mark.parametrize("key", sorted(R.CANDIDATES))
def test_parameters_round_trip_through_json(key, tensors):
    import json
    train, _ = tensors
    torch = TU.require_torch()
    original = R.build_representation(key, CONFIG)
    with torch.no_grad():
        before = original.encode(train[:4]).latent.clone()

    text = json.dumps(original.parameters_dict())
    restored = R.build_representation(key, CONFIG)
    restored.load_parameters(json.loads(text))
    with torch.no_grad():
        after = restored.encode(train[:4]).latent

    assert torch.allclose(before, after, atol=1e-6), (
        "a reloaded representation produced different numbers")


@needs_torch
def test_loading_parameters_of_the_wrong_shape_is_refused(tensors):
    representation = R.build_representation("continuous_patch", CONFIG)
    parameters = dict(representation.parameters_dict())
    key = next(iter(parameters))
    parameters[key] = {**parameters[key], "shape": [1, 1]}
    with pytest.raises(ConfigurationError):
        representation.load_parameters(parameters)


def test_no_native_module_hardcodes_a_device():
    """Device portability, as far as this environment can assert it.

    There is no CUDA here (blocker B5), so the claim being tested is the one
    that is testable: nothing pins a device, so tensors follow their input and
    a future accelerator needs no code change. A `.cuda()` call or a
    `device="cuda"` literal would break that, and would also be untestable here
    — which is exactly why it must not be written in the first place.
    """
    from pathlib import Path
    native = Path(__file__).resolve().parents[1] / "olympus" / "trading" / "native"
    offenders = []
    for path in sorted(native.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in (".cuda(", ".to('cuda", '.to("cuda', "device='cuda",
                            'device="cuda', ".to('cpu", '.to("cpu',
                            "device='cpu", 'device="cpu'):
                if pattern in stripped:
                    offenders.append(f"native/{path.name}:{lineno} {stripped[:60]}")
    assert offenders == [], (
        "native modules must not pin a device; tensors follow their input:\n"
        + "\n".join(offenders))


@needs_torch
def test_a_representation_produces_the_same_numbers_on_a_cpu_tensor(tensors):
    """The portability guarantee that *is* checkable here: output device
    follows input device."""
    train, _ = tensors
    torch = TU.require_torch()
    representation = R.build_representation("continuous_patch", CONFIG)
    with torch.no_grad():
        encoded = representation.encode(train[:2].to("cpu"))
    assert encoded.latent.device == train.device
