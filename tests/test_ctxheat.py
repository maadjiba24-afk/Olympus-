"""Wave-2 C2 — context & skill heat (`olympus.ctxheat`).

The capability's whole claim is that heat measures **usefulness, not
frequency**, that the ledger can never carry text into the prompt, and that an
uncalibrated pin policy cannot change placement without a benchmark gate. Each
of those is an invariant here, not a comment:

  W2-I2.1 content minimisation — the API cannot even accept content
  W2-I2.2 per-user isolation, hostile ids contained
  W2-I2.3 budget + hysteresis + decay + eviction protection
  W2-I2.4 no pin-set write without a passing before/after benchmark gate
  W2-I2.5 rollback: off/shadow leave placement static and unconsumed
  + poisoning resistance: self-reported usefulness can never promote
"""
from __future__ import annotations

import ast
import json
import math
import time
from pathlib import Path

import pytest

from olympus import config, ctxheat

DAY = 86400.0
T0 = 1_700_000_000.0


# --- helpers --------------------------------------------------------------

@pytest.fixture
def shadow(monkeypatch):
    """The shipping mode: heat is recorded, proposals computed, nothing applied."""
    monkeypatch.setenv("OLYMPUS_CTXHEAT", "shadow")
    return ctxheat


@pytest.fixture
def live(monkeypatch):
    """Mode `on` — application is *allowed*, still only through the gate."""
    monkeypatch.setenv("OLYMPUS_CTXHEAT", "on")
    return ctxheat


def _entry(**over):
    """A bare entry dict for the PURE scoring tests (no I/O involved)."""
    base = {"id": "x", "kind": "wiki", "hits": 0, "useful": 0, "verifier_ok": 0,
            "reuse": 0, "avoided_cost": 0.0, "avoided_latency": 0.0,
            "corrections": 0, "last": T0, "first": T0, "provenance": "recall"}
    base.update(over)
    return base


def _verified(item_id, kind="wiki", *, times=1, user=None, now=None):
    """Give an item `times` EXTERNAL verifier acceptances (the only promotion
    signal) plus one retrieval, the way the wiring PR eventually will."""
    ctxheat.record(item_id, kind, retrieved=True, user=user, now=now,
                   provenance="recall")
    for _ in range(times):
        ctxheat.record_verifier_outcome(item_id, True, source="aletheia",
                                        kind=kind, user=user, now=now)


def _read_raw(user=None):
    return json.loads(ctxheat.ledger_path(user).read_text(encoding="utf-8"))


# --- modes ----------------------------------------------------------------

def test_default_mode_is_off():
    assert ctxheat.mode() == ctxheat.OFF
    assert ctxheat.enabled() is False


@pytest.mark.parametrize("raw,expect", [
    ("", ctxheat.OFF), ("off", ctxheat.OFF), ("0", ctxheat.OFF),
    ("nonsense", ctxheat.OFF),          # unrecognised => fail closed
    ("shadow", ctxheat.SHADOW), ("observe", ctxheat.SHADOW),
    ("on", ctxheat.ON), ("1", ctxheat.ON), ("true", ctxheat.ON),
])
def test_mode_parsing(monkeypatch, raw, expect):
    monkeypatch.setenv("OLYMPUS_CTXHEAT", raw)
    assert ctxheat.mode() == expect


# --- the core requirement: usefulness != frequency ------------------------

def test_hot_by_frequency_scores_below_hot_by_verified_usefulness():
    """THE requirement: 100 retrievals that never helped lose to 5 retrievals
    the verifier accepted 5 times."""
    frequent = _entry(id="frequent", hits=100)
    useful = _entry(id="useful", hits=5, verifier_ok=5)
    assert ctxheat.score(frequent, now=T0) < ctxheat.score(useful, now=T0)
    # ... and not marginally: verified usefulness dominates by an order of
    # magnitude, so no plausible retrieval count catches up.
    assert ctxheat.score(useful, now=T0) > 10 * ctxheat.score(frequent, now=T0)


def test_frequency_saturates_so_no_retrieval_count_beats_one_acceptance():
    once_verified = _entry(verifier_ok=1)
    for hits in (10, 1_000, 100_000, 10_000_000):
        assert ctxheat.score(_entry(hits=hits), now=T0) < ctxheat.score(
            once_verified, now=T0)


def test_self_reported_usefulness_is_capped_below_one_acceptance():
    """A self-report is evidence of nothing; it may nudge, never promote."""
    claimed = _entry(useful=1_000, hits=1_000)
    assert ctxheat.score(claimed, now=T0) < ctxheat.score(
        _entry(verifier_ok=1), now=T0)


def test_reuse_savings_and_corrections_move_the_score_in_the_right_direction():
    base = ctxheat.score(_entry(verifier_ok=2), now=T0)
    assert ctxheat.score(_entry(verifier_ok=2, reuse=4), now=T0) > base
    assert ctxheat.score(_entry(verifier_ok=2, avoided_cost=1.0), now=T0) > base
    assert ctxheat.score(_entry(verifier_ok=2, avoided_latency=5000),
                         now=T0) > base
    assert ctxheat.score(_entry(verifier_ok=2, corrections=3), now=T0) < base


