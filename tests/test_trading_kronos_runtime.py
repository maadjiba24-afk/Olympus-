"""Tests for Kronos checkpoint identity, loading, and compatibility.

Every test here runs WITHOUT torch, without a network, and without 25MB of
weights: loading is driven through an injected `CheckpointLoader` that hands
back trivial fake objects, so the verification logic — which is the part that
protects the audit trail — is exercised in full on a bare CI runner.

The through-line: an unverifiable checkpoint is not loadable. Not loadable with
a warning, not loadable in a degraded mode — refused, because
`ForecastResult.model_version` is a claim about which weights produced a number
and a claim nobody checked is worse than no claim at all.
"""

import hashlib
import sys
from datetime import datetime, timezone

import pytest

from olympus.trading.errors import (
    CheckpointVerificationError,
    ConfigurationError,
    DependencyMissing,
    IncompatibleModelError,
    ModelNotAvailable,
)
from olympus.trading.kronos_runtime import (
    DEFAULT_PREDICTOR_PIN,
    DEFAULT_TOKENIZER_PIN,
    FEATURE_COLUMNS,
    KRONOS_BASE,
    KRONOS_MINI,
    KRONOS_SMALL,
    PINS,
    TOKENIZER_2K,
    TOKENIZER_BASE,
    CheckpointPin,
    KronosRuntime,
    ModelBackend,
    SamplingParams,
    assert_symmetric_bits,
    calendar_stamps,
    checkpoint_cache_size,
    clear_checkpoint_cache,
    default_loader,
    load_predictor,
    load_tokenizer,
    normalise_device,
    pin_for,
    select_device,
    sha256_file,
    unpinned_repos,
    verify_compatibility,
)

# The two revisions upstream's own regression test pins
# (tests/test_kronos_regression.py:31-32 at commit 67b630e). Repeated verbatim
# here so a careless edit to the registry fails a test rather than silently
# repointing every forecast this system will ever record.
UPSTREAM_TOKENIZER_REV = "0e0117387f39004a9016484a186a908917e22426"
UPSTREAM_MODEL_REV = "901c26c1332695a2a8f243eb2f37243a37bea320"

OTHER_REV = "a" * 40


class FakeModel:
    """Minimal stand-in exposing only the attributes verification reads."""

    def __init__(self, *, s1_bits=10, s2_bits=10, d_in=None,
                 s1_vocab_size=None):
        self.s1_bits = s1_bits
        self.s2_bits = s2_bits
        if d_in is not None:
            self.d_in = d_in
        if s1_vocab_size is not None:
            self.s1_vocab_size = s1_vocab_size


def fake_tokenizer(**kwargs):
    kwargs.setdefault("d_in", 6)
    return FakeModel(**kwargs)


def fake_predictor(**kwargs):
    kwargs.setdefault("s1_vocab_size", 2 ** kwargs.get("s1_bits", 10))
    return FakeModel(**kwargs)


def loader_for(model, **meta):
    def loader(pin, device):
        return model, dict(meta)
    return loader


def pair_loader(tokenizer=None, predictor=None, **meta):
    tokenizer = tokenizer if tokenizer is not None else fake_tokenizer()
    predictor = predictor if predictor is not None else fake_predictor()

    def loader(pin, device):
        return (tokenizer if pin.kind == "tokenizer" else predictor), dict(meta)
    return loader


@pytest.fixture(autouse=True)
def clean_cache():
    """The checkpoint cache is process-level; tests must not share entries."""
    clear_checkpoint_cache()
    yield
    clear_checkpoint_cache()


# --- pins -------------------------------------------------------------------

def test_pin_requires_a_full_git_sha():
    with pytest.raises(ConfigurationError) as excinfo:
        CheckpointPin(repo_id="x/y", kind="tokenizer", revision="main")
    assert "40-hex" in str(excinfo.value)


@pytest.mark.parametrize("revision", ["master", "HEAD", "v1.0", "0e01173",
                                      "z" * 40, "0E0117387F39004A9016484A186A908917E22426X"])
def test_pin_rejects_anything_that_is_not_a_full_sha(revision):
    with pytest.raises(ConfigurationError):
        CheckpointPin(repo_id="x/y", kind="predictor", revision=revision)


def test_pin_accepts_and_lowercases_a_full_sha():
    pin = CheckpointPin(repo_id="x/y", kind="predictor",
                        revision=UPSTREAM_MODEL_REV.upper())
    assert pin.revision == UPSTREAM_MODEL_REV
    assert pin.pinned is True
    assert pin.model_version == "predictor:x/y@901c26c13326"


