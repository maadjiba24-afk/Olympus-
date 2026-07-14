"""Agent Behavioral Contracts (ABC): runtime Design-by-Contract for Olympus.

A behavioral contract is `C = (Preconditions, Invariants, Governance, Recovery)`
— a formal, declarative statement of what must hold around an operation, plus
what to DO when it doesn't. This is the native (no third-party AgentAssert)
formalization of governance rules that already live scattered across the action
spine, the autonomy dial, capability separation, and Aletheia verification:

  * Preconditions — must hold BEFORE the operation runs (e.g. an irreversible
    action must carry a real human approval; a tool must be inside the
    conversation's capability profile).
  * Invariants    — must hold ABOUT the operation (e.g. the risk class is known;
    a specialist's output satisfies its output contract).
  * Governance    — the policy binding: which autonomy level / approval a risk
    tier requires (wraps `actions.autonomy_level` + `_min_level_to_auto`).
  * Recovery      — what happens on violation: BLOCK (fail closed), HOLD (revert
    to awaiting-approval), REJECT (drop the output), or DEGRADE (record + allow).

Contracts are authored in YAML (`behavioral_contracts.yaml`) and enforced at
runtime. YAML is loaded with `yaml.safe_load` when PyYAML is importable (it is,
in every supported runtime); if it is not, an embedded mirror of the same
default contracts is used, so the guarantees never silently disappear for want
of a parser. A test pins the two in sync.

Design discipline (mirrors `contracts.py` / `security.py`): the predicate core is
pure — each predicate is `(ctx) -> (ok, detail)` over a supplied context, with no
I/O or config reads. The enforcement points build the context and act on the
verdict. Engine-internal errors fail OPEN (the primary spine still guards every
action); only a genuine, evaluated contract violation fails CLOSED.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# --- recovery directives --------------------------------------------------
BLOCK = "block"       # fail closed: the operation must not proceed
HOLD = "hold"         # revert to awaiting human approval
REJECT = "reject"     # drop this output, treat as missing
DEGRADE = "degrade"   # record a degradation but allow (non-blocking clause)
RECOVERIES = (BLOCK, HOLD, REJECT, DEGRADE)

# The clause kinds, in evaluation order.
CLAUSES = ("preconditions", "invariants", "governance")


class ContractViolation(Exception):
    """Raised by `enforce()` when a contract's recovery is blocking. Carries the
    contract name, the failing clause, the human-readable reasons, and the
    recovery directive so the caller can audit and act."""

    def __init__(self, contract: str, clause: str,
                 reasons: tuple[str, ...], recovery: str):
        self.contract = contract
        self.clause = clause
        self.reasons = reasons
        self.recovery = recovery
        super().__init__(f"contract '{contract}' violated at {clause}: "
                         f"{'; '.join(reasons)} → {recovery}")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    contract: str = ""
    clause: str = ""
    reasons: tuple[str, ...] = ()
    recovery: str = DEGRADE

    @property
    def blocking(self) -> bool:
        return not self.ok and self.recovery in (BLOCK, HOLD, REJECT)


# --- predicate registry (pure) --------------------------------------------
# Each predicate is (ctx: dict) -> (ok: bool, detail: str). YAML clauses name
# predicates; the engine looks them up here. Unknown names are a hard error at
# load time (a contract that references a missing check must never pass vacuously).

_PREDICATES: dict[str, Callable[[dict], tuple[bool, str]]] = {}


def predicate(name: str):
    def deco(fn: Callable[[dict], tuple[bool, str]]):
        _PREDICATES[name] = fn
        return fn
    return deco


@predicate("risk_class_known")
def _risk_class_known(ctx: dict) -> tuple[bool, str]:
    from . import actions
    rc = ctx.get("risk_class")
    if rc in actions.RISK_CLASSES:
        return True, ""
    return False, f"unknown risk class {rc!r}"


@predicate("scope_granted")
def _scope_granted(ctx: dict) -> tuple[bool, str]:
    scope = ctx.get("scope")
    if not scope:
        return True, ""
    if scope in (ctx.get("granted_scopes") or set()):
        return True, ""
    return False, f"permission {scope!r} not granted"


@predicate("approval_present_if_irreversible")
def _approval_present_if_irreversible(ctx: dict) -> tuple[bool, str]:
    """Irreversible / financial-legal actions may run ONLY with a genuine human
    approval — a set `approved_at`, not merely a flipped status flag. This is the
    formal statement of `_min_level_to_auto[...] == 99`."""
    from . import actions
    rc = ctx.get("risk_class")
    if rc not in (actions.IRREVERSIBLE, actions.FINANCIAL_LEGAL):
        return True, ""
    if ctx.get("status") == actions.APPROVED and ctx.get("approved_at"):
        return True, ""
    return False, (f"{rc} action requires an explicit human approval before "
                   "execution")


@predicate("autonomy_sufficient")
def _autonomy_sufficient(ctx: dict) -> tuple[bool, str]:
    """If the action was not explicitly approved by a human, the effective
    autonomy level must clear the risk tier's auto-run bar (the dial). Wraps
    `actions.can_auto_execute` / `_min_level_to_auto`."""
    if ctx.get("approved_at"):
        return True, ""                      # explicit approval covers the dial
    if ctx.get("can_auto"):
        return True, ""
    eff, need = ctx.get("eff_level"), ctx.get("min_level")
    return False, (f"autonomy L{eff} below the L{need} required to auto-run a "
                   f"{ctx.get('risk_class')} action without approval")


@predicate("within_autonomy_cap")
def _within_autonomy_cap(ctx: dict) -> tuple[bool, str]:
    """Effective autonomy never exceeds the capability profile's cap."""
    eff, cap = ctx.get("eff_level"), ctx.get("autonomy_cap")
    if eff is None or cap is None or eff <= cap:
        return True, ""
    return False, f"autonomy L{eff} exceeds capability-profile cap L{cap}"