def test_score_is_pure_and_bounds_absurd_claims():
    e = _entry(verifier_ok=1, avoided_cost=10 ** 9, avoided_latency=10 ** 12)
    first = ctxheat.score(e, now=T0)
    assert first == ctxheat.score(e, now=T0)          # deterministic
    assert first <= (ctxheat.W_VERIFIER
                     + ctxheat.W_COST * ctxheat.MAX_AVOIDED_COST_USD
                     + ctxheat.W_LATENCY * ctxheat.MAX_AVOIDED_LATENCY_MS / 1000
                     + ctxheat.SELF_REPORT_CAP) + 1
    assert ctxheat.score({}, now=T0) == 0.0
    assert ctxheat.score(None, now=T0) == 0.0


# --- decay ----------------------------------------------------------------

def test_heat_halves_every_halflife():
    e = _entry(verifier_ok=4, last=T0)
    fresh = ctxheat.score(e, now=T0, halflife=30)
    one_life = ctxheat.score(e, now=T0 + 30 * DAY, halflife=30)
    two_lives = ctxheat.score(e, now=T0 + 60 * DAY, halflife=30)
    assert one_life == pytest.approx(fresh / 2, rel=1e-6)
    assert two_lives == pytest.approx(fresh / 4, rel=1e-6)


def test_decay_lets_a_stale_hot_item_fall_below_a_fresh_modest_one():
    stale = _entry(id="stale", verifier_ok=8, last=T0)
    fresh = _entry(id="fresh", verifier_ok=2, last=T0 + 365 * DAY)
    now = T0 + 365 * DAY
    assert ctxheat.score(stale, now=now, halflife=30) < ctxheat.score(
        fresh, now=now, halflife=30)


def test_halflife_knob_is_read_from_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT_HALFLIFE_DAYS", "10")
    assert ctxheat.halflife_days() == 10.0
    e = _entry(verifier_ok=1, last=T0)
    assert ctxheat.score(e, now=T0 + 10 * DAY) == pytest.approx(
        ctxheat.score(e, now=T0) / 2, rel=1e-6)


# --- W2-I2.1 content minimisation ----------------------------------------

def test_recording_api_cannot_accept_content_at_all():
    """The signature IS the contract: there is no content parameter to drop
    silently, so a caller cannot even attempt to persist user text."""
    for kwarg in ("content", "text", "snippet", "body", "excerpt", "summary"):
        with pytest.raises(TypeError):
            ctxheat.record("abc123", "wiki", **{kwarg: "SECRET-PLANTED-TEXT"})
        with pytest.raises(TypeError):
            ctxheat.record_verifier_outcome("abc123", True, source="aletheia",
                                            **{kwarg: "SECRET-PLANTED-TEXT"})


def test_stored_entry_has_exactly_the_declared_fields(shadow):
    _verified("page-a", "wiki", times=2)
    ctxheat.record("page-a", "wiki", retrieved=True, useful=True, reused=True,
                   avoided_cost_usd=0.5, avoided_latency_ms=250,
                   corrected=True, provenance="recall")
    raw = _read_raw()
    assert set(raw["entries"]) == {"wiki:page-a"}
    stored = raw["entries"]["wiki:page-a"]
    assert tuple(sorted(stored)) == tuple(sorted(ctxheat._ENTRY_FIELDS))
    # every string value comes from a closed vocabulary or is a reference id
    assert stored["kind"] in ctxheat.KINDS
    assert stored["provenance"] in ctxheat.PROVENANCES
    assert ctxheat._ID_CHARS.match(stored["id"])
    for name, value in stored.items():
        if name not in ("id", "kind", "provenance"):
            assert isinstance(value, (int, float)) and not isinstance(value, str)


def test_prose_shaped_ids_are_refused_so_text_cannot_ride_in_as_an_id(shadow):
    secret = "the user's card number is 4111 1111 1111 1111"
    for hostile in (secret, "id with spaces", "a" * 400, "", None,
                    "line\nbreak", "emoji \U0001f600"):
        assert ctxheat.record(hostile, "wiki", retrieved=True) is False
    assert ctxheat.ledger_path() is not None
    assert not ctxheat.ledger_path().exists()        # nothing was written
    assert ctxheat.stats()["rejected"] >= 5


def test_unknown_kind_is_refused_and_unknown_provenance_is_coerced(shadow):
    assert ctxheat.record("abc", "SECRET-KIND-WITH-TEXT", retrieved=True) is False
    assert ctxheat.record("abc", "wiki", retrieved=True,
                          provenance="SECRET-PROVENANCE-TEXT") is True
    assert ctxheat.entry("abc", "wiki")["provenance"] == "unknown"
    body = ctxheat.ledger_path().read_text(encoding="utf-8")
    assert "SECRET" not in body


