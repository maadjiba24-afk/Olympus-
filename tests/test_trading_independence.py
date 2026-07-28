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
