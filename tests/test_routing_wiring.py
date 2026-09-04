"""W2-PR11: Wave-2 model qualification + routing substitution on the LIVE seam.

The Wave-2 completion report (§2) scored A1/A2/A4/A5 as FAIL/PARTIAL for one
reason only — `modelgrade` and `routesub` were built, tested and reversible, but
nothing in the live selection path consulted them. This suite covers the wiring
itself, in `config.ModelPool.for_specialist`:

  flag-off    with `OLYMPUS_ROUTESUB` unset the seam is not merely inert, it is
              NOT CONSULTED: the module's entry points are replaced with raisers
              and every pick is still byte-identical to today's.
  shadow      a decision row IS written and the returned member is unchanged.
  on   (A1/A4) a qualified, cheaper, measured-equivalent candidate IS used, and
              the recorded row carries the live preferred/substituted routes,
              the reason and the estimated savings.
  A5          verification work is never substituted below the verifier floor —
              not when the cheaper candidate is otherwise fully qualified (the
              doctrine block), and not when the relaxable doctrine block is
              explicitly turned off (the W2-I3.1 floor, which no flag relaxes).
  A2          a member `modelgrade` marks FROZEN/QUARANTINED for the cell is not
              selected while another qualified member exists; when NO member is
              qualified the heuristic pick survives (the deliberate fail-open)
              AND the event is recorded, so it is never silent.
  fail-safe   `routesub` raising, `modelgrade` raising, and `modelgrade`
              disabled all leave the pick exactly as the heuristic chose it.
  pairing     both flag names are in the orchestrator's `tr.meta` assembly AND
              in `replay_run`'s env-reproduction list. Source-scanned, which is
              the house style for this invariant (see `test_wave2_rollback`):
              the failure it guards against — a substituted run replaying on a
              different member — is a property of the SOURCE pairing, and a
              runtime test would need a recorded run to detect it after the
              fact.

`conftest.isolated_memory` gives each test a private MEMORY_DIR and
`preserve_environ` restores the flags, so no test can leak evidence or a ledger
into another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olympus import config, modelgate, modelgrade, routesub

# Prices from config.price_per_mtok: opus 30.0, haiku 1.5 — so haiku is a
# genuinely CHEAPER candidate, which is the only kind `routesub` considers.
OPUS = config.Settings(provider="anthropic", model="claude-opus-4-8",
                       api_key="k")
HAIKU = config.Settings(provider="anthropic", model="claude-haiku-4",
                        api_key="k")
GEMINI = config.Settings(provider="openai", model="gemini-2.5-pro", api_key="k")

OPUS_ID = "anthropic/claude-opus-4-8"
HAIKU_ID = "anthropic/claude-haiku-4"

POOL = config.ModelPool(members=(OPUS, HAIKU))
BIG_POOL = config.ModelPool(members=(OPUS, HAIKU, GEMINI))
SOLO = config.ModelPool(members=(OPUS,))

CODER = "hephaestus"          # role=coding  -> heuristic picks OPUS
VERIFIER = "aletheia"         # routesub.is_verification() -> True


@pytest.fixture(autouse=True)
def _clean_warmth():
    """Warmth is process-global; a leaked entry would make an unrelated member
    a substitution candidate."""
    routesub.clear_warmth()
    yield
    routesub.clear_warmth()


@pytest.fixture(autouse=True)
def _no_wave2_flags(monkeypatch):
    """Start every test from the shipped defaults, whatever the ambient env."""
    for name in ("OLYMPUS_ROUTESUB", "OLYMPUS_MODELGRADE", "OLYMPUS_REPLAY",
                 "OLYMPUS_SPECIALIST_MODELS", "OLYMPUS_LEARNED_ROUTING",
                 "OLYMPUS_BANDIT_ROUTING", "OLYMPUS_SOVEREIGN"):
        monkeypatch.delenv(name, raising=False)
    yield


def graded(monkeypatch, mode: str = "") -> None:
    """Enable the evidence store, and `routesub` in `mode` when given.

    Band and confidence are pinned rather than left at their PROVISIONAL
    defaults so a later calibration of those constants cannot silently turn
    these tests vacuous."""
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "1")
    if mode:
        monkeypatch.setenv("OLYMPUS_ROUTESUB", mode)
    monkeypatch.setenv("OLYMPUS_ROUTESUB_BAND", "0.25")
    monkeypatch.setenv("OLYMPUS_ROUTESUB_CONFIDENCE", "0.5")


def cell_of(specialist: str):
    """The exact cell the live seam will look evidence up under."""
    return routesub.cell_for(specialist)


def seed(member, cell, *, success=1.0, n=60, dimension="quality"):
    """Measured evidence — the ONLY thing that can promote a member (W2-I1.1)."""
    assert modelgrade.observe(provider=member.provider, model=member.model,
                              cell=cell, dimension=dimension, success=success,
                              n=n, source="test")


def freeze(monkeypatch, member_id: str, severity: str = "quarantine") -> None:
    """Reflect a `modelgate` freeze/quarantine marker for one member."""
    monkeypatch.setattr(modelgate, "frozen_members",
                        lambda: {member_id: {"severity": severity,
                                             "reason": "test marker"}})


def rows(kind: str | None = None) -> list[dict]:
    out = routesub.decisions()
    return [r for r in out if kind is None or r.get("kind") == kind]


# --- flag off: the seam is not consulted at all ----------------------------

_CASES = [(POOL, CODER), (POOL, "plutus"), (POOL, VERIFIER), (POOL, "argus"),
          (BIG_POOL, CODER), (BIG_POOL, "chronos"), (SOLO, CODER),
          (SOLO, VERIFIER), (POOL, "not-a-specialist")]


def test_flag_off_is_byte_identical_and_never_consults_wave2(monkeypatch):
    """With `OLYMPUS_ROUTESUB` unset, `for_specialist` returns EXACTLY what it
    returns today — proven by making every Wave-2 entry point raise. If the seam
    were consulted at all (even to be ignored), these picks would change or the
    raiser would escape."""
    baseline = [pool.for_specialist(key) for pool, key in _CASES]

    def boom(*a, **k):
        raise AssertionError("Wave-2 policy consulted while its flag is off")

    for mod, names in ((routesub, ("choose", "evaluate", "enabled", "mode",
                                   "cell_for", "member_id")),
                       (modelgrade, ("card", "status", "qualified", "enabled",
                                     "cell_key"))):
        for name in names:
            monkeypatch.setattr(mod, name, boom)

    after = [pool.for_specialist(key) for pool, key in _CASES]
    assert after == baseline
    for got, want in zip(after, baseline):
        assert got is want               # same object, not merely equal
    assert not routesub.ledger_path().exists(), "off mode wrote a ledger"


def test_modelgrade_flag_off_leaves_protected_cells_alone(monkeypatch):
    """The A2 guard is gated on `OLYMPUS_MODELGRADE` too: with the store off,
    even the verify cell keeps today's heuristic pick."""
    def boom(*a, **k):
        raise AssertionError("modelgrade consulted while its flag is off")

    monkeypatch.setattr(modelgrade, "status", boom)
    monkeypatch.setattr(modelgrade, "card", boom)
    assert POOL.for_specialist(VERIFIER) is OPUS
    assert POOL.for_specialist(CODER) is OPUS


