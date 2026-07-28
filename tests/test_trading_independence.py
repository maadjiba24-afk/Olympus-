"""Olympus does not depend on Kronos — gates G1 and G5.

The claim these tests make precise:

> No Olympus trading module imports Kronos, names Kronos in an identifier, or
> carries "kronos" in a string that is used as a runtime value. Deleting the
> two Kronos modules breaks nothing but their own tests.

What is deliberately **not** forbidden is citation. `features.py` refers to
`KRONOS_TEARDOWN §12.9` five times because that defect is the reason
`causal_window_normalise` exists; `errors.py` explains that its "never silently
corrupt" rule comes from the tokenizer bug. Deleting those comments would delete
the reasoning, not a dependency. So the rule is scoped to **imports,
identifiers, and runtime string values** — the things that make code actually
depend on something — and docstrings and comments are exempt by design.

That distinction is the whole reason this is an AST test rather than a grep: a
grep cannot tell a citation from a coupling.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

TRADING = Path(__file__).resolve().parent.parent / "olympus" / "trading"

#: The Kronos implementation. Allowed to be about Kronos; that is its job.
KRONOS_MODULES = {"kronos_adapter.py", "kronos_runtime.py"}

#: `__init__.py`'s lazy-import table maps attribute names to module paths, so it
#: necessarily contains the two module names. This is the one enumerated
#: exemption, and it is narrow: only string constants that are exactly a Kronos
#: module's name or dotted path are permitted, and only in this file.
LAZY_TABLE_EXEMPT = {"kronos_adapter", "kronos_runtime",
                     ".kronos_adapter", ".kronos_runtime"}


def olympus_modules() -> list[Path]:
    """Every trading module that is not part of the Kronos implementation."""
    return sorted(p for p in TRADING.rglob("*.py")
                  if p.name not in KRONOS_MODULES)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_the_exemption_list_is_not_silently_empty():
    """A structural test that scans nothing passes vacuously."""
    modules = olympus_modules()
    assert len(modules) > 40, f"expected the whole trading package, got {len(modules)}"
    for name in KRONOS_MODULES:
        assert (TRADING / name).exists(), f"{name} is declared but absent"


# --- G1a: no imports ---------------------------------------------------------

def test_no_olympus_module_imports_kronos():
    """The strongest form of the claim: not one import, anywhere."""
    offenders = []
    for path in olympus_modules():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ImportFrom):
                target = (node.module or "")
                names = [a.name for a in node.names]
                if "kronos" in target.lower() or any("kronos" in n.lower() for n in names):
                    offenders.append(f"{path.name}:{node.lineno} from {target} import "
                                     + ", ".join(names))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "kronos" in alias.name.lower():
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
    assert offenders == [], (
        "no Olympus trading module may import the Kronos implementation:\n"
        + "\n".join(offenders))


# --- G1b: no identifiers -----------------------------------------------------

def test_no_olympus_identifier_names_kronos():
    """Classes, functions, arguments and assigned names must be model-neutral.

    A `KronosMomentumStrategy` in a module that works with any forecast is a
    module that reads as Kronos-specific to every future maintainer, whatever
    its body does.
    """
    offenders = []
    for path in olympus_modules():
        for node in ast.walk(parse(path)):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.arg):
                name = node.arg
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                name = node.attr
            if name and "kronos" in name.lower():
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert offenders == [], (
        "no Olympus trading identifier may name Kronos:\n" + "\n".join(offenders))


def test_no_olympus_dataclass_field_names_kronos():
    """Field names become dict keys and audit-record keys, so they outlive the
    class that declared them."""
    offenders = []
    for path in olympus_modules():
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if "kronos" in node.target.id.lower():
                    offenders.append(f"{path.name}:{node.lineno} {node.target.id}")
    assert offenders == [], "\n".join(offenders)


# --- G1c: no runtime strings -------------------------------------------------

def _string_constants(tree: ast.Module) -> list[tuple[int, str]]:
    """Every string literal that is not a docstring.

    Docstrings are the citations this test deliberately permits, so they are
    removed first — by identity, not by heuristic.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def test_no_olympus_runtime_string_names_kronos():
    """Source labels, strategy ids, registry keys and reason codes.

    These are what actually travel: a `source="kronos"` ends up in a persisted
    `Signal`, and a `key="kronos_forecast"` ends up in an agent registry. A
    string that reaches a record is a dependency in a way a comment is not.
    """
    offenders = []
    for path in olympus_modules():
        exempt = LAZY_TABLE_EXEMPT if path.name == "__init__.py" else set()
        for lineno, text in _string_constants(parse(path)):
            if "kronos" not in text.lower():
                continue
            if text in exempt:
                continue
            # A multi-line prose constant is a citation that happens not to be a
            # docstring — the module-level notes in `contracts.py`, say. The
            # rule targets short label-shaped values.
            if len(text) > 120 or "\n" in text:
                continue
            offenders.append(f"{path.name}:{lineno} {text!r}")
    assert offenders == [], (
        "no Olympus trading runtime string may name Kronos:\n"
        + "\n".join(offenders))


