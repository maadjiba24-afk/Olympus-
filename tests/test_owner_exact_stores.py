"""P2 v5: the credential vault, the preference store, and legacy records.

Three exact-owner defects survived v4, each because a store normalized the
principal INTERNALLY — so handing it an exact owner changed nothing.

1. `vault._load/_store` keyed on `memory.safe_id(user)`. Reproduced on v4:

       vault.put("tg-a.b", "google", bundle)
       memory.set_user("tg-a-b"); gmail._access_token()   -> "A-SECRET-TOKEN"

   `google_oauth.connected("tg-a-b")` was True, `vault.names` listed A's
   entries, and saved site passwords and browser session cookies merged the
   same way.

2. `prefs._path` keyed on `memory.safe_id(user)`. That one file carries the
   autonomy level, granted action scopes, per-action daily limits, the
   capability profile and its `max_autonomy` cap, operator settings and their
   authorized-site list, earned-autonomy opt-in and pending secure-capture
   state. Reproduced on v4: `set_autonomy("tg-a.b", 4)` made
   `autonomy_level("tg-a-b")` 4, and `grant_scope` leaked the same way.

3. Legacy jobs and actions were treated as belonging to whoever's exact
   identity equalled their stored NORMALIZED owner. Reproduced on v4: a job
   persisted with `user="tg-a-b"` ran under `tg-a-b`, showed them `tg-a.b`'s
   prompt and filed the answer in their private `job_reports`; a pre-v4 action
   record exposed its title, preview and payload to the same collider.
   Equalling a lossy value is not proof of ownership — it is precisely what
   every collision victim's collider also does.

THE STORE BOUNDARY IS REAL IN THIS MODULE. The decisive tests use the actual
encrypted vault, the actual prefs JSON files and the actual `schedule.json`, in
a temporary MEMORY_DIR. Nothing between the caller and the bytes is stubbed —
that is the whole point, since the defect lived one line below where a mock
would have sat.
"""

from __future__ import annotations

import json

import pytest

from olympus import (actions, capprofile, config, memory, prefs, scheduler,
                     store, trust, vault)

# The two classic collision pairs. `safe_id` collapses every run of
# non-[A-Za-z0-9_-] to a single "-" and truncates at 64 characters.
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
    store.reset()
    memory.set_user("shared")
    yield
    memory.set_user("shared")
    store.reset()


def test_the_collisions_are_real():
    """The premise, asserted rather than assumed."""
    assert memory.safe_id(A) == memory.safe_id(B) == "tg-a-b"
    assert memory.safe_id(LONG_A) == memory.safe_id(LONG_B)
    assert A != B and LONG_A != LONG_B


# =========================================================================
# 1. The credential vault
# =========================================================================

@needs_crypto
@pytest.mark.parametrize("first,second", PAIRS)
def test_vault_does_not_share_secrets_between_colliding_principals(first,
                                                                   second):
    """Real `vault.put`/`get`/`names` over the real encrypted store."""
    vault.put(first, "google", {"access_token": "FIRST-TOKEN"})
    vault.put(first, "site:acme.test", {"username": "alice",
                                        "password": "FIRST-PASSWORD"})
    vault.put(first, "cookies:acme.test", {"cookies": [{"name": "sid",
                                                        "value": "FIRST-SID"}]})

    assert vault.get(second, "google") is None
    assert vault.get(second, "site:acme.test") is None
    assert vault.get(second, "cookies:acme.test") is None
    assert vault.names(second) == []
    # ...and the owner still has everything.
    assert vault.get(first, "google") == {"access_token": "FIRST-TOKEN"}
    assert vault.names(first) == ["cookies:acme.test", "google",
                                  "site:acme.test"]


@needs_crypto
@pytest.mark.parametrize("first,second", PAIRS)
def test_each_principal_keeps_its_own_secret_under_the_same_name(first, second):
    vault.put(first, "google", {"access_token": "FIRST-TOKEN"})
    vault.put(second, "google", {"access_token": "SECOND-TOKEN"})
    assert vault.get(first, "google") == {"access_token": "FIRST-TOKEN"}
    assert vault.get(second, "google") == {"access_token": "SECOND-TOKEN"}
    vault.delete(second, "google")
    assert vault.get(first, "google") is not None, "delete crossed owners"
    assert vault.get(second, "google") is None


