"""Kronos checkpoint identity, loading, and the model boundary.

This module owns everything about *which weights are running* and nothing about
what a forecast means. `kronos_adapter` owns the meaning. The split exists
because the two failure modes are completely different: a wrong checkpoint is a
provenance failure that invalidates the audit trail, while a wrong forecast is a
modelling failure that the risk engine is there to survive.

No Kronos source is vendored here. Olympus drives the upstream classes through
their public API (`from_pretrained`, `encode`, `decode`, `decode_s1`,
`decode_s2`) and supplies its own generation loop, because two of the upstream
loop's behaviours are defects we must not inherit — see the repairs below.

Why pinning is not optional
---------------------------
`ForecastResult.model_version` is written into the immutable trail. If the
checkpoint behind it can change under the same name, that field is a lie and
every recorded decision becomes unreproducible. So a revision is a *required*
part of a `CheckpointPin`, it must be a full 40-hex git sha, and a bare branch
name like ``"main"`` is refused rather than resolved. The two revisions in
`PINS` below are the ones upstream's own regression test pins
(`tests/test_kronos_regression.py:31-32` at commit ``67b630e``), which makes
them the only publicly known-good pair.

The other three public checkpoints (Kronos-mini, Kronos-Tokenizer-2k,
Kronos-base) are registered *without* revisions. They are real and usable, but
nobody has published a verified revision for them, so `load_*` refuses them
until an operator supplies one explicitly via `CheckpointPin.with_revision`.
Refusing beats silently tracking a moving branch.

Repairs applied at this layer
-----------------------------
* **teardown §12.2 — asymmetric bits silently corrupt.**
  `KronosTokenizer.indices_to_bits(half=True)` builds its bit mask from
  ``codebook_dim // 2`` (`model/kronos.py:129`) instead of from `s1_bits` /
  `s2_bits`, so any configuration where the two differ round-trips to the wrong
  numbers with no error. `CheckpointPin` and `verify_compatibility` both raise
  `ConfigurationError` on ``s1_bits != s2_bits``. This is the single most
  important guard in the file: the upstream failure is silent, and a silent
  numerical corruption inside a trading system is unbounded loss.

* **teardown §12.1 — `top_k` and `top_p` do not compose.**
  `top_k_top_p_filtering` returns immediately after the top-k branch
  (`model/kronos.py:347-352`), so `top_p` is ignored whenever ``top_k > 0``
  despite the docstring promising "top-k and/or nucleus". `KronosConfig`
  rejects configurations that set both — that is the primary repair. The
  sampler in `TorchKronosBackend` additionally implements the composition
  correctly, so the two agree instead of one silently overriding the other.

* **teardown §12.7 — sampled paths are averaged inside inference.**
  `auto_regressive_inference` collapses `sample_count` draws to a mean before
  returning (`model/kronos.py:467`), destroying exactly the dispersion a risk
  engine needs. `ModelBackend.generate` returns every path.

* **teardown §12.6 — `pred_len >= max_context` raises an opaque pandas error.**
  The final decode window is capped at `max_context`, so the caller gets
  ``Shape of passed values is (16, 6), indices imply (20, 6)``. Guarded in
  `KronosConfig` and again in the backend with a named error.

What is deliberately NOT here
-----------------------------
There is no built-in synthetic/fake backend shipped in this module. The
teardown found upstream shipping a "backtester" whose `simple_prediction` is a
random walk (§12.14) — anyone reading its output was reading noise. A fake that
lives in the product is a fake that eventually gets wired into a decision, so
the deterministic fakes live in the test suite where they cannot be imported by
accident. `ModelBackend` is an ABC precisely so tests can supply their own.

Import discipline: torch and numpy are imported *inside* functions and their
absence raises `DependencyMissing`, never a bare `ImportError`. Everything in
this module that does not touch weights — pins, compatibility rules, device
string validation, calendar features — is pure stdlib and runs in CI.
"""

from __future__ import annotations

import abc
import hashlib
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .contracts import ensure_utc
from .errors import (
    CheckpointVerificationError,
    ConfigurationError,
    DependencyMissing,
    IncompatibleModelError,
    ModelNotAvailable,
)

#: The feature vector Kronos consumes and emits, in order. Fixed by the
#: pretrained weights (`d_in == 6`); it is not a configuration knob.
FEATURE_COLUMNS: tuple[str, ...] = (
    "open", "high", "low", "close", "volume", "amount")

#: The five calendar features `TemporalEmbedding` expects, in order. Derived
#: from timestamps rather than taken from the caller so a forecast cannot be
#: conditioned on a stamp that disagrees with its own bars.
STAMP_COLUMNS: tuple[str, ...] = ("minute", "hour", "weekday", "day", "month")

#: A revision must be a full git object id. Abbreviated shas are refused: they
#: are ambiguous by construction (an abbreviation that is unique today can stop
#: being unique tomorrow), and an ambiguous provenance record is not a record.
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: `cpu`, `mps`, `cuda`, or `cuda:<index>`. Validated by shape here and by
#: existence in `select_device` when torch is present.
_DEVICE = re.compile(r"^(cpu|mps|cuda(:[0-9]+)?)$")

_VALID_KINDS = ("tokenizer", "predictor")


