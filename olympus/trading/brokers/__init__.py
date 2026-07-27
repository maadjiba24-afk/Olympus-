"""Broker adapters — the only place the system talks to a venue.

`base.BrokerAdapter` is the contract; `paper.PaperBroker` is a fully functional
simulated venue that is both the paper-trading counterparty and the backtester's
fill engine, which is what makes "the same code runs in backtest, paper, and
live" true rather than aspirational.

No real broker adapter ships here. Writing one against a sandbox that cannot be
authenticated to in this environment and calling it supported would be exactly
the kind of overclaim this package exists to avoid. The ABC plus the paper
implementation is the contract a real adapter must satisfy.
"""

from __future__ import annotations

from .base import (BrokerAdapter, BrokerCapabilities, BrokerCredentials,
                   resolve_credentials)
from .paper import (FeeModel, PaperBroker, SlippageModel)

__all__ = ["BrokerAdapter", "BrokerCapabilities", "BrokerCredentials",
           "resolve_credentials", "PaperBroker", "FeeModel", "SlippageModel"]