@needs_crypto
@pytest.mark.parametrize("first,second", PAIRS)
def test_google_oauth_does_not_hand_over_another_principals_token(first,
                                                                 second):
    """Through the real `google_oauth` API, not the vault directly."""
    import time

    from olympus import google_oauth

    google_oauth_bundle = {"access_token": "FIRST-ACCESS",
                           "refresh_token": "FIRST-REFRESH",
                           "expires_at": time.time() + 3600}
    vault.put(first, "google", google_oauth_bundle)

    assert google_oauth.connected(first) is True
    assert google_oauth.connected(second) is False
    assert google_oauth.access_token(first) == "FIRST-ACCESS"
    with pytest.raises(google_oauth.OAuthError):
        google_oauth.access_token(second)


@needs_crypto
@pytest.mark.parametrize("first,second", PAIRS)
def test_gmail_reaches_only_the_bound_principals_mailbox(first, second):
    """The end of the chain: which mailbox an outbound Gmail call reaches.

    `gmail._access_token` resolves the OAuth bundle from the ambient EXACT
    principal, so this drives the real request binding rather than passing an
    owner in.
    """
    import time

    from olympus import gmail

    vault.put(first, "google", {"access_token": "FIRST-ACCESS",
                                "refresh_token": "FIRST-REFRESH",
                                "expires_at": time.time() + 3600})

    memory.set_user(first)
    assert gmail._access_token() == "FIRST-ACCESS"

    memory.set_user(second)
    with pytest.raises(Exception) as caught:
        gmail._access_token()
    assert "FIRST-ACCESS" not in str(caught.value)


@needs_crypto
def test_reserved_installation_vault_namespaces_still_work():
    """`operator` is the installation's own credential namespace (opconfig
    config secrets, secretref entries). It keeps its literal key so
    installation code that hardcodes it is unaffected."""
    from olympus import opconfig, secretref

    assert opconfig.VAULT_USER == "operator"
    assert secretref._VAULT_USER == "operator"
    assert memory.storage_key("operator") == "operator"
    assert memory.storage_key("shared") == "shared"

    vault.put("operator", "config:SMTP_PASS", "INSTALLATION-SECRET")
    assert vault.get("operator", "config:SMTP_PASS") == "INSTALLATION-SECRET"
    assert vault.names("operator") == ["config:SMTP_PASS"]
    # A tenant cannot reach it, including one whose id normalizes to it.
    for who in (A, B, "operator-x", "shared"):
        if who == "operator":
            continue
        assert vault.get(who, "config:SMTP_PASS") is None


def test_reserved_names_are_refused_as_tenant_principals():
    for name in sorted(memory.SYSTEM_OWNERS):
        assert memory.is_system_owner(name)
        with pytest.raises(ValueError, match="reserved"):
            memory.assert_not_system_owner(name)
    assert memory.assert_not_system_owner(A) == A


def test_storage_key_never_collides_with_a_reserved_name():
    """`owner_key` always appends `-<64 hex>`, so no tenant can produce a bare
    reserved name however it is spelled."""
    for who in (A, B, LONG_A, "operator ", "operator.", "Operator", "shared "):
        key = memory.storage_key(who)
        if memory.canonical_owner(who) in memory.SYSTEM_OWNERS:
            continue
        assert key not in memory.SYSTEM_OWNERS
        assert len(key.rsplit("-", 1)[-1]) == 64


# --- pre-v2 vaults: quarantined, never implicitly claimed -----------------

def _plant_legacy_vault(owner, payload):
    """Write a vault exactly as the pre-v5 `safe_id` layout did."""
    blob = vault._fernet().encrypt(json.dumps(payload).encode())
    store.backend().put(vault._NS, memory.safe_id(owner), blob)
    return memory.safe_id(owner)


