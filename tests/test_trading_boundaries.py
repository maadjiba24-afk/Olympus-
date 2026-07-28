"""Structural guarantees of the trading domain.

These tests do not exercise behaviour — they assert the *shape* of the system,
because the central safety claim ("no model can submit an order") is a
structural property, not a runtime one. A runtime check can be bypassed by the
next person who adds a convenience import; a structural test fails the build.

Three properties are pinned here:

1. **The layer boundary graph** (docs/TRADING_ARCHITECTURE.md §6). Analysis may
   not import execution, forecasters may not import brokers, strategies may not
   import risk limits or the OMS, and so on. Enforced by parsing each module's
   AST — no import side effects, so this works even for modules whose optional
   backends are absent.

2. **No new required dependencies.** Olympus ships with three required deps and
   that is a product property worth defending. Every trading module must import
   cleanly with nothing else installed; torch/numpy/pandas may appear only
   inside function bodies (lazy), never at module scope.

3. **The trading core imports without optional backends.** A plain
   `import olympus.trading` must not drag in torch.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TRADING_DIR = pathlib.Path(__file__).resolve().parent.parent / "olympus" / "trading"


def _modules() -> dict[str, pathlib.Path]:
    """Every trading module that currently exists, keyed by dotted suffix."""
    found = {}
    if not TRADING_DIR.is_dir():
        return found
    for path in sorted(TRADING_DIR.rglob("*.py")):
        rel = path.relative_to(TRADING_DIR).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        found[".".join(parts) if parts else "__init__"] = path
    return found


def _imports(path: pathlib.Path) -> list[tuple[str, int, bool]]:
    """(imported_module, lineno, is_module_scope) for every import in a file.

    `is_module_scope` distinguishes a real dependency from a lazily-imported
    optional backend. We compute it by walking only the top-level body rather
    than every node, so an import nested in a function or a `try:` inside a
    function is correctly classified as lazy.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    top_level_nodes = set()

    # Nodes whose bodies run at IMPORT time, so an import inside them is still
    # an import-time dependency. A `def`/`async def`/`lambda` body is the
    # opposite — it runs only when called, which is exactly what makes a lazy
    # optional backend lazy — so those are NOT descended into. Getting this
    # wrong flags correct lazy imports as violations.
    _EXECUTES_AT_IMPORT = (ast.If, ast.Try, ast.With, ast.For, ast.While,
                           ast.ClassDef, ast.Module)

    def _mark(nodes):
        for node in nodes:
            top_level_nodes.add(id(node))
            if not isinstance(node, _EXECUTES_AT_IMPORT):
                continue
            for attr in ("body", "orelse", "finalbody"):
                _mark(getattr(node, attr, []) or [])
            for handler in getattr(node, "handlers", []) or []:
                _mark(handler.body)

    _mark(tree.body)

    out = []
    for node in ast.walk(tree):
        scope = id(node) in top_level_nodes
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno, scope))
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # relative: .foo / ..foo
                name = "." * node.level + (node.module or "")
            else:
                name = node.module or ""
            out.append((name, node.lineno, scope))
    return out


def _resolve(name: str, module_key: str) -> str | None:
    """Map an import to the trading module it refers to, or None.

    Handles both relative (`.risk`, `from . import risk`) and absolute
    (`olympus.trading.risk`) forms, plus the `from . import a, b` shape where
    the interesting names are in the alias list rather than the module path.
    """
    if name.startswith("olympus.trading."):
        return name[len("olympus.trading."):]
    if name.startswith("."):
        stripped = name.lstrip(".")
        return stripped or None
    return None


# --- 1. the layer boundary graph -------------------------------------------

