"""Governed scaffold evolution — PROPOSE-ONLY (ADR 0003).

Adopts the Darwin Gödel Machine idea (measured, archived, benchmark-gated
self-improvement of code) with the dangerous part removed: there is **no code
path here that writes to Olympus's own source tree, and no `apply()` function.**
The engine generates a candidate patch, benchmarks it in isolation, archives the
variant, and surfaces it as a diff for a human to apply out-of-band.

Structural guarantees:
  * Non-security modules only — a fail-closed allowlist (`_EVOLVABLE`) plus an
    independent denylist (`_SECURITY_MODULES`). Unknown ⇒ not evolvable.
  * Benchmarked in isolation — a candidate must at least `compile()`; a pluggable
    benchmark may run more. The candidate source is written only to a throwaway
    temp path, never the real module.
  * Archived — every proposal (pass or fail) lands in an append-only, bounded
    archive with its result.
  * Governed — the `scaffold.propose` ABC contract gates archival-as-proposal.
  * Off by default (`OLYMPUS_SCAFFOLD_EVOLVE`); nothing auto-applies.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from . import store

_ARCHIVE_NS = "scaffold_evolve"
_ARCHIVE_KEY = "archive"
_MAX_ARCHIVE = 200
_MAX_SOURCE = 200_000        # a proposed module source is capped (chars)

# Security-critical modules that may NEVER be proposed against (independent
# second guard behind the allowlist). Basenames without the .py suffix.
_SECURITY_MODULES = frozenset({
    "security", "cmdguard", "actions", "builtin_actions", "behavioral_contracts",
    "mandate", "mandate_store", "witness", "vault", "egress", "capprofile",
    "secretref", "sandbox", "approvals", "replaygate", "replaystore", "attest",
    "opconfig", "firstrun", "config", "operator", "identity",
})

# The fail-closed allowlist: only these clearly non-security, mostly-pure modules
# may be proposed against. Curated conservatively; extend only with review.
_EVOLVABLE = frozenset({
    "transcript", "i18n", "emailstyle", "journey", "digest", "webplan",
    "ace", "media", "wiki", "recall", "spamtriage",
})


def is_evolvable(module: str) -> bool:
    """True only for an explicitly-allowlisted, non-security module. Fail-closed:
    anything unknown, or on the denylist, is NOT evolvable."""
    name = _module_name(module)
    if not name or name in _SECURITY_MODULES:
        return False
    return name in _EVOLVABLE


def _module_name(module: str) -> str:
    base = Path(str(module)).name
    return base[:-3] if base.endswith(".py") else base


def _module_path(module: str) -> Path:
    return Path(__file__).with_name(_module_name(module) + ".py")


def enabled() -> bool:
    return os.environ.get("OLYMPUS_SCAFFOLD_EVOLVE", "").strip().lower() in (
        "1", "on", "true", "yes")


# --- candidate model ------------------------------------------------------

@dataclass
class Candidate:
    id: str
    module: str
    before: str
    after: str
    compiles: bool
    benchmark_passed: bool
    score: float
    note: str
    created_at: float
    valid: bool = False          # passed the scaffold.propose contract → surfaceable
    applied: bool = False        # ALWAYS False — there is no apply path

    def to_dict(self) -> dict:
        return asdict(self)


def _compiles(source: str, module: str) -> bool:
    """Does the candidate parse as valid Python? In-process, no execution."""
    try:
        ast.parse(source, filename=_module_name(module) + ".py")
        return True
    except (SyntaxError, ValueError):
        return False


GenerateFn = Callable[[str], str]                 # (before_source) -> after_source
BenchmarkFn = Callable[[str, str], "tuple[bool, float]"]   # (tmp_path, module) -> (passed, score)


def _default_benchmark(tmp_path: str, module: str) -> tuple[bool, float]:
    """The floor benchmark: the candidate, written to a throwaway path, imports
    as a module without executing side effects at top level beyond definition.
    We only compile+parse here (no exec of the temp file into the live process);
    a richer benchmark is the caller's to inject."""
    try:
        src = Path(tmp_path).read_text(encoding="utf-8")
    except OSError:
        return False, 0.0
    return (_compiles(src, module), 1.0 if _compiles(src, module) else 0.0)