@needs_crypto
@pytest.mark.parametrize("first,second", PAIRS)
def test_a_pre_v5_vault_is_claimed_by_nobody(first, second):
    """No implicit fallback — not even for a principal whose exact identity
    equals the normalized key. That equality is what the COLLIDER also has."""
    key = _plant_legacy_vault(first, {"google": {"access_token": "LEGACY"},
                                      "site:acme.test": {"password": "LEG"}})

    for who in (first, second, key, "shared", "tg-unrelated"):
        assert vault.get(who, "google") is None, f"{who!r} claimed a legacy vault"
        assert vault.get(who, "site:acme.test") is None
        assert vault.names(who) == []

    # Operator inspection still finds it, and names entries without values.
    assert vault.legacy_tenants() == [key]
    assert vault.legacy_names(key) == ["google", "site:acme.test"]


@needs_crypto
def test_an_operator_can_explicitly_migrate_a_quarantined_vault():
    key = _plant_legacy_vault(A, {"google": {"access_token": "LEGACY"}})

    assert vault.migrate_legacy(key, A) == 1
    assert vault.get(A, "google") == {"access_token": "LEGACY"}
    assert vault.get(B, "google") is None
    assert vault.legacy_tenants() == []
    # It cannot be claimed twice.
    with pytest.raises(vault.VaultError):
        vault.migrate_legacy(key, B)


@needs_crypto
def test_migration_refuses_a_reserved_namespace_as_the_claimant():
    key = _plant_legacy_vault(A, {"google": {"access_token": "LEGACY"}})
    with pytest.raises(ValueError, match="reserved"):
        vault.migrate_legacy(key, "operator")
    assert vault.legacy_tenants() == [key], "the vault was consumed anyway"


@needs_crypto
def test_migration_never_overwrites_a_live_secret_by_default():
    vault.put(A, "google", {"access_token": "CURRENT"})
    key = _plant_legacy_vault(A, {"google": {"access_token": "LEGACY"},
                                  "other": "X"})
    assert vault.migrate_legacy(key, A) == 1          # only "other"
    assert vault.get(A, "google") == {"access_token": "CURRENT"}
    assert vault.get(A, "other") == "X"


@needs_crypto
def test_an_operator_can_discard_a_quarantined_vault():
    key = _plant_legacy_vault(A, {"google": {"access_token": "LEGACY"}})
    assert vault.discard_legacy(key) is True
    assert vault.legacy_tenants() == []
    assert vault.discard_legacy(key) is False


@needs_crypto
def test_a_reserved_namespace_is_never_reported_as_legacy():
    vault.put("operator", "config:X", "S")
    vault.put("shared", "thing", "S")
    vault.put(A, "google", {"access_token": "T"})
    assert vault.legacy_tenants() == []


# =========================================================================
# 2. Security preferences
# =========================================================================

@pytest.mark.parametrize("first,second", PAIRS)
def test_autonomy_is_not_shared_between_colliding_principals(first, second):
    """Real prefs files. Raising one principal to standing autonomy must not
    raise another's."""
    baseline = actions.autonomy_level(second)
    actions.set_autonomy(first, 4)
    assert actions.autonomy_level(first) == 4
    assert actions.autonomy_level(second) == baseline


@pytest.mark.parametrize("first,second", PAIRS)
def test_granted_scopes_are_not_shared(first, second):
    actions.grant_scope(first, "email")
    assert actions.granted_scopes(first) == {"email"}
    assert actions.granted_scopes(second) == set()
    actions.grant_scope(second, "calendar")
    assert actions.granted_scopes(first) == {"email"}
    assert actions.granted_scopes(second) == {"calendar"}
    actions.revoke_all(second)
    assert actions.granted_scopes(first) == {"email"}, "revoke crossed owners"


@pytest.mark.parametrize("first,second", PAIRS)
def test_action_daily_limits_are_not_shared(first, second):
    # Assert on the OVERRIDE, not on `limits()`: that enumerates the global
    # action registry, whose contents depend on which modules have imported.
    actions.set_limit(first, "send_email", 2)
    assert actions.daily_limit(first, "send_email") == 2
    assert prefs.get(second, "action_limits") in (None, {})
    assert (actions.daily_limit(second, "send_email")
            == actions.daily_limit("tg-entirely-unrelated", "send_email"))


