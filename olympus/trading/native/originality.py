"""Automated originality and dependency checks.

Six claims, each with a function that returns findings rather than a boolean.
Findings, because "this fails" needs to say *where* — a red light with no
location gets suppressed, and a suppressed check is worse than no check.

The six
-------
1. Native model modules do not import Kronos implementation modules.
2. Native checkpoints do not load foreign weights.
3. Native configurations do not reference foreign checkpoint identifiers.
4. Model cards identify all external methods and inspirations.
5. Any reused general-purpose dependency is disclosed.
6. Olympus ownership claims apply only to independently produced code and
   weights.

Why these live in a module and not only in tests
------------------------------------------------
A test proves the claim at CI time. A module lets the *model card* run the same
checks at card-generation time and refuse to emit a card that would state
something false — so the disclosure and the check cannot drift apart. Both
exist: `tests/test_trading_native_originality.py` runs them, and
`modelcard.build_card` runs them before it writes a claim.

What check 6 actually means
---------------------------
Ownership is not a property of a file's location. A checkpoint sitting in
`native/` that was initialised from someone else's weights is not
Olympus-owned, and a module in `native/` that transcribes a published
architecture is Olympus-*implemented* but not Olympus-*invented*. So check 6
looks for the specific claim strings and requires each one to be accompanied by
a disclosure — `assert_ownership_claims_are_narrow` is what stops
"Olympus-owned model" appearing without "trained from an Olympus dataset with
no foreign weights" beside it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ConfigurationError

#: Module names no native module may import, at any depth.
FOREIGN_MODULES: frozenset[str] = frozenset({
    "kronos", "kronos_adapter", "kronos_runtime"})

#: Substrings that indicate a weight file, a published checkpoint or a model
#: hub. None may appear in a native module or a native configuration.
FOREIGN_ARTEFACT_MARKERS: tuple[str, ...] = (
    ".safetensors", ".ckpt", "pytorch_model", "from_pretrained", "hf_hub",
    "huggingface", "torch.hub", "load_state_dict_from_url", "model_zoo",
    "timm.create_model",
)

#: General-purpose dependencies the native package reuses. Every one must be
#: disclosed on a model card. `tests/test_trading_native_originality.py`
#: asserts this list covers what the modules actually import.
DISCLOSED_DEPENDENCIES: Mapping[str, str] = {
    "torch": "PyTorch — tensors, autograd and the optimiser. A general-purpose "
             "framework, used as such. No model, weight file or architecture "
             "is taken from it or from its hub.",
    "python-stdlib": "The Python standard library — statistics, math, hashlib, "
                     "json, dataclasses. Everything outside the neural path is "
                     "stdlib only.",
}

#: External *methods* the architecture uses. Published, general techniques in
#: which Olympus claims no novelty. A model card missing any of these is
#: incomplete and `check_model_card_discloses_methods` says so.
REQUIRED_METHOD_DISCLOSURES: tuple[str, ...] = (
    "patch", "causal convolution", "mixture of experts", "attention",
    "quantile", "pinball",
)


@dataclass(frozen=True)
class Finding:
    """One violation, with the location that makes it fixable."""

    check: str
    location: str
    detail: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "context", dict(self.context))

    def to_dict(self) -> dict:
        return {"check": self.check, "location": self.location,
                "detail": self.detail, "context": dict(self.context)}

    def __str__(self) -> str:                            # pragma: no cover
        return f"[{self.check}] {self.location}: {self.detail}"


def native_root() -> Path:
    return Path(__file__).resolve().parent


def native_modules(root: Path | None = None) -> list[Path]:
    return sorted((root or native_root()).rglob("*.py"))


# ===========================================================================
# 1. imports
# ===========================================================================

def check_no_foreign_imports(root: Path | None = None) -> list[Finding]:
    """No native module imports a Kronos implementation module, at any depth.

    At any depth: a lazy import inside a function is exactly how a dependency
    gets reintroduced without anyone editing an import block, so this walks the
    whole AST rather than only module-level statements.
    """
    findings: list[Finding] = []
    for path in native_modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            else:
                continue
            for name in names:
                parts = {p.lower() for p in name.split(".")}
                if parts & FOREIGN_MODULES or "kronos" in name.lower():
                    findings.append(Finding(
                        check="no_foreign_imports",
                        location=f"native/{path.name}:{node.lineno}",
                        detail=f"imports {name!r}",
                        context={"module": name}))
    return findings


# ===========================================================================
# 2. checkpoints
# ===========================================================================

#: Modules permitted to contain a foreign-artefact marker, and why. Enumerated
#: rather than pattern-matched: an exemption a reviewer can read is an
#: exemption a reviewer can withdraw, and a heuristic that guessed at intent
#: would let a real load hide behind a comment.
#:
#: The single entry names these artefacts *in order to refuse* them, which is
#: the opposite of using one. Two other modules were on this list and were
#: removed: `checkpoint.py`, whose refusal table matches origin *shapes* rather
#: than these markers, and `evaluation.py`, which no longer names any external
#: model — its unavailable-arm helper takes the name from its caller.
MARKER_EXEMPT: Mapping[str, str] = {
    "originality.py": "this file defines the marker list the check searches "
                      "for; it must contain every marker by construction",
}


def check_no_foreign_weight_sources(root: Path | None = None) -> list[Finding]:
    """No native module references a weight file, hub or pretrained loader.

    `MARKER_EXEMPT` names the modules that must contain a marker in order to
    refuse it, with the reason. Everything else is flagged.
    """
    findings: list[Finding] = []
    for path in native_modules(root):
        if path.name in MARKER_EXEMPT:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            for marker in FOREIGN_ARTEFACT_MARKERS:
                if marker in lowered:
                    findings.append(Finding(
                        check="no_foreign_weight_sources",
                        location=f"native/{path.name}:{lineno}",
                        detail=f"references {marker!r}",
                        context={"line": line.strip()[:100]}))
    return findings


def check_exemptions_are_still_needed(root: Path | None = None) -> list[Finding]:
    """An exemption for a module that no longer needs one is a hole.

    The exemption list is the weakest part of the previous check, so it has its
    own: a module listed as exempt that contains no marker has had its
    exemption outlive its reason, and the entry should be removed rather than
    left as a standing permission.
    """
    present = {p.name: p.read_text(encoding="utf-8").lower()
               for p in native_modules(root)}
    findings = []
    for name, reason in MARKER_EXEMPT.items():
        text = present.get(name)
        if text is None:
            findings.append(Finding(
                check="stale_exemption", location=f"native/{name}",
                detail="exempted module does not exist"))
        elif not any(marker in text for marker in FOREIGN_ARTEFACT_MARKERS):
            findings.append(Finding(
                check="stale_exemption", location=f"native/{name}",
                detail="exempted but contains no marker; the exemption has "
                       "outlived its reason and should be removed",
                context={"reason": reason}))
    return findings


def check_checkpoint_origin(checkpoint: Any) -> list[Finding]:
    """A checkpoint's declared origin is an Olympus checkpoint or nothing.

    `checkpoint.assert_olympus_origin` already refuses a foreign origin at
    construction. This is the same rule applied to an object that already
    exists — a checkpoint deserialised from a record written by an older or
    modified code path.
    """
    from .checkpoint import assert_olympus_origin
    findings: list[Finding] = []
    origin = getattr(checkpoint, "initialised_from", "")
    try:
        assert_olympus_origin(origin)
    except Exception as error:                           # noqa: BLE001
        findings.append(Finding(
            check="checkpoint_origin",
            location=getattr(checkpoint, "checkpoint_id", "<unknown>"),
            detail=f"declares a foreign origin: {error}",
            context={"initialised_from": str(origin)}))

    parameters = dict(getattr(checkpoint, "parameters", {}) or {})
    for key, value in parameters.items():
        if isinstance(value, str):
            lowered = value.lower()
            for marker in FOREIGN_ARTEFACT_MARKERS:
                if marker in lowered:
                    findings.append(Finding(
                        check="checkpoint_origin",
                        location=getattr(checkpoint, "checkpoint_id", "<unknown>"),
                        detail=f"parameter {key!r} references {marker!r}",
                        context={"key": key}))
    return findings


# ===========================================================================
# 3. configurations
# ===========================================================================

def check_configuration(config: Mapping[str, Any], *,
                        label: str = "<config>") -> list[Finding]:
    """No configuration references a foreign checkpoint identifier.

    Walks nested mappings and sequences, because a model configuration is a
    tree and a `repo_id` three levels down is exactly as loaded as one at the
    top.
    """
    findings: list[Finding] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            lowered = node.lower()
            if any(name in lowered.split("/") or name in lowered.split(".")
                   for name in FOREIGN_MODULES) or "kronos" in lowered:
                findings.append(Finding(
                    check="configuration",
                    location=f"{label}{path}",
                    detail=f"names a foreign model: {node[:60]!r}"))
            for marker in FOREIGN_ARTEFACT_MARKERS:
                if marker in lowered:
                    findings.append(Finding(
                        check="configuration",
                        location=f"{label}{path}",
                        detail=f"references {marker!r}",
                        context={"value": node[:100]}))

    walk(config, "")
    return findings


# ===========================================================================
# 4 and 5. disclosure
# ===========================================================================

def check_model_card_discloses_methods(card: Mapping[str, Any]) -> list[Finding]:
    """A card names every external method the architecture actually uses."""
    disclosed = " ".join(str(x).lower()
                         for x in card.get("external_methods") or ())
    findings = [
        Finding(check="method_disclosure", location="model_card.external_methods",
                detail=f"does not disclose the use of {method!r}",
                context={"method": method})
        for method in REQUIRED_METHOD_DISCLOSURES if method not in disclosed]
    if not card.get("external_methods"):
        findings.append(Finding(
            check="method_disclosure", location="model_card.external_methods",
            detail="the card discloses no external methods at all, which for "
                   "this architecture cannot be true"))
    return findings


def check_dependencies_disclosed(card: Mapping[str, Any],
                                 root: Path | None = None) -> list[Finding]:
    """Every third-party package the native modules import is on the card."""
    imported: set[str] = set()
    for path in native_modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `level > 0` is a relative import — `from .tasks import ...`
                # or `from ...witness import ...`. Those are this repository's
                # own modules and are not dependencies to disclose; counting
                # them would bury the two real ones under thirty of our own.
                if node.level:
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top and top not in _STDLIB and top != "olympus":
                    imported.add(top)

    disclosed = {str(k).lower() for k in card.get("dependencies") or {}}
    findings = []
    for package in sorted(imported):
        if package.lower() not in disclosed:
            findings.append(Finding(
                check="dependency_disclosure",
                location="model_card.dependencies",
                detail=f"{package!r} is imported by a native module and is not "
                       f"disclosed on the card",
                context={"package": package}))
    return findings


#: Standard-library top-level names the native package uses. Deliberately a
#: list rather than `sys.stdlib_module_names`, so that a *new* stdlib import
#: is a visible edit here rather than an invisible pass.
_STDLIB: frozenset[str] = frozenset({
    "abc", "ast", "dataclasses", "datetime", "enum", "hashlib", "importlib",
    "json", "math", "os", "pathlib", "platform", "random", "re", "resource",
    "statistics", "subprocess", "sys", "time", "typing", "collections",
    "functools", "itertools", "warnings", "__future__",
    # Added by Phase 4's research isolation. `ctypes` is the notable one: it
    # reaches libc for `unshare`, `prctl` and `mount`, which is how the worker
    # gets a network namespace and a seccomp filter. Listing it here is the
    # visible edit this hand-maintained set exists to force.
    "ctypes", "shutil", "signal", "tempfile",
    # Added by the pre-merge hardening. `errno` and `threading` come with the
    # cgroup scope and the report pipe — the drain runs on a thread so a
    # runner writing a large report cannot deadlock against a parent sitting in
    # `wait()`. `secrets` generates the per-run nonce that makes a report line
    # forged by generated code discardable.
    "errno", "secrets", "threading",
})


# ===========================================================================
# 6. ownership claims
# ===========================================================================

#: Claim strings that may only appear beside a disclosure.
OWNERSHIP_CLAIMS: tuple[str, ...] = (
    "olympus-owned", "olympus owns", "independently trained",
    "olympus-native model", "owned by olympus",
)

#: What must accompany such a claim for it to be narrow enough to be true.
REQUIRED_QUALIFIERS: tuple[str, ...] = (
    "no foreign weights", "trained from an olympus dataset",
    "olympus pipeline", "independently produced",
)


def check_ownership_claims_are_narrow(card: Mapping[str, Any]) -> list[Finding]:
    """An ownership claim on the card is accompanied by what it rests on.

    Ownership is not a property of a file's location. A checkpoint that was
    initialised from someone else's weights is not Olympus-owned wherever it
    lives, and an architecture transcribed from a paper is Olympus-implemented
    rather than Olympus-invented. So a claim must travel with the disclosure
    that makes it narrow.
    """
    text = " ".join(str(v) for v in _flatten(card)).lower()
    findings: list[Finding] = []
    claimed = [c for c in OWNERSHIP_CLAIMS if c in text]
    if claimed and not any(q in text for q in REQUIRED_QUALIFIERS):
        findings.append(Finding(
            check="ownership_claim",
            location="model_card",
            detail="the card claims Olympus ownership without stating what it "
                   "rests on; a claim with no disclosure beside it is a claim "
                   "nobody can check",
            context={"claims": claimed,
                     "expected_one_of": list(REQUIRED_QUALIFIERS)}))
    if card.get("weights_trained_on_real_data") and not card.get("real_data_source"):
        findings.append(Finding(
            check="ownership_claim", location="model_card",
            detail="the card claims weights trained on real data without "
                   "naming the source"))
    return findings


def _flatten(node: Any) -> Iterable[Any]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield key
            yield from _flatten(value)
    elif isinstance(node, (list, tuple, set)):
        for value in node:
            yield from _flatten(value)
    else:
        yield node


# ===========================================================================
# the whole audit
# ===========================================================================

def audit(card: Mapping[str, Any] | None = None, *,
          checkpoints: Sequence[Any] = (),
          configurations: Sequence[Mapping[str, Any]] = (),
          root: Path | None = None) -> list[Finding]:
    """All six checks. Empty is the only passing state."""
    findings: list[Finding] = []
    findings.extend(check_no_foreign_imports(root))
    findings.extend(check_no_foreign_weight_sources(root))
    findings.extend(check_exemptions_are_still_needed(root))
    for checkpoint in checkpoints:
        findings.extend(check_checkpoint_origin(checkpoint))
    for index, configuration in enumerate(configurations):
        findings.extend(check_configuration(configuration,
                                            label=f"config[{index}]"))
    if card is not None:
        findings.extend(check_model_card_discloses_methods(card))
        findings.extend(check_dependencies_disclosed(card, root))
        findings.extend(check_ownership_claims_are_narrow(card))
    return findings


def assert_original(**kwargs: Any) -> None:
    """`audit`, raising. For a pipeline that must not ship a violation."""
    findings = audit(**kwargs)
    if findings:
        raise ConfigurationError(
            "originality audit failed",
            findings=[f.to_dict() for f in findings])


def audit_report(**kwargs: Any) -> dict:
    findings = audit(**kwargs)
    by_check: dict[str, list] = {}
    for finding in findings:
        by_check.setdefault(finding.check, []).append(finding.to_dict())
    return {"clean": not findings, "n_findings": len(findings),
            "by_check": by_check,
            "checks_run": ["no_foreign_imports", "no_foreign_weight_sources",
                           "stale_exemption", "checkpoint_origin",
                           "configuration", "method_disclosure",
                           "dependency_disclosure", "ownership_claim"],
            "marker_exemptions": dict(MARKER_EXEMPT),
            "disclosed_dependencies": dict(DISCLOSED_DEPENDENCIES)}


__all__ = ["FOREIGN_MODULES", "FOREIGN_ARTEFACT_MARKERS", "MARKER_EXEMPT",
           "DISCLOSED_DEPENDENCIES", "REQUIRED_METHOD_DISCLOSURES", "Finding",
           "native_modules", "check_no_foreign_imports",
           "check_no_foreign_weight_sources",
           "check_exemptions_are_still_needed", "check_checkpoint_origin",
           "check_configuration", "check_model_card_discloses_methods",
           "check_dependencies_disclosed", "OWNERSHIP_CLAIMS",
           "REQUIRED_QUALIFIERS", "check_ownership_claims_are_narrow", "audit",
           "assert_original", "audit_report"]