def test_pin_rejects_unknown_kind():
    with pytest.raises(ConfigurationError):
        CheckpointPin(repo_id="x/y", kind="encoder")


def test_pin_rejects_malformed_sha256():
    with pytest.raises(ConfigurationError):
        CheckpointPin(repo_id="x/y", kind="tokenizer", sha256="deadbeef")


def test_teardown_12_2_asymmetric_bits_rejected_by_pin():
    """§12.2 — indices_to_bits(half=True) masks both halves with
    codebook_dim//2 bits, so an asymmetric split decodes to wrong numbers with
    no error anywhere. The pin refuses to describe such a checkpoint."""
    with pytest.raises(ConfigurationError) as excinfo:
        CheckpointPin(repo_id="x/y", kind="tokenizer",
                      expected_s1_bits=6, expected_s2_bits=4)
    assert "12.2" in str(excinfo.value)


def test_teardown_12_2_symmetric_bits_accepted():
    pin = CheckpointPin(repo_id="x/y", kind="tokenizer",
                        expected_s1_bits=10, expected_s2_bits=10)
    assert pin.expected_s1_bits == pin.expected_s2_bits == 10


def test_assert_symmetric_bits_is_the_shared_rule():
    assert_symmetric_bits(8, 8, where="unit")
    with pytest.raises(ConfigurationError):
        assert_symmetric_bits(8, 9, where="unit")


def test_registry_carries_the_upstream_pinned_revisions():
    assert TOKENIZER_BASE.repo_id == "NeoQuasar/Kronos-Tokenizer-base"
    assert TOKENIZER_BASE.revision == UPSTREAM_TOKENIZER_REV
    assert KRONOS_SMALL.repo_id == "NeoQuasar/Kronos-small"
    assert KRONOS_SMALL.revision == UPSTREAM_MODEL_REV
    assert DEFAULT_TOKENIZER_PIN is TOKENIZER_BASE
    assert DEFAULT_PREDICTOR_PIN is KRONOS_SMALL
    assert PINS[TOKENIZER_BASE.repo_id] is TOKENIZER_BASE


def test_registry_marks_the_other_public_checkpoints_unpinned():
    assert TOKENIZER_2K.pinned is False
    assert KRONOS_MINI.pinned is False
    assert KRONOS_BASE.pinned is False
    assert set(unpinned_repos()) == {
        "NeoQuasar/Kronos-Tokenizer-2k", "NeoQuasar/Kronos-mini",
        "NeoQuasar/Kronos-base"}


def test_registry_records_the_model_zoo_context_lengths():
    assert TOKENIZER_BASE.max_context == 512
    assert KRONOS_SMALL.max_context == 512
    assert KRONOS_BASE.max_context == 512
    assert KRONOS_MINI.max_context == 2048
    assert TOKENIZER_2K.max_context == 2048


def test_pin_for_refuses_to_invent_an_unknown_checkpoint():
    assert pin_for("NeoQuasar/Kronos-small") is KRONOS_SMALL
    with pytest.raises(ConfigurationError):
        pin_for("NeoQuasar/Kronos-large")


def test_pin_to_dict_is_json_safe():
    import json
    json.dumps(KRONOS_SMALL.to_dict())


# --- loading refusals -------------------------------------------------------

def test_unpinned_checkpoint_is_refused():
    with pytest.raises(CheckpointVerificationError) as excinfo:
        load_predictor(KRONOS_MINI, "cpu", loader=loader_for(fake_predictor()))
    assert "unpinned" in str(excinfo.value)


def test_operator_may_pin_an_unpinned_checkpoint_explicitly():
    pin = KRONOS_MINI.with_revision(OTHER_REV)
    model, identity = load_predictor(
        pin, "cpu", loader=loader_for(fake_predictor()))
    assert identity["revision"] == OTHER_REV
    assert identity["repo_id"] == "NeoQuasar/Kronos-mini"


def test_revision_mismatch_is_fatal():
    loader = loader_for(fake_predictor(), resolved_revision=OTHER_REV)
    with pytest.raises(CheckpointVerificationError) as excinfo:
        load_predictor(KRONOS_SMALL, "cpu", loader=loader)
    assert excinfo.value.details["expected"] == UPSTREAM_MODEL_REV
    assert excinfo.value.details["resolved"] == OTHER_REV


def test_pin_kind_must_match_the_call():
    with pytest.raises(ConfigurationError):
        load_predictor(TOKENIZER_BASE, "cpu",
                       loader=loader_for(fake_tokenizer()))
    with pytest.raises(ConfigurationError):
        load_tokenizer(KRONOS_SMALL, "cpu", loader=loader_for(fake_predictor()))