# ---------------------------------------------------------------------------
# lazy backends
# ---------------------------------------------------------------------------

def _import_torch():
    """Import torch or raise the typed error.

    A bare `ImportError` escaping into a trading process is indistinguishable
    from a typo; `DependencyMissing` carries a code the audit trail and the
    operator UI can both act on.
    """
    try:
        import torch  # noqa: F401  (returned to the caller)
    except Exception as exc:                     # ImportError, or a broken build
        raise DependencyMissing(
            "torch is required to load or run Kronos checkpoints; install the "
            "'kronos' extra. The rest of olympus.trading runs without it.",
            package="torch", extra="kronos") from exc
    return torch


def _optional_torch_version() -> str | None:
    """torch's version when it is importable, else None.

    Used only to stamp `identity`. It must never raise: a test harness driving
    a fake loader has no torch and is still entitled to a complete identity
    record (with the version recorded as unknown rather than fabricated).
    """
    try:
        import torch
        return str(torch.__version__)
    except Exception:
        return None


def _import_upstream_kronos():
    """Import the upstream Kronos model package.

    Upstream is not an installable package — no `setup.py`, no
    `pyproject.toml`, no root `__init__.py` (teardown §1) — so it is reachable
    only if the operator has put the clone on `sys.path`. Both plausible import
    names are tried before giving up.
    """
    import importlib
    for name in ("kronos.model", "model"):
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        if hasattr(module, "KronosTokenizer") and hasattr(module, "Kronos"):
            return module
    raise DependencyMissing(
        "the upstream Kronos model package is not importable. Olympus does not "
        "vendor Kronos source; clone github.com/shiyu-coder/Kronos and put it "
        "on sys.path, or pass an explicit loader= to load_tokenizer/"
        "load_predictor.",
        package="kronos-model", extra="kronos")


# ---------------------------------------------------------------------------
# device selection
# ---------------------------------------------------------------------------

def normalise_device(device: str) -> str:
    """Validate the *shape* of a device string without importing torch.

    Split from `select_device` so `KronosConfig` can reject ``"gpu0"`` at
    construction time in a CI process that has no torch at all.
    """
    if not isinstance(device, str) or not _DEVICE.match(device.strip()):
        raise ConfigurationError(
            "device must be one of 'cpu', 'mps', 'cuda', or 'cuda:<index>'",
            got=repr(device))
    return device.strip()


def select_device(requested: str | None = None, *, probe: bool = True) -> str:
    """Resolve the device to run on, preferring cuda → mps → cpu.

    Auto-detection needs torch and therefore raises `DependencyMissing` without
    it. An *explicitly requested* device is shape-checked always and
    existence-checked whenever torch is importable, because "cuda:3 on a
    two-GPU box" must fail here with a readable message rather than deep inside
    a forward pass.
    """
    if requested is None:
        torch = _import_torch()
        if torch.cuda.is_available():
            return "cuda:0"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"

    device = normalise_device(requested)
    if not probe:
        return device
    try:
        import torch
    except Exception:
        # No torch: the shape is all we can check. The load itself will raise
        # DependencyMissing a moment later, which is the honest error.
        return device
    if device.startswith("cuda"):
        if not torch.cuda.is_available():
            raise ConfigurationError(
                "cuda was requested but torch reports no CUDA device",
                requested=device)
        if ":" in device:
            index = int(device.split(":", 1)[1])
            count = torch.cuda.device_count()
            if index >= count:
                raise ConfigurationError(
                    "cuda device index is out of range", requested=device,
                    devices_available=count)
    elif device == "mps":
        mps = getattr(torch.backends, "mps", None)
        if mps is None or not mps.is_available():
            raise ConfigurationError(
                "mps was requested but torch reports no MPS device",
                requested=device)
    return device


# ---------------------------------------------------------------------------
# calendar features
# ---------------------------------------------------------------------------

def calendar_stamps(timestamps: Sequence[datetime]) -> tuple[tuple[int, ...], ...]:
    """Derive Kronos's five calendar features from bar timestamps.

    Recomputed from the timestamps that accompany the bars rather than accepted
    from a caller, so the temporal conditioning can never disagree with the
    price series it conditions. All arithmetic is on the UTC instant; the model
    was pretrained on whatever local convention its training data carried, and
    inventing a per-venue localisation here would be an unvalidated guess.
    """
    stamps = []
    for value in timestamps:
        ts = ensure_utc(value, field_name="forecast timestamp")
        stamps.append((ts.minute, ts.hour, ts.weekday(), ts.day, ts.month))
    return tuple(stamps)


# ---------------------------------------------------------------------------
# checkpoint pins
# ---------------------------------------------------------------------------

def assert_symmetric_bits(s1_bits: int, s2_bits: int, *, where: str) -> None:
    """Refuse ``s1_bits != s2_bits`` — teardown §12.2.

    `KronosTokenizer.indices_to_bits(half=True)` masks with
    ``2 ** arange(codebook_dim // 2)`` for *both* halves
    (`model/kronos.py:129`), so an asymmetric split decodes the fine token with
    the wrong number of bits. Verified upstream: with ``s1_bits=6, s2_bits=4``
    the half-mode round-trip diverges from full-mode by 0.384 max-abs, versus
    exactly 0.0 when symmetric. There is no assertion and no documentation
    upstream — the numbers are simply wrong. Olympus makes it loud.
    """
    if int(s1_bits) != int(s2_bits):
        raise ConfigurationError(
            "Kronos requires s1_bits == s2_bits: the upstream tokenizer masks "
            "both halves with codebook_dim//2 bits (teardown 12.2) and "
            "silently decodes asymmetric configurations to wrong values",
            where=where, s1_bits=int(s1_bits), s2_bits=int(s2_bits))


