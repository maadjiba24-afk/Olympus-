"""The broker interface.

One abstract class, deliberately narrow. Every venue-specific quirk lives behind
it, and nothing above it — strategies, forecasters, the risk engine — holds a
reference to it.

Credentials
-----------
The rule that matters most here: **an adapter never receives raw secrets from a
caller.** It receives a `BrokerCredentials` handle, which is a *reference* to a
vault entry, and resolves it through `olympus.vault` itself. Two consequences:

* A forecasting or language-model component that somehow obtained a credentials
  object still has nothing usable — it holds a name, not a key.
* Secrets never pass through the call sites that get logged, serialised into
  audit records, or captured in tracebacks.

`__repr__` and `__str__` are overridden to guarantee the handle cannot print
secret material even if someone logs it, and that is tested rather than assumed.

Capabilities
------------
`BrokerCapabilities` lets the execution layer refuse an unsupported order type
*before* submitting, rather than discovering it from a venue rejection. Finding
out at the venue costs a round trip, a rejected order in the audit trail, and —
on some venues — a rate-limit strike.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..contracts import (AccountSnapshot, Candle, Fill, Order, OrderType,
                         Position, Quote, TimeInForce)
from ..errors import BrokerError, BrokerUnavailable, ConfigurationError


@dataclass(frozen=True)
class BrokerCredentials:
    """An opaque handle to secrets held in `olympus.vault`.

    Carries no secret material. `ref` is the vault entry name; `user` is the
    vault namespace it lives under.
    """
    ref: str
    user: str = "shared"

    def __post_init__(self):
        if not self.ref:
            raise ConfigurationError("credential ref must be non-empty")

    # Both dunders are overridden: a handle that leaks through an f-string, a
    # log line, or a traceback repr would defeat the whole arrangement.
    def __repr__(self) -> str:
        return f"BrokerCredentials(ref={self.ref!r}, user={self.user!r})"

    def __str__(self) -> str:
        return f"<credentials {self.ref}>"


def resolve_credentials(handle: BrokerCredentials) -> Mapping[str, Any]:
    """Read the actual secret from the vault. Adapters call this; callers do not.

    Raises `BrokerUnavailable` rather than returning None so a missing
    credential fails loudly at connect time instead of producing a confusing
    authentication error later.
    """
    from olympus import vault
    try:
        value = vault.get(handle.user, handle.ref)
    except Exception as exc:                             # noqa: BLE001
        raise BrokerUnavailable(f"credential store unavailable: {exc}",
                                ref=handle.ref) from exc
    if value is None:
        raise BrokerUnavailable("no such credential", ref=handle.ref,
                                user=handle.user)
    if isinstance(value, str):
        return {"value": value}
    return dict(value)


@dataclass(frozen=True)
class BrokerCapabilities:
    """What a venue actually supports. Declared, not discovered."""
    order_types: frozenset[OrderType] = frozenset({OrderType.MARKET,
                                                   OrderType.LIMIT})
    time_in_force: frozenset[TimeInForce] = frozenset({TimeInForce.DAY,
                                                       TimeInForce.GTC})
    supports_short: bool = False
    supports_fractional: bool = False
    supports_replace: bool = False
    supports_streaming: bool = False
    #: True when the venue itself deduplicates on our client order id. When
    #: False the execution layer must query-then-adopt rather than rely on it.
    honours_client_order_id: bool = True

    def __post_init__(self):
        object.__setattr__(self, "order_types",
                           frozenset(OrderType(o) for o in self.order_types))
        object.__setattr__(self, "time_in_force",
                           frozenset(TimeInForce(t) for t in self.time_in_force))

    def supports_order(self, order: Order) -> bool:
        return (order.order_type in self.order_types
                and order.time_in_force in self.time_in_force)

    def to_dict(self) -> dict:
        return {
            "order_types": sorted(o.value for o in self.order_types),
            "time_in_force": sorted(t.value for t in self.time_in_force),
            "supports_short": self.supports_short,
            "supports_fractional": self.supports_fractional,
            "supports_replace": self.supports_replace,
            "supports_streaming": self.supports_streaming,
            "honours_client_order_id": self.honours_client_order_id,
        }


class BrokerAdapter(ABC):
    """The contract every venue integration must satisfy."""

    name: str = "abstract"
    venue: str = "none"

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities: ...

    # -- lifecycle --------------------------------------------------------

    @abstractmethod
    def connect(self, credentials: BrokerCredentials | None = None) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    def health(self) -> dict:
        """Cheap liveness probe. Must not raise — a health check that throws
        cannot be used by the monitor that is supposed to notice outages."""
        try:
            return {"ok": self.is_connected(), "name": self.name,
                    "venue": self.venue}
        except Exception as exc:                         # noqa: BLE001
            return {"ok": False, "name": self.name, "error": str(exc)}

    # -- account ----------------------------------------------------------

    @abstractmethod
    def get_account(self) -> AccountSnapshot: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    # -- orders -----------------------------------------------------------

    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """Submit. MUST be idempotent on `order.client_order_id`: a repeat of an
        id the venue already has returns that order rather than creating a
        second one."""

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> Order: ...

    @abstractmethod
    def get_order(self, client_order_id: str) -> Order | None: ...

    @abstractmethod
    def get_open_orders(self) -> list[Order]: ...

    @abstractmethod
    def get_fills(self, since: datetime | None = None) -> list[Fill]: ...

    def replace_order(self, client_order_id: str, *, quantity=None,
                      limit_price=None) -> Order:
        raise BrokerError("this venue does not support order replacement",
                          venue=self.venue)

    # -- market data ------------------------------------------------------

    def get_quote(self, instrument_key: str) -> Quote | None:
        return None

    def get_candles(self, instrument_key: str, timeframe: str,
                    start: datetime | None = None,
                    end: datetime | None = None) -> list[Candle]:
        return []

    # -- guards -----------------------------------------------------------

    def _require_connected(self) -> None:
        if not self.is_connected():
            raise BrokerUnavailable("broker is not connected", venue=self.venue)

    def _require_supported(self, order: Order) -> None:
        if not self.capabilities.supports_order(order):
            raise BrokerError(
                "venue does not support this order type or time-in-force",
                venue=self.venue, order_type=order.order_type.value,
                time_in_force=order.time_in_force.value)


__all__ = ["BrokerAdapter", "BrokerCapabilities", "BrokerCredentials",
           "resolve_credentials"]