def test_loader_returning_nothing_is_a_model_not_available():
    with pytest.raises(ModelNotAvailable):
        load_predictor(KRONOS_SMALL, "cpu", loader=lambda pin, device: (None, {}))


def test_geometry_mismatch_against_the_pin_is_fatal():
    """The pin's bit widths are inferred (teardown 10.2 marks them [I]); a
    disagreement means either the guess or the weights changed, and both make
    the recorded model_version describe something else."""
    loader = loader_for(fake_predictor(s1_bits=8, s2_bits=8, s1_vocab_size=256))
    with pytest.raises(CheckpointVerificationError) as excinfo:
        load_predictor(KRONOS_SMALL, "cpu", loader=loader)
    assert excinfo.value.details["attribute"] == "s1_bits"


def test_teardown_12_2_asymmetric_checkpoint_rejected_at_load():
    """A checkpoint whose own bits are asymmetric is refused even when the pin
    declares no expectation — the corruption is a property of the weights."""
    pin = CheckpointPin(repo_id="local/asym", kind="tokenizer",
                        revision=OTHER_REV, max_context=512)
    loader = loader_for(FakeModel(s1_bits=6, s2_bits=4, d_in=6))
    with pytest.raises(ConfigurationError) as excinfo:
        load_tokenizer(pin, "cpu", loader=loader)
    assert "12.2" in str(excinfo.value)


# --- hashing ----------------------------------------------------------------

def test_sha256_verification_passes_and_is_recorded(tmp_path):
    blob = tmp_path / "model.safetensors"
    blob.write_bytes(b"kronos weights")
    digest = hashlib.sha256(b"kronos weights").hexdigest()
    assert sha256_file(str(blob)) == digest

    pin = KRONOS_SMALL.with_sha256(digest)
    _, identity = load_predictor(
        pin, "cpu",
        loader=loader_for(fake_predictor(), weights_path=str(blob)))
    assert identity["sha256"] == digest


def test_sha256_mismatch_is_fatal(tmp_path):
    blob = tmp_path / "model.safetensors"
    blob.write_bytes(b"tampered")
    pin = KRONOS_SMALL.with_sha256("0" * 64)
    with pytest.raises(CheckpointVerificationError):
        load_predictor(pin, "cpu",
                       loader=loader_for(fake_predictor(),
                                         weights_path=str(blob)))


def test_declared_sha256_with_no_hashable_file_is_fatal():
    pin = KRONOS_SMALL.with_sha256("0" * 64)
    with pytest.raises(CheckpointVerificationError) as excinfo:
        load_predictor(pin, "cpu", loader=loader_for(fake_predictor()))
    assert "cannot be verified" in str(excinfo.value)


# --- identity ---------------------------------------------------------------

def test_identity_carries_every_provenance_field():
    _, identity = load_predictor(
        KRONOS_SMALL, "cpu",
        loader=loader_for(fake_predictor(), torch_version="2.13.0"))
    for key in ("repo_id", "revision", "resolved_revision", "sha256", "device",
                "torch_version", "model_version", "kind", "max_context"):
        assert key in identity, key
    assert identity["repo_id"] == "NeoQuasar/Kronos-small"
    assert identity["revision"] == UPSTREAM_MODEL_REV
    assert identity["resolved_revision"] == UPSTREAM_MODEL_REV
    assert identity["device"] == "cpu"
    assert identity["torch_version"] == "2.13.0"
    assert identity["model_version"].endswith("@901c26c13326")


# --- compatibility ----------------------------------------------------------

def test_compatible_pair_reports_its_geometry():
    geometry = verify_compatibility(fake_tokenizer(), fake_predictor())
    assert geometry == {"s1_bits": 10, "s2_bits": 10, "d_in": 6,
                        "s1_vocab_size": 1024, "s2_vocab_size": 1024}


def test_teardown_12_2_asymmetric_bits_rejected():
    """Regression for the silent-corruption defect: `s1_bits != s2_bits` must
    raise ConfigurationError, not IncompatibleModelError and not nothing."""
    tokenizer = fake_tokenizer(s1_bits=6, s2_bits=4)
    predictor = fake_predictor(s1_bits=6, s2_bits=4, s1_vocab_size=64)
    with pytest.raises(ConfigurationError) as excinfo:
        verify_compatibility(tokenizer, predictor)
    assert "12.2" in str(excinfo.value)