@dataclass(frozen=True)
class CheckpointPin:
    """An exact, verifiable statement of which weights to run.

    Everything except `repo_id` and `kind` is optional *as data* but the
    expectations that are present are enforced at load time: a pin is a claim,
    and loading is where the claim is checked. A checkpoint that does not match
    its pin raises `CheckpointVerificationError` rather than loading with a
    warning, because a warning in a batch job is a warning nobody reads.
    """
    repo_id: str
    kind: str                                    # "tokenizer" | "predictor"
    #: Full 40-hex git sha. None means UNPINNED — `load_*` will refuse it.
    revision: str | None = None
    #: Optional sha256 of the weights file, checked when the loader reports a
    #: path. Guards against a mutated cache, not just a moved branch.
    sha256: str | None = None
    expected_s1_bits: int | None = None
    expected_s2_bits: int | None = None
    expected_d_in: int | None = None
    #: Context the checkpoint was trained for. Bounds both `context_length` and
    #: `horizon` (teardown §12.6).
    max_context: int | None = None
    #: Free-text note recorded in `identity` — e.g. where a value came from.
    note: str = ""

    def __post_init__(self):
        if not self.repo_id or not str(self.repo_id).strip():
            raise ConfigurationError("checkpoint repo_id must be non-empty")
        if self.kind not in _VALID_KINDS:
            raise ConfigurationError(
                "checkpoint kind must be 'tokenizer' or 'predictor'",
                repo_id=self.repo_id, got=self.kind)
        if self.revision is not None:
            revision = str(self.revision).strip().lower()
            if not _GIT_SHA.match(revision):
                raise ConfigurationError(
                    "checkpoint revision must be a full 40-hex git sha; branch "
                    "names such as 'main' track a moving target and make every "
                    "recorded model_version unreproducible",
                    repo_id=self.repo_id, got=str(self.revision))
            object.__setattr__(self, "revision", revision)
        if self.sha256 is not None:
            digest = str(self.sha256).strip().lower()
            if not _SHA256.match(digest):
                raise ConfigurationError(
                    "checkpoint sha256 must be 64 hex characters",
                    repo_id=self.repo_id, got=str(self.sha256))
            object.__setattr__(self, "sha256", digest)
        if self.expected_s1_bits is not None and self.expected_s2_bits is not None:
            assert_symmetric_bits(self.expected_s1_bits, self.expected_s2_bits,
                                  where=f"pin {self.repo_id}")
        for name in ("expected_s1_bits", "expected_s2_bits", "expected_d_in",
                     "max_context"):
            value = getattr(self, name)
            if value is not None and int(value) <= 0:
                raise ConfigurationError(f"{name} must be > 0",
                                         repo_id=self.repo_id, got=value)

    @property
    def pinned(self) -> bool:
        return self.revision is not None

    @property
    def model_version(self) -> str:
        """Stable, human-readable identity string written into every record."""
        rev = self.revision[:12] if self.revision else "UNPINNED"
        return f"{self.kind}:{self.repo_id}@{rev}"

    def with_revision(self, revision: str) -> "CheckpointPin":
        """Operator escape hatch for the checkpoints nobody has pinned yet."""
        return replace(self, revision=revision)

    def with_sha256(self, digest: str) -> "CheckpointPin":
        return replace(self, sha256=digest)

    def to_dict(self) -> dict:
        return {
            "repo_id": self.repo_id, "kind": self.kind,
            "revision": self.revision, "sha256": self.sha256,
            "expected_s1_bits": self.expected_s1_bits,
            "expected_s2_bits": self.expected_s2_bits,
            "expected_d_in": self.expected_d_in,
            "max_context": self.max_context, "pinned": self.pinned,
            "note": self.note,
        }


#: Architecture note for every entry below. Upstream does NOT publish the
#: pretrained hyperparameters in the repository; the `s1_bits=10, s2_bits=10,
#: d_in=6` values are the `.get()` fallback defaults used for random-init in
#: `finetune_csv/train_sequential.py:93-224` (teardown §10.2, marked [I] —
#: inferred, not authoritative). They are recorded here as *expectations* that
#: `load_*` checks against the checkpoint it actually loaded, so a wrong guess
#: surfaces as a loud `CheckpointVerificationError` rather than as a silently
#: mis-shaped model. `max_context` is from the README model zoo table.
_ARCH_NOTE = ("s1/s2 bits inferred from finetune_csv random-init fallbacks "
              "(teardown 10.2); verified against the loaded checkpoint")

#: Revisions taken from upstream's own regression test, which pins them for
#: bit-exact reproducibility (`tests/test_kronos_regression.py:31-32` at commit
#: 67b630e). They are the only publicly verified checkpoint revisions.
TOKENIZER_BASE = CheckpointPin(
    repo_id="NeoQuasar/Kronos-Tokenizer-base",
    kind="tokenizer",
    revision="0e0117387f39004a9016484a186a908917e22426",
    expected_s1_bits=10, expected_s2_bits=10, expected_d_in=6,
    max_context=512,
    note="pinned by upstream tests/test_kronos_regression.py; " + _ARCH_NOTE)