# --- shadow: records, changes nothing --------------------------------------

def test_shadow_mode_records_a_decision_but_returns_the_same_member(monkeypatch):
    graded(monkeypatch, "shadow")
    cell = cell_of(CODER)
    seed(OPUS, cell)
    seed(HAIKU, cell)

    assert POOL.for_specialist(CODER) is OPUS      # routing UNCHANGED

    recorded = rows("decision")
    assert len(recorded) == 1, recorded
    row = recorded[0]
    assert row["mode"] == "shadow"
    assert row["substituted"] is False             # never in shadow
    assert row["counterfactual"] is True           # ...but it WOULD have
    assert row["preferred_route"] == OPUS_ID
    assert row["candidate_route"] == HAIKU_ID
    assert row["substituted_route"] == ""
    assert row["specialist"] == CODER


# --- on: a qualified cheaper candidate is used (A1/A4) ---------------------

def test_on_mode_substitutes_a_qualified_cheaper_candidate(monkeypatch):
    graded(monkeypatch, "on")
    cell = cell_of(CODER)
    seed(OPUS, cell)
    seed(HAIKU, cell)

    assert POOL.for_specialist(CODER) is HAIKU     # the live pick MOVED

    row = rows("decision")[0]
    assert row["mode"] == "on"
    assert row["substituted"] is True
    assert row["preferred_route"] == OPUS_ID       # original preferred route
    assert row["substituted_route"] == HAIKU_ID    # substituted route
    assert row["reason"] and row["blocked_by"] == ""
    assert row["estimated_savings_usd"] > 0        # estimated savings
    assert row["cell"] == modelgrade.cell_key(cell)


