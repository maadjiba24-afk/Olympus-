"""The native model is quarantined, and the quarantine is structural.

> The native model currently loses to all simple matched baselines and has an
> unresolved location-bias blocker. Do not attempt to hide or cosmetically
> improve this result.

So this module does not hide it. It states it, in one place, as data other code
reads and tests assert against:

* the matched evaluation (`docs/OLYMPUS_VS_KRONOS.md`) returned **insufficient
  evidence**; the native arm lost to a gradient-boosted tree, to a linear fit
  and to persistence on the same split, the same costs and the same metrics;
* **B8** is open: on the held-out synthetic split the model's error is
  0.025636 with a bias of +0.025038 — roughly 2.7σ of location, with the spread
  itself approximately right. A model whose entire error is a constant offset
  has not learned the thing it was asked to learn.

Why a module and not a note in the docs
---------------------------------------
A documented limitation is a limitation until somebody is in a hurry. Eight
properties are therefore enforced in code and asserted in
`tests/test_trading_native_quarantine.py`:

1. it is **experimental** — `experimental` is a computed property returning
   True while any blocker is open, and there is no argument that sets it;
2. it is **not the default forecaster** — nothing in `forecast.py` names it,
   and no default anywhere resolves to it;
3. it is **not automatically registered** — importing the package registers
   nothing, so no registry lookup can find it by accident;
4. it **cannot participate in live trading** — `Purpose.LIVE` raises at
   construction, before an instance exists to be handed to a live path;
5. it **cannot become production eligible** — `production_eligible` is computed
   from the open blockers, so approving it in a registry changes nothing here;
6. it **cannot pass the promotion gate** while B8 is open — `GateLedger`
   consults `assert_promotable` and refuses;
7. it **cannot be selected by an autonomous agent** — selection requires a
   named operator, and `autonomously_selectable` is False;
8. it is **disabled by default** — `enabled()` needs both an explicit
   environment flag *and* zero open blockers, so setting the flag today still
   returns False.

Property 8 deserves the emphasis. `OLYMPUS_NATIVE_MODEL_ENABLED=1` does not
enable the model. It expresses an operator's intent to enable it, and the
blockers still say no. That is the difference between a flag and a gate, and it
is why the flag alone cannot be the thing somebody flips at 2am.

Resolving a blocker
-------------------
By editing `BLOCKERS` and stating the evidence, in a commit a reviewer can
read — not by passing an argument. `resolution_evidence` on each entry names
what would have to be true, so "resolved" is a claim someone signs rather than
a boolean somebody flipped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from ..errors import TradingError

#: Bumped when the property set or the blocker list changes shape.
QUARANTINE_SCHEMA_VERSION = 1

#: The environment variable an operator sets to express intent. Intent is not
#: permission — see `enabled()`.
ENABLE_FLAG = "OLYMPUS_NATIVE_MODEL_ENABLED"

#: Names the quarantine recognises as the native model. Matched by prefix,
#: because a checkpoint reports `olympus-native/<kind>-<hash>` and a registry
#: entry reports `olympus-native`, and a quarantine that only matched one of
#: them would be a quarantine with a documented way round it.
QUARANTINED_PREFIXES: tuple[str, ...] = ("olympus-native", "native-",
                                         "olympus_native")


class QuarantineViolation(TradingError):
    """The native model was asked to do something it is not permitted to do."""

    code = "NATIVE_MODEL_QUARANTINED"


@dataclass(frozen=True)
class Blocker:
    """One reason the native model is not production material."""

    id: str
    title: str
    detail: str
    #: What would have to be shown to close it. Named so that "resolved" is a
    #: claim with a shape rather than a boolean somebody set.
    resolution_evidence: tuple[str, ...]
    resolved: bool = False

    def __post_init__(self):
        object.__setattr__(self, "resolution_evidence",
                           tuple(self.resolution_evidence))
        if not self.resolution_evidence:
            raise TradingError(
                "a blocker must say what would resolve it; without that it is "
                "a complaint rather than a gate", blocker=self.id)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "detail": self.detail,
                "resolved": self.resolved,
                "resolution_evidence": list(self.resolution_evidence)}


#: Every blocker between the native model and production. Editing this list is
#: how one is closed, and the diff is the record.
BLOCKERS: tuple[Blocker, ...] = (
    Blocker(
        id="B8",
        title="location bias: the model's error is almost entirely a constant "
              "offset",
        detail="On the held-out synthetic split, MAE is 0.025636 with a mean "
               "signed error of +0.025038 — about 2.7 sigma of location, with "
               "the predicted spread approximately correct. A model whose "
               "whole error is an offset has not learned the conditional mean "
               "it was trained for, and correcting the offset after the fact "
               "would fit the test split.",
        resolution_evidence=(
            "the mean signed error on a held-out split is within one standard "
            "error of zero, without a post-hoc offset term",
            "the same holds on a second split the model was not selected on",
            "the fix is in the model or the training objective, and is "
            "identifiable in the diff",
        )),
    Blocker(
        id="B9",
        title="the native model loses to every simple matched baseline",
        detail="docs/OLYMPUS_VS_KRONOS.md: on identical data, costs, splits "
               "and metrics the native arm lost to a gradient-boosted tree, a "
               "linear fit and persistence. The matched evaluation returned "
               "INSUFFICIENT EVIDENCE, which is not a tie.",
        resolution_evidence=(
            "the native arm beats persistence and the tree on the matched "
            "protocol, with the Holm-corrected p-value reported",
            "the win survives a second non-overlapping period",
        )),
    Blocker(
        id="B1",
        title="no market data has ever been read",
        detail="Every fit is on synthetic series; docs/"
               "TRADING_EXTERNAL_VALIDATION.md section 1 records that no market "
               "data endpoint is reachable from this environment. Results here "
               "are evidence about the pipeline, not about markets.",
        resolution_evidence=(
            "a dataset manifest naming a real venue, a real instrument and a "
            "real date range, with its provenance record",
            "an evaluation on that dataset rather than on synthetic series",
        )),
    Blocker(
        id="B3",
        title="no broker has ever been reached",
        detail="Not even a testnet: docs/TRADING_EXTERNAL_VALIDATION.md "
               "records http=000 for the paper endpoint. Nothing here has met "
               "an order book, so the execution model is unvalidated.",
        resolution_evidence=(
            "a paper-trading session against a real broker testnet, with fills",
            "realised slippage compared against the cost model's prediction",
        )),
)


def open_blockers() -> tuple[Blocker, ...]:
    """The unresolved ones. Computed; nothing caches this."""
    return tuple(b for b in BLOCKERS if not b.resolved)


def blocker(blocker_id: str) -> Blocker:
    for entry in BLOCKERS:
        if entry.id == blocker_id:
            return entry
    raise KeyError(blocker_id)


class Purpose(str, Enum):
    """What a native forecaster is being built for.

    Not the same enum as `modes.Mode`, and deliberately so: a mode is a
    property of the *installation*, while this is a property of one object's
    construction. A research forecaster inside a live installation is fine; a
    live forecaster inside a research installation is not.
    """

    RESEARCH = "research"
    BACKTEST = "backtest"
    EVALUATION = "evaluation"
    #: Paper trading against a real broker. Blocked: B3 says no broker has
    #: been reached, so this would be the first, and it is a promotion-gate
    #: stage rather than a thing to do by constructing an object.
    PAPER = "paper"
    #: Alongside a live incumbent on live inputs. Blocked while B1 is open —
    #: there are no live inputs here to run alongside.
    SHADOW = "shadow"
    LIVE = "live"


#: The only purposes a quarantined model may be built for. Everything else
#: raises at construction, before an instance exists to be passed anywhere.
RESEARCH_PURPOSES: frozenset[Purpose] = frozenset(
    {Purpose.RESEARCH, Purpose.BACKTEST, Purpose.EVALUATION})


def is_quarantined(identifier: str) -> bool:
    """Does this model id or version name the quarantined native model?"""
    text = str(identifier or "").strip().lower()
    return any(text.startswith(prefix) or f"/{prefix}" in text
               or prefix in text.split(":")[-1]
               for prefix in QUARANTINED_PREFIXES)


def experimental() -> bool:
    """True while any blocker is open. There is no argument that sets this."""
    return bool(open_blockers())


def production_eligible() -> bool:
    """False while any blocker is open, whatever a registry says.

    A second, independent refusal on top of `registry.assert_usable`. Approving
    the model in the registry would satisfy that one; it does not satisfy this
    one, because this one is not about paperwork.
    """
    return not open_blockers()


def autonomously_selectable() -> bool:
    """False. An autonomous actor may not put this model anywhere.

    Constant rather than derived from the blockers: even with every blocker
    closed, moving a model into use is a permission expansion, and permission
    expansion is human-only everywhere else in Olympus.
    """
    return False


def enabled() -> bool:
    """Both the operator's flag **and** zero open blockers. Today: False.

    The conjunction is the point. A flag alone would mean the quarantine is one
    environment variable from being lifted by somebody who has not read why it
    exists.
    """
    flag = str(os.environ.get(ENABLE_FLAG, "")).strip().lower()
    intent = flag in ("1", "true", "yes", "on")
    return intent and not open_blockers()


def enable_intent() -> bool:
    """Whether the flag is set, separately from whether it did anything."""
    flag = str(os.environ.get(ENABLE_FLAG, "")).strip().lower()
    return flag in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# the refusals
# ---------------------------------------------------------------------------

def assert_purpose(purpose: Purpose | str) -> Purpose:
    """Refuse construction for anything but research. Called from `__init__`.

    Enforced at construction rather than at use, because an object that exists
    can be passed somewhere its constructor never saw.
    """
    chosen = Purpose(purpose)
    blockers = open_blockers()
    if chosen in RESEARCH_PURPOSES:
        return chosen
    if not blockers:
        return chosen
    raise QuarantineViolation(
        "the Olympus-native model is quarantined and may be built only for "
        "research, backtesting or evaluation; it loses to every simple matched "
        "baseline and its error is almost entirely a constant offset",
        purpose=chosen.value,
        permitted=sorted(p.value for p in RESEARCH_PURPOSES),
        open_blockers=[b.id for b in blockers])


def assert_not_live(mode: Any) -> None:
    """Refuse a live mode. Independent of `assert_purpose`, on purpose.

    Two checks that can each fail on their own is the difference between a
    quarantine and a single `if` somebody deletes.
    """
    is_live = bool(getattr(mode, "is_live", False))
    blockers = open_blockers()
    if is_live and blockers:
        raise QuarantineViolation(
            "the Olympus-native model may not be used in a live mode",
            mode=getattr(mode, "value", str(mode)),
            open_blockers=[b.id for b in blockers])


def assert_promotable(identifiers: Iterable[str]) -> None:
    """Refuse the promotion gate for the quarantined model.

    Takes the whole set of identifiers a promotion mentions rather than one
    name, because a promotion that reaches the gate under a different label is
    the failure mode this is for.
    """
    blockers = open_blockers()
    if not blockers:
        return
    named = sorted({str(value) for value in identifiers
                    if is_quarantined(str(value))})
    if named:
        raise QuarantineViolation(
            "the Olympus-native model cannot pass the promotion gate while its "
            "blockers are open; promoting a model that loses to persistence "
            "would make the gate a formality",
            named=named, open_blockers=[b.id for b in blockers],
            blocker_titles=[b.title for b in blockers])


def assert_autonomous_selection_refused(actor: Any) -> None:
    """Refuse selection by anything that is not a named operator."""
    kind = str(getattr(getattr(actor, "kind", None), "value",
                       getattr(actor, "kind", ""))).lower()
    if kind != "operator":
        raise QuarantineViolation(
            "an autonomous actor may not select the Olympus-native model; "
            "putting a model into use is a permission expansion",
            actor=str(getattr(actor, "name", actor)), kind=kind or "(none)")


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def status() -> dict:
    """The eight properties, as data. Every value computed, none settable."""
    blockers = open_blockers()
    return {"schema_version": QUARANTINE_SCHEMA_VERSION,
            "model": "olympus-native",
            "experimental": experimental(),
            "is_default_forecaster": False,
            "automatically_registered": False,
            "usable_in_live_trading": False,
            "production_eligible": production_eligible(),
            "promotable": not blockers,
            "autonomously_selectable": autonomously_selectable(),
            "enabled": enabled(),
            "enable_flag": ENABLE_FLAG,
            "enable_intent": enable_intent(),
            "permitted_purposes": sorted(p.value for p in RESEARCH_PURPOSES),
            "open_blockers": [b.to_dict() for b in blockers],
            "resolved_blockers": [b.to_dict() for b in BLOCKERS
                                  if b.resolved],
            "summary": summary()}


def summary() -> str:
    blockers = open_blockers()
    if not blockers:
        return ("every declared blocker is resolved; the model is still not "
                "promoted, and promotion is still human-only")
    return ("quarantined: " + ", ".join(f"{b.id} ({b.title})"
                                        for b in blockers))


def describe() -> str:                                   # pragma: no cover
    lines = ["Olympus-native model quarantine", "=" * 31, ""]
    for name, value in status().items():
        if name in ("open_blockers", "resolved_blockers", "summary"):
            continue
        lines.append(f"  {name:<26} {value}")
    lines.append("")
    for entry in open_blockers():
        lines.append(f"  [{entry.id}] {entry.title}")
        lines.append(f"        {entry.detail}")
        for item in entry.resolution_evidence:
            lines.append(f"        - would resolve: {item}")
    return "\n".join(lines)


def promotion_identifiers(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    """Every string in a promotion's evidence, for `assert_promotable`."""
    out: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                out.append(str(key))
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)

    walk(dict(evidence or {}))
    return tuple(out)


__all__ = ["QUARANTINE_SCHEMA_VERSION", "ENABLE_FLAG", "QUARANTINED_PREFIXES",
           "QuarantineViolation", "Blocker", "BLOCKERS", "open_blockers",
           "blocker", "Purpose", "RESEARCH_PURPOSES", "is_quarantined",
           "experimental", "production_eligible", "autonomously_selectable",
           "enabled", "enable_intent", "assert_purpose", "assert_not_live",
           "assert_promotable", "assert_autonomous_selection_refused",
           "status", "summary", "describe", "promotion_identifiers"]