def test_incompatible_bit_split_raises_incompatible_model():
    tokenizer = fake_tokenizer(s1_bits=10, s2_bits=10)
    predictor = fake_predictor(s1_bits=8, s2_bits=8, s1_vocab_size=256)
    with pytest.raises(IncompatibleModelError):
        verify_compatibility(tokenizer, predictor)


def test_incompatible_feature_width_raises_incompatible_model():
    with pytest.raises(IncompatibleModelError) as excinfo:
        verify_compatibility(fake_tokenizer(d_in=5), fake_predictor())
    assert excinfo.value.details["expected"] == len(FEATURE_COLUMNS)


def test_incompatible_vocabulary_raises_incompatible_model():
    predictor = fake_predictor(s1_bits=10, s2_bits=10, s1_vocab_size=999)
    with pytest.raises(IncompatibleModelError):
        verify_compatibility(fake_tokenizer(), predictor)


def test_model_without_bit_attributes_cannot_be_verified():
    class Opaque:
        pass

    with pytest.raises(IncompatibleModelError):
        verify_compatibility(Opaque(), fake_predictor())


# --- devices ----------------------------------------------------------------

@pytest.mark.parametrize("device", ["cpu", "mps", "cuda", "cuda:0", "cuda:3"])
def test_valid_device_strings(device):
    assert normalise_device(device) == device


@pytest.mark.parametrize("device", ["gpu", "cuda:", "CUDA", "cuda:x", "", None,
                                    "tpu:0"])
def test_invalid_device_strings_rejected_without_torch(device):
    with pytest.raises(ConfigurationError):
        normalise_device(device)


def test_explicit_device_survives_a_missing_torch(monkeypatch):
    """The shape check must not require torch: CI validates configuration in a
    process that has no CUDA and no torch at all."""
    monkeypatch.setitem(sys.modules, "torch", None)
    assert select_device("cuda:0") == "cuda:0"


def test_device_auto_detection_without_torch_is_dependency_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(DependencyMissing) as excinfo:
        select_device(None)
    assert excinfo.value.code == "DEPENDENCY_MISSING"


def test_default_loader_without_torch_is_dependency_missing(monkeypatch):
    """Never a bare ImportError — the audit trail needs a code."""
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(DependencyMissing):
        default_loader(KRONOS_SMALL, "cpu")


# --- calendar features ------------------------------------------------------

def test_calendar_stamps_match_the_five_temporal_features():
    ts = datetime(2026, 3, 2, 14, 35, tzinfo=timezone.utc)   # a Monday
    assert calendar_stamps([ts]) == ((35, 14, 0, 2, 3),)


def test_calendar_stamps_reject_naive_datetimes():
    from olympus.trading.errors import DataValidationError
    with pytest.raises(DataValidationError):
        calendar_stamps([datetime(2026, 3, 2, 14, 35)])


# --- runtime ----------------------------------------------------------------

def test_runtime_loads_verifies_and_exposes_a_backend():
    runtime = KronosRuntime.load(device="cpu", loader=pair_loader(
        torch_version="2.13.0"))
    try:
        assert runtime.device == "cpu"
        assert runtime.max_context == 512
        assert runtime.closed is False
        assert isinstance(runtime.backend(), ModelBackend)
        identity = runtime.identity
        assert identity["geometry"]["s1_bits"] == 10
        assert identity["tokenizer"]["repo_id"] == TOKENIZER_BASE.repo_id
        assert identity["predictor"]["repo_id"] == KRONOS_SMALL.repo_id
        assert UPSTREAM_MODEL_REV[:12] in identity["model_version"]
        assert UPSTREAM_TOKENIZER_REV[:12] in identity["model_version"]
    finally:
        runtime.close()


def test_runtime_takes_the_shorter_max_context_of_the_pair():
    """A 2k tokenizer paired with a 512 predictor can only be driven 512 deep."""
    runtime = KronosRuntime.load(
        tokenizer_pin=TOKENIZER_2K.with_revision(OTHER_REV),
        predictor_pin=KRONOS_SMALL, device="cpu", loader=pair_loader())
    try:
        assert runtime.max_context == 512
    finally:
        runtime.close()


def test_runtime_refuses_an_incompatible_pair():
    loader = pair_loader(tokenizer=fake_tokenizer(s1_bits=10, s2_bits=10),
                         predictor=fake_predictor(s1_bits=8, s2_bits=8,
                                                  s1_vocab_size=256))
    pin = KRONOS_SMALL.with_revision(OTHER_REV)
    # The pin's expectations would catch this first, so use a bespoke pin that
    # declares nothing and let verify_compatibility be the thing that fires.
    bespoke = CheckpointPin(repo_id="local/pred", kind="predictor",
                            revision=OTHER_REV, max_context=512)
    assert pin.expected_s1_bits == 10
    with pytest.raises(IncompatibleModelError):
        KronosRuntime.load(predictor_pin=bespoke, device="cpu", loader=loader)