def test_the_lazy_import_exemption_is_only_used_by_init():
    """The one exemption must not spread."""
    for path in olympus_modules():
        if path.name == "__init__.py":
            continue
        for _, text in _string_constants(parse(path)):
            assert text not in LAZY_TABLE_EXEMPT, (
                f"{path.name} uses the lazy-import exemption, which belongs to "
                "__init__.py alone")


# --- G5: deleting Kronos breaks nothing --------------------------------------

def test_every_olympus_module_imports_without_the_kronos_modules_present():
    """Gate G5, executed rather than asserted.

    Runs in a subprocess with the two Kronos modules blocked at import time. If
    any Olympus module imports one — even lazily, even in a function that runs
    at import — this fails with the offender named.

    A subprocess because the check has to poison `sys.modules`, and doing that
    in-process would leave every later test in this session holding stale
    module objects. That mistake was made once already in this suite and is not
    worth repeating.
    """
    probe = r"""
import importlib, pkgutil, sys

class Blocker:
    def find_module(self, name, path=None):
        return self if name.split(".")[-1] in ("kronos_adapter", "kronos_runtime") else None
    def load_module(self, name):
        raise ImportError(f"BLOCKED: {name}")
    # PEP 451
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[-1] in ("kronos_adapter", "kronos_runtime"):
            raise ImportError(f"BLOCKED: {name}")
        return None

sys.meta_path.insert(0, Blocker())

import olympus.trading as T
failures = []
for info in pkgutil.walk_packages(T.__path__, prefix="olympus.trading."):
    leaf = info.name.split(".")[-1]
    if leaf in ("kronos_adapter", "kronos_runtime"):
        continue
    try:
        importlib.import_module(info.name)
    except ImportError as exc:
        if "BLOCKED" in str(exc):
            failures.append(f"{info.name} -> {exc}")
    except Exception:
        pass          # unrelated import problems are not this test's business
print("FAILURES:" + ";".join(failures))
"""
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=180)
    line = [ln for ln in result.stdout.splitlines() if ln.startswith("FAILURES:")]
    assert line, f"probe did not run: {result.stdout}\n{result.stderr}"
    failures = line[0][len("FAILURES:"):].strip()
    assert failures == "", (
        "these modules reach the Kronos implementation, so deleting it would "
        "break Olympus:\n" + failures.replace(";", "\n"))


# --- the Kronos side is allowed to be about Kronos ---------------------------

def test_the_kronos_modules_are_still_present_and_are_about_kronos():
    """Decoupling is not deletion. Kronos stays as a replaceable provider,
    a benchmark and a challenger — it simply stops being depended upon."""
    from olympus.trading import kronos_adapter, kronos_runtime
    assert kronos_adapter.KronosForecaster
    assert kronos_runtime.ModelBackend


def test_the_retired_identifiers_are_recorded_with_their_successors():
    """A reason code is permanently stable once shipped, so it cannot simply
    vanish: an audit entry written before the rename still carries the old
    string and has to remain interpretable."""
    from olympus.trading import kronos_adapter as K
    from olympus.trading import strategy as S
    assert K.RETIRED_REASON_CODES["KRONOS_FORECAST_DIRECTIONAL"] == \
        S.REASON_FORECAST_DIRECTIONAL
    assert K.RETIRED_STRATEGY_IDS["kronos-momentum"] == S.ForecastMomentumStrategy.id


def test_a_retired_strategy_id_is_not_reused():
    """A renamed strategy must not inherit the old one's performance history."""
    from olympus.trading import kronos_adapter as K
    from olympus.trading import strategy as S
    assert S.ForecastMomentumStrategy.id not in K.RETIRED_STRATEGY_IDS