def test_on_mode_never_substitutes_without_decision_evidence(monkeypatch):
    """The live seam keeps its incumbent when the audit row cannot persist."""
    graded(monkeypatch, "on")
    cell = cell_of(CODER)
    seed(OPUS, cell)
    seed(HAIKU, cell)
    monkeypatch.setattr(routesub, "_append", lambda _row: False)

    assert POOL.for_specialist(CODER) is OPUS
    assert rows("decision") == []


def test_on_mode_without_evidence_keeps_the_heuristic_pick(monkeypatch):
    """No measured evidence => `untested` => the fail-safe answer is the
    quality-first pick. Enabling the flags on a fresh install changes no route."""
    graded(monkeypatch, "on")
    assert POOL.for_specialist(CODER) is OPUS
    assert rows("decision")[0]["blocked_by"] in routesub.PRECONDITIONS


# --- A5: the verifier floor is never crossed -------------------------------

def test_verify_role_is_not_substituted_even_when_candidate_is_qualified(
        monkeypatch):
    """W2-I3.1 / gate A5, hardest case: the cheaper candidate is FULLY qualified
    for the verify cell and clears the verifier floor outright, and the verify
    role is still not substituted (the head of the routing is protected)."""
    graded(monkeypatch, "on")
    cell = cell_of(VERIFIER)
    seed(OPUS, cell, success=1.0)
    seed(HAIKU, cell, success=1.0)
    assert modelgrade.qualified(HAIKU_ID, cell)
    assert routesub.verification_score(
        modelgrade.card(HAIKU_ID, cell)) >= routesub.verifier_floor()

    assert POOL.for_specialist(VERIFIER) is OPUS
    assert rows("decision")[0]["blocked_by"] == "verifier_protected"


def test_verify_role_floor_holds_when_the_doctrine_block_is_relaxed(monkeypatch):
    """`OLYMPUS_ROUTESUB_PROTECT_VERIFIER=0` relaxes the DOCTRINE block only.
    The measured W2-I3.1 floor below it is not relaxable by any flag: a
    candidate that is `qualified` but measures below the verification floor is
    still refused."""
    graded(monkeypatch, "on")
    monkeypatch.setenv("OLYMPUS_ROUTESUB_PROTECT_VERIFIER", "0")
    cell = cell_of(VERIFIER)
    seed(OPUS, cell, success=1.0)
    seed(HAIKU, cell, success=0.9)                 # qualified, but lo < 0.90
    assert modelgrade.qualified(HAIKU_ID, cell)
    assert routesub.verification_score(
        modelgrade.card(HAIKU_ID, cell)) < routesub.verifier_floor()

    assert POOL.for_specialist(VERIFIER) is OPUS
    assert rows("decision")[0]["blocked_by"] == "verifier_floor"