@pytest.mark.parametrize("first,second", PAIRS)
def test_capability_profile_and_autonomy_cap_are_not_shared(first, second):
    """The most security-relevant preference: `max_autonomy`."""
    capprofile.assign(first, "guest")
    assert capprofile.of_user(first) == "guest"
    assert capprofile.of_user(second) == "full"
    assert capprofile.autonomy_cap(first) == 0
    assert capprofile.autonomy_cap(second) == 4
    capprofile.clear(first)
    assert capprofile.of_user(first) == "full"


@pytest.mark.parametrize("first,second", PAIRS)
def test_restricting_one_principal_does_not_restrict_the_collider(first,
                                                                  second):
    """And the reverse: a colliding id must not be able to widen itself by
    inheriting an unrestricted profile assignment."""
    capprofile.assign(first, "reader")
    capprofile.assign(second, "guest")
    assert capprofile.of_user(first) == "reader"
    assert capprofile.of_user(second) == "guest"
    assert capprofile.autonomy_cap(first) == 1
    assert capprofile.autonomy_cap(second) == 0


@pytest.mark.parametrize("first,second", PAIRS)
def test_operator_settings_and_authorized_sites_are_not_shared(first, second):
    from olympus import operator

    operator.set_advanced(first, True)
    operator.authorize_site(first, "acme.test", "manual")

    assert operator.advanced(first) is True
    assert operator.advanced(second) is False
    assert operator.authorized(first, "acme.test") is True
    assert operator.authorized(second, "acme.test") is False


@pytest.mark.parametrize("first,second", PAIRS)
def test_earned_autonomy_optin_is_not_shared(first, second):
    trust.set_enabled(first, True)
    assert trust.enabled(first) is True
    assert trust.enabled(second) is False


@pytest.mark.parametrize("first,second", PAIRS)
def test_pending_secure_capture_is_not_shared(first, second):
    """A pending capture names the domain a password prompt will be shown for.
    A collider must neither see it nor be able to clear it."""
    from olympus import securecapture

    securecapture.request(first, "acme.test")
    assert securecapture.pending(first) == "acme.test"
    assert securecapture.pending(second) is None
    securecapture.clear(second)
    assert securecapture.pending(first) == "acme.test", "clear crossed owners"


@pytest.mark.parametrize("first,second", PAIRS)
def test_model_pin_is_not_shared(first, second):
    """A pin steers which model sees a principal's conversation."""
    from olympus import modelpin

    prefs.set(first, modelpin._PREF_KEY, "sonnet")
    assert prefs.get(first, modelpin._PREF_KEY) == "sonnet"
    assert prefs.get(second, modelpin._PREF_KEY) is None
    assert modelpin.get_pin(first) == "sonnet"
    assert modelpin.get_pin(second) is None


def test_installation_preferences_stay_at_their_documented_location():
    """`shared` carries the installation-wide daily budget cap and must keep
    working — and keep its exact path, which other code and operators know."""
    prefs.set("shared", "daily_budget", 12.5)
    assert prefs.get("shared", "daily_budget") == 12.5
    assert prefs._path("shared") == config.MEMORY_DIR / "prefs.json"
    assert (config.MEMORY_DIR / "prefs.json").is_file()
    # No tenant reaches it, including one that normalizes to "shared".
    for who in (A, B, "shared-x"):
        assert prefs.get(who, "daily_budget") is None


def test_a_reserved_namespace_other_than_shared_gets_its_own_file():
    prefs.set("operator", "thing", 1)
    assert prefs.get("operator", "thing") == 1
    assert prefs.get("shared", "thing") is None
    assert prefs._path("operator") != prefs._path("shared")


def test_preferences_are_outside_the_memory_export_and_delete_scope():
    """Security settings must not ride along in a memory export, nor be wiped
    by a whole-scope memory delete."""
    actions.set_autonomy(A, 4)
    path = prefs._path(A)
    roots = (memory._memory_roots(A) + memory._memory_roots("shared")
             + memory._memory_roots(all_users=True))
    for root in roots:
        assert root not in path.parents, f"prefs live inside export root {root}"


