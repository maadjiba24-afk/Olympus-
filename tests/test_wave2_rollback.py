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
    when the guard is off.

    Scope note (W2-PR11). The orchestrator may NAME a Wave-2 flag — its
    `OLYMPUS_*` environment string and the `tr.meta` key that records it — in the
    run-metadata and `replay_run` reproduction lists. Any flag that can change
    the decision path MUST be paired into both, or a replay of a run made with
    that flag on diverges; that pairing is a plain `os.environ` read, and a
    string is not a call. What stays forbidden is what this gate is actually
    about: importing or invoking a Wave-2 policy module from the decision path.
    The substitution/qualification decision itself lives in
    `config.ModelPool.for_specialist`, behind each module's own default-off flag.

    Scope note (W2-PR13). `watchdog` has been REMOVED from the list below,
    honestly and deliberately: its evidence gate (A9) is now closed on the live
    path, so the orchestrator legitimately imports it and opens one progress
    lease per run. Textual absence was only ever a proxy for the real
    invariant, and for a wired capability the proxy is simply wrong. The
    invariant itself — *Wave-2 policy is not consulted while DISABLED* — is
    unchanged and is now asserted directly, on behaviour rather than on source
    text, by `test_disabled_watchdog_is_never_consulted` below (and in full, on
    a complete run, by tests/test_watchdog_wiring.py).
    """
    src = Path(config.__file__).with_name("orchestrator.py").read_text()
    for name in ("modelgrade", "ctxheat", "routesub"):
        for call in (f"import {name}", f"from .{name}", f"{name}."):
            assert call not in src, (
                f"orchestrator uses {name} ({call!r}); Wave-2 policy must stay "
                "unwired from the decision path until its evidence gate passes")
    assert "streamguard.StreamPathology" in src


def test_disabled_watchdog_is_never_consulted(monkeypatch):
    """The wired capability, stated as the invariant that survives wiring.

    With `OLYMPUS_WATCHDOG` at its shipped default the orchestrator and the
    heartbeat must not build a lease, classify anything or write a byte —
    proved by replacing `ProgressLease` with a constructor that raises, which
    turns any consultation into a hard failure rather than a silent one."""
    from olympus import heartbeat, orchestrator, trace

    monkeypatch.delenv("OLYMPUS_WATCHDOG", raising=False)
    monkeypatch.setattr(watchdog, "ProgressLease", _exploding_lease)

    assert watchdog.mode() == "off"
    assert orchestrator._wd_open(trace.Trace("chat", "shared")) is None
    assert heartbeat._watch_job("probe", lambda: "ran") == "ran"
    assert not watchdog.forensics_dir().exists()


def _exploding_lease(*a, **kw):
    raise AssertionError("a disabled watchdog was consulted")


def test_wave2_routing_flags_are_paired_into_meta_and_replay():
    """The flag-pairing invariant for the Wave-2 capabilities that ARE wired.

    `OLYMPUS_ROUTESUB` / `OLYMPUS_MODELGRADE` alter which model a specialist
    runs on, and `OLYMPUS_WATCHDOG=enforce` can cancel a run outright and so
    truncate its decision path — all three therefore belong in `tr.meta` AND in
    `replay_run`'s env reproduction. Rollback depends on the pair: a run
    recorded with substitution on must replay against the same routing, a run
    recorded under enforce must replay under enforce, and a run recorded with
    any of them off must not pick up a later operator's flag."""
    src = Path(config.__file__).with_name("orchestrator.py").read_text()
    meta, replay = src.split("def replay_run(", 1)
    for flag, key in (("OLYMPUS_ROUTESUB", "routesub_mode"),
                      ("OLYMPUS_MODELGRADE", "modelgrade_enabled"),
                      ("OLYMPUS_WATCHDOG", "watchdog")):
        assert f'tr.meta["{key}"]' in meta, f"{flag} not recorded in tr.meta"
        assert flag in meta, f"{flag} not read into run metadata"
        assert flag in replay, f"{flag} not reproduced by replay_run"
        assert f'_meta.get("{key}"' in replay, (
            f"replay_run does not restore {flag} from the recorded meta")


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