KRONOS_SMALL = CheckpointPin(
    repo_id="NeoQuasar/Kronos-small",
    kind="predictor",
    revision="901c26c1332695a2a8f243eb2f37243a37bea320",
    expected_s1_bits=10, expected_s2_bits=10,
    max_context=512,
    note="pinned by upstream tests/test_kronos_regression.py; " + _ARCH_NOTE)

# Real, downloadable, and deliberately UNPINNED: no verified revision has been
# published for these, so `load_*` refuses them until an operator states one.
TOKENIZER_2K = CheckpointPin(
    repo_id="NeoQuasar/Kronos-Tokenizer-2k", kind="tokenizer",
    expected_s1_bits=10, expected_s2_bits=10, expected_d_in=6,
    max_context=2048,
    note="no verified revision published; supply one via with_revision()")

KRONOS_MINI = CheckpointPin(
    repo_id="NeoQuasar/Kronos-mini", kind="predictor",
    expected_s1_bits=10, expected_s2_bits=10, max_context=2048,
    note="no verified revision published; supply one via with_revision()")

KRONOS_BASE = CheckpointPin(
    repo_id="NeoQuasar/Kronos-base", kind="predictor",
    expected_s1_bits=10, expected_s2_bits=10, max_context=512,
    note="no verified revision published; supply one via with_revision()")

#: The known public checkpoints, keyed by repo id. Kronos-large is absent
#: because upstream has not released it (teardown §10.2).
PINS: Mapping[str, CheckpointPin] = {
    p.repo_id: p for p in (TOKENIZER_BASE, KRONOS_SMALL, TOKENIZER_2K,
                           KRONOS_MINI, KRONOS_BASE)
}

#: The pair Olympus uses by default: the only combination whose revisions are
#: both publicly verified.
DEFAULT_TOKENIZER_PIN = TOKENIZER_BASE
DEFAULT_PREDICTOR_PIN = KRONOS_SMALL


def pin_for(repo_id: str) -> CheckpointPin:
    """Look up a known checkpoint. Unknown ids raise rather than defaulting."""
    try:
        return PINS[str(repo_id)]
    except KeyError:
        raise ConfigurationError(
            "unknown Kronos checkpoint; construct a CheckpointPin explicitly "
            "if you are running your own fine-tune",
            repo_id=str(repo_id), known=sorted(PINS)) from None


def unpinned_repos() -> tuple[str, ...]:
    """Registered checkpoints that `load_*` will refuse as-is."""
    return tuple(sorted(k for k, p in PINS.items() if not p.pinned))


# ---------------------------------------------------------------------------
# compatibility
# ---------------------------------------------------------------------------

def _int_attr(obj: Any, name: str, *, label: str) -> int:
    value = getattr(obj, name, None)
    if value is None:
        raise IncompatibleModelError(
            f"{label} does not expose {name!r}; its geometry cannot be "
            "verified and an unverifiable model is not loadable",
            attribute=name, label=label)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise IncompatibleModelError(
            f"{label}.{name} is not an integer", attribute=name, label=label,
            got=repr(value)) from None


def verify_compatibility(tokenizer: Any, predictor: Any) -> dict:
    """Prove a tokenizer and a predictor speak the same vocabulary.

    Kronos's two stages are trained together but distributed as separate HF
    repos, so nothing structurally prevents pairing a 2k tokenizer with a small
    predictor. The result would not crash — the embedding tables would simply be
    indexed with tokens that mean something else. That is precisely the class of
    silent corruption this package exists to eliminate, so every shared
    dimension is checked and a mismatch is fatal.

    Raises `ConfigurationError` for the asymmetric-bits defect (§12.2, checked
    first because it is a property of *either* model alone) and
    `IncompatibleModelError` when the two models disagree.

    Returns the agreed geometry, which goes into the identity record.
    """
    t_s1 = _int_attr(tokenizer, "s1_bits", label="tokenizer")
    t_s2 = _int_attr(tokenizer, "s2_bits", label="tokenizer")
    p_s1 = _int_attr(predictor, "s1_bits", label="predictor")
    p_s2 = _int_attr(predictor, "s2_bits", label="predictor")

    assert_symmetric_bits(t_s1, t_s2, where="tokenizer")
    assert_symmetric_bits(p_s1, p_s2, where="predictor")

    if (t_s1, t_s2) != (p_s1, p_s2):
        raise IncompatibleModelError(
            "tokenizer and predictor disagree on the token split; they would "
            "index each other's embedding tables with meaningless ids",
            tokenizer_bits=[t_s1, t_s2], predictor_bits=[p_s1, p_s2])

    d_in = getattr(tokenizer, "d_in", None)
    if d_in is not None and int(d_in) != len(FEATURE_COLUMNS):
        raise IncompatibleModelError(
            "tokenizer d_in does not match the OHLCVA feature vector",
            expected=len(FEATURE_COLUMNS), got=int(d_in),
            columns=list(FEATURE_COLUMNS))

    vocab = getattr(predictor, "s1_vocab_size", None)
    if vocab is not None and int(vocab) != 2 ** p_s1:
        raise IncompatibleModelError(
            "predictor s1 vocabulary does not match its own bit width",
            s1_bits=p_s1, expected_vocab=2 ** p_s1, got=int(vocab))

    return {"s1_bits": t_s1, "s2_bits": t_s2,
            "d_in": int(d_in) if d_in is not None else len(FEATURE_COLUMNS),
            "s1_vocab_size": 2 ** t_s1, "s2_vocab_size": 2 ** t_s2}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