def test_absurd_savings_claims_are_refused_not_clamped(shadow):
    assert ctxheat.record("abc", "wiki", avoided_cost_usd=10 ** 9) is False
    assert ctxheat.record("abc", "wiki", avoided_cost_usd=-5) is False
    assert ctxheat.record("abc", "wiki", avoided_latency_ms=float("inf")) is False
    assert ctxheat.record("abc", "wiki", avoided_latency_ms=-1) is False
    assert ctxheat.entries() == {}


def test_a_ledger_that_grows_a_content_field_is_quarantined_on_read(shadow):
    _verified("page-a", "wiki")
    path = ctxheat.ledger_path()
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["entries"]["wiki:page-a"]["content"] = "INJECTED PROMPT TEXT"
    path.write_text(json.dumps(blob), encoding="utf-8")

    assert ctxheat.entries() == {}                    # never half-parsed
    assert list(path.parent.glob("context_heat.corrupt.*.json"))
    assert not path.exists()
    assert ctxheat.stats()["quarantined"] >= 1


# --- W2-I2.2 isolation ----------------------------------------------------

def test_global_and_per_user_ledgers_are_separate_files(shadow):
    _verified("shared-page", "wiki", times=3)                 # global scope
    _verified("mine", "wiki", times=3, user="alice")          # per-user scope

    global_path = ctxheat.ledger_path()
    alice_path = ctxheat.ledger_path("alice")
    assert global_path == config.MEMORY_DIR / "context_heat.json"
    assert alice_path == config.MEMORY_DIR / "users" / "alice" / "context_heat.json"
    assert set(ctxheat.entries()) == {"wiki:shared-page"}
    assert set(ctxheat.entries("alice")) == {"wiki:mine"}


def test_heat_never_leaks_between_users_or_into_the_global_scope(shadow):
    _verified("doc", "wiki", times=9, user="alice")
    ctxheat.record("doc", "wiki", retrieved=True, user="bob")

    assert ctxheat.entry("doc", "wiki", user="alice")["verifier_ok"] == 9
    assert ctxheat.entry("doc", "wiki", user="bob")["verifier_ok"] == 0
    assert ctxheat.entry("doc", "wiki") is None                 # global untouched
    # and the hot user's item is not pinnable for anyone else
    assert ctxheat.pinned_ids(ctxheat.propose_pins(2000, user="alice"))
    assert ctxheat.propose_pins(2000, user="bob") == []
    assert ctxheat.propose_pins(2000) == []


@pytest.mark.parametrize("hostile", [
    "../../../etc/passwd", "..", "../..", "/etc/shadow", "a/../../b",
    "\\..\\..\\windows", "..%2f..%2fetc", "  ", "x" * 500, "user\x00null",
])
def test_hostile_user_ids_stay_inside_the_memory_dir(shadow, hostile):
    assert ctxheat.record("abc", "wiki", retrieved=True, user=hostile) is True
    path = ctxheat.ledger_path(hostile)
    root = Path(config.MEMORY_DIR).resolve()
    assert path is not None
    assert root in path.resolve().parents
    assert path.exists()
    for created in Path(config.MEMORY_DIR).rglob("context_heat.json"):
        assert root in created.resolve().parents


def test_scope_labels_are_stable_and_shared_maps_to_global():
    assert ctxheat.scope_of(None) == "global"
    assert ctxheat.scope_of("") == "global"
    assert ctxheat.scope_of("shared") == "global"
    assert ctxheat.scope_of("../../etc/passwd") == "user:etc-passwd"


# --- W2-I2.3 pin policy: budget, hysteresis, decay, protection -----------

def test_pin_budget_cap_is_respected(shadow):
    for i in range(10):
        _verified(f"page-{i}", "wiki", times=10 - i)
    est = {f"wiki:page-{i}": 100 for i in range(10)}
    proposals = ctxheat.propose_pins(250, est_tokens=est)
    pinned = [p for p in proposals if p.pinned]
    assert sum(p.est_tokens for p in pinned) <= 250
    assert len(pinned) == 2
    # and the budget picks the HOTTEST items, not just any two
    assert {p.item_id for p in pinned} == {"page-0", "page-1"}


