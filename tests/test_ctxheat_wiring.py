"""W2-PR15 — ctxheat wired into `recall` behind its benchmark gate.

Closes acceptance gate A3 (live). `ctxheat` shipped complete and inert: its
module banner said so outright — "it is deliberately NOT wired into
`recall`/`wiki`/`skills`/`orchestrator` — nothing imports it". A3 speaks about
the live system, so the capability could not close its gate while nothing
called it.

This suite pins the wiring to the invariants rather than to the plumbing:

  W2-I2.1  content minimisation AT THE WIRE — the retrieval seam passes ids and
           kinds and nothing else. `record()` has no content parameter, so this
           is structurally guaranteed; the test asserts the call site keeps it
           honest anyway (a `content=`/`text=` kwarg would be a TypeError, and
           no memory TEXT appears in any argument).
  W2-I2.4  no pin-set change without a passing benchmark gate. Retrieval has no
           benchmark to run, so it supplies NO `gate_fn` — which means
           `apply_pins()` REFUSES, and `on` behaves exactly as `shadow`.
  W2-I2.5  rollback is one flag: `off` (the default) is fully inert.

Deliberately NOT tested, because it is deliberately NOT wired:
`record_verifier_outcome()`. It requires a source from
TRUSTED_VERIFIER_SOURCES, and the retrieval seam runs before the answer exists
— it cannot see whether Aletheia accepted a memory. Faking a source would forge
the one signal the capability is built to demand. The consequence is asserted
instead (`test_recall_only_heat_is_never_pin_eligible`): with no verifier
signal, recall-only heat can never be promoted into the prompt.
"""
from __future__ import annotations

import json

import pytest

from olympus import config, ctxheat, recall, usermem


# --- helpers ---------------------------------------------------------------

def _off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CTXHEAT", raising=False)