# --- pre-v2 preference files: quarantined ---------------------------------

def _plant_legacy_prefs(owner, data):
    d = config.MEMORY_DIR / "users" / memory.safe_id(owner)
    d.mkdir(parents=True, exist_ok=True)
    (d / "prefs.json").write_text(json.dumps(data), encoding="utf-8")
    return memory.safe_id(owner)


@pytest.mark.parametrize("first,second", PAIRS)
def test_a_pre_v5_preference_file_is_claimed_by_nobody(first, second):
    """Nobody inherits it — and nobody is WIDENED by its absence either.

    Ordinary defaults are NOT the fail-closed direction: they would turn a
    legacy `capability_profile="guest"` into "full". The whole collision group
    gets the restrictive quarantine posture instead — see the dedicated
    quarantine tests below.
    """
    key = _plant_legacy_prefs(first, {"autonomy": 4, "scopes": ["email"],
                                      "capability_profile": "full",
                                      "action_limits": {"send_email": 99}})

    for who in (first, second, key):
        assert actions.autonomy_level(who) != 4, f"{who!r} inherited autonomy"
        assert actions.granted_scopes(who) == set()
        assert prefs.get(who, "action_limits") == {}
        assert prefs.get(who, "capability_profile") == "guest"
        assert prefs.is_quarantined(who) is True

    assert prefs.legacy_owners() == [key]
    assert prefs.legacy_keys(key) == ["action_limits", "autonomy",
                                      "capability_profile", "scopes"]


def test_an_operator_can_explicitly_migrate_quarantined_preferences():
    key = _plant_legacy_prefs(A, {"autonomy": 4, "scopes": ["email"]})
    assert prefs.migrate_legacy(key, A) == 2
    assert actions.autonomy_level(A) == 4
    assert actions.granted_scopes(A) == {"email"}
    assert actions.autonomy_level(B) != 4
    assert prefs.legacy_owners() == []


def test_preference_migration_refuses_a_reserved_namespace():
    key = _plant_legacy_prefs(A, {"autonomy": 4})
    with pytest.raises(ValueError, match="reserved"):
        prefs.migrate_legacy(key, "shared")
    assert prefs.legacy_owners() == [key]


def test_an_operator_can_discard_quarantined_preferences():
    key = _plant_legacy_prefs(A, {"autonomy": 4})
    assert prefs.discard_legacy(key) is True
    assert prefs.legacy_owners() == []
    assert prefs.discard_legacy(key) is False


# =========================================================================
# 3. Legacy scheduler jobs
# =========================================================================

LEGACY_PROMPT = "A'S PRIVATE SCHEDULED PROMPT"


def _plant_legacy_job(owner, name="payroll", **extra):
    """A job persisted exactly as a pre-version build wrote it: no
    `owner_version` field at all, and `user` already normalized."""
    record = {
        "name": name, "interval": 3600, "prompt": LEGACY_PROMPT,
        "deliver_to": "", "user": memory.safe_id(owner), "enabled": True,
        "last_run": 0.0, "created": 1.0, "skill": "", "kind": "interval",
        "watch_pid": 0, "label": "", "started_at": 0.0,
        "resume_attempts": 0, "watch_cmd": "", "last_hash": "",
    }
    record.update(extra)
    path = config.MEMORY_DIR / "schedule.json"
    existing = (json.loads(path.read_text(encoding="utf-8"))
                if path.exists() else [])
    existing.append(record)
    path.write_text(json.dumps(existing), encoding="utf-8")
    return record


@pytest.mark.parametrize("first,second", PAIRS)
def test_a_pre_version_job_is_invisible_to_the_normalized_collider(first,
                                                                  second):
    """v4 returned it to whoever equalled the stored normalized owner, prompt
    and all."""
    _plant_legacy_job(first)

    for who in (first, second, memory.safe_id(first), "shared"):
        listed = scheduler.jobs(who)
        assert listed == [], f"{who!r} was shown a pre-version job: {listed}"