def test_max_pins_cap_is_respected(shadow, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT_MAX_PINS", "3")
    for i in range(8):
        _verified(f"m-{i}", "memory", times=8 - i)
    proposals = ctxheat.propose_pins(100_000)
    assert len([p for p in proposals if p.pinned]) == 3


def test_zero_budget_pins_nothing(shadow):
    _verified("page", "wiki", times=50)
    assert ctxheat.pinned_ids(ctxheat.propose_pins(0)) == []


def test_hysteresis_keeps_an_incumbent_against_a_marginal_challenger(shadow):
    _verified("incumbent", "wiki", times=4)
    _verified("challenger", "wiki", times=4)
    # nudge the challenger marginally ahead on frequency alone
    for _ in range(50):
        ctxheat.record("challenger", "wiki", retrieved=True)
    est = {"wiki:incumbent": 100, "wiki:challenger": 100}

    raw = {k: ctxheat.score(v, now=time.time())
           for k, v in ctxheat.entries().items()}
    assert raw["wiki:challenger"] > raw["wiki:incumbent"]      # it IS ahead...

    proposals = ctxheat.propose_pins(100, est_tokens=est,
                                     incumbents=["wiki:incumbent"])
    assert ctxheat.pinned_ids(proposals) == ["wiki:incumbent"]  # ...but not by
    assert [p.action for p in proposals if p.pinned] == [ctxheat.KEEP]  # enough


def test_a_decisively_better_challenger_does_displace_the_incumbent(shadow):
    _verified("incumbent", "wiki", times=4)
    _verified("challenger", "wiki", times=20)
    est = {"wiki:incumbent": 100, "wiki:challenger": 100}
    proposals = ctxheat.propose_pins(100, est_tokens=est,
                                     incumbents=["wiki:incumbent"])
    assert ctxheat.pinned_ids(proposals) == ["wiki:challenger"]
    dropped = [p for p in proposals if p.action == ctxheat.DROP]
    assert [p.item_id for p in dropped] == ["incumbent"]


def test_no_ping_pong_when_scores_sit_on_a_near_tie(shadow):
    """Feed each proposal back as the incumbent set: placement must converge,
    not oscillate — pin churn is what makes learned pinning cost money."""
    _verified("a", "wiki", times=5)
    _verified("b", "wiki", times=5)
    ctxheat.record("b", "wiki", retrieved=True)        # b is a hair ahead
    est = {"wiki:a": 100, "wiki:b": 100}

    pins = ["wiki:a"]
    seen = []
    for _ in range(6):
        pins = ctxheat.pinned_ids(
            ctxheat.propose_pins(100, est_tokens=est, incumbents=pins))
        seen.append(tuple(pins))
    assert len(set(seen)) == 1 and seen[0] == ("wiki:a",)


def test_eviction_protection_shields_a_hot_item_from_a_never_useful_challenger(
        shadow, monkeypatch):
    # let a never-verified item be *eligible* so protection is what does the
    # work (otherwise the eligibility floor alone would hide the effect)
    monkeypatch.setenv("OLYMPUS_CTXHEAT_MIN_VERIFIED", "0")
    _verified("hot", "wiki", times=3)                 # >= protect_n()
    for _ in range(60):                               # loud but never verified
        ctxheat.record("loud", "wiki", retrieved=True, reused=True, useful=True)

    est = {"wiki:hot": 100, "wiki:loud": 100}
    scores = {k: ctxheat.score(v, now=time.time())
              for k, v in ctxheat.entries().items()}
    assert scores["wiki:loud"] > scores["wiki:hot"] * (1 + ctxheat.hysteresis())

    proposals = ctxheat.propose_pins(100, est_tokens=est,
                                     incumbents=["wiki:hot"])
    assert ctxheat.pinned_ids(proposals) == ["wiki:hot"]
    assert ctxheat.explain("hot", "wiki")["eviction_protected"] is True


def test_protection_does_not_shield_against_a_genuinely_useful_challenger(
        shadow):
    _verified("hot", "wiki", times=3)
    _verified("hotter", "wiki", times=30)
    est = {"wiki:hot": 100, "wiki:hotter": 100}
    proposals = ctxheat.propose_pins(100, est_tokens=est, incumbents=["wiki:hot"])
    assert ctxheat.pinned_ids(proposals) == ["wiki:hotter"]


def test_swap_cap_bounds_pin_set_churn(shadow, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT_MAX_SWAPS", "1")
    for name in ("old1", "old2", "old3"):
        _verified(name, "wiki", times=2)
    for name in ("new1", "new2", "new3"):
        _verified(name, "wiki", times=40)
    est = {f"wiki:{n}": 100 for n in
           ("old1", "old2", "old3", "new1", "new2", "new3")}
    proposals = ctxheat.propose_pins(300, est_tokens=est,
                                     incumbents=["wiki:old1", "wiki:old2",
                                                 "wiki:old3"])
    adds = [p for p in proposals if p.action == ctxheat.ADD]
    assert len(adds) == 1
    assert len([p for p in proposals if p.pinned]) == 3


def test_value_density_prefers_the_cheaper_of_two_similar_items(shadow):
    _verified("small", "memory", times=5)
    _verified("huge", "wiki", times=6)
    est = {"memory:small": 40, "wiki:huge": 4000}
    proposals = ctxheat.propose_pins(1000, est_tokens=est)
    assert ctxheat.pinned_ids(proposals) == ["memory:small"]


def test_stale_incumbent_ids_are_dropped(shadow):
    _verified("real", "wiki", times=5)
    proposals = ctxheat.propose_pins(1000, incumbents=["wiki:deleted-page"])
    drops = [p for p in proposals if p.action == ctxheat.DROP]
    assert [(p.item_id, p.reason) for p in drops] == [
        ("deleted-page", "stale_or_ineligible")]


def test_fully_decayed_items_earn_no_residency(shadow):
    _verified("ancient", "wiki", times=5, now=T0)
    assert ctxheat.propose_pins(1000, now=T0 + 3650 * DAY) == []


# --- W2-I2.4 the benchmark gate ------------------------------------------

def test_apply_refuses_without_a_gate(live):
    _verified("page", "wiki", times=5)
    proposals = ctxheat.propose_pins(1000)
    assert [p for p in proposals if p.pinned]

    result = ctxheat.apply_pins(proposals)
    assert result.applied is False
    assert result.reason == "no_benchmark_gate"
    assert ctxheat.pins_path().exists() is False
    assert ctxheat._read_pins() == []


def test_apply_refuses_when_the_gate_fails(live):
    _verified("page", "wiki", times=5)
    calls = []

    def gate(before, after):
        calls.append((before, after))
        return False                       # the benchmark regressed

    result = ctxheat.apply_pins(ctxheat.propose_pins(1000), gate_fn=gate)
    assert result.applied is False
    assert result.reason == "benchmark_gate_failed"
    assert result.gate_called is True
    assert len(calls) == 1
    before, after = calls[0]
    assert before["pins"] == [] and "wiki:page" in after["pins"]
    assert ctxheat.pins_path().exists() is False


def test_apply_refuses_when_the_gate_raises(live):
    _verified("page", "wiki", times=5)

    def gate(before, after):
        raise RuntimeError("benchmark harness exploded")

    result = ctxheat.apply_pins(ctxheat.propose_pins(1000), gate_fn=gate)
    assert result.applied is False
    assert result.reason == "benchmark_gate_error"
    assert ctxheat.pins_path().exists() is False


def test_apply_refuses_a_non_callable_gate(live):
    _verified("page", "wiki", times=5)
    result = ctxheat.apply_pins(ctxheat.propose_pins(1000), gate_fn="yes please")
    assert result.applied is False
    assert result.reason == "no_benchmark_gate"


def test_apply_writes_the_pin_set_only_after_a_passing_gate(live):
    _verified("page", "wiki", times=5)
    proposals = ctxheat.propose_pins(1000)
    result = ctxheat.apply_pins(proposals, gate_fn=lambda b, a: True,
                                reason="calibration sweep 1")
    assert result.applied is True
    assert result.pins == ["wiki:page"]
    stored = json.loads(ctxheat.pins_path().read_text(encoding="utf-8"))
    assert stored["gate"] == "passed"
    assert stored["provisional"] is True
    assert [p["id"] for p in stored["pins"]] == ["page"]
    # the applied set is content-minimised too
    assert set(stored["pins"][0]) == {"id", "kind", "pinned_at", "est_tokens",
                                      "score", "verifier_ok"}
    assert ctxheat.applied_pins() == ctxheat._read_pins()


def test_clear_pins_needs_no_gate_because_unpinning_is_the_safe_direction(live):
    _verified("page", "wiki", times=5)
    ctxheat.apply_pins(ctxheat.propose_pins(1000), gate_fn=lambda b, a: True)
    assert ctxheat.pins_path().exists()
    assert ctxheat.clear_pins() is True
    assert ctxheat.pins_path().exists() is False
    assert ctxheat.applied_pins() == []


# --- W2-I2.5 rollback: shadow and off change nothing ---------------------

def test_shadow_mode_computes_proposals_but_applies_nothing(shadow):
    _verified("page", "wiki", times=5)
    proposals = ctxheat.propose_pins(1000)
    assert ctxheat.pinned_ids(proposals) == ["wiki:page"]

    gate_calls = []
    result = ctxheat.apply_pins(proposals,
                                gate_fn=lambda b, a: gate_calls.append(1) or True)
    assert result.applied is False
    assert result.reason == "shadow"
    assert gate_calls == []                       # nothing to gate: no write
    assert ctxheat.pins_path().exists() is False
    assert ctxheat.applied_pins() == []


def test_shadow_mode_logs_the_counterfactual(shadow):
    _verified("page", "wiki", times=5)
    ctxheat.apply_pins(ctxheat.propose_pins(1000), reason="nightly")
    rows = ctxheat.shadow_log()
    assert len(rows) == 1
    row = rows[0]
    assert row["applied"] is False
    assert row["outcome"] == "shadow_counterfactual_only"
    assert row["pins"] == ["wiki:page"] and row["add"] == ["wiki:page"]
    assert row["mode"] == "shadow" and row["provisional"] is True
    assert row["scope"] == "global"
    # counterfactual rows are content-minimised as well
    assert "SECRET" not in json.dumps(row)


def test_shadow_pin_state_is_never_consumed_even_if_a_pin_file_exists(shadow):
    """W2-I2.5: rollback is one flag. A pin file left by an experiment must not
    change placement once the flag is back to shadow/off."""
    ctxheat.pins_path().parent.mkdir(parents=True, exist_ok=True)
    ctxheat.pins_path().write_text(json.dumps({
        "schema_version": ctxheat.SCHEMA_VERSION,
        "pins": [{"id": "page", "kind": "wiki"}]}), encoding="utf-8")
    assert ctxheat.applied_pins() == []            # shadow: not consumed


def test_off_mode_is_fully_inert(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT", "off")
    assert ctxheat.record("page", "wiki", retrieved=True) is False
    assert ctxheat.record_verifier_outcome("page", True, source="aletheia",
                                           kind="wiki") is False
    assert ctxheat.propose_pins(1000) == []
    assert ctxheat.entries() == {}
    assert ctxheat.entry("page", "wiki") is None
    assert ctxheat.applied_pins() == []
    result = ctxheat.apply_pins([], gate_fn=lambda b, a: True)
    assert result.applied is False and result.reason == "mode_off"
    assert not Path(config.MEMORY_DIR).exists()    # not one byte written


def test_only_the_gated_recall_seam_consumes_a_pin_set(shadow):
    """W2-I2.4/W2-I2.5 in their strongest form, updated by W2-PR15.

    Until that PR this test asserted that NOTHING consumed the placement API —
    the library shipped inert. W2-PR15 wires exactly one consumer, so the guard
    now pins the shape of that wiring instead of forbidding it; deleting the
    guard rather than tightening it would have retired the protection along
    with the condition.

    Three things must stay true:

    1. `recall.py` is the ONLY consumer — wiki/skills/orchestrator must still
       not read a pin set (the orchestrator is separately forbidden from
       naming ctxheat at all by `test_wave2_rollback`);
    2. that consumer supplies NO `gate_fn`, so `apply_pins()` refuses there and
       `on` degrades to `shadow` (W2-I2.4: no unmeasured pin-set write path);
    3. NOTHING calls `record_verifier_outcome()`. The promotion signal has no
       honest source yet — the retrieval seam runs before the answer exists —
       and a call from anywhere else would be the poisoning path the capability
       is built to refuse. Prose ABOUT it (recall.py documents why it is
       unwired) is fine; a call is not, so this is decided from the AST.
    """
    pkg = Path(__file__).resolve().parent.parent / "olympus"
    placement_api = ("propose_pins", "apply_pins", "applied_pins", "pins_path",
                     "clear_pins")
    consumers = {}
    mentions = []
    verifier_callers = []
    for path in sorted(pkg.glob("*.py")):
        if path.name == "ctxheat.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ctxheat" in text:
            mentions.append(path.name)
        hits = [name for name in placement_api if name in text]
        if hits:
            consumers[path.name] = hits
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", ""))
            if name == "record_verifier_outcome":
                verifier_callers.append(path.name)

    assert set(consumers) <= {"recall.py"}, \
        f"pin state consumed outside the gated recall seam: {consumers}"
    assert not verifier_callers, (
        "record_verifier_outcome has no trusted source at any wired seam; "
        f"called from {verifier_callers}")
    # `retention.py` names ctxheat ONLY as a path glob, because deleting a
    # principal must remove its heat ledger too or P5-A12 ("deletion removes
    # all derived data") would be false. Allowing the mention without checking
    # what kind of mention it is would weaken the guard, so the allowance is
    # paired with an AST check that it never IMPORTS the module — a path
    # string cannot consume a pin set; an import could.
    assert set(mentions) <= {"config.py", "doctor.py", "experiments.py",
                             "recall.py", "retention.py"}, \
        f"ctxheat wired in early: {mentions}"
    ret_src = (pkg / "retention.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(ret_src)):
        if isinstance(node, ast.ImportFrom) and node.module == "ctxheat":
            raise AssertionError("retention.py imports ctxheat")
        if isinstance(node, ast.ImportFrom) and node.module in (None, "."):
            assert not any(a.name == "ctxheat" for a in node.names), \
                "retention.py imports ctxheat — it may only name its PATH"
        if isinstance(node, ast.Import):
            assert not any("ctxheat" in a.name for a in node.names), \
                "retention.py imports ctxheat — it may only name its PATH"

    # The wiring must offer NO benchmark gate, or `on` would stop degrading to
    # shadow and heat could reach the prompt unmeasured.
    recall_src = (pkg / "recall.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(recall_src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "apply_pins":
            gates = [kw.value for kw in node.keywords if kw.arg == "gate_fn"]
            assert gates and all(isinstance(g, ast.Constant) and g.value is None
                                 for g in gates), \
                "recall must pass gate_fn=None — it has no benchmark to run"


def test_the_doctor_liveness_reader_only_reads(shadow):
    """The one existing consumer (C9 optimization liveness) is a PURE READ of
    the ledger — it must not create state or change placement."""
    from olympus import doctor

    _verified("page", "wiki", times=2)
    ctxheat.record("page", "wiki", retrieved=True, reused=True,
                   avoided_cost_usd=0.5)
    row = doctor._liveness_context_heat()
    assert row["configured"] is True
    assert row["hits"] == 1 and row["misses"] >= 1
    assert "shadow mode" in row["inactivity_reason"] or row["activated"]
    assert ctxheat.applied_pins() == []
    assert ctxheat.pins_path().exists() is False


# --- poisoning resistance -------------------------------------------------

def test_the_recording_path_cannot_self_grant_verifier_acceptance(shadow):
    for _ in range(500):
        ctxheat.record("evil", "wiki", retrieved=True, useful=True,
                       verifier_accepted=True, reused=True)
    stored = ctxheat.entry("evil", "wiki")
    assert stored["verifier_ok"] == 0
    assert stored["useful"] == 500
    assert ctxheat.stats()["verifier_claims_refused"] >= 500
    # ... and it therefore cannot reach the pin set
    assert ctxheat.propose_pins(100_000) == []


def test_self_reported_usefulness_never_outranks_a_verified_item(shadow):
    for _ in range(1_000):
        ctxheat.record("evil", "wiki", retrieved=True, useful=True,
                       verifier_accepted=True)
    _verified("honest", "wiki", times=1)
    scores = {k: ctxheat.score(v, now=time.time())
              for k, v in ctxheat.entries().items()}
    assert scores["wiki:evil"] < scores["wiki:honest"]

    est = {"wiki:evil": 100, "wiki:honest": 100}
    assert ctxheat.pinned_ids(
        ctxheat.propose_pins(100, est_tokens=est)) == ["wiki:honest"]


def test_an_untrusted_source_cannot_move_the_verifier_counter(shadow):
    ctxheat.record("evil", "wiki", retrieved=True)
    for source in ("", None, "self", "the-item-itself", "aletheia-lookalike",
                   "user", "recall"):
        assert ctxheat.record_verifier_outcome(
            "evil", True, source=source, kind="wiki") is False
    assert ctxheat.entry("evil", "wiki")["verifier_ok"] == 0
    # the real verifier, by its trusted label, does move it
    assert ctxheat.record_verifier_outcome("evil", True, source="Aletheia",
                                           kind="wiki") is True
    assert ctxheat.entry("evil", "wiki")["verifier_ok"] == 1


def test_a_rejected_verifier_outcome_is_recorded_as_a_correction(shadow):
    _verified("page", "wiki", times=3)
    hot = ctxheat.score(ctxheat.entry("page", "wiki"), now=time.time())
    ctxheat.record_verifier_outcome("page", False, source="aletheia",
                                    kind="wiki")
    stored = ctxheat.entry("page", "wiki")
    assert stored["corrections"] == 1 and stored["verifier_ok"] == 3
    assert ctxheat.score(stored, now=time.time()) < hot


def test_verifier_outcome_without_a_kind_refuses_an_ambiguous_id(shadow):
    _verified("dup", "wiki")
    _verified("dup", "skill")
    assert ctxheat.record_verifier_outcome("dup", True,
                                           source="aletheia") is False
    assert ctxheat.record_verifier_outcome("unknown-id", True,
                                           source="aletheia") is False
    _verified("solo", "memory")
    assert ctxheat.record_verifier_outcome("solo", True,
                                           source="aletheia") is True
    assert ctxheat.entry("solo", "memory")["verifier_ok"] == 2


def test_trusted_sources_cannot_be_widened_from_the_environment(
        shadow, monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT_TRUSTED_SOURCES", "attacker")
    monkeypatch.setenv("OLYMPUS_TRUSTED_VERIFIER_SOURCES", "attacker")
    ctxheat.record("evil", "wiki", retrieved=True)
    assert ctxheat.record_verifier_outcome("evil", True, source="attacker",
                                           kind="wiki") is False


# --- resilience -----------------------------------------------------------

@pytest.mark.parametrize("blob", [
    "not json at all", "", "[]", "{}", '{"schema_version": 99, "entries": {}}',
    '{"schema_version": 1, "entries": []}',
    '{"schema_version": 1, "entries": {"wiki:a": {"id": "a"}}}',
    '{"schema_version": 1, "entries": {"wiki:a": "text"}}',
])
def test_a_corrupt_ledger_is_quarantined_and_the_store_starts_fresh(
        shadow, blob):
    path = ctxheat.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(blob, encoding="utf-8")

    assert ctxheat.entries() == {}                       # no raise
    assert list(path.parent.glob("context_heat.corrupt.*.json"))
    assert ctxheat.record("fresh", "wiki", retrieved=True) is True
    assert set(ctxheat.entries()) == {"wiki:fresh"}


def test_a_mismatched_entry_key_is_quarantined(shadow):
    _verified("page", "wiki")
    path = ctxheat.ledger_path()
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["entries"]["wiki:someone-elses-id"] = blob["entries"].pop("wiki:page")
    path.write_text(json.dumps(blob), encoding="utf-8")
    assert ctxheat.entries() == {}


def test_a_corrupt_pin_file_means_static_placement_not_an_error(live):
    ctxheat.pins_path().parent.mkdir(parents=True, exist_ok=True)
    ctxheat.pins_path().write_text("{{{ not json", encoding="utf-8")
    assert ctxheat.applied_pins() == []
    assert ctxheat.propose_pins(1000) == []


def test_missing_ledger_means_static_placement(shadow):
    assert ctxheat.entries() == {}
    assert ctxheat.propose_pins(1000) == []
    assert ctxheat.applied_pins() == []


# --- surface / operator ---------------------------------------------------

def test_provisional_constants_are_declared_with_owner_and_knobs():
    assert ctxheat.provisional() is True
    assert ctxheat.CALIBRATION_OWNER
    for name in ("hysteresis", "halflife_days", "protect_n", "min_verified",
                 "max_swaps", "max_pins", "pin_budget_tokens"):
        entry = ctxheat.PROVISIONAL_CONSTANTS[name]
        assert entry["env"].startswith("OLYMPUS_")
        assert entry["why"]
    doc = ctxheat.__doc__ or ""
    assert "PROVISIONAL" in doc and "shadow" in doc


def test_policy_knobs_are_env_overridable(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT_HYSTERESIS", "0.9")
    monkeypatch.setenv("OLYMPUS_CTXHEAT_PROTECT_N", "7")
    monkeypatch.setenv("OLYMPUS_CTXHEAT_MAX_SWAPS", "5")
    monkeypatch.setenv("OLYMPUS_CTXHEAT_MAX_PINS", "9")
    monkeypatch.setenv("OLYMPUS_PIN_BUDGET_TOKENS", "999")
    monkeypatch.setenv("OLYMPUS_CTXHEAT_MIN_VERIFIED", "4")
    assert ctxheat.hysteresis() == 0.9
    assert ctxheat.protect_n() == 7
    assert ctxheat.max_swaps() == 5
    assert ctxheat.max_pins() == 9
    assert ctxheat.pin_budget_tokens() == 999
    assert ctxheat.min_verified() == 4


def test_garbage_knob_values_fall_back_to_defaults(monkeypatch):
    for name in ("OLYMPUS_CTXHEAT_HYSTERESIS", "OLYMPUS_CTXHEAT_HALFLIFE_DAYS",
                 "OLYMPUS_CTXHEAT_PROTECT_N", "OLYMPUS_CTXHEAT_MAX_SWAPS"):
        monkeypatch.setenv(name, "not-a-number")
    assert ctxheat.hysteresis() == ctxheat.DEFAULT_HYSTERESIS
    assert ctxheat.halflife_days() == ctxheat.DEFAULT_HALFLIFE_DAYS
    assert ctxheat.protect_n() == ctxheat.DEFAULT_PROTECT_N
    assert ctxheat.max_swaps() == ctxheat.DEFAULT_MAX_SWAPS


def test_explain_answers_why_an_item_is_hot(shadow):
    _verified("page", "wiki", times=4)
    out = ctxheat.explain("page", "wiki")
    assert out["found"] is True and out["scope"] == "global"
    assert out["components"]["verifier"] == pytest.approx(4 * ctxheat.W_VERIFIER)
    assert out["pin_eligible"] is True and out["eviction_protected"] is True
    assert out["score"] > 0
    missing = ctxheat.explain("nope", "wiki")
    assert missing["found"] is False and missing["score"] == 0.0


def test_proposals_are_content_minimised(shadow):
    _verified("page", "wiki", times=3)
    [proposal] = ctxheat.propose_pins(1000)
    assert set(proposal.to_dict()) == {"id", "kind", "action", "score",
                                       "est_tokens", "verifier_ok", "reason"}
    assert proposal.reason in ("highest_value_density", "incumbent_retained")
    assert proposal.pinned is True and proposal.key == "wiki:page"


def test_est_tokens_falls_back_per_kind_when_the_caller_supplies_nothing():
    assert ctxheat.est_tokens_for({"kind": "memory", "id": "a"}) == \
        ctxheat.DEFAULT_EST_TOKENS["memory"]
    assert ctxheat.est_tokens_for({"kind": "wiki", "id": "a"},
                                  {"wiki:a": 12}) == 12
    assert ctxheat.est_tokens_for({"kind": "wiki", "id": "a"}, {"a": 7}) == 7
    assert ctxheat.est_tokens_for({"kind": "wiki", "id": "a"},
                                  {"wiki:a": 0}) > 0        # never a zero divisor


def test_ledger_is_bounded_and_sheds_the_coldest_entries(shadow, monkeypatch):
    monkeypatch.setattr(ctxheat, "_MAX_ENTRIES", 5)
    for i in range(12):
        _verified(f"p-{i:02d}", "wiki", times=i + 1)
    stored = ctxheat.entries()
    assert len(stored) == 5
    assert "wiki:p-11" in stored and "wiki:p-00" not in stored


def test_repeated_records_accumulate_counters_not_content(shadow):
    for _ in range(3):
        ctxheat.record("page", "wiki", retrieved=True, reused=True,
                       avoided_cost_usd=0.25, avoided_latency_ms=100,
                       now=T0)
    stored = ctxheat.entry("page", "wiki")
    assert stored["hits"] == 3 and stored["reuse"] == 3
    assert stored["avoided_cost"] == pytest.approx(0.75)
    assert stored["avoided_latency"] == pytest.approx(300)
    assert stored["first"] == T0 and stored["last"] == T0
    assert math.isfinite(ctxheat.score(stored, now=T0))
