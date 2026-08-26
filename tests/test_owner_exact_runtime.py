"""P2 v6: the runtime that USES the owner-exact stores, and legacy posture.

v5 made the vault, the preference store and the record layouts owner-exact.
Three defects then lived one layer above or beside them.

1. `orchestrator.Olympus.__init__` ran `self.user = memory.safe_id(user)`, so
   the exact principal was destroyed before any of those stores was reached.
   `Olympus(user="tg-a.b").user` was `"tg-a-b"`. The scheduler's DEFAULT
   production runner is `Olympus(user=job.user).ask(prompt)`, so a job owned by
   `tg-a.b` ran with `tg-a-b`'s preferences, vault, granted scopes and memory —
   and only the final report was filed back under `tg-a.b`.

2. Quarantining a legacy preference file and then falling back to ordinary
   DEFAULTS widened privilege: a legacy `capability_profile="guest"` read as
   "full", a legacy `autonomy=0` read as L1, and a strict `action_limits` entry
   vanished into an unlimited class default.

3. Quarantining a legacy vault removed its secrets from the always-on outbound
   scan, so a legacy OAuth token could be emitted verbatim by any principal in
   the collision group — and an unreadable one failed OPEN.

WHAT IS AND IS NOT STUBBED. For the composed-path tests the ONLY stub is the
model/backend response boundary (`olympus.backend`). Olympus, memory, prefs,
vault, actions, capprofile and the scheduler all run for real against a real
temporary MEMORY_DIR and a real encrypted vault — the defects lived exactly
where a convenience mock would have been.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import quote

import pytest

from olympus import (actions, backend, capprofile, config, memory, prefs,
                     scheduler, security, store, trust, vault)

A = "tg-a.b"
B = "tg-a-b"
LONG_A = "tg-" + "w" * 70 + "AAA"
LONG_B = "tg-" + "w" * 70 + "BBB"
PAIRS = [(A, B), (LONG_A, LONG_B)]

needs_crypto = pytest.mark.skipif(
    not vault._HAVE_CRYPTO, reason="cryptography backend unavailable here")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "correct horse battery staple")
    monkeypatch.setenv("OLYMPUS_MODEL", "claude-opus-4-8")
    store.reset()
    memory.set_user("shared")
    yield
    memory.set_user("shared")
    store.reset()


# =========================================================================
# 1. The exact principal survives into the real runtime
# =========================================================================

@pytest.mark.parametrize("who", [A, B, LONG_A, LONG_B, "shared", "cli"])
def test_olympus_preserves_the_exact_principal(who):
    from olympus import orchestrator

    bot = orchestrator.Olympus(user=who)
    assert bot.user == memory.canonical_owner(who), (
        f"Olympus normalized {who!r} to {bot.user!r}")


@pytest.mark.parametrize("first,second", PAIRS)
def test_olympus_does_not_collapse_colliding_principals(first, second):
    from olympus import orchestrator

    assert memory.safe_id(first) == memory.safe_id(second)
    assert (orchestrator.Olympus(user=first).user
            != orchestrator.Olympus(user=second).user)


def _stub_model(monkeypatch, answer="THE-ANSWER"):
    """Stub ONLY the model/backend response boundary.

    Every call the council makes to a provider funnels through `backend`.
    Nothing above it — Olympus, memory, prefs, vault, actions, scheduler — is
    replaced, so the composed tests below exercise the real code.
    """
    monkeypatch.setattr(backend, "complete_json",
                        lambda s, sys_, msgs, schema, **kw: {
                            "mode": "direct", "direct_reply": answer,
                            "specialists": [], "brief": "",
                            "needs_verification": False})
    monkeypatch.setattr(backend, "complete_text",
                        lambda s, sys_, msgs, **kw: answer)
    monkeypatch.setattr(backend, "complete_text_once",
                        lambda s, sys_, msgs, **kw: answer)
    monkeypatch.setattr(backend, "run_agent",
                        lambda s, sys_, task, **kw: answer)
    monkeypatch.setattr(backend, "run_agent_counted",
                        lambda s, sys_, task, **kw: (answer, 0))


@pytest.fixture()
def _probe(monkeypatch):
    """Capture what the REAL Olympus sees, from inside the real run."""
    from olympus import orchestrator

    seen: dict = {}
    original = orchestrator.Olympus._route

    def _route(self, user_message):
        seen["self.user"] = self.user
        seen["current_owner"] = memory.current_owner()
        seen["current_user"] = memory.current_user()
        seen["autonomy"] = actions.autonomy_level(self.user)
        seen["scopes"] = actions.granted_scopes(self.user)
        seen["profile"] = capprofile.of_user(self.user)
        seen["vault_names"] = vault.names(self.user)
        seen["token"] = vault.get(self.user, "google")
        return original(self, user_message)

    monkeypatch.setattr(orchestrator.Olympus, "_route", _route)
    return seen


@needs_crypto
@pytest.mark.parametrize("first,second", PAIRS)
def test_a_scheduled_job_runs_as_its_exact_owner(first, second, monkeypatch,
                                                 _probe):
    """THE composed path. `scheduler.run_due` with its DEFAULT production
    runner, which builds a real `Olympus(user=job.user)` and calls `ask`.

    v5 ran first's job as second: `self.user` was the normalized value, so the
    run read second's preferences and second's credential vault.
    """
    _stub_model(monkeypatch, "MARKER-FOR-FIRST")

    # `second` holds a real preference and a real encrypted token that `first`
    # must never see. Written through the real APIs, not planted.
    actions.set_autonomy(second, 4)
    actions.grant_scope(second, "email")
    capprofile.assign(second, "full")
    vault.put(second, "google", {"access_token": "SECOND-ONLY-TOKEN"})

    scheduler.add("payroll", 3600, "first's private prompt", user=first)
    log = scheduler.run_due(now=1e12)              # DEFAULT runner: no injection

    assert log and "ran scheduled job" in log[0], log
    assert _probe["self.user"] == first, (
        f"the run executed as {_probe['self.user']!r}, not {first!r}")
    assert _probe["current_owner"] == first
    assert _probe["current_user"] == memory.safe_id(first)
    # ...and it could not reach `second`'s settings or credentials.
    assert _probe["autonomy"] == actions.DEFAULT_AUTONOMY
    assert _probe["scopes"] == set()
    assert _probe["token"] is None, "the run read the collider's OAuth token"
    assert _probe["vault_names"] == []
    # The report is readable by its owner and by nobody else.
    assert "MARKER-FOR-FIRST" in memory.search_for(first, "MARKER-FOR-FIRST")
    assert "MARKER-FOR-FIRST" not in memory.search_for(second,
                                                       "MARKER-FOR-FIRST")


@needs_crypto
def test_the_run_leaves_the_callers_identity_exactly_as_it_found_it(monkeypatch):
    _stub_model(monkeypatch)
    memory.set_user(LONG_A)
    before = (memory.current_user(), memory.current_owner())
    scheduler.add("j", 3600, "p", user=A)
    scheduler.run_due(now=1e12)
    assert (memory.current_user(), memory.current_owner()) == before


@pytest.mark.parametrize("who", [A, LONG_A])
def test_ask_binds_the_exact_principal(who, monkeypatch, _probe):
    from olympus import orchestrator

    _stub_model(monkeypatch)
    memory.set_user("someone-else-entirely")
    orchestrator.Olympus(user=who).ask("hello")
    assert _probe["current_owner"] == who
    assert _probe["self.user"] == who


@pytest.mark.parametrize("who", [A, LONG_A])
def test_ask_ephemeral_binds_the_exact_principal(who, monkeypatch, _probe):
    from olympus import orchestrator

    _stub_model(monkeypatch)
    memory.set_user("someone-else-entirely")
    bot = orchestrator.Olympus(user=who)
    try:
        bot.ask_ephemeral("hello")
    except Exception:
        pass                     # only the binding is under test here
    assert memory.current_owner() == who


def test_worker_threads_bind_the_exact_owner_not_the_namespace(monkeypatch):
    """`_dispatch_dag` fans out to threads, which do not inherit ContextVars —
    each rebinds from `self.user`. If that value is normalized, or if the
    rebinding used `current_user()`, every specialist runs as the collider."""
    from olympus import orchestrator
    from olympus.specialists import SPECIALISTS

    seen: list[tuple[str, str]] = []

    def fake_run_counted(self, task, settings=None, effort="high"):
        seen.append((memory.current_owner(), memory.current_user()))
        return f"output[{self.key}]", 0

    monkeypatch.setattr(SPECIALISTS["plutus"].__class__, "run_counted",
                        fake_run_counted)
    bot = orchestrator.Olympus(user=A)
    from olympus import trace as trace_mod
    bot._dispatch_dag(
        [{"id": "a", "specialist": "plutus", "task": "t", "depends_on": []},
         {"id": "b", "specialist": "peitho", "task": "t2", "depends_on": []}],
        trace_mod.Trace("t"))

    assert seen, "no specialist ran"
    assert {owner for owner, _ in seen} == {A}, seen
    assert {ns for _, ns in seen} == {memory.safe_id(A)}, seen


def test_orchestrator_never_normalizes_the_principal():
    """Structural. `self.user` is handed to prefs, vault, actions, operator and
    trust — all owner-exact — so normalizing it defeats every one at once."""
    import ast
    import inspect
    import pathlib

    from olympus import orchestrator

    path = pathlib.Path(inspect.getfile(orchestrator))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Attribute) and target.attr == "user"
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"):
            continue
        call = node.value
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in ("safe_id", "current_user")):
            offenders.append(str(node.lineno))
    assert not offenders, (
        "orchestrator assigns a normalized value to self.user at line(s) "
        + ", ".join(offenders) + " — use memory.canonical_owner")


def test_identity_sensitive_modules_bind_owners_exactly():
    """Structural sweep of the request/background constructors that hand an
    owner to Olympus, prefs, vault, actions or the scheduler."""
    import ast
    import pathlib

    # `gateway` MINTS its principal from a transport key (`f"{prefix}-{safe_id
    # (user_key)}"`) and `companion`/`gateway` build filenames; both are
    # documented normalized/path-only uses and are excluded deliberately.
    checked = ["olympus/goals.py", "olympus/scheduler.py",
               "olympus/mcp_server.py", "olympus/subagents.py",
               "olympus/discovery.py"]
    offenders = []
    for rel in checked:
        path = pathlib.Path(rel)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Olympus"):
                for kw in node.keywords:
                    if kw.arg != "user":
                        continue
                    if (isinstance(kw.value, ast.Call)
                            and isinstance(kw.value.func, ast.Attribute)
                            and kw.value.func.attr in ("safe_id",
                                                       "current_user")):
                        offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "an owner is normalized on its way into Olympus at "
        + ", ".join(offenders))


# =========================================================================
# 2. Legacy preference quarantine must RESTRICT, never widen
# =========================================================================

def _plant_legacy_prefs(owner, data):
    d = config.MEMORY_DIR / "users" / memory.safe_id(owner)
    d.mkdir(parents=True, exist_ok=True)
    (d / "prefs.json").write_text(json.dumps(data), encoding="utf-8")
    return memory.safe_id(owner)


RESTRICTIVE_LEGACY = {
    "capability_profile": "guest",
    "autonomy": 0,
    "scopes": [],
    "action_limits": {"send_email": 1},
    "operator": {"sites": {"acme.test": {"login": "manual"}},
                 "advanced": True, "enabled": True},
    "earned_autonomy": True,
    "pending_secure_login": "acme.test",
}


@pytest.mark.parametrize("first,second", PAIRS)
def test_a_restrictive_legacy_file_is_never_widened(first, second):
    """THE blocker. v5 turned guest/L0/limit-1 into full/L1/unlimited for the
    whole collision group — a quarantine that granted privilege."""
    _plant_legacy_prefs(first, RESTRICTIVE_LEGACY)

    for who in (first, second, memory.safe_id(first)):
        assert prefs.is_quarantined(who) is True, who
        assert capprofile.of_user(who) == "guest", f"{who!r} was widened"
        assert capprofile.autonomy_cap(who) == 0
        assert actions.autonomy_level(who) == 0
        assert actions.granted_scopes(who) == set()
        assert trust.enabled(who) is False
        # A missing limit must NOT become unlimited (0) because of quarantine.
        for kind in ("send_email", "write_document", "unregistered_type"):
            limit = actions.daily_limit(who, kind)
            assert limit > 0, f"{who!r}/{kind} is unlimited under quarantine"
            assert limit <= 1


@pytest.mark.parametrize("first,second", PAIRS)
def test_each_quarantine_posture_value_is_restrictive_on_its_own(first, second):
    """The posture entries individually, not only through their consumers.

    `actions.autonomy_level` takes `min(level, capprofile.autonomy_cap(user))`,
    and the quarantine profile caps at 0 — so a widened `autonomy` posture is
    invisible through that path. Assert the stored posture directly, or the two
    layers hide each other's regressions.
    """
    _plant_legacy_prefs(first, RESTRICTIVE_LEGACY)
    for who in (first, second):
        assert prefs.get(who, "autonomy") == 0
        assert prefs.get(who, "scopes") == []
        assert prefs.get(who, "capability_profile") == "guest"
        assert prefs.get(who, "action_limits") == {}
        assert prefs.get(who, "operator") == {"sites": {}, "advanced": False}
        assert prefs.get(who, "earned_autonomy") is False
        assert prefs.get(who, "pending_secure_login") is None


def test_the_quarantine_gates_are_not_deleted():
    """Structural, and deliberately so.

    `daily_limit` and `can_auto_execute` consult `prefs.is_quarantined`
    explicitly. Both are REDUNDANT with the L0/guest posture in every reachable
    state today — the capability cap of 0 already blocks unattended execution —
    which is exactly why a behavioural test cannot distinguish their removal.
    They exist because a caller may supply its own effective level (earned
    per-domain trust) and because a class default of 0 means *unlimited*, so
    this guard keeps them from being tidied away as dead code.
    """
    import ast
    import inspect
    import pathlib

    path = pathlib.Path(inspect.getfile(actions))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    guarded = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "is_quarantined"):
                guarded.add(fn.name)
    missing = {"daily_limit", "can_auto_execute"} - guarded
    assert not missing, (
        f"the quarantine gate was removed from {', '.join(sorted(missing))}")


@pytest.mark.parametrize("first,second", PAIRS)
def test_quarantine_disables_operator_and_secure_capture(first, second):
    from olympus import operator, securecapture

    _plant_legacy_prefs(first, RESTRICTIVE_LEGACY)
    for who in (first, second):
        assert operator.authorized(who, "acme.test") is False
        assert operator.advanced(who) is False
        assert operator.sites(who) == {}
        assert securecapture.pending(who) is None


@pytest.mark.parametrize("first,second", PAIRS)
def test_nothing_unattended_or_standing_runs_under_quarantine(first, second):
    """Explicitly, not merely implied by the L0 posture: a caller may supply
    its own effective level (earned per-domain trust)."""
    actions.register(actions.ActionType(
        name="v6trivial", risk_class=actions.TRIVIAL, scope="",
        preview=lambda p: "preview", execute=lambda p: {"ok": True},
        undo=lambda r: "undone", binds_user=True))
    try:
        _plant_legacy_prefs(first, RESTRICTIVE_LEGACY)
        for who in (first, second):
            a = actions.prepare(who, "v6trivial", {})
            assert actions.can_auto_execute(a) is False
            assert actions.can_auto_execute(a, level=4) is False
            assert actions.can_auto_execute(a, level=99) is False
            assert actions.auto_or_hold(a).status == actions.PREPARED
    finally:
        actions._REGISTRY.pop("v6trivial", None)


@pytest.mark.parametrize("first,second", PAIRS)
def test_a_permissive_legacy_file_is_also_not_inherited(first, second):
    """The other direction still holds: quarantine grants nothing either."""
    _plant_legacy_prefs(first, {"autonomy": 4, "scopes": ["email"],
                                "capability_profile": "full"})
    for who in (first, second):
        assert actions.autonomy_level(who) == 0
        assert actions.granted_scopes(who) == set()
        assert capprofile.of_user(who) == "guest"


@pytest.mark.parametrize("first,second", PAIRS)
def test_writing_a_new_exact_file_does_not_clear_quarantine(first, second):
    """Only an explicit operator resolution may clear it — otherwise a tenant
    escapes the posture by setting any preference at all."""
    _plant_legacy_prefs(first, RESTRICTIVE_LEGACY)

    actions.set_autonomy(first, 4)
    actions.grant_scope(first, "email")
    capprofile.assign(first, "full")
    trust.set_enabled(first, True)

    assert prefs.is_quarantined(first) is True
    assert actions.autonomy_level(first) == 0, "an exact write cleared quarantine"
    assert actions.granted_scopes(first) == set()
    assert capprofile.of_user(first) == "guest"
    assert trust.enabled(first) is False
    # The values ARE stored; they simply do not take effect yet.
    assert prefs._raw_get(first, "autonomy") == 4


def test_migration_clears_quarantine_and_restores_the_real_settings():
    key = _plant_legacy_prefs(A, RESTRICTIVE_LEGACY)
    assert prefs.migrate_legacy(key, A) == len(RESTRICTIVE_LEGACY)

    assert prefs.is_quarantined(A) is False
    assert prefs.is_quarantined(B) is False
    assert capprofile.of_user(A) == "guest"        # the real legacy value
    assert actions.autonomy_level(A) == 0
    assert actions.daily_limit(A, "send_email") == 1
    # ...and B, who never owned it, gets ordinary defaults.
    assert capprofile.of_user(B) == "full"
    assert actions.autonomy_level(B) == actions.DEFAULT_AUTONOMY


def test_migration_moves_every_key_despite_the_active_clamp():
    """`migrate_legacy` must compare against RAW stored values: while the legacy
    file is still on disk the clamp is active, so a `get`-based "already
    present?" check reports a posture value for every security key and skips
    the entire migration."""
    key = _plant_legacy_prefs(A, RESTRICTIVE_LEGACY)
    moved = prefs.migrate_legacy(key, A)
    assert moved == len(RESTRICTIVE_LEGACY), (
        f"only {moved} of {len(RESTRICTIVE_LEGACY)} keys migrated")
    for name, value in RESTRICTIVE_LEGACY.items():
        assert prefs._raw_get(A, name) == value


def test_discard_clears_quarantine():
    key = _plant_legacy_prefs(A, RESTRICTIVE_LEGACY)
    assert prefs.discard_legacy(key) is True
    assert prefs.is_quarantined(A) is False
    assert capprofile.of_user(A) == "full"
    assert actions.autonomy_level(A) == actions.DEFAULT_AUTONOMY


def test_installation_namespaces_are_never_quarantined():
    """`shared` and `operator` are addressed by literal name under the
    documented explicit policy, so their files were never ambiguous."""
    (config.MEMORY_DIR / "users" / "shared").mkdir(parents=True, exist_ok=True)
    (config.MEMORY_DIR / "users" / "shared" / "prefs.json").write_text(
        json.dumps({"autonomy": 4}), encoding="utf-8")

    for name in sorted(memory.SYSTEM_OWNERS):
        assert prefs.is_quarantined(name) is False
        assert prefs.quarantine_reason(name) is None
    prefs.set("shared", "daily_budget", 12.5)
    assert prefs.get("shared", "daily_budget") == 12.5


def test_an_unaffected_principal_is_not_quarantined():
    _plant_legacy_prefs(A, RESTRICTIVE_LEGACY)
    for who in ("tg-someone-else", LONG_B):
        assert prefs.is_quarantined(who) is False
        assert capprofile.of_user(who) == "full"
    assert prefs.quarantine_reason(A) is not None
    assert "migrate or discard" in prefs.quarantine_reason(A)


def test_the_posture_map_cannot_be_poisoned_by_a_caller():
    _plant_legacy_prefs(A, RESTRICTIVE_LEGACY)
    got = prefs.get(A, "operator")
    got["sites"]["evil.test"] = {"login": "manual"}
    got["advanced"] = True
    assert prefs.get(A, "operator") == {"sites": {}, "advanced": False}


def test_quarantine_detection_lives_in_exactly_one_place():
    """Structural. The blocker asked for a central API rather than duplicated
    legacy detection: no module outside `prefs` may reconstruct the legacy
    preference path for itself."""
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("olympus").glob("*.py")):
        if path.name == "prefs.py":
            continue
        src = path.read_text(encoding="utf-8")
        if "prefs.json" in src:
            for i, line in enumerate(src.splitlines(), 1):
                if "prefs.json" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        "legacy preference detection is duplicated at " + ", ".join(offenders)
        + " — use prefs.is_quarantined")


# =========================================================================
# 3. Quarantined vault secrets stay in the outbound scan
# =========================================================================

TOKEN = "LEGACY-PRIVATE-TOKEN-123456789"


def _plant_legacy_vault(owner, payload):
    blob = vault._fernet().encrypt(json.dumps(payload).encode())
    store.backend().put(vault._NS, memory.safe_id(owner), blob)
    return memory.safe_id(owner)


def _forms(value):
    return {
        "raw": value,
        "base64": base64.b64encode(value.encode()).decode().rstrip("="),
        "hex": value.encode().hex(),
        "urlencoded": quote(value, safe=""),
    }


@needs_crypto
@pytest.mark.parametrize("first,second", PAIRS)
@pytest.mark.parametrize("form", sorted(_forms(TOKEN)))
def test_a_quarantined_secret_is_still_refused_outbound(first, second, form):
    """v5 dropped these from the scan entirely, so the token could be emitted
    verbatim by anyone in the collision group."""
    _plant_legacy_vault(first, {"google": {"access_token": TOKEN}})
    payload = f"here you go: {_forms(TOKEN)[form]} -- end"

    for who in (first, second):
        reason = security.secret_exfil_reason(payload, who)
        assert reason, f"{who!r} could send the {form} form of a legacy secret"
        assert TOKEN not in reason, "the reason leaked the secret"
        assert _forms(TOKEN)[form] not in reason
        assert memory.safe_id(first) not in reason, "the reason leaked a key"
        assert "google" not in reason, "the reason leaked an entry name"


@needs_crypto
def test_clean_content_still_passes_with_a_quarantined_vault():
    """Conservative, not indiscriminate: unrelated content still sends."""
    _plant_legacy_vault(A, {"google": {"access_token": TOKEN}})
    assert security.secret_exfil_reason("an ordinary sentence", A) is None
    assert security.secret_exfil_reason("an ordinary sentence", B) is None


@needs_crypto
def test_an_unreadable_quarantined_vault_fails_closed():
    """v5 failed OPEN here: an undecryptable vault meant no scan at all."""
    store.backend().put(vault._NS, memory.safe_id(A), b"not-decryptable-bytes")
    for who in (A, B):
        reason = security.secret_exfil_reason("anything at all", who)
        assert reason, f"{who!r} sent freely past an unreadable legacy vault"
        assert "cannot be read" in reason
        assert "not-decryptable-bytes" not in reason


@needs_crypto
def test_a_structurally_invalid_quarantined_vault_fails_closed():
    blob = vault._fernet().encrypt(json.dumps(["not", "a", "mapping"]).encode())
    store.backend().put(vault._NS, memory.safe_id(A), blob)
    assert security.secret_exfil_reason("anything", A)


@needs_crypto
def test_an_unaffected_principal_is_not_held_by_someone_elses_vault():
    _plant_legacy_vault(A, {"google": {"access_token": TOKEN}})
    assert security.secret_exfil_reason(TOKEN, "tg-unrelated") is None
    assert security.secret_exfil_reason("anything", "tg-unrelated") is None


@needs_crypto
def test_credential_paths_still_refuse_the_quarantined_vault():
    """Scanning it must not re-open it for authentication."""
    from olympus import google_oauth

    _plant_legacy_vault(A, {"google": {"access_token": TOKEN}})
    for who in (A, B):
        assert vault.get(who, "google") is None
        assert vault.names(who) == []
        assert google_oauth.connected(who) is False
    assert vault.legacy_tenants() == [memory.safe_id(A)]


@needs_crypto
def test_normal_exact_scanning_resumes_after_migration():
    key = _plant_legacy_vault(A, {"google": {"access_token": TOKEN}})
    assert vault.migrate_legacy(key, A) == 1

    # A now owns it exactly: named in the reason, and B is free again.
    reason = security.secret_exfil_reason(TOKEN, A)
    assert reason and "vault:google" in reason
    assert security.secret_exfil_reason(TOKEN, B) is None
    assert security.secret_exfil_reason("anything", B) is None


@needs_crypto
def test_normal_exact_scanning_resumes_after_discard():
    key = _plant_legacy_vault(A, {"google": {"access_token": TOKEN}})
    assert vault.discard_legacy(key) is True
    for who in (A, B):
        assert security.secret_exfil_reason(TOKEN, who) is None


@needs_crypto
def test_installation_namespaces_are_not_scanned_as_legacy():
    vault.put("operator", "config:X", TOKEN)
    assert vault._legacy_key_for("operator") is None
    assert vault._legacy_key_for("shared") is None
    # The operator's own vault is live, so it is scanned the ordinary way.
    reason = security.secret_exfil_reason(TOKEN, "operator")
    assert reason and "vault:config:X" in reason


@needs_crypto
def test_the_scan_path_never_returns_secret_values():
    """`legacy_scan` takes a predicate and returns only a generic reason, so no
    value, label, entry name or ciphertext can reach a caller or a log."""
    _plant_legacy_vault(A, {"google": {"access_token": TOKEN},
                            "site:acme.test": {"password": "P" * 40}})
    seen: list[str] = []
    out = vault.legacy_scan(A, lambda value: seen.append(value) or False)
    assert out is None
    assert seen, "the predicate never ran"
    # The predicate is called INSIDE vault; the return value carries nothing.
    for reason in (vault._LEGACY_MATCH_REASON, vault._LEGACY_UNREADABLE_REASON):
        assert TOKEN not in reason
        assert "P" * 40 not in reason
        assert "google" not in reason
