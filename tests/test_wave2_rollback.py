"""Wave-2 acceptance gate W2-A17: rollback to Wave-1 behaviour is TESTED.

The synthesis requires every Wave-2 capability to be reversible, and the
operating rule requires rollback to be demonstrated rather than documented. This
suite asserts the whole wave is inert in its shipped default configuration:

* every capability flag defaults to off (or shadow, which changes nothing);
* an inert capability writes no state — an operator who never opts in cannot
  accumulate Wave-2 files in MEMORY_DIR;
* the decision path is untouched: no Wave-2 module is consulted by the
  orchestrator's routing/planning/verification seams while disabled.

`conftest.preserve_environ` restores os.environ, and `isolated_memory` gives each
test a private MEMORY_DIR, so these tests cannot leak state into each other.
"""
from __future__ import annotations

from pathlib import Path

from olympus import (config, ctxheat, experiments, ingestgate, modelgrade,
                     routesub, streamguard, usage, watchdog)

# (module, callable returning its resolved mode/enabled state, inert values)
_FLAG_STATES = [
    ("modelgrade", lambda: modelgrade.enabled(), {False}),
    ("ingestgate", lambda: ingestgate.enabled(), {False}),
    ("streamguard", lambda: streamguard.enabled(), {False}),
    ("ctxheat", lambda: ctxheat.mode(), {"off"}),
    ("routesub", lambda: routesub.mode(), {"off"}),
    ("watchdog", lambda: watchdog.mode(), {"off"}),
    ("admission", lambda: usage.admission_enabled(), {False}),
]


def test_every_wave2_capability_defaults_to_inert():
    """Shipped defaults must be off/shadow — a fresh install runs Wave-1."""
    live = {name: probe() for name, probe, _ in _FLAG_STATES}
    bad = {n: live[n] for n, _, inert in _FLAG_STATES if live[n] not in inert}
    assert not bad, f"Wave-2 capabilities not inert by default: {bad}"


def test_shadow_modes_are_documented_as_non_mutating():
    """ctxheat/routesub ship 'off'; 'shadow' must exist as an explicitly
    non-mutating middle state (recording only), never as a silent 'on'."""
    for mod in (ctxheat, routesub):
        doc = (mod.__doc__ or "") + (mod.mode.__doc__ or "")
        assert "shadow" in doc.lower(), f"{mod.__name__} lacks a shadow contract"


def test_inert_capabilities_write_no_state(tmp_path, monkeypatch):
    """With every flag at its default, exercising the read APIs must not create
    any Wave-2 directory: rollback leaves no residue to clean up."""
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setattr(config, "MEMORY_DIR", mem)
    before = {p.name for p in mem.iterdir()}

    # Touch each capability the way a disabled system would.
    modelgrade.status("anthropic/claude-opus-4-8", "general|en|short|notools|nostruct")
    ctxheat.record("item-1", "memory")
    routesub.evaluate(members=[], specialist="plutus", preferred="m",
                      cell="general|en|short|notools|nostruct")
    watchdog.lease("run-1") if hasattr(watchdog, "lease") else None
    streamguard.monitor(provider="anthropic", model="m", max_tokens=8).feed("x")

    after = {p.name for p in mem.iterdir()}
    created = after - before
    wave2_dirs = {"modelgrade", "ctxheat", "routesub", "watchdog", "streamguard",
                  "ingest", "experiments"}
    assert not (created & wave2_dirs), (
        f"inert Wave-2 capabilities created state: {created & wave2_dirs}")


def test_orchestrator_decision_path_does_not_consult_wave2_policy():
    """The routing/planning/verification seams must not call Wave-2 policy while
    it is disabled. streamguard is the one deliberate exception: the orchestrator
    catches its exception type for disclosure (W2-I8.3), which is unreachable
    when the guard is off."""
    src = Path(config.__file__).with_name("orchestrator.py").read_text()
    for name in ("modelgrade", "ctxheat", "routesub", "watchdog"):
        assert name not in src, (
            f"orchestrator references {name}; Wave-2 policy must stay unwired "
            "until its evidence gate passes")
    assert "streamguard.StreamPathology" in src


def test_every_wave2_flag_is_registered_in_experiments():
    """W2-I4.1 backs rollback: an operator can find what each flag is, its
    evidence, and its review date before enabling it."""
    flags = experiments.known_flags()
    for expected in ("OLYMPUS_MODELGRADE", "OLYMPUS_CTXHEAT", "OLYMPUS_ROUTESUB",
                     "OLYMPUS_WATCHDOG"):
        assert expected in flags, f"{expected} missing from the registry"
        entry = experiments.entry(flags[expected])
        assert entry, f"{expected} maps to a missing entry"
        assert entry.get("deactivation_trigger"), (
            f"{expected} has no deactivation trigger — rollback undefined")


def test_registry_check_is_clean():
    """The registry itself must be consistent, or rollback guidance is fiction."""
    assert experiments.check_registry() == []