# --- G2 / G16: the native package -------------------------------------------

NATIVE = TRADING / "native"

#: Constants the Kronos weights impose on Kronos. A native model that inherited
#: one would be a Kronos derivative wearing Olympus names, whatever its module
#: is called. Listed in docs/OLYMPUS_KRONOS_DEPENDENCY_MAP.md §2.
KRONOS_IMPOSED = ("KRONOS_FEATURES", "TEMPORAL_FEATURES", "s1_bits", "s2_bits",
                  "max_context", "BSQ", "BinarySphericalQuantization")


def native_modules() -> list[Path]:
    return sorted(NATIVE.rglob("*.py"))


def test_the_native_package_exists_and_is_scanned():
    modules = native_modules()
    assert len(modules) >= 6, f"expected the native package, got {len(modules)}"


def test_any_kronos_mention_in_native_is_confined_to_the_module_docstring():
    """G2's prose half, made mechanical.

    The G1 tests above already cover `native/` for imports, identifiers and
    runtime strings, because `olympus_modules()` walks the whole package. What
    this adds is a rule about *where* prose may mention Kronos at all.

    A module docstring may: `data.py` opens by saying that every leakage defect
    the teardown found was a data-pipeline defect, which is why the embargo
    exists, and `__init__.py` states outright that Kronos is a benchmark and not
    an ancestor. Both are provenance a reviewer should meet at the top of the
    file.

    The *body* may not. A comment three hundred lines down reasoning about what
    Kronos does is a native module deriving its design from Kronos, and the
    difference between that and a citation is exactly the line this test draws.
    """
    offenders = []
    for path in native_modules():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        docstring_end = 0
        body = tree.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstring_end = body[0].value.end_lineno or 0
        for lineno, line in enumerate(text.splitlines(), 1):
            if "kronos" in line.lower() and lineno > docstring_end:
                offenders.append(f"native/{path.name}:{lineno} {line.strip()[:70]}")
    assert offenders == [], (
        "a native module may cite Kronos in its module docstring and nowhere "
        "else:\n" + "\n".join(offenders))


def test_no_native_module_imports_a_kronos_source_module():
    """The named structural check: no native module imports Kronos source.

    `test_no_olympus_module_imports_kronos` above already covers this, because
    `olympus_modules()` walks the whole trading package including `native/`.
    This exists as a *separate, named* test anyway, for two reasons. It states
    the guarantee in the vocabulary the phase plan uses, so a reader looking for
    "the test that proves the native model does not import Kronos" finds one
    rather than an argument about coverage. And it checks every import at any
    depth — inside a function, inside a class, inside a conditional — where the
    G1 test's rule about *identifiers* would catch the name but not necessarily
    the import statement that a lazy loader would use.

    A lazy import inside a function is exactly how a dependency gets
    reintroduced without anyone editing an import block.
    """
    forbidden = {"kronos", "kronos_adapter", "kronos_runtime"}
    offenders = []
    for path in native_modules():
        for node in ast.walk(parse(path)):       # any depth, not module scope
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                parts = {part.lower() for part in name.split(".")}
                if parts & forbidden or "kronos" in name.lower():
                    offenders.append(f"native/{path.name}:{node.lineno} {name}")
    assert offenders == [], (
        "a native module imports Kronos source:\n" + "\n".join(offenders))


def test_no_native_module_reads_an_external_weight_or_vocabulary_file():
    """A codebook loaded from somewhere else is a borrowed representation.

    `representations.py` builds two quantised candidates, and a codebook is the
    one component here that could plausibly be initialised from a published
    artefact rather than trained. It is not: the check is that no native module
    contains a path or hub identifier of the shape a weight file has.
    `checkpoint.assert_olympus_origin` enforces the same thing at runtime; this
    is the same rule applied to the source.
    """
    suspicious = (".safetensors", ".ckpt", ".pt\"", ".pt'", "pytorch_model",
                  "from_pretrained", "hf_hub", "huggingface", "torch.hub",
                  "load_state_dict_from_url")
    offenders = []
    for path in native_modules():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            for marker in suspicious:
                if marker in lowered:
                    offenders.append(f"native/{path.name}:{lineno} {line.strip()[:70]}")
    assert offenders == [], (
        "a native module references an external weight source:\n"
        + "\n".join(offenders))