@predicate("output_contract_satisfied")
def _output_contract_satisfied(ctx: dict) -> tuple[bool, str]:
    """A specialist's output satisfies its output contract (reuses contracts.py)."""
    result = ctx.get("contract_result")
    if result is None or result.ok:
        return True, ""
    return False, "; ".join(result.violations)


@predicate("aletheia_verified")
def _aletheia_verified(ctx: dict) -> tuple[bool, str]:
    """When a verification verdict is present it must not be a hard rejection —
    Aletheia's hallucination gate. Absent verdict is permissive (not every path
    runs verify); a present 'reject'/'fail' verdict blocks acceptance."""
    verdict = ctx.get("verify_verdict")
    if verdict is None:
        return True, ""
    if str(verdict).lower() in ("reject", "rejected", "fail", "failed",
                                "hallucination", "unsupported"):
        return False, f"Aletheia verdict: {verdict}"
    return True, ""


_SENSITIVITY_RANK = {"normal": 0, "high": 1}


@predicate("rewrite_preserves_provenance")
def _rewrite_preserves_provenance(ctx: dict) -> tuple[bool, str]:
    """A sleep-time memory rewrite must carry forward the union of its sources'
    provenance — a consolidation may add provenance, never launder it away."""
    src = set(ctx.get("source_provenance") or [])
    new = set(ctx.get("new_provenance") or [])
    missing = src - new
    if missing:
        return False, f"rewrite drops source provenance: {sorted(missing)}"
    return True, ""


@predicate("rewrite_preserves_trust")
def _rewrite_preserves_trust(ctx: dict) -> tuple[bool, str]:
    """A rewrite must not downgrade sensitivity or change the memory type — it
    keeps the original trust labels."""
    src_rank = max((_SENSITIVITY_RANK.get(s, 0)
                    for s in (ctx.get("source_sensitivities") or [])),
                   default=0)
    new_rank = _SENSITIVITY_RANK.get(ctx.get("new_sensitivity", "normal"), 0)
    if new_rank < src_rank:
        return False, "rewrite downgrades memory sensitivity"
    st, nt = ctx.get("source_type"), ctx.get("new_type")
    if st is not None and nt is not None and st != nt:
        return False, f"rewrite changes memory type {st!r}→{nt!r}"
    return True, ""


@predicate("rewrite_verified")
def _rewrite_verified(ctx: dict) -> tuple[bool, str]:
    """The rewrite passed Aletheia: it asserts nothing unsupported by its
    sources. A missing verdict is treated as UNverified (fail closed) — unlike
    aletheia_verified on the output path, a memory commit must be affirmatively
    checked."""
    if ctx.get("verify_supported") is True:
        return True, ""
    claims = ctx.get("unsupported_claims") or []
    return False, ("rewrite not verified by Aletheia"
                   + (f": {claims}" if claims else ""))


@predicate("mandate_signature_valid")
def _mandate_signature_valid(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("mandate_signature_valid")),
            "" if ctx.get("mandate_signature_valid") else "invalid signature")


@predicate("mandate_not_expired")
def _mandate_not_expired(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("mandate_not_expired")),
            "" if ctx.get("mandate_not_expired") else "mandate expired")


@predicate("mandate_fresh_nonce")
def _mandate_fresh_nonce(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("mandate_fresh_nonce")),
            "" if ctx.get("mandate_fresh_nonce") else "nonce replay")


@predicate("mandate_intent_contained")
def _mandate_intent_contained(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("mandate_intent_contained")),
            "" if ctx.get("mandate_intent_contained")
            else "cart exceeds its intent mandate")


