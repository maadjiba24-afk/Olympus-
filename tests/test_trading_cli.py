"""Operator console.

The load-bearing tests are the negative ones: the console must offer no route to
enabling live trading, editing a limit, or submitting an order. An operator
surface that grew a convenient shortcut would quietly become the way around
every gate the rest of the domain enforces.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading import cli as C
from olympus.trading.audit import AuditTrail, EventType
from olympus.trading.brokers import PaperBroker
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Fill, Instrument, Mode, Side)
from olympus.trading.killswitch import KillSwitchRegistry, KillSwitchScope
from olympus.trading.oms import OrderStore
from olympus.trading.portfolio import PortfolioManager
from olympus.trading import risk as R

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="BTCUSDT", exchange="BINANCE",
                  tick_size=Decimal("0.01"), lot_size=Decimal("0.00001"),
                  min_quantity=Decimal("0.00001"), allow_fractional=True)


@pytest.fixture()
def console():
    clock = FixedClock(T0)
    portfolio = PortfolioManager(starting_cash=Decimal("10000"), clock=clock)
    portfolio.apply_fill(Fill(fill_id="f1", client_order_id="c1",
                              instrument_key=INST.key, side=Side.BUY,
                              quantity=Decimal("0.1"), price=Decimal("42000"),
                              ts=T0, fee=Decimal("0.42")), INST)
    portfolio.mark(INST.key, 42500.0)
    broker = PaperBroker(clock=clock, instruments={INST.key: INST})
    broker.connect()
    audit = AuditTrail("cli", clock=clock)
    audit.record(EventType.MARKET_DATA_RECEIVED, {"bars": 1})
    return C.TradingConsole(
        killswitches=KillSwitchRegistry(clock=clock), portfolio=portfolio,
        order_store=OrderStore(clock=clock), broker=broker, audit=audit,
        risk_limits=R.RiskLimits.conservative(), clock=clock)


def run(console, *argv):
    return C.run(console, list(argv))


# --- what it must NOT be able to do ---------------------------------------

def test_the_console_offers_no_command_that_enables_live_trading():
    parser = C.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set()
    for action in actions:
        if isinstance(action.choices, dict):
            commands |= set(action.choices)
    forbidden = {"go-live", "live", "enable-live", "mode", "set-mode",
                 "submit", "order", "trade", "buy", "sell", "set-limit",
                 "limits-set", "disengage", "resume"}
    assert not (commands & forbidden), (
        f"the operator console must not offer {commands & forbidden}: enabling "
        "live trading, editing limits and submitting orders all have their own "
        "gated paths, and a shortcut here would become the way around them")
    assert "kill" in commands and "status" in commands


def test_the_console_has_no_method_that_clears_a_kill_switch():
    """Stopping is easy; starting is not offered at all."""
    methods = {n for n, _ in inspect.getmembers(C.TradingConsole,
                                                predicate=inspect.isfunction)}
    assert "engage_kill_switch" in methods
    for banned in ("disengage_kill_switch", "clear_kill_switch",
                   "enable_live", "set_mode", "submit_order", "set_limits"):
        assert banned not in methods


def test_the_console_module_cannot_reach_execution_or_the_limits_store():
    """Checked on the parsed AST, not the raw text: the module docstring names
    these deliberately, to explain what the console cannot do, and a substring
    scan would flag its own explanation."""
    import ast
    tree = ast.parse(inspect.getsource(C))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = "." * (node.level or 0) + (node.module or "")
            for alias in node.names:
                imported.add(f"{base}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name:
                called.add(name)

    for banned in ("LimitsStore", "ModeController", "ExecutionEngine"):
        assert not any(banned in i for i in imported), (
            f"cli.py imports {banned}; the console is read-mostly and must not "
            "be able to act through that path")
        assert banned not in called, f"cli.py constructs {banned}"
    assert not any(i.startswith(".execution") for i in imported)


# --- status ----------------------------------------------------------------

def test_status_defaults_to_paper_and_says_live_is_disabled(console):
    out = run(console, "status")
    assert "mode: paper" in out
    assert "live trading disabled" in out


def test_status_reports_an_engaged_kill_switch_prominently(console):
    console.killswitches.engage(KillSwitchScope.GLOBAL, reason="manual halt",
                                by="alice")
    out = run(console, "status")
    assert "ENGAGED" in out and "manual halt" in out


def test_status_marks_an_automatic_trip_as_auto(console):
    from olympus.trading.killswitch import CODE_DAILY_LOSS
    console.killswitches.engage(KillSwitchScope.GLOBAL, reason="daily loss",
                                code=CODE_DAILY_LOSS, by="monitor", auto=True)
    assert "(auto)" in run(console, "status")


def test_status_json_is_machine_readable(console):
    import json
    data = json.loads(run(console, "status", "--json"))
    assert data["mode"] == "paper"
    assert data["live_orders_permitted"] is False


def test_status_survives_a_subsystem_that_throws():
    """Half the system being down is exactly when an operator needs this."""
    class Exploding:
        def health(self):
            raise RuntimeError("broker on fire")

    out = C.TradingConsole(broker=Exploding(), clock=FixedClock(T0)).render_status()
    assert "mode: paper" in out          # still renders
    assert out                            # and does not raise


def test_status_with_nothing_configured_still_renders():
    out = C.TradingConsole(clock=FixedClock(T0)).render_status()
    assert "mode: paper" in out
    assert "(no registry configured)" in out


# --- portfolio, orders, models, limits ------------------------------------

def test_portfolio_shows_position_exposure_and_pnl(console):
    out = run(console, "portfolio")
    assert "BINANCE:BTCUSDT" in out
    assert "equity" in out and "gross exposure" in out
    assert "unrealised P&L" in out


def test_portfolio_without_one_configured_says_so():
    assert "no portfolio" in C.TradingConsole().render_portfolio()


def test_orders_reports_none_when_flat(console):
    assert "no open orders" in run(console, "orders")


def test_limits_shows_the_policy_hash(console):
    out = run(console, "limits")
    assert "policy" in out
    assert "max_order_value" in out


def test_models_without_a_registry_says_so(console):
    assert "no model registry" in run(console, "models")


def test_strategies_without_a_manager_says_so(console):
    assert "no strategy manager" in run(console, "strategies")


# --- the two write actions -------------------------------------------------

def test_kill_engages_the_switch_for_real(console):
    out = run(console, "kill", "--scope", "global", "--reason", "spread blowout",
              "--operator", "alice")
    assert "ENGAGED" in out
    assert console.killswitches.is_engaged(KillSwitchScope.GLOBAL) is True


def test_kill_can_target_a_single_instrument(console):
    run(console, "kill", "--scope", "instrument", "--target", INST.key,
        "--reason", "halted", "--operator", "bob")
    assert console.killswitches.check(instrument_key=INST.key) is not None


def test_kill_requires_a_reason_and_an_operator(console):
    with pytest.raises(SystemExit):
        run(console, "kill", "--scope", "global")


def test_reconcile_reports_a_break_and_says_it_is_not_auto_corrected():
    clock = FixedClock(T0)
    broker = PaperBroker(clock=clock, instruments={INST.key: INST})
    broker.connect()
    broker.force_desync(INST.key, Decimal("3"))
    portfolio = PortfolioManager(starting_cash=Decimal("10000"), clock=clock)
    from olympus.trading.reconcile import Reconciler
    console = C.TradingConsole(
        reconciler=Reconciler(broker=broker, order_store=OrderStore(clock=clock),
                              portfolio=portfolio, clock=clock),
        clock=clock)
    out = run(console, "reconcile")
    assert "FAILED" in out
    assert "NOT auto-corrected" in out


def test_reconcile_without_one_configured_says_so(console):
    assert "no reconciler" in run(console, "reconcile")


# --- audit -----------------------------------------------------------------

@pytest.mark.requires_crypto
def test_verify_audit_reports_a_clean_chain(console):
    out = run(console, "verify-audit")
    assert "VERIFIED" in out


@pytest.mark.requires_crypto
def test_verify_audit_reports_tampering():
    from olympus import ledger
    clock = FixedClock(T0)
    audit = AuditTrail("cli-tamper", clock=clock)
    audit.record(EventType.RISK_DECISION, {"verdict": "rejected"})
    path = ledger._path("trading-cli-tamper")
    path.write_text(path.read_text().replace("rejected", "approved"))
    out = C.TradingConsole(audit=AuditTrail("cli-tamper", clock=clock),
                           clock=clock).verify_audit()
    assert "TAMPERED" in out


def test_verify_audit_without_a_trail_says_so():
    assert "no audit trail" in C.TradingConsole().verify_audit()


# --- feeds -----------------------------------------------------------------

def test_status_reports_feed_quality_and_gaps():
    from olympus.trading.contracts import Candle
    from olympus.trading.ingest import (IngestedCandle, IngestionConfig,
                                        IngestionService, ReplayProvider)
    from olympus.trading.storage import CandleStore

    clock = FixedClock(T0 + timedelta(minutes=7, seconds=30))
    bars = []
    for i in (0, 1, 2, 5, 6):                       # 3 and 4 missing
        o = T0 + timedelta(minutes=i)
        bars.append(IngestedCandle(
            candle=Candle(instrument_key=INST.key, timeframe="1m", ts_open=o,
                          ts_close=o + timedelta(minutes=1), open=100, high=101,
                          low=99, close=100.5, volume=1.0),
            received_at=o + timedelta(minutes=1), source="replay"))
    svc = IngestionService(provider=ReplayProvider(bars, clock=clock),
                           store=CandleStore(), clock=clock,
                           config=IngestionConfig(timeframe="1m"))
    svc.backfill(INST.key, "1m", start=T0, end=T0 + timedelta(minutes=10))

    console = C.TradingConsole(ingestion=svc, clock=clock,
                               series=[(INST.key, "1m")])
    out = console.render_status()
    assert "degraded" in out
    assert "gaps=2" in out