def test_a2_guard_never_moves_verification_below_the_floor(monkeypatch):
    """The A2 guard has its own path to a different member (escaping a frozen
    incumbent), so it re-asserts the floor: a merely-`qualified` verifier below
    the verification floor is not selected even to replace a quarantined one."""
    graded(monkeypatch)                            # routesub OFF: guard only
    cell = cell_of(VERIFIER)
    seed(OPUS, cell, success=1.0)
    seed(HAIKU, cell, success=0.9)                 # qualified, below the floor
    freeze(monkeypatch, OPUS_ID)

    assert POOL.for_specialist(VERIFIER) is OPUS   # fail-open, not a downgrade
    guards = rows("modelgrade_guard")
    assert len(guards) == 1 and guards[0]["guard_action"] == "kept_unqualified"


# --- A2: no unqualified model on a protected cell --------------------------

def test_frozen_member_is_replaced_by_a_qualified_one(monkeypatch):
    """A2: `modelgrade` marks the heuristic pick QUARANTINED for this cell, so
    the next QUALIFIED member in the pool's own role-fallback order is used."""
    graded(monkeypatch)                            # routesub stays OFF
    cell = cell_of(CODER)
    seed(HAIKU, cell)
    freeze(monkeypatch, OPUS_ID)
    assert modelgrade.status(OPUS_ID, cell) == modelgrade.QUARANTINED

    assert POOL.for_specialist(CODER) is HAIKU

    guards = rows("modelgrade_guard")
    assert len(guards) == 1, guards
    assert guards[0]["guard_action"] == "requalified"
    assert guards[0]["preferred_route"] == OPUS_ID
    assert guards[0]["substituted_route"] == HAIKU_ID
    assert guards[0]["preferred_state"] == modelgrade.QUARANTINED
    assert guards[0]["substituted"] is False       # not a COST substitution


def test_frozen_pick_survives_when_nothing_is_qualified_and_is_recorded(
        monkeypatch):
    """The deliberate fail-open: with the incumbent frozen and NO member
    qualified, today's heuristic pick is kept (refusing to answer on thin
    evidence would be worse than the Wave-1 behaviour) — but the event is
    RECORDED, so an operator can see every protected cell that ran unqualified.
    """
    graded(monkeypatch)
    freeze(monkeypatch, OPUS_ID)                   # HAIKU is untested

    assert POOL.for_specialist(CODER) is OPUS

    guards = rows("modelgrade_guard")
    assert len(guards) == 1, guards
    assert guards[0]["guard_action"] == "kept_unqualified"
    assert guards[0]["preferred_route"] == OPUS_ID
    assert guards[0]["candidate_route"] == ""
    assert guards[0]["fallback"] is True
    assert "fail-open" in guards[0]["reason"]


def test_unprotected_cell_with_no_evidence_is_untouched_and_unrecorded(
        monkeypatch):
    """Narrowness of "protected": with the store on, no evidence and no freeze,
    a normal specialist keeps its route and no guard row is written — enabling
    `OLYMPUS_MODELGRADE` on an unmeasured deployment is a no-op."""
    graded(monkeypatch)
    assert POOL.for_specialist(CODER) is OPUS
    assert POOL.for_specialist("plutus") is OPUS
    assert rows("modelgrade_guard") == []


# --- fail-safe -------------------------------------------------------------

def test_routesub_raising_leaves_the_pick_unchanged(monkeypatch):
    graded(monkeypatch, "on")
    cell = cell_of(CODER)
    seed(OPUS, cell)
    seed(HAIKU, cell)

    def boom(*a, **k):
        raise RuntimeError("substitution exploded")

    monkeypatch.setattr(routesub, "choose", boom)
    assert POOL.for_specialist(CODER) is OPUS      # no exception, no change