#: A loader takes a pin and a device and returns `(model_object, meta)`. `meta`
#: may carry `resolved_revision` (the commit the hub actually served) and
#: `weights_path` (for hashing). Injectable so the whole verification path is
#: testable without torch, a network, or 25MB of weights.
CheckpointLoader = Callable[[CheckpointPin, str], "tuple[Any, Mapping[str, Any]]"]

_HASH_CHUNK = 1 << 20


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_loader(pin: CheckpointPin, device: str) -> tuple[Any, dict]:
    """Load from the Hugging Face Hub via upstream's `PyTorchModelHubMixin`.

    Requires torch and an importable upstream Kronos package; both raise
    `DependencyMissing`. The revision is passed through to `from_pretrained`,
    so the hub — not us — enforces that the commit exists.
    """
    torch = _import_torch()
    upstream = _import_upstream_kronos()
    cls = (upstream.KronosTokenizer if pin.kind == "tokenizer"
           else upstream.Kronos)
    try:
        model = cls.from_pretrained(pin.repo_id, revision=pin.revision)
    except Exception as exc:
        raise ModelNotAvailable(
            "could not load the Kronos checkpoint",
            repo_id=pin.repo_id, revision=pin.revision, kind=pin.kind,
            error=str(exc)) from exc
    model = model.to(device)
    model.eval()
    # `from_pretrained` does not report the commit it resolved, so the pin's
    # own revision is the resolved one by construction: it was requested
    # verbatim and the hub would have failed otherwise.
    return model, {"resolved_revision": pin.revision,
                   "torch_version": str(torch.__version__)}


def _verify_and_describe(pin: CheckpointPin, model: Any,
                         meta: Mapping[str, Any], device: str) -> dict:
    """Check a loaded object against its pin and build the identity record."""
    resolved = meta.get("resolved_revision") or pin.revision
    if pin.revision and resolved and str(resolved).lower() != pin.revision:
        raise CheckpointVerificationError(
            "the loaded checkpoint is not the pinned revision",
            repo_id=pin.repo_id, expected=pin.revision, resolved=str(resolved))

    digest = meta.get("sha256")
    weights_path = meta.get("weights_path")
    if digest is None and weights_path:
        try:
            digest = sha256_file(str(weights_path))
        except OSError:
            digest = None
    if pin.sha256:
        if digest is None:
            raise CheckpointVerificationError(
                "pin declares a sha256 but the loader reported no weights file "
                "to hash; the checkpoint cannot be verified",
                repo_id=pin.repo_id, expected=pin.sha256)
        if str(digest).lower() != pin.sha256:
            raise CheckpointVerificationError(
                "checkpoint weights hash does not match the pin",
                repo_id=pin.repo_id, expected=pin.sha256,
                got=str(digest).lower())

    # Geometry: the pin's expectations are inferred (see _ARCH_NOTE), so a
    # mismatch means the guess was wrong OR the weights changed. Either way the
    # recorded model_version would be describing something else.
    observed: dict[str, Any] = {}
    for attr, expected in (("s1_bits", pin.expected_s1_bits),
                           ("s2_bits", pin.expected_s2_bits),
                           ("d_in", pin.expected_d_in)):
        value = getattr(model, attr, None)
        if value is None:
            continue
        observed[attr] = int(value)
        if expected is not None and int(value) != int(expected):
            raise CheckpointVerificationError(
                f"checkpoint {attr} does not match the pin",
                repo_id=pin.repo_id, attribute=attr, expected=int(expected),
                got=int(value))
    if "s1_bits" in observed and "s2_bits" in observed:
        assert_symmetric_bits(observed["s1_bits"], observed["s2_bits"],
                              where=f"checkpoint {pin.repo_id}")

    identity = {
        "repo_id": pin.repo_id,
        "kind": pin.kind,
        "revision": pin.revision,
        "resolved_revision": str(resolved) if resolved else None,
        "sha256": str(digest).lower() if digest else None,
        "device": device,
        "torch_version": meta.get("torch_version") or _optional_torch_version(),
        "model_version": pin.model_version,
        "max_context": pin.max_context,
        "note": pin.note,
    }
    identity.update(observed)
    return identity


# --- process-level checkpoint cache ----------------------------------------
# Loading a checkpoint costs a download plus a few hundred MB of RAM, and a
# process that forecasts fifty instruments would otherwise pay it fifty times.
# The cache is refcounted rather than permanent so `close()` genuinely releases
# device memory once the last holder is done — a permanent cache in a
# long-running trading process is a memory leak with a nice name.

@dataclass
class _CacheEntry:
    obj: Any
    identity: dict
    refs: int