@pytest.mark.parametrize("first,second", PAIRS)
def test_a_pre_version_job_never_executes(first, second):
    """v4 ran it under the collider's identity and filed the answer in their
    private job_reports."""
    _plant_legacy_job(first)

    seen: list[tuple[str, str]] = []
    log = scheduler.run_due(now=1e12,
                            runner=lambda p, u: seen.append((p, u)) or "ANSWER")

    assert seen == [], f"a pre-version job executed: {seen}"
    assert log == []
    assert scheduler.due(1e12) == []
    assert scheduler.interrupted(1e12) == []
    for who in (first, second):
        assert LEGACY_PROMPT not in memory.search_for(who, "PRIVATE")
        assert "ANSWER" not in memory.search_for(who, "ANSWER")


@pytest.mark.parametrize("first,second", PAIRS)
def test_a_pre_version_job_is_not_mutable_by_a_tenant(first, second):
    _plant_legacy_job(first)

    for who in (first, second, memory.safe_id(first)):
        assert scheduler.remove("payroll", user=who) is False
        assert scheduler.set_enabled("payroll", False, user=who) is False

    still = scheduler.quarantined()
    assert [j.name for j in still] == ["payroll"]
    assert still[0].enabled is True, "a tenant disabled a quarantined job"


def test_an_interrupted_pre_version_job_is_not_resumed():
    """The resume path is a second way in, and it bypasses `due()`."""
    _plant_legacy_job(A, started_at=1e11, last_run=0.0, resume_attempts=0)
    seen: list[tuple[str, str]] = []
    scheduler.run_due(now=1e12,
                      runner=lambda p, u: seen.append((p, u)) or "ANSWER")
    assert seen == []


def test_a_pre_version_on_change_job_is_not_polled():
    """`changed()` runs a shell command from the record — a quarantined job
    must not even be observed."""
    _plant_legacy_job(A, kind="on_change", watch_cmd="echo hi", interval=0)
    assert scheduler.changed(1e12) == []
    assert scheduler.next_due_in(1e12) is None


def test_pre_version_jobs_stay_visible_to_operator_inspection():
    _plant_legacy_job(A)
    assert [j.name for j in scheduler.quarantined()] == ["payroll"]
    assert [j.name for j in scheduler.jobs()] == ["payroll"]
    assert scheduler.discard_quarantined() == 1
    assert scheduler.quarantined() == []
    assert scheduler.jobs() == []


@pytest.mark.parametrize("first,second", PAIRS)
def test_a_fresh_job_works_alongside_a_quarantined_one_of_the_same_name(first,
                                                                       second):
    """The quarantined record must neither be replaced by, nor block, a real
    job that a principal creates under the same name."""
    _plant_legacy_job(first)
    scheduler.add("payroll", 3600, "FRESH PROMPT", user=second)

    mine = scheduler.jobs(second)
    assert [(j.name, j.prompt) for j in mine] == [("payroll", "FRESH PROMPT")]
    assert mine[0].owner_version == scheduler.JOB_OWNER_SCHEMA_VERSION
    assert len(scheduler.quarantined()) == 1, "the legacy record was destroyed"

    seen: list[str] = []
    scheduler.run_due(now=1e12,
                      runner=lambda p, u: seen.append(p) or "FRESHANSWERMARK")
    assert seen == ["FRESH PROMPT"]
    assert "FRESHANSWERMARK" in memory.search_for(second, "FRESHANSWERMARK")
    assert "FRESHANSWERMARK" not in memory.search_for(first, "FRESHANSWERMARK")


def test_a_quarantined_job_does_not_consume_a_principals_quota(monkeypatch):
    """It is attributed to a normalized owner that is not a real principal, so
    counting it against that owner's cap would let a legacy file EVICT the live
    jobs of whoever shares the normalized name. Driven at the real cap."""
    monkeypatch.setattr(scheduler, "MAX_JOBS", 3)

    for i in range(3):
        _plant_legacy_job(A, name=f"legacy{i}")
    for i in range(3):
        scheduler.add(f"real{i}", 3600, "p", user=B, now=float(i))

    assert sorted(j.name for j in scheduler.jobs(B)) == ["real0", "real1",
                                                         "real2"], (
        "quarantined records evicted a live principal's jobs")
    assert len(scheduler.quarantined()) == 3, "legacy records were dropped"