#: module -> set of trading modules it must NOT import (checked as prefixes, so
#: "brokers" also forbids "brokers.paper"). Derived directly from
#: docs/TRADING_ARCHITECTURE.md §6; keep the two in sync.
FORBIDDEN = {
    # The spine cannot depend on its dependents.
    "contracts": {"instruments", "storage", "validate", "candles", "ta",
                  "features", "regime", "volatility", "sentiment", "forecast",
                  "signals", "strategy", "risk", "killswitch", "portfolio",
                  "oms", "brokers", "execution", "reconcile", "backtest",
                  "perf", "evaluate", "registry", "audit", "modes", "monitor",
                  "agents", "kronos_adapter", "kronos_runtime"},
    "errors": {"contracts", "clock", "risk", "execution", "brokers"},
    "clock": {"contracts", "risk", "execution", "brokers"},

    # Analysis knows nothing about trading.
    "ta": {"forecast", "signals", "strategy", "risk", "execution", "oms",
           "brokers", "portfolio", "backtest"},
    "features": {"forecast", "signals", "strategy", "risk", "execution", "oms",
                 "brokers", "portfolio", "backtest"},
    "regime": {"forecast", "signals", "strategy", "risk", "execution", "oms",
               "brokers", "portfolio", "backtest"},
    "volatility": {"forecast", "signals", "strategy", "risk", "execution",
                   "oms", "brokers", "portfolio", "backtest"},
    "validate": {"forecast", "signals", "strategy", "risk", "execution", "oms",
                 "brokers", "portfolio", "backtest"},
    "candles": {"forecast", "signals", "strategy", "risk", "execution", "oms",
                "brokers", "portfolio", "backtest"},
    "instruments": {"forecast", "signals", "strategy", "risk", "execution",
                    "oms", "brokers", "portfolio", "backtest"},
    "storage": {"forecast", "signals", "strategy", "risk", "execution", "oms",
                "brokers", "portfolio", "backtest"},

    # A forecaster must never be able to trade.
    "kronos_runtime": {"signals", "strategy", "risk", "killswitch", "execution",
                       "oms", "brokers", "portfolio", "reconcile"},
    "kronos_adapter": {"signals", "strategy", "risk", "killswitch", "execution",
                       "oms", "brokers", "portfolio", "reconcile"},
    "forecast": {"strategy", "risk", "killswitch", "execution", "oms",
                 "brokers", "reconcile"},
    "signals": {"strategy", "risk", "killswitch", "execution", "oms",
                "brokers", "reconcile"},

    # THE structural guarantee: a strategy has no route to a venue.
    "strategy": {"brokers", "oms", "execution", "reconcile", "killswitch"},

    # External content must never reach limits, credentials, or execution.
    "sentiment": {"risk", "killswitch", "execution", "oms", "brokers",
                  "portfolio", "reconcile", "modes", "registry"},

    # Determinism: the risk engine touches no venue, no forecaster, no LLM.
    "risk": {"brokers", "execution", "reconcile", "strategy", "signals",
             "forecast", "kronos_adapter", "kronos_runtime", "sentiment",
             "backtest"},
    "killswitch": {"brokers", "execution", "reconcile", "strategy", "signals",
                   "forecast", "kronos_adapter", "sentiment", "backtest"},

    # Accounting does not reach up the stack.
    "portfolio": {"strategy", "signals", "forecast", "kronos_adapter",
                  "execution", "brokers", "backtest"},
    "oms": {"strategy", "signals", "forecast", "kronos_adapter", "backtest"},
    "brokers": {"strategy", "signals", "forecast", "kronos_adapter", "risk",
                "backtest"},
    "brokers.base": {"strategy", "signals", "forecast", "kronos_adapter",
                     "risk", "backtest"},
    "brokers.paper": {"strategy", "signals", "forecast", "kronos_adapter",
                      "risk", "backtest"},

    # Execution consumes a decision, never an opinion.
    "execution": {"strategy", "signals", "forecast", "kronos_adapter",
                  "kronos_runtime", "sentiment", "backtest"},
    "reconcile": {"strategy", "signals", "forecast", "kronos_adapter",
                  "sentiment", "backtest"},
}