def test_runtime_releases_checkpoints_when_an_incompatible_pair_is_refused():
    loader = pair_loader(predictor=fake_predictor(s1_bits=8, s2_bits=8,
                                                  s1_vocab_size=256))
    bespoke = CheckpointPin(repo_id="local/pred", kind="predictor",
                            revision=OTHER_REV, max_context=512)
    with pytest.raises(IncompatibleModelError):
        KronosRuntime.load(predictor_pin=bespoke, device="cpu", loader=loader)
    assert checkpoint_cache_size() == 0


def test_runtime_close_is_idempotent_and_blocks_further_use():
    runtime = KronosRuntime.load(device="cpu", loader=pair_loader())
    runtime.close()
    runtime.close()
    assert runtime.closed is True
    with pytest.raises(ModelNotAvailable):
        runtime.tokenizer
    with pytest.raises(ModelNotAvailable):
        runtime.backend()


def test_runtime_is_a_context_manager():
    with KronosRuntime.load(device="cpu", loader=pair_loader()) as runtime:
        assert runtime.closed is False
    assert runtime.closed is True


# --- process cache ----------------------------------------------------------

def test_checkpoints_are_cached_by_repo_revision_and_device():
    calls = []
    tokenizer, predictor = fake_tokenizer(), fake_predictor()

    def counting_loader(pin, device):
        calls.append((pin.repo_id, device))
        return (tokenizer if pin.kind == "tokenizer" else predictor), {}

    first = KronosRuntime.load(device="cpu", loader=counting_loader)
    second = KronosRuntime.load(device="cpu", loader=counting_loader)
    try:
        assert len(calls) == 2                     # tokenizer + predictor, once
        assert first.tokenizer is second.tokenizer
        assert checkpoint_cache_size() == 2
    finally:
        first.close()
        second.close()


def test_cache_entries_are_released_only_when_the_last_holder_closes():
    loader = pair_loader()
    first = KronosRuntime.load(device="cpu", loader=loader)
    second = KronosRuntime.load(device="cpu", loader=loader)
    assert checkpoint_cache_size() == 2
    first.close()
    assert checkpoint_cache_size() == 2            # `second` still holds them
    second.close()
    assert checkpoint_cache_size() == 0


def test_a_different_device_is_a_different_cache_entry(monkeypatch):
    # torch is hidden so the device *probe* stays out of the way; this test is
    # about cache keying, and it must behave the same on a CI box with no
    # accelerator as on a workstation with one.
    monkeypatch.setitem(sys.modules, "torch", None)
    loader = pair_loader()
    cpu = KronosRuntime.load(device="cpu", loader=loader)
    cuda = KronosRuntime.load(device="cuda:0", loader=loader)
    try:
        assert checkpoint_cache_size() == 4
    finally:
        cpu.close()
        cuda.close()


# --- the torch backend ------------------------------------------------------

def test_torch_backend_without_torch_is_dependency_missing(monkeypatch):
    runtime = KronosRuntime.load(device="cpu", loader=pair_loader())
    backend = runtime.backend()
    monkeypatch.setitem(sys.modules, "torch", None)
    try:
        with pytest.raises(DependencyMissing):
            backend.generate([[0.0] * 6] * 4, context_stamps=((0, 0, 0, 1, 1),) * 4,
                             future_stamps=((0, 0, 0, 1, 1),), horizon=1,
                             n_samples=1, params=SamplingParams())
    finally:
        runtime.close()


def test_sampling_params_round_trip():
    params = SamplingParams(temperature=0.8, top_k=20, seed=7)
    assert params.to_dict() == {"temperature": 0.8, "top_k": 20, "top_p": None,
                                "seed": 7, "clip": 5.0}


def test_model_backend_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        ModelBackend()


def test_runtime_module_ships_no_synthetic_backend():
    """Upstream ships a 'backtester' that never calls the model (teardown
    12.14). A fake that lives in the product eventually gets wired into a
    decision, so the only concrete backend here must be the real one."""
    import olympus.trading.kronos_runtime as runtime_module
    concrete = [name for name, obj in vars(runtime_module).items()
                if isinstance(obj, type) and issubclass(obj, ModelBackend)
                and obj is not ModelBackend]
    assert concrete == ["TorchKronosBackend"]