def test_modelgrade_raising_leaves_the_pick_unchanged(monkeypatch):
    graded(monkeypatch, "on")

    def boom(*a, **k):
        raise RuntimeError("evidence store exploded")

    monkeypatch.setattr(modelgrade, "status", boom)
    monkeypatch.setattr(modelgrade, "card", boom)
    assert POOL.for_specialist(CODER) is OPUS
    assert POOL.for_specialist(VERIFIER) is OPUS


def test_modelgrade_disabled_blocks_substitution(monkeypatch):
    """`routesub` on, evidence store OFF: no ladder, therefore no substitution
    (fail-safe, not fail-open — the cheaper model does NOT win by default)."""
    monkeypatch.setenv("OLYMPUS_ROUTESUB", "on")
    monkeypatch.delenv("OLYMPUS_MODELGRADE", raising=False)
    assert POOL.for_specialist(CODER) is OPUS
    assert rows("decision")[0]["blocked_by"] == "modelgrade"


def test_replay_forces_the_seam_off(monkeypatch):
    """A replayed run reproduces its RECORDED decision rather than re-deciding
    against a ledger that has since grown — the same rule both existing adaptive
    selectors follow."""
    graded(monkeypatch, "on")
    cell = cell_of(CODER)
    seed(OPUS, cell)
    seed(HAIKU, cell)
    monkeypatch.setenv("OLYMPUS_REPLAY", "run-1")
    assert POOL.for_specialist(CODER) is OPUS


def test_pinned_specialist_still_wins_over_everything(monkeypatch):
    """Precedence is unchanged: an explicit operator pin outranks the whole
    Wave-2 layer, in both directions."""
    graded(monkeypatch, "on")
    cell = cell_of(CODER)
    seed(OPUS, cell)
    seed(HAIKU, cell)
    monkeypatch.setenv("OLYMPUS_SPECIALIST_MODELS", '{"hephaestus": "opus"}')
    assert POOL.for_specialist(CODER) is OPUS


# --- the flag-pairing invariant --------------------------------------------

def _orchestrator_source() -> str:
    return Path(config.__file__).with_name("orchestrator.py").read_text(
        encoding="utf-8"
    )

@pytest.mark.parametrize("flag,key", [
    ("OLYMPUS_ROUTESUB", "routesub_mode"),
    ("OLYMPUS_MODELGRADE", "modelgrade_enabled"),
])
def test_wave2_routing_flags_are_recorded_in_run_meta(flag, key):
    """Both flags change WHICH MODEL a specialist runs on, so both must be in
    the `tr.meta` assembly — half the pairing."""
    meta = _orchestrator_source().split("def replay_run(", 1)[0]
    assert f'tr.meta["{key}"]' in meta
    assert f'os.environ.get("{flag}"' in meta


@pytest.mark.parametrize("flag,key", [
    ("OLYMPUS_ROUTESUB", "routesub_mode"),
    ("OLYMPUS_MODELGRADE", "modelgrade_enabled"),
])
def test_wave2_routing_flags_are_reproduced_by_replay_run(flag, key):
    """...and the other half: `replay_run` must SET the flag from the recorded
    meta and RESTORE the caller's value afterwards. Without both, replaying a
    substituted run dispatches to a different member and diverges."""
    replay = _orchestrator_source().split("def replay_run(", 1)[1]
    assert f'os.environ["{flag}"]' in replay
    assert f'_meta.get("{key}"' in replay
    assert f'os.environ.pop("{flag}", None)' in replay


def test_every_meta_recorded_flag_is_reproduced_on_replay():
    """The invariant itself, not just this PR's two knobs: every `OLYMPUS_*`
    environment name read into `tr.meta` must also appear in `replay_run`. This
    is the pairing that a future wiring PR is most likely to half-do."""
    src = _orchestrator_source()
    meta, replay = src.split("def replay_run(", 1)
    import re
    named = set(re.findall(r'os\.environ\.get\("(OLYMPUS_[A-Z_]+)"', meta))
    missing = sorted(f for f in named if f not in replay)
    assert not missing, (
        f"recorded in tr.meta but never reproduced by replay_run: {missing}")