@predicate("mandate_trusted_construction")
def _mandate_trusted_construction(ctx: dict) -> tuple[bool, str]:
    return (bool(ctx.get("mandate_trusted_construction")),
            "" if ctx.get("mandate_trusted_construction")
            else "mandate not trusted-constructed (possible injection)")


@predicate("no_action_tool_in_ingesting_run")
def _no_action_tool_in_ingesting_run(ctx: dict) -> tuple[bool, str]:
    """Capability separation: a run that ingests untrusted external content must
    carry no world-affecting action tools (mirrors security.filter_tools)."""
    if not ctx.get("ingests_external"):
        return True, ""
    from . import security
    leaked = sorted(set(ctx.get("tool_names") or []) & security.ACTION_TOOLS)
    if leaked:
        return False, f"action tools present in an ingesting run: {leaked}"
    return True, ""


# --- contract model -------------------------------------------------------

@dataclass(frozen=True)
class Contract:
    name: str
    operation: str
    recovery: str = DEGRADE
    description: str = ""
    preconditions: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    governance: tuple[str, ...] = ()

    def clause(self, kind: str) -> tuple[str, ...]:
        return getattr(self, kind, ())


# The embedded mirror of behavioral_contracts.yaml — the fallback when PyYAML is
# unavailable. Kept byte-for-byte in sync with the YAML by test_abc's drift test.
_DEFAULT_CONTRACTS: dict[str, Any] = {
    "action_execution": {
        "operation": "action.execute",
        "recovery": "hold",
        "description": "Any world-affecting action, at the single execution "
                       "chokepoint, wrapping the approval spine + autonomy dial.",
        "preconditions": ["scope_granted", "approval_present_if_irreversible"],
        "invariants": ["risk_class_known"],
        "governance": ["autonomy_sufficient", "within_autonomy_cap"],
    },
    "specialist_output": {
        "operation": "specialist.output",
        "recovery": "reject",
        "description": "A specialist's final output before the orchestrator "
                       "accepts it — output contract + Aletheia verification.",
        "preconditions": [],
        "invariants": ["output_contract_satisfied"],
        "governance": ["aletheia_verified"],
    },
    "tool_loadout": {
        "operation": "run.loadout",
        "recovery": "block",
        "description": "Capability separation: an ingesting run carries no "
                       "action tools.",
        "preconditions": ["no_action_tool_in_ingesting_run"],
        "invariants": [],
        "governance": [],
    },
    "memory_rewrite": {
        "operation": "memory.rewrite",
        "recovery": "block",
        "description": "A sleep-time memory rewrite before commit: preserves "
                       "provenance and trust labels, and is Aletheia-verified.",
        "preconditions": ["rewrite_preserves_provenance",
                          "rewrite_preserves_trust"],
        "invariants": [],
        "governance": ["rewrite_verified"],
    },
    "payment_mandate": {
        "operation": "payment.mandate",
        "recovery": "block",
        "description": "An AP2 payment mandate before it could back a payment: "
                       "contained within its intent, unexpired, replay-free, "
                       "validly signed, and trusted-constructed.",
        "preconditions": ["mandate_intent_contained", "mandate_not_expired",
                          "mandate_fresh_nonce"],
        "invariants": [],
        "governance": ["mandate_signature_valid",
                       "mandate_trusted_construction"],
    },
}


def _coerce(name: str, spec: dict) -> Contract:
    def _tuple(v: Any) -> tuple[str, ...]:
        return tuple(str(x) for x in v) if isinstance(v, list) else ()
    recovery = str(spec.get("recovery", DEGRADE))
    if recovery not in RECOVERIES:
        raise ValueError(f"contract {name!r}: unknown recovery {recovery!r}")
    c = Contract(
        name=name,
        operation=str(spec.get("operation", "")),
        recovery=recovery,
        description=str(spec.get("description", "")),
        preconditions=_tuple(spec.get("preconditions")),
        invariants=_tuple(spec.get("invariants")),
        governance=_tuple(spec.get("governance")),
    )
    # Fail fast on a contract that names a predicate we don't have — a missing
    # check must never pass vacuously at runtime.
    for kind in CLAUSES:
        for pname in c.clause(kind):
            if pname not in _PREDICATES:
                raise ValueError(
                    f"contract {name!r} references unknown predicate {pname!r}")
    return c


def _yaml_path() -> Path:
    return Path(__file__).with_name("behavioral_contracts.yaml")