@pytest.mark.parametrize("module_key", sorted(FORBIDDEN))
def test_layer_boundaries_are_respected(module_key):
    """No trading module may import across a forbidden layer boundary."""
    modules = _modules()
    path = modules.get(module_key)
    if path is None:
        pytest.skip(f"olympus/trading/{module_key.replace('.', '/')}.py not present")

    forbidden = FORBIDDEN[module_key]
    violations = []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for name, lineno, _scope in _imports(path):
        target = _resolve(name, module_key)
        if target is None:
            continue
        head = target.split(".")[0]
        if head in forbidden or target in forbidden:
            violations.append(f"{module_key} imports {target} (line {lineno})")

    # `from . import risk, portfolio` puts the names in the alias list, not the
    # module path, so that shape needs its own pass.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and not node.module:
            for alias in node.names:
                head = alias.name.split(".")[0]
                if head in forbidden:
                    violations.append(
                        f"{module_key} imports {alias.name} (line {node.lineno})")

    assert not violations, (
        "layer boundary violation (see docs/TRADING_ARCHITECTURE.md §6):\n  "
        + "\n  ".join(sorted(set(violations))))


def test_strategy_module_has_no_execution_surface():
    """The load-bearing case, asserted on its own so a failure is unmissable.

    If `strategy.py` can reach a broker, the OMS, or the execution engine, then
    the claim "no model can submit an order" is false regardless of what any
    policy document says.
    """
    modules = _modules()
    path = modules.get("strategy")
    if path is None:
        pytest.skip("olympus/trading/strategy.py not present")

    banned = {"brokers", "oms", "execution", "reconcile"}
    hits = []
    for name, lineno, _ in _imports(path):
        target = _resolve(name, "strategy")
        if target and target.split(".")[0] in banned:
            hits.append(f"{target} (line {lineno})")
    assert not hits, (
        "strategy.py must have NO route to a venue; found: " + ", ".join(hits))


#: Modules that must be *deterministic*: same inputs ⟹ same outputs, always.
#: A risk engine that consults the wall clock, a random number generator, the
#: network, or a language model is not auditable — you cannot replay a decision
#: and get the same answer, which defeats the entire point of recording it.
DETERMINISTIC = ("risk", "killswitch", "oms", "perf")

#: Import names that break determinism. `random`/`secrets` introduce
#: unreproducibility; `time`/`datetime` smuggle in wall-clock reads that should
#: come from the injected clock; the network and LLM stacks introduce both.
NONDETERMINISTIC_IMPORTS = {
    "random", "secrets", "socket", "urllib", "http", "requests", "httpx",
    "aiohttp", "anthropic", "openai",
}

#: Olympus modules that pull in the LLM stack. A deterministic module reaching
#: any of these means an LLM can influence an authorisation decision.
LLM_MODULES = {"llm", "agent", "orchestrator", "specialists", "moa",
               "consensus", "subagents", "tools"}


@pytest.mark.parametrize("module_key", DETERMINISTIC)
def test_deterministic_modules_have_no_nondeterministic_imports(module_key):
    """The risk engine's decisions must be replayable.

    `datetime`/`time` are permitted only for *type* usage, which is why this
    checks imports of the RNG, network, and LLM stacks rather than banning
    `datetime` outright — the clock discipline is enforced separately by
    `test_deterministic_modules_do_not_read_the_wall_clock`.
    """
    modules = _modules()
    path = modules.get(module_key)
    if path is None:
        pytest.skip(f"olympus/trading/{module_key}.py not present")

    violations = []
    for name, lineno, _scope in _imports(path):
        head = name.split(".")[0]
        if head in NONDETERMINISTIC_IMPORTS:
            violations.append(f"`{name}` (line {lineno})")
        if name.startswith("olympus."):
            sub = name.split(".")[1] if "." in name else ""
            if sub in LLM_MODULES:
                violations.append(f"`{name}` — LLM stack (line {lineno})")
        # `from . import x` / `from .. import x` reaching the LLM stack
        if name in ("..", ".."):
            continue
    assert not violations, (
        f"{module_key}.py must be deterministic and must not reach the RNG, "
        f"the network, or the LLM stack; found: {', '.join(violations)}")


