"""Earned per-domain autonomy — the honest freedom engine.

Olympus should not have to ask permission for every safe, reversible action on a
site it has already proven itself on. But it must never talk itself into more
authority than it has earned, and any surprise must cost it that authority at
once. That is the whole shape of this module.

Trust is graded per *domain*, from Olympus's own witnessed action history. It is
EARNED SLOWLY — an unbroken run of clean, governed successes on that exact domain
— and it SNAPS BACK FAST: a single surprise (a failed run, a reversal/undo, a
rejection, or a human-verification checkpoint — all of which land as non-success
in the immutable audit log) resets the domain to zero.

Two hard invariants keep this inside the moat:

  1. Earned trust can only ever widen auto-execution for REVERSIBLE actions
     (trivial / notable). The approval gate on IRREVERSIBLE, FINANCIAL, and LEGAL
     actions is never touched — money, deletions, and other one-way steps always
     need a human, no matter how trusted the site is.
  2. The score is a PURE FUNCTION of the append-only action audit log. There is
     no mutable trust counter for a prompt-injected agent to inflate, and the
     result is always re-capped by the conversation's capability profile — so an
     ingesting or guest run can never be lifted by it.

It is OFF until opted in (the ``OLYMPUS_EARNED_AUTONOMY`` instance switch or a
per-user setting via ``olympus earned-autonomy on``), and it never bypasses a
permission scope: the operator scope, domain authorization, and daily runaway
caps all still apply underneath it.
"""

from __future__ import annotations

from . import actions, config, prefs

# Trust tiers — a step function of the clean-run streak.
PROBATION, TRUSTED, ESTABLISHED = 0, 1, 2
_TIER_NAMES = {PROBATION: "probation", TRUSTED: "trusted",
               ESTABLISHED: "established"}

# Earn slowly: a domain must accrue this many CONSECUTIVE clean governed runs to
# reach each tier. Snap-back is implicit — any non-success resets the streak, so
# regaining a tier means rebuilding the whole run (the asymmetry is the point).
_TRUSTED_AFTER = 5
_ESTABLISHED_AFTER = 20

_PREF_KEY = "earned_autonomy"

# Every governed operator action type shares this prefix; each is domain-scoped,
# scope-gated, and audit-logged, so all of them both earn and risk trust.
_OPERATE_PREFIX = "browser_operate"


# --- opt-in switch --------------------------------------------------------

def enabled(user: str) -> bool:
    """Earned autonomy is OFF until opted in — via the instance-wide env switch
    or this user's own setting. Either turns it on; neither means off."""
    if config.earned_autonomy_enabled():
        return True
    return bool(prefs.get(user, _PREF_KEY, False))


def set_enabled(user: str, on: bool) -> str:
    prefs.set(user, _PREF_KEY, bool(on))
    if on:
        return ("Earned autonomy ON — Olympus may auto-run safe, REVERSIBLE "
                "actions on a site once it has a long clean track record there. "
                "Money, deletions, and other irreversible steps always still ask, "
                "and any surprise resets a site's earned trust to zero.")
    return ("Earned autonomy OFF — every action falls back to your global "
            "autonomy level.")


# --- the streak: a pure function of the audit log -------------------------

def _domain_of(a) -> str:
    return ((getattr(a, "payload", None) or {}).get("domain") or "").strip().lower()


def _is_operate(a) -> bool:
    return str(getattr(a, "type", "")).startswith(_OPERATE_PREFIX)


def streak(user: str, domain: str) -> int:
    """Consecutive clean governed runs on `domain` since the last surprise.

    Derived from the immutable action audit trail, never a stored counter: an
    EXECUTED operator action increments the streak; a FAILED, REJECTED, or UNDONE
    one — which is how a checkpoint handoff, a template drift, a reversal, or a
    human decline all surface — resets it to zero. Nothing an injected agent can
    write inflates this, because it only reads outcomes the spine already sealed.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return 0
    hist = [a for a in actions.history(user, limit=1000)
            if _is_operate(a) and _domain_of(a) == domain]
    hist.sort(key=lambda a: getattr(a, "created_at", 0.0))   # oldest first
    s = 0
    for a in hist:
        if a.status == actions.EXECUTED:
            s += 1
        elif a.status in (actions.FAILED, actions.REJECTED, actions.UNDONE):
            s = 0                                             # snap back, fast
    return s


def tier(user: str, domain: str) -> int:
    s = streak(user, domain)
    if s >= _ESTABLISHED_AFTER:
        return ESTABLISHED
    if s >= _TRUSTED_AFTER:
        return TRUSTED
    return PROBATION


def tier_name(t: int) -> str:
    return _TIER_NAMES.get(t, "probation")


# --- the gate modifier ----------------------------------------------------

def effective_autonomy(user: str, domain: str) -> int:
    """The autonomy level to use for a REVERSIBLE action on `domain`, folding in
    earned trust. Returns the user's global level unchanged when earned autonomy
    is off or the domain hasn't earned anything.

    The result is always re-capped by the conversation's capability profile, so a
    boost can never lift an ingesting/guest run above its ceiling. It never raises
    the bar for irreversible/financial/legal actions: those map to a minimum level
    of 99 in the spine and stay there regardless of what this returns.
    """
    base = actions.autonomy_level(user)
    if not enabled(user):
        return base
    t = tier(user, domain)
    if t >= ESTABLISHED:
        boosted = max(base, actions.L4_STANDING)     # auto-run standing reversible flows
    elif t >= TRUSTED:
        boosted = max(base, actions.L3_AUTO_SAFE)    # auto-run trivial reversible actions
    else:
        boosted = base
    # Re-apply the capability-profile cap: the max() above may have exceeded it.
    from . import capprofile
    return min(boosted, capprofile.autonomy_cap(user))


# --- introspection + self-evolution surface -------------------------------

def domains(user: str) -> list[str]:
    """Domains that appear in the user's governed action history, newest first."""
    seen: list[str] = []
    for a in actions.history(user, limit=1000):
        if _is_operate(a):
            d = _domain_of(a)
            if d and d not in seen:
                seen.append(d)
    return seen


def report(user: str) -> str:
    """The earned-trust ladder: each domain's tier and clean streak, most-trusted
    first. Read-only — shows where Olympus has earned freedom and where a surprise
    recently snapped it back to probation."""
    header = ("Earned autonomy: ON" if enabled(user) else
              "Earned autonomy: off (your global autonomy level governs every site)")
    graded = [(tier(user, d), streak(user, d), d) for d in domains(user)]
    if not graded:
        return header + "\n  No governed site history yet."
    graded.sort(key=lambda r: (-r[0], -r[1], r[2]))
    lines = [header]
    for t, s, d in graded:
        if t == ESTABLISHED:
            toward = ""
        else:
            nxt_streak = _ESTABLISHED_AFTER if t == TRUSTED else _TRUSTED_AFTER
            toward = f" — {nxt_streak - s} more clean run(s) to {_TIER_NAMES[t + 1]}"
        lines.append(f"  • {d}: {_TIER_NAMES[t]} (clean streak {s}){toward}")
    return "\n".join(lines)


def earned_hint(user: str) -> str:
    """One-line self-evolution note for the operator review: which proven sites
    have earned auto-run for safe actions (the moat compounding — a site's clean
    history buys Olympus freedom there, so it stops asking on the easy things)."""
    if not enabled(user):
        return ""
    earned = [d for d in domains(user) if tier(user, d) >= TRUSTED]
    if not earned:
        return ""
    return ("Earned autonomy on proven sites (safe/reversible actions auto-run): "
            + ", ".join(earned[:5]))