@pytest.mark.parametrize("constant", KRONOS_IMPOSED)
def test_no_native_module_inherits_a_kronos_imposed_constant(constant):
    """These exist because the pretrained weights require them. Arriving at the
    same six-column feature order by our own argument is fine; importing the
    constant that encodes somebody else's is not."""
    offenders = [f"native/{p.name}" for p in native_modules()
                 if constant in p.read_text(encoding="utf-8")]
    assert offenders == [], (
        f"{constant} is a Kronos-imposed constant: " + ", ".join(offenders))


def test_no_native_module_imports_torch_at_module_scope():
    """The trading core is pure stdlib and `tests/test_deps_claim.py` guards it.
    When the neural work lands, torch is imported lazily inside functions behind
    a `native` extra — never at module scope."""
    offenders = []
    for path in native_modules():
        tree = parse(path)
        for node in tree.body:                    # module scope only
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names]
                         + [getattr(node, "module", "") or ""])
                for name in names:
                    if name.split(".")[0] in {"torch", "numpy", "scipy",
                                              "pandas", "sklearn"}:
                        offenders.append(f"native/{path.name}:{node.lineno} {name}")
    assert offenders == [], "\n".join(offenders)


def test_the_native_package_is_covered_by_the_kernel_audit():
    """G16. Added from the first commit rather than when it grows a model: the
    moment to prove a component cannot reach the kernel is before there is
    anything in it worth being tempted by."""
    from olympus.trading import kernel as K
    covered = {m for m in K.EVOLUTION_MODULES if m.startswith("olympus.trading.native")}
    assert len(covered) >= 6, f"native modules missing from the audit: {covered}"
    assert K.audit_evolution_modules() == []


def test_the_native_package_cannot_reach_the_safety_kernel():
    from olympus.trading import kernel as K
    findings = K.audit_evolution_modules(
        [m for m in K.EVOLUTION_MODULES if ".native" in m])
    assert findings == [], "\n".join(str(f) for f in findings)


def test_a_native_checkpoint_cannot_be_seeded_from_foreign_weights():
    """G3, at the boundary rather than by policy."""
    from olympus.trading.native.checkpoint import (ForeignWeightsRefused,
                                                   assert_olympus_origin)
    for origin in ("https://huggingface.co/x/y", "/weights/kronos.safetensors",
                   "shiyu-coder/Kronos-small", "~/model.pt"):
        with pytest.raises(ForeignWeightsRefused):
            assert_olympus_origin(origin)
    assert assert_olympus_origin(None) == ""


def test_the_native_forecaster_is_a_peer_of_the_baselines():
    """Independence is only useful if the native model plugs into the same
    interface everything else does."""
    from olympus.trading.forecast import Forecaster
    from olympus.trading.native.forecaster import NativeForecaster
    assert issubclass(NativeForecaster, Forecaster)


# --- the generalised surface still works -------------------------------------

def test_the_value_verdict_asks_the_same_questions_of_any_model():
    """`model_is_valuable` must not know or care which model it is judging."""
    from olympus.trading import evaluate as E
    import inspect
    source = inspect.getsource(E.value_verdict)
    assert "kronos" not in source.lower()
    assert E.model_is_valuable(None) is False, "no evidence means not valuable"


def test_the_standing_hypothesis_is_model_agnostic():
    from olympus.trading import hypotheses as H
    native = H.model_conditional_value("olympus-native")
    third_party = H.model_conditional_value("some-other-model")
    assert native.proposal_id != third_party.proposal_id
    for proposal in (native, third_party):
        assert proposal.contradicting_evidence, (
            "a standing hypothesis with only the case *for* a model would be "
            "an advocacy document")


def test_a_signal_generator_must_name_the_forecaster_it_reads():
    """No default. A signal's provenance should be the model that produced it,
    never whichever model happened to be first."""
    from olympus.trading import signals as G
    from olympus.trading.errors import ConfigurationError
    with pytest.raises(TypeError):
        G.ForecastSignalGenerator()                    # forecast_name required
    with pytest.raises(ConfigurationError):
        G.ForecastSignalGenerator(forecast_name="  ")
    assert G.ForecastSignalGenerator(forecast_name="native").source == "native"


def test_the_forecast_strategy_carries_its_forecasters_name(monkeypatch):
    from olympus.trading import strategy as S
    built = S.ForecastMomentumStrategy(signal_source="olympus-native")
    assert built.parameters()["signal_source"] == "olympus-native"