@pytest.mark.parametrize("module_key", DETERMINISTIC)
def test_deterministic_modules_do_not_read_the_wall_clock(module_key):
    """Time must arrive through the injected `Clock`, never `datetime.now()`.

    A wall-clock read inside the risk engine makes a decision unreplayable and
    silently breaks the backtest/live equivalence the whole design rests on.
    """
    modules = _modules()
    path = modules.get(module_key)
    if path is None:
        pytest.skip(f"olympus/trading/{module_key}.py not present")

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    banned = {("datetime", "now"), ("datetime", "utcnow"), ("date", "today"),
              ("time", "time"), ("time", "monotonic")}
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        owner = node.func.value
        owner_name = getattr(owner, "id", None) or getattr(owner, "attr", None)
        if (owner_name, attr) in banned:
            hits.append(f"{owner_name}.{attr}() at line {node.lineno}")
    assert not hits, (
        f"{module_key}.py reads the wall clock directly ({', '.join(hits)}); "
        "take a `clock` parameter instead — see olympus/trading/clock.py")


# --- 2. no new required dependencies ---------------------------------------

#: Third-party packages that may never be imported at module scope anywhere in
#: the trading domain. They are permitted inside function bodies, guarded by
#: errors.DependencyMissing.
HEAVY = {"torch", "numpy", "pandas", "scipy", "sklearn", "matplotlib",
         "plotly", "einops", "huggingface_hub", "safetensors", "yaml",
         "requests", "aiohttp", "ccxt"}


def test_no_heavy_imports_at_module_scope():
    """Optional backends must be imported lazily, never at import time."""
    violations = []
    for key, path in sorted(_modules().items()):
        for name, lineno, module_scope in _imports(path):
            if not module_scope:
                continue                      # lazy: fine
            head = name.split(".")[0]
            if head in HEAVY:
                violations.append(f"{key}: `{name}` at module scope (line {lineno})")
    assert not violations, (
        "Olympus has 3 required dependencies and the trading core must not add "
        "any. Move these inside the function that needs them and raise "
        "errors.DependencyMissing when absent:\n  " + "\n  ".join(violations))


def test_trading_package_imports_without_optional_backends():
    """`import olympus.trading` must be cheap and must not require torch.

    Run in a SUBPROCESS, deliberately. The obvious in-process version — evict
    `olympus.trading.*` from `sys.modules` and re-import — silently corrupts the
    rest of the suite: sibling test modules that already did
    `from olympus.trading.errors import ConfigurationError` keep a reference to
    the *old* class object, so a later `pytest.raises(ConfigurationError)`
    stops matching the freshly-imported class and fails for reasons that have
    nothing to do with the code under test. A subprocess also tests the real
    claim more honestly: what a *fresh interpreter* pulls in.
    """
    import subprocess
    import sys

    probe = (
        "import sys, olympus.trading as t;"
        "assert t.SCHEMA_VERSION >= 1;"
        "heavy = sorted(m for m in ('torch','numpy','pandas','scipy') "
        "               if m in sys.modules);"
        "print(','.join(heavy))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent))
    assert proc.returncode == 0, (
        f"`import olympus.trading` failed in a clean interpreter:\n{proc.stderr}")
    pulled = proc.stdout.strip()
    assert not pulled, (
        f"importing olympus.trading pulled in {pulled} — the lazy-import "
        "contract in olympus/trading/__init__.py is broken")


def test_public_contracts_are_exported():
    """The spine's public surface is part of the contract other modules code
    against; losing an export silently breaks callers."""
    import olympus.trading as t

    for name in ("Instrument", "Candle", "Quote", "ForecastResult",
                 "TradeIntent", "RiskDecision", "Order", "Fill", "Position",
                 "PortfolioSnapshot", "Mode", "Side", "Verdict",
                 "DataQualityReport", "Signal"):
        assert hasattr(t, name), f"olympus.trading.{name} is no longer exported"


# --- 3. every trading module is importable ---------------------------------

def test_every_trading_module_imports_cleanly():
    """A module that cannot be imported cannot be safe, tested, or audited."""
    import importlib

    failures = []
    for key, path in sorted(_modules().items()):
        if key == "__init__":
            continue
        target = f"olympus.trading.{key}"
        try:
            importlib.import_module(target)
        except Exception as exc:                       # noqa: BLE001
            failures.append(f"{target}: {type(exc).__name__}: {exc}")
    assert not failures, "trading modules failed to import:\n  " + "\n  ".join(failures)
