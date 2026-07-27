"""Operator interface for the trading domain.

A read-mostly console. The two write operations it offers — engaging a kill
switch and running a reconciliation — are both *safety* actions, and that is the
whole design: an operator surface should make stopping easy and starting hard.

What this interface deliberately cannot do
------------------------------------------
It cannot enable live trading, edit a risk limit, submit an order, or clear an
automatic kill-switch trip. Those go through `modes.ModeController` (operator
token + nine gates + an audit event), `risk.LimitsStore` (operator token), the
execution engine (needs an approving `RiskDecision`), and an explicit
`operator_override` respectively. Adding a convenient shortcut here would
quietly become the way around all of them, so the shortcuts do not exist.

Engaging a kill switch is offered without ceremony on purpose: the asymmetry
between "stop trading" and "start trading" is the point. Stopping is always
allowed; starting never happens here.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .contracts import Mode, jsonable


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return f"{value:,.8f}".rstrip("0").rstrip(".")
    if isinstance(value, float):
        return f"{value:,.4f}"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _table(rows: Sequence[tuple[str, Any]], *, indent: str = "  ") -> str:
    if not rows:
        return f"{indent}(none)"
    width = max(len(str(k)) for k, _ in rows)
    return "\n".join(f"{indent}{str(k):<{width}}  {_fmt(v)}" for k, v in rows)


class TradingConsole:
    """Renders the state of a wired trading system.

    Every collaborator is optional and duck-typed. A console that raised because
    one subsystem was absent would be useless in exactly the situation an
    operator most needs it — half the system down — so each section degrades to
    a stated error instead.
    """

    def __init__(self, *, mode_controller=None, killswitches=None,
                 portfolio=None, order_store=None, risk_limits=None,
                 broker=None, ingestion=None, strategies=None, registry=None,
                 reconciler=None, audit=None, clock=None, series=()):
        self.mode_controller = mode_controller
        self.killswitches = killswitches
        self.portfolio = portfolio
        self.order_store = order_store
        self.risk_limits = risk_limits
        self.broker = broker
        self.ingestion = ingestion
        self.strategies = strategies
        self.registry = registry
        self.reconciler = reconciler
        self.audit = audit
        self.series = tuple(series)
        if clock is None:
            from .clock import default_clock
            clock = default_clock()
        self.clock = clock

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
        """Run a probe, returning `default` on any failure.

        Broad by design: the console's job is to report, and a subsystem that
        throws is itself the most important thing to report.
        """
        try:
            return fn()
        except Exception as exc:                          # noqa: BLE001
            return default if default is not None else f"error: {exc}"

    # -- sections ----------------------------------------------------------

    def mode(self) -> Mode:
        if self.mode_controller is None:
            return Mode.PAPER
        return self._safe(self.mode_controller.current, Mode.PAPER)

    def status(self) -> dict:
        mode = self.mode()
        engaged = self._safe(
            lambda: [s.to_dict() for s in self.killswitches.engaged()], [])\
            if self.killswitches else []
        return {
            "mode": getattr(mode, "value", str(mode)),
            "live_orders_permitted": bool(getattr(mode, "is_live", False)),
            "kill_switches_engaged": len(engaged) if isinstance(engaged, list) else "unknown",
            "broker": self._safe(lambda: self.broker.health(), {"ok": False})
            if self.broker else None,
            "feeds": [self._safe(lambda s=s: self.ingestion.health(*s).to_dict())
                      for s in self.series] if self.ingestion else [],
            "reconciliation_age_s": self._safe(
                lambda: self.reconciler.age_seconds()) if self.reconciler else None,
            "as_of": jsonable(self.clock.now()),
        }

    def render_status(self) -> str:
        data = self.status()
        mode = data["mode"]
        banner = "LIVE ORDERS PERMITTED" if data["live_orders_permitted"] \
            else "live trading disabled"
        lines = [f"olympus trading — mode: {mode}  ({banner})",
                 f"  as of {data['as_of']}", ""]

        lines.append("kill switches")
        if self.killswitches is None:
            lines.append("  (no registry configured)")
        else:
            engaged = self._safe(lambda: self.killswitches.engaged(), [])
            if isinstance(engaged, str):
                lines.append(f"  {engaged}")
            elif not engaged:
                lines.append("  none engaged")
            else:
                for state in engaged:
                    lines.append(
                        f"  ENGAGED {state.scope.value}:{state.target or '*'} "
                        f"[{state.code}] {state.reason}"
                        f"{' (auto)' if state.auto else ''}")
        lines.append("")

        lines.append("broker")
        lines.append(_table(sorted((data["broker"] or {}).items()))
                     if data["broker"] else "  (none configured)")
        lines.append("")

        lines.append("data feeds")
        if not data["feeds"]:
            lines.append("  (none configured)")
        for feed in data["feeds"]:
            if isinstance(feed, str):
                lines.append(f"  {feed}")
                continue
            lines.append(f"  {feed['series']}: {feed['quality']} "
                         f"age={_fmt(feed['age_s'])}s gaps={feed['gaps_detected']} "
                         f"reconnects={feed['reconnects']}")
        lines.append("")

        lines.append("reconciliation")
        age = data["reconciliation_age_s"]
        lines.append(f"  last success: {_fmt(age)}s ago" if age is not None
                     else "  never succeeded")
        return "\n".join(lines)

    def render_portfolio(self) -> str:
        if self.portfolio is None:
            return "no portfolio configured"
        snapshot = self._safe(lambda: self.portfolio.snapshot())
        if isinstance(snapshot, str):
            return snapshot
        rows = [("cash", snapshot.cash), ("equity", snapshot.equity),
                ("gross exposure", snapshot.gross_exposure),
                ("net exposure", snapshot.net_exposure),
                ("unrealised P&L", snapshot.unrealised_pnl),
                ("realised P&L", snapshot.realised_pnl),
                ("fees paid", snapshot.fees_paid),
                ("daily P&L", self._safe(lambda: self.portfolio.daily_pnl())),
                ("drawdown", self._safe(lambda: self.portfolio.drawdown()))]
        out = ["portfolio", _table(rows), "", "positions"]
        positions = snapshot.open_positions
        if not positions:
            out.append("  (flat)")
        for key, pos in sorted(positions.items()):
            mark = snapshot.marks.get(key)
            out.append(f"  {key:<24} qty={_fmt(pos.quantity):>14} "
                       f"avg={_fmt(pos.average_price):>12} "
                       f"mark={_fmt(mark):>12}")
        return "\n".join(out)

    def render_orders(self) -> str:
        if self.order_store is None:
            return "no order store configured"
        orders = self._safe(lambda: self.order_store.open_orders(), [])
        if isinstance(orders, str):
            return orders
        if not orders:
            return "orders\n  (no open orders)"
        out = ["orders"]
        for o in orders:
            out.append(f"  {o.client_order_id:<28} {o.instrument_key:<20} "
                       f"{o.side.value:<4} {o.order_type.value:<6} "
                       f"{_fmt(o.quantity):>12} {o.status.value:<18} "
                       f"filled={_fmt(o.filled_quantity)}")
        return "\n".join(out)

    def render_strategies(self) -> str:
        if self.strategies is None:
            return "no strategy manager configured"
        records = self._safe(lambda: self.strategies.all(), None)
        if records is None or isinstance(records, str):
            records = self._safe(lambda: list(self.strategies.active()), [])
        if isinstance(records, str):
            return records
        if not records:
            return "strategies\n  (none registered)"
        out = ["strategies"]
        for r in records:
            status = getattr(getattr(r, "status", None), "value", "active")
            ident = getattr(r, "strategy_id", None) or getattr(r, "id", "?")
            out.append(f"  {ident:<28} {status}")
        return "\n".join(out)

    def render_models(self) -> str:
        if self.registry is None:
            return "no model registry configured"
        records = self._safe(lambda: self.registry.list(), [])
        if isinstance(records, str):
            return records
        if not records:
            return "models\n  (none registered)"
        out = ["models"]
        for r in records:
            out.append(f"  {getattr(r, 'model_id', '?'):<32} "
                       f"{getattr(r, 'status', '?'):<10} "
                       f"rev={getattr(r, 'revision', None) or '—'}")
        return "\n".join(out)

    def render_limits(self) -> str:
        if self.risk_limits is None:
            return "no risk limits loaded"
        limits = self.risk_limits
        rows = [(name, getattr(limits, name))
                for name in sorted(getattr(limits, "__dataclass_fields__", {}))]
        head = f"risk limits (policy {self._safe(limits.policy_hash)})"
        return head + "\n" + _table(rows)

    # -- the two write actions --------------------------------------------

    def engage_kill_switch(self, scope: str, target: str = "", *,
                           reason: str, operator: str) -> str:
        """Stop trading. Never gated — the asymmetry is the point."""
        if self.killswitches is None:
            return "no kill-switch registry configured"
        from .killswitch import KillSwitchScope
        state = self.killswitches.engage(
            KillSwitchScope(scope), target, reason=reason, by=operator)
        return f"ENGAGED {state.key}: {state.reason}"

    def reconcile(self) -> str:
        if self.reconciler is None:
            return "no reconciler configured"
        report = self.reconciler.reconcile()
        if report.ok:
            return "reconciliation OK — local and broker state agree"
        lines = ["reconciliation FAILED"]
        for brk in report.breaks:
            lines.append(f"  {brk.kind:<9} {brk.key:<24} "
                         f"local={_fmt(brk.local)} remote={_fmt(brk.remote)}")
        lines.append("  position breaks are NOT auto-corrected — an operator "
                     "must decide whether a fill was missed or the venue is wrong")
        return "\n".join(lines)

    def verify_audit(self) -> str:
        if self.audit is None:
            return "no audit trail configured"
        result = self._safe(lambda: self.audit.verify(), {})
        if isinstance(result, str):
            return result
        verdict = "VERIFIED" if result.get("ok") else "TAMPERED OR BROKEN"
        rows = [("result", verdict), ("events", result.get("count")),
                ("verified prefix", result.get("verified")),
                ("attested", result.get("attested"))]
        out = ["audit trail", _table(rows)]
        for problem in result.get("problems", [])[:5]:
            out.append(f"  ! {problem}")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------

def build_parser(prog: str = "olympus trading") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="Operator console for the Olympus trading domain")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="mode, kill switches, feeds, broker, reconciliation")
    status.add_argument("--json", action="store_true",
                        help="machine-readable output")
    sub.add_parser("portfolio", help="cash, exposure, positions, P&L, drawdown")
    sub.add_parser("orders", help="open orders")
    sub.add_parser("strategies", help="registered strategies and their status")
    sub.add_parser("models", help="model registry")
    sub.add_parser("limits", help="active risk limits and policy hash")
    sub.add_parser("reconcile", help="run a reconciliation against the broker")
    sub.add_parser("verify-audit", help="verify the audit chain")

    kill = sub.add_parser("kill", help="ENGAGE a kill switch (stopping is never gated)")
    kill.add_argument("--scope", choices=["global", "strategy", "instrument"],
                      default="global")
    kill.add_argument("--target", default="")
    kill.add_argument("--reason", required=True)
    kill.add_argument("--operator", required=True)

    return parser


def run(console: TradingConsole, argv: Sequence[str] | None = None,
        parser: argparse.ArgumentParser | None = None) -> str:
    """Dispatch one command against a wired console. Returns text to print."""
    parser = parser or build_parser()
    args = parser.parse_args(list(argv or []))

    if args.command == "status":
        return json.dumps(console.status(), indent=2) \
            if getattr(args, "json", False) else console.render_status()
    if args.command == "portfolio":
        return console.render_portfolio()
    if args.command == "orders":
        return console.render_orders()
    if args.command == "strategies":
        return console.render_strategies()
    if args.command == "models":
        return console.render_models()
    if args.command == "limits":
        return console.render_limits()
    if args.command == "reconcile":
        return console.reconcile()
    if args.command == "verify-audit":
        return console.verify_audit()
    if args.command == "kill":
        return console.engage_kill_switch(
            args.scope, args.target, reason=args.reason, operator=args.operator)
    return parser.format_help()               # pragma: no cover


__all__ = ["TradingConsole", "build_parser", "run"]