_CACHE: dict[tuple[str, str, str], _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(pin: CheckpointPin, device: str) -> tuple[str, str, str]:
    return (pin.repo_id, pin.revision or "", device)


def _acquire(key, factory) -> tuple[Any, dict]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            entry.refs += 1
            return entry.obj, dict(entry.identity)
    # Loading happens OUTSIDE the lock: it is slow and may hit the network, and
    # holding a process-wide lock across it would serialise every forecaster.
    # Two threads racing the same key means one load is discarded, which is
    # wasteful but never incorrect.
    obj, identity = factory()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            _CACHE[key] = _CacheEntry(obj=obj, identity=dict(identity), refs=1)
            return obj, dict(identity)
        entry.refs += 1
        return entry.obj, dict(entry.identity)


def _release(key) -> None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return
        entry.refs -= 1
        if entry.refs <= 0:
            _CACHE.pop(key, None)


def clear_checkpoint_cache() -> None:
    """Drop every cached checkpoint regardless of refcount (tests, shutdown)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def checkpoint_cache_size() -> int:
    with _CACHE_LOCK:
        return len(_CACHE)


def _load(pin: CheckpointPin, kind: str, device: str | None,
          loader: CheckpointLoader | None) -> tuple[Any, dict, tuple]:
    if pin.kind != kind:
        raise ConfigurationError(
            f"expected a {kind} pin", repo_id=pin.repo_id, got=pin.kind)
    if not pin.pinned:
        raise CheckpointVerificationError(
            "refusing to load an unpinned checkpoint: without a revision the "
            "weights behind this name can change and every forecast recorded "
            "against it becomes unreproducible. Supply one with "
            "CheckpointPin.with_revision(<40-hex sha>).",
            repo_id=pin.repo_id, kind=pin.kind)
    resolved_device = select_device(device)
    key = _cache_key(pin, resolved_device)
    use = loader or default_loader

    def factory():
        obj, meta = use(pin, resolved_device)
        if obj is None:
            raise ModelNotAvailable("loader returned no model",
                                    repo_id=pin.repo_id, kind=pin.kind)
        return obj, _verify_and_describe(pin, obj, dict(meta or {}),
                                         resolved_device)

    obj, identity = _acquire(key, factory)
    return obj, identity, key


def load_tokenizer(pin: CheckpointPin, device: str | None = None, *,
                   loader: CheckpointLoader | None = None
                   ) -> tuple[Any, dict]:
    """Load a pinned tokenizer. Returns `(model, identity)`."""
    obj, identity, _ = _load(pin, "tokenizer", device, loader)
    return obj, identity


def load_predictor(pin: CheckpointPin, device: str | None = None, *,
                   loader: CheckpointLoader | None = None
                   ) -> tuple[Any, dict]:
    """Load a pinned predictor. Returns `(model, identity)`."""
    obj, identity, _ = _load(pin, "predictor", device, loader)
    return obj, identity


# ---------------------------------------------------------------------------
# the model boundary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SamplingParams:
    """Everything that makes generation reproducible, in one record.

    Written verbatim into `ForecastResult.inference_params`, which is what
    makes "reproduce this forecast a year from now" a mechanical operation
    rather than an archaeology project.
    """
    temperature: float = 1.0
    top_k: int | None = None
    top_p: float | None = None
    seed: int = 0
    clip: float = 5.0

    def to_dict(self) -> dict:
        return {"temperature": self.temperature, "top_k": self.top_k,
                "top_p": self.top_p, "seed": self.seed, "clip": self.clip}


class ModelBackend(abc.ABC):
    """The single surface `kronos_adapter` is allowed to touch.

    Deliberately *not* `encode`/`decode_s1`/`decode_s2`. Exposing tokens across
    this boundary would put Kronos's internal vocabulary into Olympus's
    forecasting layer, and the adapter would then have to care about bit
    splits, ring buffers, and hierarchical heads — none of which have anything
    to do with what a forecast means. The adapter needs exactly one thing:
    `n_samples` plausible futures in the same normalised space it supplied.

    Everything here is in **normalised space**. The adapter owns instance
    normalisation and denormalisation because that is where the leakage repair
    lives (teardown §12.9); a backend that normalised internally could reach
    statistics the adapter never intended it to see.
    """

    @property
    @abc.abstractmethod
    def identity(self) -> Mapping[str, Any]:
        """Provenance of the weights behind this backend."""

    @property
    @abc.abstractmethod
    def max_context(self) -> int:
        """Longest token window the checkpoint supports."""

    @abc.abstractmethod
    def generate(
        self,
        context: Sequence[Sequence[float]],
        *,
        context_stamps: Sequence[Sequence[int]],
        future_stamps: Sequence[Sequence[int]],
        horizon: int,
        n_samples: int,
        params: SamplingParams,
    ) -> tuple[tuple[tuple[float, ...], ...], ...]:
        """Draw `n_samples` independent futures.

        `context` is `[T][6]` normalised OHLCVA. The return is
        `[n_samples][horizon][6]`, still normalised. **Every** path is
        returned; averaging is the caller's decision, not the model's
        (teardown §12.7 — upstream averages inside inference and throws the
        dispersion away).

        Implementations must be a deterministic function of
        `(context, stamps, horizon, n_samples, params)` — `params.seed` is the
        only entropy source, and per-path seeds must be derived from it.
        """


class TorchKronosBackend(ModelBackend):
    """Drives real Kronos weights through their public API.

    The generation loop is Olympus's own rather than upstream's
    `auto_regressive_inference`, for two reasons that are not stylistic:
    upstream averages the samples before returning (§12.7) and its logit filter
    silently ignores `top_p` whenever `top_k > 0` (§12.1). Both are load-bearing
    for a risk engine, so both are reimplemented here against the same public
    `encode` / `decode_s1` / `decode_s2` / `decode` methods.

    No KV cache, same as upstream (§14.2): this re-runs the full stack per step,
    which is `O(horizon x context^2)`. Correctness first; the cache is a
    separate, testable optimisation and pretending otherwise would be a
    performance claim we have not measured.
    """

    def __init__(self, runtime: "KronosRuntime"):
        self._runtime = runtime

    @property
    def identity(self) -> Mapping[str, Any]:
        return self._runtime.identity

    @property
    def max_context(self) -> int:
        return self._runtime.max_context

    def _filter(self, logits, params: SamplingParams, torch):
        """top-k then nucleus, composed in that order — the §12.1 repair.

        `KronosConfig` already refuses configurations that set both, so in
        practice at most one branch fires. Implementing the composition anyway
        means the two layers agree rather than one silently overriding the
        other if the policy is ever relaxed.
        """
        import torch.nn.functional as F

        logits = logits / float(params.temperature)
        if params.top_k:
            k = min(max(int(params.top_k), 1), logits.size(-1))
            threshold = torch.topk(logits, k, dim=-1).values[..., -1, None]
            logits = logits.masked_fill(logits < threshold, float("-inf"))
        if params.top_p is not None and float(params.top_p) < 1.0:
            ordered, order = torch.sort(logits, descending=True, dim=-1)
            cumulative = torch.cumsum(F.softmax(ordered, dim=-1), dim=-1)
            drop = cumulative > float(params.top_p)
            # Always keep the most probable token, and keep the first token
            # that crosses the threshold (shift right) so the nucleus is
            # inclusive rather than truncating the mode away.
            drop[..., 1:] = drop[..., :-1].clone()
            drop[..., 0] = False
            logits = logits.masked_fill(
                drop.scatter(-1, order, drop), float("-inf"))
        return logits

    def generate(self, context, *, context_stamps, future_stamps, horizon,
                 n_samples, params):
        torch = _import_torch()
        import torch.nn.functional as F

        max_context = self.max_context
        if horizon >= max_context:
            # §12.6: upstream caps the final decode window at max_context and
            # then slices `-pred_len`, producing a pandas shape error whose
            # message names neither pred_len nor max_context.
            raise ConfigurationError(
                "horizon must be strictly less than the checkpoint's "
                "max_context (teardown 12.6)",
                horizon=horizon, max_context=max_context)
        if n_samples < 1:
            raise ConfigurationError("n_samples must be >= 1", got=n_samples)

        device = self._runtime.device
        tokenizer = self._runtime.tokenizer
        predictor = self._runtime.predictor

        rows = [[float(v) for v in row] for row in context]
        stamp_rows = [[float(v) for v in row]
                      for row in (list(context_stamps) + list(future_stamps))]

        with torch.no_grad():
            x = torch.tensor(rows, dtype=torch.float32, device=device)
            x = torch.clamp(x, -float(params.clip), float(params.clip))
            x = x.unsqueeze(0).expand(n_samples, -1, -1).contiguous()
            stamps = torch.tensor(stamp_rows, dtype=torch.float32, device=device)
            stamps = stamps.unsqueeze(0).expand(n_samples, -1, -1).contiguous()

            # Sampling runs on a CPU generator so a run is reproducible from its
            # seed regardless of which device the weights sit on; CUDA RNG
            # streams are not portable across driver or device counts.
            generator = torch.Generator(device="cpu")
            generator.manual_seed(int(params.seed))

            tokens = tokenizer.encode(x, half=True)
            pre, post = tokens[0], tokens[1]

            for _ in range(horizon):
                length = pre.size(1)
                start = max(0, length - max_context)
                window_pre = pre[:, start:length].contiguous()
                window_post = post[:, start:length].contiguous()
                window_stamp = stamps[:, start:length, :].contiguous()

                s1_logits, hidden = predictor.decode_s1(
                    window_pre, window_post, window_stamp)
                s1_logits = self._filter(s1_logits[:, -1, :], params, torch)
                s1_next = torch.multinomial(
                    F.softmax(s1_logits, dim=-1).cpu(), 1,
                    generator=generator).to(device)

                s2_logits = predictor.decode_s2(hidden, s1_next)
                s2_logits = self._filter(s2_logits[:, -1, :], params, torch)
                s2_next = torch.multinomial(
                    F.softmax(s2_logits, dim=-1).cpu(), 1,
                    generator=generator).to(device)

                pre = torch.cat([pre, s1_next], dim=1)
                post = torch.cat([post, s2_next], dim=1)

            total = pre.size(1)
            start = max(0, total - max_context)
            decoded = tokenizer.decode(
                [pre[:, start:total].contiguous(),
                 post[:, start:total].contiguous()], half=True)
            future = decoded[:, -horizon:, :].detach().to("cpu").tolist()

        return tuple(tuple(tuple(float(v) for v in step) for step in path)
                     for path in future)


class KronosRuntime:
    """A loaded, verified tokenizer/predictor pair with an explicit lifetime.

    Constructed through `load`, which enforces pinning, hash and geometry
    verification, and cross-model compatibility before anything can forecast.
    `close()` is explicit rather than left to the garbage collector because
    device memory is a bounded resource and CPython's collector makes no
    promises about when a several-hundred-megabyte model goes away.
    """

    def __init__(self, *, tokenizer: Any, predictor: Any, device: str,
                 tokenizer_identity: Mapping[str, Any],
                 predictor_identity: Mapping[str, Any],
                 geometry: Mapping[str, Any] | None = None,
                 max_context: int | None = None,
                 _cache_keys: Sequence[tuple] = ()):
        self._tokenizer = tokenizer
        self._predictor = predictor
        self._device = device
        self._tokenizer_identity = dict(tokenizer_identity)
        self._predictor_identity = dict(predictor_identity)
        self._geometry = dict(geometry or {})
        self._cache_keys = tuple(_cache_keys)
        self._closed = False
        contexts = [c for c in (max_context,
                                self._tokenizer_identity.get("max_context"),
                                self._predictor_identity.get("max_context"))
                    if c]
        if not contexts:
            raise ConfigurationError(
                "max_context is unknown for this checkpoint pair; it bounds "
                "both the context window and the horizon and cannot be guessed")
        # The shorter of the two binds: a 2k tokenizer paired with a 512
        # predictor can only ever be driven 512 tokens deep.
        self._max_context = int(min(contexts))

    @classmethod
    def load(cls, *, tokenizer_pin: CheckpointPin = DEFAULT_TOKENIZER_PIN,
             predictor_pin: CheckpointPin = DEFAULT_PREDICTOR_PIN,
             device: str | None = None,
             loader: CheckpointLoader | None = None,
             verify: bool = True) -> "KronosRuntime":
        resolved_device = select_device(device)
        tokenizer, tok_identity, tok_key = _load(
            tokenizer_pin, "tokenizer", resolved_device, loader)
        try:
            predictor, pred_identity, pred_key = _load(
                predictor_pin, "predictor", resolved_device, loader)
        except Exception:
            _release(tok_key)
            raise
        geometry: dict = {}
        try:
            if verify:
                geometry = verify_compatibility(tokenizer, predictor)
        except Exception:
            _release(tok_key)
            _release(pred_key)
            raise
        return cls(tokenizer=tokenizer, predictor=predictor,
                   device=resolved_device, tokenizer_identity=tok_identity,
                   predictor_identity=pred_identity, geometry=geometry,
                   _cache_keys=(tok_key, pred_key))

    # -- accessors ---------------------------------------------------------
    def _check_open(self) -> None:
        if self._closed:
            raise ModelNotAvailable(
                "this KronosRuntime has been closed", device=self._device)

    @property
    def tokenizer(self) -> Any:
        self._check_open()
        return self._tokenizer

    @property
    def predictor(self) -> Any:
        self._check_open()
        return self._predictor

    @property
    def device(self) -> str:
        return self._device

    @property
    def max_context(self) -> int:
        return self._max_context

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def model_version(self) -> str:
        """One string naming both halves — what lands in `model_version`."""
        return (f"{self._predictor_identity.get('model_version', 'unknown')}"
                f"+{self._tokenizer_identity.get('model_version', 'unknown')}")

    @property
    def identity(self) -> dict:
        return {
            "model_version": self.model_version,
            "device": self._device,
            "max_context": self._max_context,
            "torch_version": (self._predictor_identity.get("torch_version")
                              or self._tokenizer_identity.get("torch_version")),
            "tokenizer": dict(self._tokenizer_identity),
            "predictor": dict(self._predictor_identity),
            "geometry": dict(self._geometry),
        }

    def backend(self) -> ModelBackend:
        self._check_open()
        return TorchKronosBackend(self)

    # -- lifetime ----------------------------------------------------------
    def close(self) -> None:
        """Release the checkpoints. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._tokenizer = None
        self._predictor = None
        for key in self._cache_keys:
            _release(key)
        self._cache_keys = ()

    def __enter__(self) -> "KronosRuntime":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:  # pragma: no cover - trivial
        state = "closed" if self._closed else "open"
        return (f"KronosRuntime({self.model_version}, device={self._device}, "
                f"{state})")


__all__ = [
    "FEATURE_COLUMNS", "STAMP_COLUMNS",
    "PINS", "DEFAULT_PREDICTOR_PIN", "DEFAULT_TOKENIZER_PIN",
    "KRONOS_BASE", "KRONOS_MINI", "KRONOS_SMALL", "TOKENIZER_2K",
    "TOKENIZER_BASE",
    "CheckpointLoader", "CheckpointPin", "KronosRuntime", "ModelBackend",
    "SamplingParams", "TorchKronosBackend",
    "assert_symmetric_bits", "calendar_stamps", "checkpoint_cache_size",
    "clear_checkpoint_cache", "default_loader", "load_predictor",
    "load_tokenizer", "normalise_device", "pin_for", "select_device",
    "sha256_file", "unpinned_repos", "verify_compatibility",
]
