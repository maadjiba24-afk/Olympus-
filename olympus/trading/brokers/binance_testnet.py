"""Binance Spot **Testnet** adapter.

The trading counterpart to `ingest.BinanceSpotProvider`: same venue, same symbol
conventions, but pointed at `testnet.binance.vision`, which is Binance's
sandbox. Balances there are play money issued by the exchange.

⚠️ **This adapter has never completed a real request in this environment.**
Every Binance host — production and testnet alike — is refused by the org egress
policy with HTTP 403 at CONNECT. What is verified here is the request signing,
the response parsing, the idempotency mapping and the error handling, all
against Binance's documented wire formats via a recorded-response transport.
What is *not* verified is that the venue accepts any of it. See
docs/TRADING_STATUS.md before trusting this against a real sandbox.

Safety properties this adapter is built to hold
-----------------------------------------------
* **Testnet only, enforced.** `_require_testnet` refuses any base URL that is
  not a known Binance sandbox host. Pointing this class at `api.binance.com` by
  editing a config string raises rather than trading real money — the one
  mistake that must be impossible to make quietly.
* **Spot only, long only.** No margin endpoints are implemented and a SELL is
  refused unless it is covered by a held position, so the adapter cannot open a
  short even if a strategy proposed one.
* **Credentials never leave the adapter.** The secret is read from
  `olympus.vault` through a `BrokerCredentials` handle, used only to compute an
  HMAC, and never logged, returned, or placed in an exception. `__repr__` shows
  the base URL and nothing else.
* **Idempotency via `newClientOrderId`.** Binance rejects a duplicate client
  order id with code -2010, which is exactly the behaviour `execution.py`'s
  query-then-adopt depends on: a retried submit either creates the one order or
  is refused, never creates a second.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from ..clock import Clock, default_clock
from ..contracts import (AccountSnapshot, Candle, Fill, Instrument, Order,
                         OrderStatus, OrderType, Position, Quote, Side,
                         TimeInForce, ensure_utc, to_decimal)
from ..errors import (BrokerError, BrokerUnavailable, ConfigurationError,
                      DuplicateOrderError, ExecutionError)
from .base import (BrokerAdapter, BrokerCapabilities, BrokerCredentials,
                   resolve_credentials)

#: Hosts recognised as Binance sandboxes. Anything else is refused.
_TESTNET_HOSTS = frozenset({
    "testnet.binance.vision",
    "testnet.binancefuture.com",          # listed only to be explicitly refused
})
_SPOT_TESTNET = "https://testnet.binance.vision"

#: Binance order status -> ours. `EXPIRED_IN_MATCH` is a newer status; mapping
#: it explicitly avoids an unknown status silently becoming "still open".
_STATUS = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "PENDING_CANCEL": OrderStatus.PENDING_CANCEL,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}

_ORDER_TYPE_OUT = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP: "STOP_LOSS",
    OrderType.STOP_LIMIT: "STOP_LOSS_LIMIT",
}

_TIF_OUT = {TimeInForce.GTC: "GTC", TimeInForce.IOC: "IOC",
            TimeInForce.FOK: "FOK", TimeInForce.DAY: "GTC"}

#: Binance error codes worth distinguishing.
_DUPLICATE_CODE = -2010          # "Duplicate order sent" / rejections
_UNKNOWN_ORDER = -2013           # "Order does not exist"


class BinanceTestnetBroker(BrokerAdapter):
    """Binance Spot Testnet behind the Olympus `BrokerAdapter` contract."""

    name = "binance-testnet"
    venue = "BINANCE"

    def __init__(self, *, clock: Clock | None = None,
                 base_url: str = _SPOT_TESTNET,
                 instruments: Mapping[str, Instrument] | None = None,
                 quote_asset: str = "USDT",
                 recv_window_ms: int = 5000,
                 request_timeout: float = 15.0,
                 transport: Callable[..., Any] | None = None):
        self.clock = clock or default_clock()
        self.base_url = self._require_testnet(base_url)
        self._instruments = dict(instruments or {})
        self.quote_asset = quote_asset.upper()
        self.recv_window_ms = int(recv_window_ms)
        self.request_timeout = request_timeout
        #: Injected for tests: a callable with the same shape as `_http`.
        #: Production leaves it None and uses the stdlib path.
        self._transport = transport

        self._credentials: BrokerCredentials | None = None
        self._api_key: str | None = None
        self._api_secret: str | None = None
        self._connected = False

    # -- the guard that makes this class safe to exist --------------------

    @staticmethod
    def _require_testnet(base_url: str) -> str:
        """Refuse anything that is not a Binance spot sandbox.

        Deliberately an allow-list of hosts rather than a "not production"
        check: a typo, a copied config, or a well-meaning edit that swaps in
        `api.binance.com` must fail loudly at construction. Futures testnet is
        named and refused too — it is a derivatives venue, which this vertical
        excludes.
        """
        parsed = urllib.parse.urlparse(base_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            raise ConfigurationError(
                "broker base URL must be https", base_url=base_url)
        if host == "testnet.binancefuture.com":
            raise ConfigurationError(
                "this adapter is spot-only; futures testnet is a derivatives "
                "venue and is out of scope for this vertical", host=host)
        if host not in _TESTNET_HOSTS:
            raise ConfigurationError(
                "refusing a non-testnet Binance host: this adapter may only be "
                "pointed at a sandbox, and live trading is not enabled",
                host=host, allowed=sorted(_TESTNET_HOSTS))
        return base_url.rstrip("/")

    def __repr__(self) -> str:                            # pragma: no cover
        return f"BinanceTestnetBroker(base_url={self.base_url!r})"

    # -- capabilities ------------------------------------------------------

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
            time_in_force=frozenset({TimeInForce.GTC, TimeInForce.IOC,
                                     TimeInForce.FOK, TimeInForce.DAY}),
            # Spot testnet: no shorting, no margin. Declared so the OMS refuses
            # an unsupported intent up front instead of at the venue.
            supports_short=False,
            supports_fractional=True,
            # Binance has no atomic replace on spot; cancel-and-new is not the
            # same thing and pretending otherwise would hide a race.
            supports_replace=False,
            supports_streaming=False,
            honours_client_order_id=True)

    # -- lifecycle ---------------------------------------------------------

    def connect(self, credentials: BrokerCredentials | None = None) -> None:
        if credentials is None:
            raise BrokerUnavailable(
                "binance testnet requires credentials; pass a vault reference",
                venue=self.venue)
        secrets = resolve_credentials(credentials)
        api_key = secrets.get("api_key") or secrets.get("key")
        api_secret = secrets.get("api_secret") or secrets.get("secret")
        if not api_key or not api_secret:
            raise BrokerUnavailable(
                "credential entry is missing api_key/api_secret",
                ref=credentials.ref)
        self._credentials = credentials
        self._api_key = str(api_key)
        self._api_secret = str(api_secret)
        # Prove the key works before anything else believes we are connected.
        self._signed("GET", "/api/v3/account")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._api_key = None
        self._api_secret = None

    def is_connected(self) -> bool:
        return self._connected

    def health(self) -> dict:
        """Never raises. A health probe that throws cannot be used by the
        monitor whose job is to notice the outage.

        Catches `ExecutionError` rather than `BrokerError` deliberately:
        `BrokerUnavailable` is a *sibling* of `BrokerError`, not a subclass, so
        narrowing to `BrokerError` would let the unreachable case — the exact
        case this probe exists for — escape.
        """
        try:
            self._public("GET", "/api/v3/ping")
            return {"ok": self._connected, "name": self.name,
                    "venue": self.venue, "reachable": True}
        except ExecutionError as exc:
            return {"ok": False, "name": self.name, "venue": self.venue,
                    "reachable": False, "error": exc.message}
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "name": self.name, "venue": self.venue,
                    "reachable": False, "error": str(exc)[:200]}

    # -- HTTP --------------------------------------------------------------

    def _http(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
              signed: bool = False) -> Any:
        if self._transport is not None:
            return self._transport(method, path, params=dict(params or {}),
                                   signed=signed)
        import urllib.error
        import urllib.request

        query = dict(params or {})
        headers = {"Accept": "application/json",
                   "User-Agent": "olympus-trading/1.0"}
        if signed:
            if not self._api_secret:
                raise BrokerUnavailable("not connected", venue=self.venue)
            query["timestamp"] = int(self.clock.now().timestamp() * 1000)
            query["recvWindow"] = self.recv_window_ms
            encoded = urllib.parse.urlencode(query)
            signature = hmac.new(self._api_secret.encode(), encoded.encode(),
                                 hashlib.sha256).hexdigest()
            encoded = f"{encoded}&signature={signature}"
            headers["X-MBX-APIKEY"] = self._api_key or ""
        else:
            encoded = urllib.parse.urlencode(query)

        url = f"{self.base_url}{path}"
        data = None
        if method in ("POST", "DELETE", "PUT"):
            data = encoded.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif encoded:
            url = f"{url}?{encoded}"

        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            try:
                payload = json.loads(body)
            except Exception:                            # noqa: BLE001
                payload = {}
            # NOTE: the URL is deliberately NOT included — a signed URL carries
            # the HMAC, and putting it in an exception is how a credential
            # derivative ends up in a log file.
            raise BrokerError("binance rejected the request",
                              status=exc.code, path=path,
                              binance_code=payload.get("code"),
                              binance_msg=payload.get("msg", body)) from exc
        except Exception as exc:                         # noqa: BLE001
            raise BrokerUnavailable("binance testnet unreachable", path=path,
                                    error=str(exc)) from exc

    def _public(self, method: str, path: str, **kw) -> Any:
        return self._http(method, path, signed=False, **kw)

    def _signed(self, method: str, path: str, **kw) -> Any:
        return self._http(method, path, signed=True, **kw)

    # -- symbols -----------------------------------------------------------

    def _symbol(self, instrument_key: str) -> str:
        return instrument_key.split(":", 1)[-1].replace("-", "").upper()

    def _key(self, symbol: str) -> str:
        return f"{self.venue}:{symbol.upper()}"

    # -- account -----------------------------------------------------------

    def get_account(self) -> AccountSnapshot:
        self._require_connected()
        data = self._signed("GET", "/api/v3/account")
        balances = {b["asset"].upper(): b for b in data.get("balances", [])}
        quote = balances.get(self.quote_asset, {})
        free = to_decimal(quote.get("free", "0"), field_name="free")
        locked = to_decimal(quote.get("locked", "0"), field_name="locked")
        # Equity here is cash only: valuing the non-quote balances needs marks,
        # and inventing them would make a reconciliation break look like a
        # rounding difference. The portfolio owns valuation.
        return AccountSnapshot(
            ts=self.clock.now(), cash=free, equity=free + locked,
            currency=self.quote_asset, buying_power=free,
            margin_used=Decimal("0"), source=self.name)

    def get_positions(self) -> list[Position]:
        """Non-quote balances as long positions.

        A spot exchange has balances, not positions, and the mapping is
        deliberately shallow: quantity is what the venue says we hold, and
        `average_price` is left at zero because the venue does not report a cost
        basis. `reconcile.py` compares quantities; taking the venue's word on a
        basis it does not have would corrupt P&L.
        """
        self._require_connected()
        data = self._signed("GET", "/api/v3/account")
        out: list[Position] = []
        for balance in data.get("balances", []):
            asset = balance["asset"].upper()
            if asset == self.quote_asset:
                continue
            total = (to_decimal(balance.get("free", "0"), field_name="free")
                     + to_decimal(balance.get("locked", "0"), field_name="locked"))
            if total == 0:
                continue
            out.append(Position(
                instrument_key=self._key(f"{asset}{self.quote_asset}"),
                quantity=total, average_price=Decimal("0"),
                updated_at=self.clock.now()))
        return out

    # -- orders ------------------------------------------------------------

    def _to_order(self, payload: Mapping[str, Any], *,
                  fallback: Order | None = None) -> Order:
        symbol = str(payload.get("symbol", "")).upper()
        key = self._key(symbol) if symbol else (
            fallback.instrument_key if fallback else "")
        side = Side.BUY if str(payload.get("side", "BUY")).upper() == "BUY" \
            else Side.SELL
        raw_type = str(payload.get("type", "MARKET")).upper()
        order_type = {v: k for k, v in _ORDER_TYPE_OUT.items()}.get(
            raw_type, OrderType.MARKET)
        status = _STATUS.get(str(payload.get("status", "NEW")).upper())
        if status is None:
            # An unmapped status must not silently read as "still open" — that
            # is how a terminal order gets resubmitted.
            raise BrokerError("unrecognised binance order status",
                              status=payload.get("status"), symbol=symbol)
        executed = to_decimal(payload.get("executedQty", "0"),
                              field_name="executedQty")
        quote_qty = to_decimal(payload.get("cummulativeQuoteQty", "0"),
                               field_name="cummulativeQuoteQty")
        avg = (quote_qty / executed) if executed > 0 else None
        quantity = to_decimal(payload.get("origQty", "0"), field_name="origQty")
        if quantity <= 0 and fallback is not None:
            quantity = fallback.quantity

        ts_ms = payload.get("transactTime") or payload.get("time") \
            or payload.get("updateTime")
        created = (datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
                   if ts_ms else self.clock.now())

        return Order(
            client_order_id=str(payload.get("clientOrderId")
                                or (fallback.client_order_id if fallback else "")),
            instrument_key=key, side=side, order_type=order_type,
            quantity=quantity, created_at=created, status=status,
            limit_price=(to_decimal(payload["price"], field_name="price")
                         if payload.get("price") not in (None, "", "0.00000000")
                         else None),
            filled_quantity=executed, average_fill_price=avg,
            broker_order_id=(str(payload["orderId"])
                             if payload.get("orderId") is not None else None),
            updated_at=self.clock.now(),
            intent_id=fallback.intent_id if fallback else "",
            strategy_id=fallback.strategy_id if fallback else "",
            mode=fallback.mode if fallback else Order.__dataclass_fields__["mode"].default,
            time_in_force=fallback.time_in_force if fallback else TimeInForce.GTC)

    def submit_order(self, order: Order) -> Order:
        self._require_connected()
        self._require_supported(order)
        self._require_long_only(order)

        params: dict[str, Any] = {
            "symbol": self._symbol(order.instrument_key),
            "side": order.side.value.upper(),
            "type": _ORDER_TYPE_OUT[order.order_type],
            "quantity": format(order.quantity, "f"),
            # THE idempotency hinge: Binance rejects a repeat of this id, which
            # is what makes execution.py's query-then-adopt retry safe.
            "newClientOrderId": order.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if order.order_type is OrderType.LIMIT:
            if order.limit_price is None:
                raise ExecutionError("limit order requires a limit price",
                                     client_order_id=order.client_order_id)
            params["price"] = format(order.limit_price, "f")
            params["timeInForce"] = _TIF_OUT[order.time_in_force]

        try:
            payload = self._signed("POST", "/api/v3/order", params=params)
        except BrokerError as exc:
            if exc.details.get("binance_code") == _DUPLICATE_CODE:
                # The venue already has this id. Adopt rather than resubmit —
                # this is the branch that prevents a doubled position.
                existing = self.get_order(order.client_order_id)
                if existing is not None:
                    return existing
                raise DuplicateOrderError(
                    "binance reports a duplicate client order id but will not "
                    "return the order",
                    client_order_id=order.client_order_id) from exc
            raise
        return self._to_order(payload, fallback=order)

    def _require_long_only(self, order: Order) -> None:
        """Refuse a SELL that is not covered by a held balance.

        Spot testnet cannot short, so an uncovered sell would simply be
        rejected by the venue — but catching it here keeps the *reason* precise
        and stops a strategy bug from looking like a venue problem.
        """
        if order.side is Side.BUY:
            return
        held = {p.instrument_key: p.quantity for p in self.get_positions()}
        available = held.get(order.instrument_key, Decimal("0"))
        if order.quantity > available:
            raise ExecutionError(
                "refusing an uncovered SELL: this vertical is long-only spot "
                "and shorting is out of scope",
                client_order_id=order.client_order_id,
                requested=str(order.quantity), held=str(available))

    def cancel_order(self, client_order_id: str) -> Order:
        self._require_connected()
        existing = self.get_order(client_order_id)
        if existing is None:
            raise BrokerError("unknown order", client_order_id=client_order_id)
        payload = self._signed("DELETE", "/api/v3/order", params={
            "symbol": self._symbol(existing.instrument_key),
            "origClientOrderId": client_order_id})
        return self._to_order(payload, fallback=existing)

    def get_order(self, client_order_id: str) -> Order | None:
        """Look an order up by OUR id — the query half of query-then-adopt."""
        self._require_connected()
        for symbol in self._candidate_symbols():
            try:
                payload = self._signed("GET", "/api/v3/order", params={
                    "symbol": symbol, "origClientOrderId": client_order_id})
            except BrokerError as exc:
                if exc.details.get("binance_code") == _UNKNOWN_ORDER:
                    continue
                raise
            return self._to_order(payload)
        return None

    def _candidate_symbols(self) -> list[str]:
        """Symbols to search when resolving a client order id.

        Binance's order lookup is per-symbol, so an id alone is not enough. The
        configured instrument allow-list is the search space, which is another
        reason this vertical keeps that list small.
        """
        return [self._symbol(k) for k in self._instruments] or []

    def get_open_orders(self) -> list[Order]:
        self._require_connected()
        payload = self._signed("GET", "/api/v3/openOrders")
        return [self._to_order(row) for row in payload]

    def get_fills(self, since: datetime | None = None) -> list[Fill]:
        self._require_connected()
        out: list[Fill] = []
        for symbol in self._candidate_symbols():
            params: dict[str, Any] = {"symbol": symbol}
            if since is not None:
                params["startTime"] = int(
                    ensure_utc(since, field_name="since").timestamp() * 1000)
            for row in self._signed("GET", "/api/v3/myTrades", params=params):
                out.append(Fill(
                    fill_id=f"binance-{row['id']}",
                    client_order_id=str(row.get("clientOrderId") or ""),
                    instrument_key=self._key(symbol),
                    side=Side.BUY if row.get("isBuyer") else Side.SELL,
                    quantity=to_decimal(row["qty"], field_name="qty"),
                    price=to_decimal(row["price"], field_name="price"),
                    ts=datetime.fromtimestamp(int(row["time"]) / 1000,
                                              tz=timezone.utc),
                    fee=to_decimal(row.get("commission", "0"),
                                   field_name="commission"),
                    fee_currency=str(row.get("commissionAsset", "")),
                    broker_fill_id=str(row["id"]),
                    liquidity="maker" if row.get("isMaker") else "taker"))
        out.sort(key=lambda f: f.ts)
        return out

    def replace_order(self, client_order_id: str, *, quantity=None,
                      limit_price=None) -> Order:
        raise BrokerError(
            "binance spot has no atomic replace; cancel-and-new is a different "
            "operation with a real race window, so it is not offered here",
            venue=self.venue)

    # -- market data (for reconciliation marks) ---------------------------

    def get_quote(self, instrument_key: str) -> Quote | None:
        try:
            data = self._public("GET", "/api/v3/ticker/bookTicker",
                                params={"symbol": self._symbol(instrument_key)})
        except ExecutionError:
            # ExecutionError, not BrokerError: BrokerUnavailable is a sibling
            # class, and an unreachable venue must yield "no quote" rather than
            # propagating into a caller that only wanted a mark.
            return None
        return Quote(instrument_key=instrument_key, ts=self.clock.now(),
                     bid=float(data["bidPrice"]), ask=float(data["askPrice"]),
                     bid_size=float(data.get("bidQty", 0.0)),
                     ask_size=float(data.get("askQty", 0.0)), source=self.name)


__all__ = ["BinanceTestnetBroker"]