def test_a_principals_own_quota_still_applies(monkeypatch):
    """The cap is not disabled by the quarantine carve-out."""
    monkeypatch.setattr(scheduler, "MAX_JOBS", 2)
    for i in range(4):
        scheduler.add(f"real{i}", 3600, "p", user=B, now=float(i))
    assert len(scheduler.jobs(B)) == 2


def test_the_web_agenda_is_owner_filtered_at_the_source():
    """`web._agenda_view` used to build its list from the operator-wide
    `scheduler.jobs()` and filter on `j.user`, which surfaced quarantined jobs —
    prompt included — to whichever principal equalled the normalized owner."""
    from olympus import web

    _plant_legacy_job(A)
    scheduler.add("mine", 3600, "MY PROMPT", user=B)
    scheduler.add("global", 3600, "SHARED PROMPT", user="shared")

    view = web._agenda_view(B)
    names = {t["name"] for t in view["tasks"]}
    prompts = {t["prompt"] for t in view["tasks"]}

    assert "payroll" not in names, "a quarantined job reached the web agenda"
    assert LEGACY_PROMPT not in prompts
    assert {"mine", "global"} <= names, names
    shared_view = web._agenda_view("shared")
    assert "payroll" not in {t["name"] for t in shared_view["tasks"]}


def test_a_quarantined_job_is_never_due_on_its_own():
    """The `Job.due` gate, asserted directly. `scheduler.due()` filters through
    `_runnable()` as well, so without this the two layers hide each other."""
    job = scheduler.Job(name="payroll", interval=1, prompt="p", user=B,
                        enabled=True, last_run=0.0)
    assert job.owner_version == 0
    assert scheduler.quarantined_job(job) is True
    assert job.due(1e12) is False

    fresh = scheduler.Job(name="payroll", interval=1, prompt="p", user=B,
                          enabled=True, last_run=0.0,
                          owner_version=scheduler.JOB_OWNER_SCHEMA_VERSION)
    assert scheduler.quarantined_job(fresh) is False
    assert fresh.due(1e12) is True


def test_an_unparseable_ownership_version_fails_closed():
    job = scheduler.Job(name="x", interval=1, prompt="p", user=B)
    job.owner_version = "not-a-number"
    assert scheduler.quarantined_job(job) is True
    assert job.due(1e12) is False


def test_the_run_path_reads_only_runnable_jobs():
    """Structural. `due`, `changed`, `interrupted` and `next_due_in` each decide
    whether a job executes; every one must filter quarantined records at the
    source rather than relying on `Job.due` alone."""
    import ast
    import inspect
    import pathlib

    must_filter = {"due", "changed", "interrupted", "next_due_in"}
    path = pathlib.Path(inspect.getfile(scheduler))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in must_filter:
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_load"):
                offenders.append(f"{fn.name}:{node.lineno}")
    assert not offenders, (
        "a run-path function reads unfiltered jobs at " + ", ".join(offenders)
        + " — use _runnable()")


# =========================================================================
# 4. Legacy actions (the v4 claim, corrected)
# =========================================================================

def _plant_legacy_action(owner, action_id="legacy01"):
    d = config.MEMORY_DIR / "actions" / memory.safe_id(owner)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{action_id}.json").write_text(json.dumps({
        "id": action_id, "user": memory.safe_id(owner), "type": "write_document",
        "title": "A'S PRIVATE TITLE",
        "payload": {"content": "A'S PRIVATE DOCUMENT BODY"},
        "risk_class": actions.NOTABLE, "reversible": True,
        "boundary_version": 3, "status": actions.PREPARED,
        "preview": "A'S PRIVATE PREVIEW", "why": "", "edited": False,
        "result": {}, "error": "", "created_at": 1.0,
        "approved_at": None, "executed_at": None,
    }), encoding="utf-8")
    return action_id


