"""The immutable safety kernel, and the proof that self-evolution cannot reach it.

Phase 10 gives Olympus the ability to learn, research, and propose changes to
itself. That ability is only acceptable if there is a bounded set of components
it provably cannot change. This module *is* that boundary.

Three separate mechanisms, because any one of them alone is a single point of
failure:

1. **Declaration.** `SAFETY_KERNEL` names every protected component and the
   concrete modules implementing it. A component that is not on this list is
   not protected, so the list is the thing to review.

2. **Sealing.** `seal()` hashes the source of every kernel module. A seal taken
   at deploy time and re-verified later detects modification whatever produced
   it — an agent, a bad merge, a corrupted checkout. This catches change that
   already happened.

3. **Structural denial.** `audit_evolution_modules()` parses every
   self-evolution module and fails if any of them imports a kernel mutation
   entry point. This catches change *before* it can happen, at the only moment
   that is cheap: when the code is written. It is the check `test_trading_
   kernel.py` runs, and the reason gate 13 is a fact about the codebase rather
   than a promise about behaviour.

The asymmetry is deliberate and worth stating plainly: **Olympus may recommend
changes to the kernel and may never apply them.** `propose_kernel_change()`
exists and returns a document. There is no `apply_kernel_change()`, here or
anywhere else, and adding one would fail `test_no_module_can_apply_a_kernel_
change`.

What is *not* claimed
---------------------
This is a defence against the system's own machinery, not against an operator
with a shell. Anyone who can edit the source can edit this file. The guarantee
is: no code path that Olympus can reach autonomously leads to a kernel change,
and any out-of-band change is detected by the seal. That is the achievable
guarantee; a stronger claim would be false.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, SafetyKernelViolation

# ---------------------------------------------------------------------------
# 1. declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KernelComponent:
    """One protected component: what it is, where it lives, why it is sealed."""

    name: str
    description: str
    #: Dotted module paths, relative to `olympus.` for repo modules.
    modules: tuple[str, ...]
    #: What breaks if this becomes self-modifiable. Written for the reviewer
    #: who has to decide whether a proposed exemption is acceptable.
    rationale: str

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "modules": list(self.modules), "rationale": self.rationale}


#: The eleven kernel components named in the Phase 10 specification, mapped to
#: the modules that actually implement them. Order is stable for reporting.
SAFETY_KERNEL: tuple[KernelComponent, ...] = (
    KernelComponent(
        name="risk_policy_enforcement",
        description="The deterministic, non-LLM gate every trade intent passes.",
        modules=("olympus.trading.risk",),
        rationale="A system that can widen its own limits has no limits. This "
                  "is the component whose whole value is being unreachable "
                  "from the code it constrains."),
    KernelComponent(
        name="kill_switches",
        description="Global, strategy and instrument halts, plus auto-trips.",
        modules=("olympus.trading.killswitch",),
        rationale="Stopping must always outrank starting. A component that "
                  "could clear its own halt could not be relied on to stop."),
    KernelComponent(
        name="credential_vault",
        description="Encrypted storage of broker credentials.",
        modules=("olympus.vault",),
        rationale="Forecasting and language components must never receive raw "
                  "credentials; a self-modifiable vault would let them."),
    KernelComponent(
        name="permission_system",
        description="Who may perform which privileged action.",
        modules=("olympus.trading.kernel", "olympus.trading.capabilities"),
        rationale="Self-granted permission is not permission. The governance "
                  "check must not be editable by the thing it governs."),
    KernelComponent(
        name="capability_boundaries",
        description="The research sandbox's denial of production resources.",
        modules=("olympus.trading.lab",),
        rationale="An experiment that can widen its own sandbox is not "
                  "isolated, and its results are not trustworthy either."),
    KernelComponent(
        name="order_authorisation",
        description="Requirement that every order cite an approving decision, "
                    "and the adapters that carry it to a venue.",
        modules=("olympus.trading.execution", "olympus.trading.oms",
                 "olympus.trading.brokers"),
        rationale="The rule that no model submits an unauthorised order is "
                  "enforced here; editable, it is a comment. The adapters are "
                  "included because that is where credentials are used and "
                  "where a testnet host allow-list is enforced — a generated "
                  "patch to either is a human decision."),
    KernelComponent(
        name="live_mode_gates",
        description="The nine gates guarding live trading.",
        modules=("olympus.trading.modes",),
        rationale="Live trading must never be enabled because a connection "
                  "worked, and never by the system's own judgement."),
    KernelComponent(
        name="audit_ledger",
        description="Hash-chained, signed, append-only event history.",
        modules=("olympus.trading.audit", "olympus.ledger"),
        rationale="Concealing a negative result is the failure mode that makes "
                  "every other safeguard unverifiable after the fact."),
    KernelComponent(
        name="model_isolation",
        description="Model registration, pinning and approval.",
        modules=("olympus.trading.registry",),
        rationale="An unpinned or self-approved checkpoint makes the recorded "
                  "model version a lie and the audit trail useless."),
    KernelComponent(
        name="human_approval",
        description="Operator-token requirements on every promotion.",
        modules=("olympus.trading.governance",),
        rationale="The external approval path is the whole point of controlled "
                  "evolution; self-approval collapses it."),
    KernelComponent(
        name="deployment_signing",
        description="Versioned, reversible deployment records.",
        modules=("olympus.trading.rollback",),
        rationale="Without a signed previous version there is nothing to roll "
                  "back to, so every other guarantee becomes one-way."),
)

#: Flat set of every protected module path.
KERNEL_MODULES: frozenset[str] = frozenset(
    m for component in SAFETY_KERNEL for m in component.modules)


#: Modules implementing controlled self-evolution. These are the ones audited
#: structurally: they are where an autonomous capability could plausibly grow a
#: shortcut into the kernel, so they are held to the strictest import rules in
#: the package.
EVOLUTION_MODULES: tuple[str, ...] = (
    "olympus.trading.knowledge",
    "olympus.trading.outcomes",
    "olympus.trading.drift",
    "olympus.trading.hypotheses",
    "olympus.trading.lab",
    "olympus.trading.proposals",
    "olympus.trading.champion",
    "olympus.trading.evolution",
    # The native forecasting work. Added from its first commit rather than when
    # it grows a model: the moment to prove a component cannot reach the kernel
    # is before there is anything in it worth being tempted by.
    "olympus.trading.native",
    "olympus.trading.native.state",
    "olympus.trading.native.data",
    "olympus.trading.native.quantile",
    "olympus.trading.native.checkpoint",
    "olympus.trading.native.train",
    "olympus.trading.native.forecaster",
    "olympus.trading.native.torchutil",
    "olympus.trading.native.encoder",
    "olympus.trading.native.trunk",
    "olympus.trading.native.neural",
    "olympus.trading.native.schema",
    "olympus.trading.native.dataset",
    "olympus.trading.native.interfaces",
    "olympus.trading.native.representations",
    "olympus.trading.native.baselines",
    "olympus.trading.native.benchmark",
    "olympus.trading.native.tasks",
    "olympus.trading.native.model",
    "olympus.trading.native.abstain",
    "olympus.trading.native.result",
    "olympus.trading.native.pipeline",
    "olympus.trading.native.serve",
    "olympus.trading.native.evaluation",
    "olympus.trading.native.originality",
    "olympus.trading.native.modelcard",
)


#: Attribute names that *mutate* the kernel. An evolution module may not import
#: or call any of them. Names rather than objects, because this is checked on
#: the parsed AST of source that is never executed during the check.
FORBIDDEN_KERNEL_CALLS: frozenset[str] = frozenset({
    # risk limits
    "LimitsStore", "save_limits",
    # kill switches: engaging is allowed (emergency shutdown is an autonomous
    # power by design); *clearing* one is not, and neither is the registry's
    # write side.
    "disengage", "clear_switch", "clear_all",
    # live mode
    "ModeController", "enable_live", "request_live",
    # credentials
    "vault", "get_secret", "decrypt", "BrokerCredentials",
    # execution
    "ExecutionEngine", "submit_order", "place_order",
    # model approval
    "approve",
    # audit rewriting
    "truncate", "rewrite", "delete_events",
})

#: Modules an evolution module may not import *at all*, in any form. Importing
#: `execution` even without calling it puts a submission path one attribute
#: lookup away from an autonomous caller.
FORBIDDEN_IMPORTS: frozenset[str] = frozenset({
    "olympus.trading.execution",
    "olympus.trading.brokers",
    "olympus.vault",
    "olympus.trading.modes",
})


def kernel_component(name: str) -> KernelComponent:
    for component in SAFETY_KERNEL:
        if component.name == name:
            return component
    raise ConfigurationError("unknown safety-kernel component", name=name,
                             known=[c.name for c in SAFETY_KERNEL])


def is_kernel_module(module: str) -> bool:
    """True if `module` is protected, including submodules.

    Prefix matching on a dotted boundary: `olympus.trading.brokers.paper` is
    protected because `olympus.trading.brokers` is, but `olympus.trading.riskier`
    is not protected merely because `olympus.trading.risk` is.
    """
    module = str(module or "").strip()
    if not module:
        return False
    return any(module == k or module.startswith(k + ".") for k in KERNEL_MODULES)


def _module_to_path(module: str) -> Path:
    """Resolve a dotted module to a file, without importing it.

    Deliberately import-free: the seal must be computable for a module that
    cannot be imported (missing optional dependency, syntax error mid-edit),
    because those are exactly the states in which someone wants to ask what
    changed.
    """
    root = Path(__file__).resolve().parent.parent.parent
    candidate = root / Path(*module.split(".")).with_suffix(".py")
    if candidate.exists():
        return candidate
    package = root / Path(*module.split(".")) / "__init__.py"
    if package.exists():
        return package
    raise ConfigurationError("kernel module has no source file on disk",
                             module=module, looked_in=str(candidate))


def path_to_module(path: Any) -> str:
    """Best-effort inverse of `_module_to_path`, for change proposals.

    Returns a dotted path when `path` is inside the repository, otherwise the
    input unchanged — a proposal naming a file outside the tree is somebody
    else's problem, but must not silently look unprotected.
    """
    p = Path(path)
    root = Path(__file__).resolve().parent.parent.parent
    try:
        rel = p.resolve().relative_to(root) if p.is_absolute() else p
    except ValueError:
        return str(path)
    parts = list(rel.parts)
    if not parts or not parts[-1].endswith(".py"):
        return str(path)
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def assert_not_kernel(target: Any, *, what: str = "target") -> str:
    """Refuse an operation aimed at the safety kernel.

    Accepts a dotted module or a file path. This is the call every code-change
    proposal passes through; it is the reason a generated patch cannot name
    `risk.py` and be accepted as an ordinary change.
    """
    module = str(target)
    if module.endswith(".py") or "/" in module or "\\" in module:
        module = path_to_module(target)
    if is_kernel_module(module):
        owner = next((c.name for c in SAFETY_KERNEL
                      if any(module == m or module.startswith(m + ".")
                             for m in c.modules)), "unknown")
        raise SafetyKernelViolation(
            f"{what} names a safety-kernel module; Olympus may propose a "
            "change to the kernel for human review but may never apply one",
            target=module, component=owner)
    return module


# ---------------------------------------------------------------------------
# 2. sealing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KernelSeal:
    """A content hash of the whole kernel, plus per-module digests.

    Per-module digests are kept alongside the aggregate so a failed
    verification names *which* module changed. An aggregate alone would tell an
    operator that something is wrong and nothing about where.
    """

    digest: str
    modules: Mapping[str, str]
    sealed_at: datetime | None = None
    sealed_by: str = "system"

    def __post_init__(self):
        object.__setattr__(self, "modules", dict(self.modules))
        if self.sealed_at is not None:
            object.__setattr__(self, "sealed_at",
                               ensure_utc(self.sealed_at, field_name="sealed_at"))

    def to_dict(self) -> dict:
        return {"digest": self.digest, "modules": dict(self.modules),
                "sealed_at": jsonable(self.sealed_at), "sealed_by": self.sealed_by}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KernelSeal":
        when = data.get("sealed_at")
        return cls(digest=str(data.get("digest", "")),
                   modules=data.get("modules") or {},
                   sealed_at=datetime.fromisoformat(when) if isinstance(when, str) and when else None,
                   sealed_by=str(data.get("sealed_by", "system")))


def seal(*, clock: Clock | None = None, sealed_by: str = "system") -> KernelSeal:
    """Hash every kernel module's source.

    Hashing bytes rather than a parsed form is intentional. A comment change in
    `risk.py` breaks the seal, and that is correct: the reviewer should look. A
    seal that ignored comments would ignore a comment that says why a limit
    exists.
    """
    digests: dict[str, str] = {}
    for module in sorted(KERNEL_MODULES):
        try:
            source = _module_to_path(module).read_bytes()
        except ConfigurationError:
            # A declared-but-absent kernel module is itself a finding: record
            # it as a sentinel so verification reports the disappearance rather
            # than quietly sealing a smaller kernel.
            digests[module] = "missing"
            continue
        digests[module] = hashlib.sha256(source).hexdigest()
    aggregate = hashlib.sha256(
        "\n".join(f"{m}:{d}" for m, d in sorted(digests.items())).encode("utf-8")
    ).hexdigest()
    return KernelSeal(digest=aggregate, modules=digests,
                      sealed_at=(clock or default_clock()).now(),
                      sealed_by=sealed_by)


@dataclass(frozen=True)
class SealVerification:
    """The result of comparing a stored seal against the code on disk."""

    intact: bool
    expected: str
    actual: str
    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"intact": self.intact, "expected": self.expected,
                "actual": self.actual, "changed": list(self.changed),
                "added": list(self.added), "removed": list(self.removed)}

    def describe(self) -> str:
        if self.intact:
            return f"safety kernel intact ({len(self.expected) and self.expected[:12]}…)"
        parts = []
        if self.changed:
            parts.append("changed: " + ", ".join(self.changed))
        if self.added:
            parts.append("added: " + ", ".join(self.added))
        if self.removed:
            parts.append("removed: " + ", ".join(self.removed))
        return "SAFETY KERNEL MODIFIED — " + "; ".join(parts or ["digest mismatch"])


def verify_seal(expected: KernelSeal | Mapping[str, Any]) -> SealVerification:
    """Compare a previously taken seal with the code as it stands now."""
    if not isinstance(expected, KernelSeal):
        expected = KernelSeal.from_dict(expected)
    current = seal()
    changed = tuple(sorted(
        m for m, d in expected.modules.items()
        if m in current.modules and current.modules[m] != d))
    added = tuple(sorted(set(current.modules) - set(expected.modules)))
    removed = tuple(sorted(set(expected.modules) - set(current.modules)))
    intact = (current.digest == expected.digest and not changed
              and not added and not removed)
    return SealVerification(intact=intact, expected=expected.digest,
                            actual=current.digest, changed=changed,
                            added=added, removed=removed)


# ---------------------------------------------------------------------------
# 3. structural denial
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KernelAccessFinding:
    """One way a module could reach the kernel."""

    module: str
    line: int
    kind: str            # "import" | "call" | "attribute"
    detail: str

    def to_dict(self) -> dict:
        return {"module": self.module, "line": self.line, "kind": self.kind,
                "detail": self.detail}

    def __str__(self) -> str:  # pragma: no cover - reporting aid
        return f"{self.module}:{self.line} {self.kind} {self.detail}"


def _resolve_relative(module: str, node: ast.ImportFrom) -> str:
    """Turn `from ..vault import x` inside `olympus.trading.foo` into `olympus.vault`."""
    if not node.level:
        return node.module or ""
    parts = module.split(".")[:-node.level] if node.level else module.split(".")
    if node.module:
        parts = parts + node.module.split(".")
    return ".".join(parts)


def scan_module_for_kernel_access(module: str,
                                  source: str | None = None
                                  ) -> list[KernelAccessFinding]:
    """Parse one module and report every route it has into the kernel.

    Import-free by construction: the module is read from disk and parsed, never
    executed. Auditing code by importing it would run the very code the audit
    exists to distrust.
    """
    if source is None:
        source = _module_to_path(module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    findings: list[KernelAccessFinding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _resolve_relative(module, node)
            for alias in node.names:
                full = f"{target}.{alias.name}" if target else alias.name
                if any(full == f or full.startswith(f + ".")
                       for f in FORBIDDEN_IMPORTS) or target in FORBIDDEN_IMPORTS:
                    findings.append(KernelAccessFinding(
                        module=module, line=node.lineno, kind="import",
                        detail=f"from {target} import {alias.name}"))
                elif alias.name in FORBIDDEN_KERNEL_CALLS and is_kernel_module(target):
                    findings.append(KernelAccessFinding(
                        module=module, line=node.lineno, kind="import",
                        detail=f"from {target} import {alias.name}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == f or alias.name.startswith(f + ".")
                       for f in FORBIDDEN_IMPORTS):
                    findings.append(KernelAccessFinding(
                        module=module, line=node.lineno, kind="import",
                        detail=f"import {alias.name}"))
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in FORBIDDEN_KERNEL_CALLS:
                findings.append(KernelAccessFinding(
                    module=module, line=node.lineno, kind="call",
                    detail=f"{name}(...)"))
    return findings


def audit_evolution_modules(modules: Sequence[str] | None = None
                            ) -> list[KernelAccessFinding]:
    """Scan every self-evolution module. Empty result is the passing state.

    This is gate 13 in executable form. It runs in the test suite, so a future
    change that imports `ExecutionEngine` into the hypothesis engine fails CI
    rather than shipping.
    """
    findings: list[KernelAccessFinding] = []
    for module in (modules if modules is not None else EVOLUTION_MODULES):
        try:
            findings.extend(scan_module_for_kernel_access(module))
        except ConfigurationError:
            continue                      # not yet written; nothing to audit
    return findings


def assert_evolution_isolated(modules: Sequence[str] | None = None) -> None:
    """Raise if any evolution module can reach the kernel."""
    findings = audit_evolution_modules(modules)
    if findings:
        raise SafetyKernelViolation(
            "self-evolution modules must have no route into the safety kernel",
            findings=[f.to_dict() for f in findings])


# ---------------------------------------------------------------------------
# recommendation — the one thing Olympus may do to the kernel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KernelChangeRecommendation:
    """A document. Explicitly not an action.

    Returned by `propose_kernel_change`, which has no counterpart that applies
    it. Reviewers get the reasoning; nothing in the package consumes this
    object to mutate anything.
    """

    component: str
    summary: str
    rationale: str
    proposed_by: str
    evidence: tuple[str, ...] = ()
    risk_if_accepted: str = ""
    created_at: datetime | None = None

    def __post_init__(self):
        if self.created_at is not None:
            object.__setattr__(self, "created_at",
                               ensure_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_dict(self) -> dict:
        return {"component": self.component, "summary": self.summary,
                "rationale": self.rationale, "proposed_by": self.proposed_by,
                "evidence": list(self.evidence),
                "risk_if_accepted": self.risk_if_accepted,
                "created_at": jsonable(self.created_at),
                "applied": False,
                "note": "recommendation only; the safety kernel is not "
                        "self-modifiable and no code path applies this"}


def propose_kernel_change(*, component: str, summary: str, rationale: str,
                          proposed_by: str,
                          evidence: Iterable[str] = (),
                          risk_if_accepted: str = "",
                          clock: Clock | None = None
                          ) -> KernelChangeRecommendation:
    """Record a recommendation that a human change the kernel.

    Validates the component name so a recommendation cannot quietly refer to
    something that is not actually protected — which would read, to a reviewer
    skimming, as though the kernel had an area it does not.
    """
    kernel_component(component)                      # raises on unknown name
    if not str(summary).strip():
        raise ConfigurationError("a kernel-change recommendation needs a summary")
    if not str(proposed_by).strip():
        raise ConfigurationError(
            "a kernel-change recommendation must name its proposer")
    return KernelChangeRecommendation(
        component=component, summary=summary, rationale=rationale,
        proposed_by=proposed_by, evidence=tuple(evidence),
        risk_if_accepted=risk_if_accepted,
        created_at=(clock or default_clock()).now())


def describe_kernel() -> str:
    """Human-readable kernel report, for the operator console."""
    lines = [f"safety kernel — {len(SAFETY_KERNEL)} components, "
             f"{len(KERNEL_MODULES)} modules (not self-modifiable)"]
    for component in SAFETY_KERNEL:
        lines.append(f"  {component.name}: {', '.join(component.modules)}")
    return "\n".join(lines)


__all__ = [
    "KernelComponent", "SAFETY_KERNEL", "KERNEL_MODULES", "EVOLUTION_MODULES",
    "FORBIDDEN_KERNEL_CALLS", "FORBIDDEN_IMPORTS", "kernel_component",
    "is_kernel_module", "path_to_module", "assert_not_kernel",
    "KernelSeal", "seal", "SealVerification", "verify_seal",
    "KernelAccessFinding", "scan_module_for_kernel_access",
    "audit_evolution_modules", "assert_evolution_isolated",
    "KernelChangeRecommendation", "propose_kernel_change", "describe_kernel",
]