def _raw_defaults() -> dict[str, Any]:
    """The default contract specs — from the packaged YAML when PyYAML is
    importable, else the embedded mirror."""
    try:
        import yaml
    except ImportError:
        return dict(_DEFAULT_CONTRACTS)
    try:
        data = yaml.safe_load(_yaml_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:      # missing/corrupt packaged file
        from . import errors
        errors.capture("behavioral_contracts.load", err)
        return dict(_DEFAULT_CONTRACTS)
    return data.get("contracts", {}) if isinstance(data, dict) else {}


def _operator_overrides() -> dict[str, Any]:
    """Operator-authored contracts from MEMORY_DIR/behavioral_contracts.yaml —
    additive/override. Ignored (never fatal) if absent, unparsable, or PyYAML is
    missing."""
    from . import config
    path = config.MEMORY_DIR / "behavioral_contracts.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return {}
    return data.get("contracts", {}) if isinstance(data, dict) else {}


def load() -> dict[str, Contract]:
    """All contracts, keyed by name (operator overrides win). Invalid contracts
    raise — a governance layer that silently drops a malformed rule is worse than
    one that refuses to start."""
    specs = {**_raw_defaults(), **_operator_overrides()}
    out: dict[str, Contract] = {}
    for name, spec in specs.items():
        if isinstance(spec, dict):
            out[str(name)] = _coerce(str(name), spec)
    return out


def contracts_for(operation: str, registry: dict[str, Contract] | None = None
                  ) -> list[Contract]:
    reg = registry if registry is not None else load()
    return [c for c in reg.values() if c.operation == operation]


# --- evaluation + enforcement ---------------------------------------------

def evaluate(contract: Contract, ctx: dict) -> Verdict:
    """Evaluate one contract against a context. Clauses run in order
    (preconditions → invariants → governance); the FIRST failing clause yields
    the verdict, carrying the contract's recovery directive."""
    for kind in CLAUSES:
        reasons: list[str] = []
        for pname in contract.clause(kind):
            pred = _PREDICATES.get(pname)
            if pred is None:                  # defended at load; belt-and-braces
                reasons.append(f"missing predicate {pname!r}")
                continue
            ok, detail = pred(ctx)
            if not ok:
                reasons.append(detail or pname)
        if reasons:
            return Verdict(ok=False, contract=contract.name, clause=kind,
                           reasons=tuple(reasons), recovery=contract.recovery)
    return Verdict(ok=True, contract=contract.name)


def check(operation: str, ctx: dict,
          registry: dict[str, Contract] | None = None) -> Verdict:
    """Evaluate every contract bound to `operation`; return the first violation,
    else an ok verdict. Never raises on predicate/engine error — a broken
    predicate degrades to 'ok' so the primary spine remains the real guard
    (engine failures fail OPEN; evaluated violations fail CLOSED)."""
    try:
        for c in contracts_for(operation, registry):
            v = evaluate(c, ctx)
            if not v.ok:
                return v
    except Exception as err:
        from . import errors, evolve
        errors.capture("behavioral_contracts.check", err, context=operation)
        evolve.record("abc", evolve.DEGRADED, f"engine error on {operation}")
        return Verdict(ok=True, contract="", clause="engine-error")
    return Verdict(ok=True)


def enabled() -> bool:
    """ABC enforcement is on by default (the defaults encode already-true
    invariants, so enabling changes no happy-path behavior). `OLYMPUS_ABC=off`
    is the kill switch. Read live so replay restores the recorded setting."""
    return os.environ.get("OLYMPUS_ABC", "on").strip().lower() not in (
        "0", "off", "false", "no")


def enforce(operation: str, ctx: dict,
            registry: dict[str, Contract] | None = None) -> Verdict:
    """Evaluate and, on a blocking violation, raise `ContractViolation`. No-op
    (returns an ok verdict) when ABC is disabled. Records OK/blocking outcomes to
    feature-evolution telemetry."""
    if not enabled():
        return Verdict(ok=True, contract="", clause="disabled")
    v = check(operation, ctx, registry)
    from . import evolve
    if v.ok:
        return v
    if v.blocking:
        evolve.record("abc", evolve.OK,
                      f"blocked {v.contract}/{v.clause} → {v.recovery}")
        raise ContractViolation(v.contract, v.clause, v.reasons, v.recovery)
    evolve.record("abc", evolve.DEGRADED, f"{v.contract}/{v.clause}")
    return v


# --- context builders for the wired enforcement points --------------------

def action_context(action, action_type) -> dict:
    """Build the evaluation context for `action.execute` from the action spine
    state — the autonomy dial, approval status, scope, and auto-run eligibility."""
    from . import actions, capprofile
    return {
        "risk_class": action.risk_class,
        "status": action.status,
        "approved_at": action.approved_at,
        "scope": getattr(action_type, "scope", "") if action_type else "",
        "granted_scopes": actions.granted_scopes(action.user),
        "eff_level": actions.autonomy_level(action.user),
        "autonomy_cap": capprofile.autonomy_cap(action.user),
        "min_level": actions._min_level_to_auto(action.risk_class),
        "can_auto": actions.can_auto_execute(action),
    }