def _shadow(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT", "shadow")


def _on(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTXHEAT", "on")


def _seed(user="u", n=6):
    """A few genuinely retrievable memories about one topic.

    Importance is stepped so every item scores DIFFERENTLY: `retrieve()` ranks
    by score and equal scores leave the tie to store order, which
    `usermem.touch` perturbs. A strict ranking is what lets these tests assert
    on order at all — and order is precisely what heat is allowed to change."""
    return [usermem.add_memory(
        user, type="project",
        content=f"bakery app milestone number {i} with delivery detail",
        confidence=0.9, importance=round(0.4 + i * 0.05, 3))
        for i in range(n)]


QUERY = "bakery app milestone delivery"


def _ids(mems):
    return [m["id"] for m in mems]


def _spy(monkeypatch, name):
    """Wrap a ctxheat function, recording (args, kwargs) and delegating."""
    calls = []
    original = getattr(ctxheat, name)

    def wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(ctxheat, name, wrapper)
    return calls


def _heat_files():
    """Every file ctxheat could have written, anywhere under MEMORY_DIR."""
    root = config.MEMORY_DIR
    if not root.exists():
        return set()
    return {p.name for p in root.rglob("*")
            if p.is_file() and ("heat" in p.name or "pins" in p.name)}


# --- 1. flag off ⇒ untouched behaviour (W2-I2.5) --------------------------

def test_flag_off_recall_is_untouched_even_if_ctxheat_explodes(monkeypatch):
    """The rollback proof: with the flag off, ctxheat is never consulted, so a
    ctxheat that raises on every call cannot affect retrieval."""
    _off(monkeypatch)
    seeded = _seed()

    def boom(*args, **kwargs):
        raise RuntimeError("ctxheat must not be called while off")

    for name in ("record", "propose_pins", "apply_pins", "applied_pins",
                 "record_verifier_outcome"):
        monkeypatch.setattr(ctxheat, name, boom)

    hits = recall.retrieve("u", QUERY)
    assert _ids(hits) == _ids(recall.retrieve("u", QUERY))
    assert set(_ids(hits)) <= set(_ids(seeded))
    assert hits
    assert recall.context_block("u", QUERY).startswith("\n\n## Relevant")


def test_flag_off_is_byte_identical_and_writes_no_heat_state(monkeypatch):
    """Identical items, identical order, and no residue in MEMORY_DIR."""
    _off(monkeypatch)
    _seed()
    baseline = _ids(recall.retrieve("u", QUERY))
    block = recall.context_block("u", QUERY)
    assert baseline
    assert _heat_files() == set()

    _shadow(monkeypatch)
    assert _ids(recall.retrieve("u", QUERY)) == baseline
    assert recall.context_block("u", QUERY) == block


# --- 2. shadow records but returns identical items ------------------------

def test_shadow_records_retrievals(monkeypatch):
    """Every returned item is recorded as a RETRIEVAL, in the user's scope."""
    _shadow(monkeypatch)
    _seed()
    calls = _spy(monkeypatch, "record")
    hits = recall.retrieve("u", QUERY)
    assert hits

    recorded = {}
    for args, kwargs in calls:
        item_id, kind = args
        assert kind == "memory"
        assert kwargs["retrieved"] is True
        assert kwargs["user"] == "u"
        assert kwargs["provenance"] == "recall"
        recorded[item_id] = kwargs
    assert set(recorded) == set(_ids(hits))

    entries = ctxheat.entries("u")
    assert set(entries) == {ctxheat.key("memory", i) for i in _ids(hits)}
    for entry in entries.values():
        assert entry["hits"] == 1
        assert entry["verifier_ok"] == 0        # no verifier signal at this seam
        assert entry["provenance"] == "recall"


def test_shadow_returns_identical_items_and_order(monkeypatch):
    """W2-I2.5: shadow records and proposes but changes NOTHING about what
    recall returns — same items, same order, same context block."""
    _off(monkeypatch)
    _seed(n=10)
    baseline = _ids(recall.retrieve("u", QUERY))
    baseline_block = recall.context_block("u", QUERY)

    _shadow(monkeypatch)
    for _ in range(3):                      # heat accumulates across turns…
        assert _ids(recall.retrieve("u", QUERY)) == baseline
    assert recall.context_block("u", QUERY) == baseline_block
    assert any(e["hits"] >= 3 for e in ctxheat.entries("u").values())


def test_shadow_computes_a_proposal_and_logs_the_counterfactual(monkeypatch):
    """`propose_pins` really runs in shadow (this is the mode that ships), and
    `apply_pins` records the counterfactual rather than applying it."""
    _shadow(monkeypatch)
    _seed()
    proposed = _spy(monkeypatch, "propose_pins")
    applied = _spy(monkeypatch, "apply_pins")
    recall.retrieve("u", QUERY)

    assert proposed, "shadow must let propose_pins compute"
    assert applied and applied[0][1]["gate_fn"] is None
    results = ctxheat.shadow_log("u")
    assert results
    assert results[-1]["outcome"] == "shadow_counterfactual_only"
    assert results[-1]["applied"] is False
    assert ctxheat.pins_path("u") is not None
    assert not ctxheat.pins_path("u").exists()


def test_shadow_passes_measured_item_sizes_not_the_provisional_fallback(
        monkeypatch):
    """`est_tokens` is the same SIZE recall charges against its own budget, so
    pin economics use measured sizes rather than ctxheat's PROVISIONAL per-kind
    guesses. A size is a number, not content."""
    _shadow(monkeypatch)
    _seed(n=3)
    calls = _spy(monkeypatch, "propose_pins")
    hits = recall.retrieve("u", QUERY)
    est = calls[0][1]["est_tokens"]
    assert set(est) == set(_ids(hits))
    for mem in hits:
        assert est[mem["id"]] == len(mem["content"]) // 4 + 4
        assert isinstance(est[mem["id"]], int)


# --- 3. on-mode without a gate behaves as shadow (W2-I2.4 refusal) --------

def test_on_mode_without_a_gate_refuses_and_matches_shadow(monkeypatch):
    """The seam supplies no `gate_fn`, so `apply_pins` REFUSES. `on` therefore
    returns exactly what `shadow` (and `off`) return — heat cannot reach
    placement without a benchmark."""
    _off(monkeypatch)
    _seed(n=10)
    baseline = _ids(recall.retrieve("u", QUERY))

    _shadow(monkeypatch)
    shadow_ids = _ids(recall.retrieve("u", QUERY))

    _on(monkeypatch)
    results = []
    original = ctxheat.apply_pins

    def spy(proposals, **kwargs):
        out = original(proposals, **kwargs)
        results.append(out)
        return out

    monkeypatch.setattr(ctxheat, "apply_pins", spy)
    on_ids = _ids(recall.retrieve("u", QUERY))

    assert on_ids == shadow_ids == baseline
    assert results, "on-mode must still offer the proposal to the gate"
    assert results[-1].applied is False
    assert results[-1].reason == "no_benchmark_gate"
    assert results[-1].gate_called is False
    assert not ctxheat.pins_path("u").exists()


def test_on_mode_never_writes_a_pin_set_from_this_seam(monkeypatch):
    """No unmeasured pin-set write path: many turns of on-mode retrieval leave
    `pins.json` absent, so `applied_pins()` stays empty and placement static."""
    _on(monkeypatch)
    _seed(n=8)
    for _ in range(5):
        recall.retrieve("u", QUERY)
    assert ctxheat.applied_pins("u") == []
    assert not ctxheat.pins_path("u").exists()


def test_recall_only_heat_is_never_pin_eligible(monkeypatch):
    """The honest consequence of NOT faking a verifier source: retrieval alone
    moves `hits`, and `min_verified()` demands verifier acceptances, so nothing
    heated purely by recall can ever be proposed for the prompt."""
    _on(monkeypatch)
    _seed(n=6)
    for _ in range(20):
        recall.retrieve("u", QUERY)
    entries = ctxheat.entries("u")
    assert entries and all(e["hits"] >= 20 for e in entries.values())
    assert all(e["verifier_ok"] == 0 for e in entries.values())
    assert ctxheat.propose_pins(user="u") == []


def test_a_gated_pin_set_is_the_only_route_to_reordering(monkeypatch):
    """The gate is the whole mechanism, not decoration: when a pin set HAS been
    published behind a passing gate, on-mode retrieval honours it — same items,
    pinned ones first — and clearing it restores static placement."""
    _on(monkeypatch)
    seeded = _seed(n=8)
    baseline = _ids(recall.retrieve("u", QUERY))
    target = baseline[-1]                   # the least relevant returned item
    assert target != baseline[0]

    # A pin set published the ONLY legal way: through apply_pins' gate.
    entry = next(m for m in seeded if m["id"] == target)
    proposal = ctxheat.PinProposal(item_id=target, kind="memory",
                                   action=ctxheat.ADD, score=9.0,
                                   est_tokens=len(entry["content"]) // 4 + 4,
                                   verifier_ok=3, reason="test")
    result = ctxheat.apply_pins([proposal], gate_fn=lambda before, after: True,
                                user="u", reason="benchmark passed")
    assert result.applied is True

    reordered = _ids(recall.retrieve("u", QUERY))
    assert reordered[0] == target
    assert sorted(reordered) == sorted(baseline)   # same set, only order moved

    ctxheat.clear_pins("u")                 # one-step rollback
    assert _ids(recall.retrieve("u", QUERY)) == baseline


def test_a_failing_gate_leaves_placement_static(monkeypatch):
    """A gate that fails (or raises) publishes nothing, so retrieval is
    unchanged — the refusal path is not merely logged, it is load-bearing."""
    _on(monkeypatch)
    _seed(n=8)
    baseline = _ids(recall.retrieve("u", QUERY))
    proposal = ctxheat.PinProposal(item_id=baseline[-1], kind="memory",
                                   action=ctxheat.ADD, score=9.0,
                                   est_tokens=10, verifier_ok=3, reason="t")
    assert ctxheat.apply_pins([proposal], gate_fn=lambda b, a: False,
                              user="u").applied is False

    def raiser(before, after):
        raise RuntimeError("benchmark harness down")

    assert ctxheat.apply_pins([proposal], gate_fn=raiser,
                              user="u").applied is False
    assert _ids(recall.retrieve("u", QUERY)) == baseline


# --- 4. content minimisation at the wire (W2-I2.1) ------------------------

def test_no_memory_content_ever_crosses_the_wire(monkeypatch):
    """Nothing recognisable as item TEXT may appear in any argument passed to
    ctxheat — not positionally, not in a kwarg, not inside a nested value."""
    _shadow(monkeypatch)
    secret = "the passphrase is hunter2 and the bakery opens at dawn"
    usermem.add_memory("u", type="project", content=secret, confidence=0.95,
                       importance=0.9)
    _seed(n=3)

    seen: list[str] = []
    for name in ("record", "propose_pins", "apply_pins", "applied_pins"):
        original = getattr(ctxheat, name)

        def wrapper(*args, _orig=original, **kwargs):
            seen.append(json.dumps([args, kwargs], default=repr))
            return _orig(*args, **kwargs)

        monkeypatch.setattr(ctxheat, name, wrapper)

    hits = recall.retrieve("u", "bakery passphrase dawn milestone")
    assert any(m["content"] == secret for m in hits)
    blob = " ".join(seen)
    assert "hunter2" not in blob
    assert "passphrase" not in blob
    assert "dawn" not in blob
    for mem in hits:
        assert mem["content"] not in blob
        assert mem["id"] in blob            # the reference DID cross


def test_the_ledger_on_disk_holds_no_content(monkeypatch):
    """End to end: the file the wiring produces contains ids, counters and
    closed-vocabulary labels — never a word of the memory."""
    _shadow(monkeypatch)
    secret = "my accountant is Priya Raman at Ledgerworks"
    usermem.add_memory("u", type="relationship", content=secret,
                       confidence=0.95, importance=0.9)
    assert recall.retrieve("u", "accountant Priya Ledgerworks")

    path = ctxheat.ledger_path("u")
    raw = path.read_text(encoding="utf-8")
    assert "Priya" not in raw and "Ledgerworks" not in raw
    for entry in json.loads(raw)["entries"].values():
        assert set(entry) == set(ctxheat._ENTRY_FIELDS)
        assert entry["kind"] == "memory"


def test_the_seam_cannot_pass_content_at_all(monkeypatch):
    """W2-I2.1 is structural, not conventional: `record()` has no content
    parameter, so a future edit that tried to pass one would fail loudly."""
    _shadow(monkeypatch)
    with pytest.raises(TypeError):
        ctxheat.record("abc123", "memory", content="text")
    with pytest.raises(TypeError):
        ctxheat.record("abc123", "memory", text="text")


def test_heat_is_isolated_per_user(monkeypatch):
    """W2-I2.2: one user's retrieval never heats another's ledger."""
    _shadow(monkeypatch)
    _seed("alice", n=3)
    _seed("bob", n=3)
    recall.retrieve("alice", QUERY)
    assert ctxheat.entries("alice")
    assert ctxheat.entries("bob") == {}


# --- 5. fail-safe on ctxheat errors ---------------------------------------

@pytest.mark.parametrize("broken", ["record", "propose_pins", "apply_pins",
                                    "applied_pins"])
def test_a_ctxheat_error_never_breaks_retrieval(monkeypatch, broken):
    """Heat is telemetry, never a retrieval dependency: whichever call blows
    up, the user still gets exactly the memories they would have got."""
    _off(monkeypatch)
    _seed(n=6)
    baseline = _ids(recall.retrieve("u", QUERY))
    baseline_block = recall.context_block("u", QUERY)

    _on(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError(f"{broken} exploded")

    monkeypatch.setattr(ctxheat, broken, boom)
    assert _ids(recall.retrieve("u", QUERY)) == baseline
    assert recall.context_block("u", QUERY) == baseline_block


def test_a_broken_ctxheat_import_is_survivable(monkeypatch):
    """Even `enabled()` failing (a corrupt module state) degrades to unheated
    retrieval rather than an exception in the user's face."""
    _shadow(monkeypatch)
    _seed(n=4)
    monkeypatch.setattr(ctxheat, "enabled",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert recall._ctxheat() is None
    assert recall.retrieve("u", QUERY)


def test_a_corrupt_ledger_quarantines_and_retrieval_continues(monkeypatch):
    """A tampered ledger is quarantined (reject-never-repair) and retrieval is
    unaffected — the failure mode is static placement, never a broken turn."""
    _shadow(monkeypatch)
    _seed(n=4)
    baseline = _ids(recall.retrieve("u", QUERY))
    path = ctxheat.ledger_path("u")
    path.write_text('{"schema_version": 1, "entries": {"memory:x": '
                    '{"id": "x", "smuggled": "raw user content"}}}',
                    encoding="utf-8")
    assert _ids(recall.retrieve("u", QUERY)) == baseline
    assert any(p.name.startswith("context_heat.corrupt.")
               for p in path.parent.iterdir())