# --- propose (the only entry that produces a candidate) -------------------

def propose(module: str, generate: GenerateFn, *,
            benchmark: BenchmarkFn | None = None) -> Candidate:
    """Generate + benchmark + archive a candidate patch for `module`. NEVER
    writes to the real module. Raises `ValueError` if the module is not
    evolvable, and `behavioral_contracts.ContractViolation` if the candidate
    fails the `scaffold.propose` contract (it is still archived as a failed
    proposal for the record). Returns the Candidate."""
    if not is_evolvable(module):
        raise ValueError(
            f"module {module!r} is not evolvable (security or not allowlisted)")
    before = _module_path(module).read_text(encoding="utf-8")
    after = str(generate(before))[:_MAX_SOURCE]
    compiles = bool(after.strip()) and _compiles(after, module)

    passed, score = False, 0.0
    if compiles:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".py", prefix="scaffold_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(after)
            bench = benchmark or _default_benchmark
            passed, score = bench(tmp, module)
        except Exception as err:
            from . import errors
            errors.capture("scaffold_evolve.benchmark", err)
            passed, score = False, 0.0
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # Governance: the scaffold.propose contract decides whether this candidate is
    # a SURFACEABLE proposal. A failing candidate is still archived — as a failed
    # variant, for the record — but flagged invalid and never surfaced. (There is
    # no action to execute here, so "block" means "excluded from proposals",
    # not raised: propose-only can never apply anything.)
    from . import behavioral_contracts as abc
    verdict = abc.check("scaffold.propose", {
        "target_evolvable": is_evolvable(module),
        "candidate_compiles": compiles,
        "benchmark_passed": bool(passed)})

    cand = Candidate(
        id="sc_" + uuid.uuid4().hex[:12], module=_module_name(module),
        before=before, after=after, compiles=compiles,
        benchmark_passed=bool(passed), score=float(score),
        note=("ok" if verdict.ok else
              "; ".join(verdict.reasons) or "rejected by benchmark"),
        created_at=time.time(), valid=verdict.ok, applied=False)
    _archive(cand)
    return cand


# --- archive (append-only, bounded) ---------------------------------------

def _archive(cand: Candidate) -> None:
    cur = archive()
    cur.append(cand.to_dict())
    store.backend().put(_ARCHIVE_NS, _ARCHIVE_KEY,
                        json.dumps(cur[-_MAX_ARCHIVE:]).encode("utf-8"))
    try:
        from . import evolve
        evolve.log_event("scaffold_evolve", "proposal", {
            "module": cand.module, "compiles": cand.compiles,
            "benchmark_passed": cand.benchmark_passed, "score": cand.score})
    except Exception:
        pass


def archive() -> list[dict]:
    blob = store.backend().get(_ARCHIVE_NS, _ARCHIVE_KEY)
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def proposals(module: str | None = None, *, valid_only: bool = False
              ) -> list[dict]:
    """Archived candidates. `valid_only` returns only those that passed the
    scaffold.propose contract (surfaceable, human-reviewable proposals)."""
    out = archive()
    if module:
        out = [c for c in out if c.get("module") == _module_name(module)]
    if valid_only:
        out = [c for c in out if c.get("valid")]
    return out


def render_diff(candidate: dict | Candidate) -> str:
    """A unified diff of a proposal (before → after) for human review. This is
    the ONLY thing that leaves the engine toward a human — there is no apply."""
    c = candidate.to_dict() if isinstance(candidate, Candidate) else candidate
    module = c.get("module", "?")
    diff = difflib.unified_diff(
        str(c.get("before", "")).splitlines(),
        str(c.get("after", "")).splitlines(),
        fromfile=f"olympus/{module}.py", tofile=f"proposal/{c.get('id', '?')}",
        lineterm="")
    header = (f"# scaffold proposal {c.get('id', '?')} for {module} "
              f"[{'PASS' if c.get('benchmark_passed') else 'FAIL'}] "
              f"— review and apply by hand; nothing auto-applies")
    return "\n".join([header, *diff])