@pytest.mark.parametrize("first,second", PAIRS)
def test_no_tenant_reads_a_pre_v4_action(first, second):
    """The v4 rule — readable by a principal whose exact identity equals the
    stored normalized owner — was wrong. That equality is exactly what the
    collider has. An action's title, preview and payload are private, so
    refusing only to EXECUTE it is not enough."""
    _plant_legacy_action(first)

    for who in (first, second, memory.safe_id(first), "shared", "tg-other"):
        assert actions.get(who, "legacy01") is None
        assert actions.pending(who) == []
        assert actions.history(who) == []
        with pytest.raises(ValueError, match="no such action"):
            actions.approve(who, "legacy01")


def test_pre_v4_actions_remain_operator_visible():
    _plant_legacy_action(A)
    assert "legacy01" in {a.id for a in actions.pending_all()}
    found = actions.legacy_actions()
    assert [a.id for a in found] == ["legacy01"]
    assert found[0].preview == "A'S PRIVATE PREVIEW"
    assert actions.discard_legacy_actions() == 1
    assert actions.legacy_actions() == []


def test_fresh_actions_still_work_beside_a_legacy_record():
    _plant_legacy_action(A)
    actions.register(actions.ActionType(
        name="v5test", risk_class=actions.NOTABLE, scope="",
        preview=lambda p: "preview", execute=lambda p: {"ok": True},
        undo=lambda r: "undone", binds_user=True))
    try:
        a = actions.prepare(A, "v5test", {})
        b = actions.prepare(B, "v5test", {})
        assert [x.id for x in actions.pending(A)] == [a.id]
        assert [x.id for x in actions.pending(B)] == [b.id]
        assert actions.approve(A, a.id).status == actions.EXECUTED
        assert actions.get(B, a.id) is None
    finally:
        actions._REGISTRY.pop("v5test", None)


# =========================================================================
# Structural guards
# =========================================================================

def test_vault_and_prefs_never_key_storage_on_safe_id():
    """`safe_id` merges principals. Neither store may use it for a storage key
    again — the one permitted use is locating a QUARANTINED legacy record."""
    import ast
    import inspect
    import pathlib

    # The permitted uses are locating or detecting a QUARANTINED legacy record —
    # never deriving a live storage key.
    allowed = {"vault": {"legacy_tenants", "legacy_names", "migrate_legacy",
                         "discard_legacy", "_legacy_key_for"},
               "prefs": {"legacy_owners", "legacy_keys", "migrate_legacy",
                         "discard_legacy", "is_quarantined"}}
    offenders = []
    for mod in (vault, prefs):
        path = pathlib.Path(inspect.getfile(mod))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if fn.name in allowed[mod.__name__.rsplit(".", 1)[-1]]:
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "safe_id"):
                    offenders.append(f"{mod.__name__}.{fn.name}:{node.lineno}")
    assert not offenders, (
        "a credential/preference store keys on safe_id at "
        + ", ".join(offenders))


def test_per_owner_action_apis_do_not_reference_the_legacy_layout():
    """`_legacy_dirs` may be called only from operator-wide functions."""
    import ast
    import inspect
    import pathlib

    operator_only = {"pending_all", "legacy_actions", "discard_legacy_actions"}
    path = pathlib.Path(inspect.getfile(actions))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name in operator_only:
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("_legacy_dirs", "_legacy_dir")):
                offenders.append(f"{fn.name}:{node.lineno}")
    assert not offenders, (
        "a per-owner action API reads the pre-v4 layout at "
        + ", ".join(offenders))


def test_every_job_constructor_stamps_the_ownership_version():
    """A constructor that forgets it writes a job that is born quarantined —
    silently disabled — so this fails the build instead."""
    import ast
    import inspect
    import pathlib

    path = pathlib.Path(inspect.getfile(scheduler))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Job"):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue                  # Job(**d): deserializing a stored record
        if not any(kw.arg == "owner_version" for kw in node.keywords):
            missing.append(str(node.lineno))
    assert not missing, (
        "scheduler.Job(...) built without owner_version at line(s) "
        + ", ".join(missing))
