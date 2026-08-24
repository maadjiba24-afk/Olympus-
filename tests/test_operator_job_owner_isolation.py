"""P1: a standing operator job's name is unique only inside its owner.

`operator.run_due` was already owner-bound — it gates each job on its stored
`user` and executes as that user. Replacement was not: `schedule()` dropped
every job matching the bare `name`, so a job called "daily" was one global slot
any tenant could take. Scheduling over it silently deleted another owner's
automation, raising no error and leaving nothing in that owner's own view
(`status_summary` is per-user) to show it had gone.

The adversary here is user B: a legitimate, fully onboarded second tenant with
their own operator gates, not an injected page. B needs no access to A's domain
and no knowledge of A's setup — only a common job name.
"""

import json

import pytest

from olympus import actions, browser, config, memory, operator

A = "tg-alice"
B = "tg-bob"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    browser.set_transport_factory(None)
    memory.set_user("cli")
    yield
    memory.set_user("shared")
    browser.set_transport_factory(None)


def _authorize(monkeypatch, *users, domain="shop.com"):
    """Turn the operator on for each user and authorize `domain` for them.
    Deliberately per-user: it is what makes the cross-tenant case realistic —
    B arms only their OWN gates and still reached A's job before this fix."""
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    monkeypatch.delenv("OLYMPUS_OPERATOR_DOMAINS", raising=False)
    for user in users:
        operator.authorize_site(user, domain, "manual")


def _stored() -> list[dict]:
    """The persisted job list, read straight off disk — never the return value
    of `schedule()`. The defect was in what survived the write."""
    path = config.MEMORY_DIR / "operator_jobs.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _by(jobs, user):
    return [j for j in jobs if j.get("user") == user]


# --- same-owner semantics are preserved -----------------------------------

def test_same_owner_same_name_replaces_and_leaves_exactly_one():
    operator.schedule(A, "daily", "shop.com", "reorder", 3600, {"n": 1})
    operator.schedule(A, "daily", "shop.com", "reorder", 7200, {"n": 2})

    jobs = _stored()
    assert len(jobs) == 1
    assert jobs[0]["user"] == A and jobs[0]["name"] == "daily"
    assert jobs[0]["interval"] == 7200          # the newer one won
    assert jobs[0]["params"] == {"n": 2}


def test_same_owner_distinct_names_are_both_kept():
    operator.schedule(A, "daily", "shop.com", "reorder", 3600)
    operator.schedule(A, "weekly", "shop.com", "reorder", 604800)

    jobs = _stored()
    assert len(jobs) == 2
    assert {j["name"] for j in jobs} == {"daily", "weekly"}
    assert all(j["user"] == A for j in jobs)


# --- the P1 defect --------------------------------------------------------

def test_two_owners_may_hold_the_same_job_name():
    """The headline case. A name is unique per owner, not per installation."""
    operator.schedule(A, "daily", "shop.com", "reorder", 3600)
    operator.schedule(B, "daily", "other.com", "reorder", 7200)

    jobs = _stored()
    assert len(jobs) == 2, "one owner's job was destroyed by the other's"
    assert {(j["user"], j["name"]) for j in jobs} == {(A, "daily"), (B, "daily")}


def test_b_scheduling_does_not_mutate_or_remove_a_job():
    """Byte-level: A's stored record must come back completely untouched —
    not merely present, but identical in every field."""
    operator.schedule(A, "daily", "shop.com", "reorder", 3600, {"cart": "A"})
    before = json.dumps(_by(_stored(), A), sort_keys=True)

    operator.schedule(B, "daily", "other.com", "checkout", 900, {"cart": "B"})

    after = json.dumps(_by(_stored(), A), sort_keys=True)
    assert after == before, "scheduling as B altered A's stored job"


def test_both_same_named_jobs_keep_their_durable_owner_and_payload():
    operator.schedule(A, "daily", "shop.com", "reorder", 3600, {"cart": "A"})
    operator.schedule(B, "daily", "other.com", "checkout", 900, {"cart": "B"})

    jobs = {j["user"]: j for j in _stored()}
    assert jobs[A]["domain"] == "shop.com" and jobs[A]["template"] == "reorder"
    assert jobs[A]["params"] == {"cart": "A"} and jobs[A]["interval"] == 3600
    assert jobs[B]["domain"] == "other.com" and jobs[B]["template"] == "checkout"
    assert jobs[B]["params"] == {"cart": "B"}
    assert jobs[B]["interval"] == 900          # above the 300s floor, unchanged


def test_a_third_owner_does_not_disturb_the_first_two():
    for user in (A, B, "tg-carol"):
        operator.schedule(user, "daily", "shop.com", "reorder", 3600)
    jobs = _stored()
    assert len(jobs) == 3
    assert len({j["user"] for j in jobs}) == 3


def test_replacement_is_scoped_when_many_owners_share_the_name():
    """Re-scheduling as A replaces exactly A's row and nothing else."""
    for user in (A, B, "tg-carol"):
        operator.schedule(user, "daily", "shop.com", "reorder", 3600)
    operator.schedule(A, "daily", "shop.com", "reorder", 1800)

    jobs = _stored()
    assert len(jobs) == 3
    by_user = {j["user"]: j for j in jobs}
    assert by_user[A]["interval"] == 1800          # replaced
    assert by_user[B]["interval"] == 3600          # untouched
    assert by_user["tg-carol"]["interval"] == 3600  # untouched


# --- owner equality must be EXACT, never `safe_id` ------------------------
#
# `memory.safe_id` replaces every run of non-[A-Za-z0-9_-] with a single "-"
# and truncates at 64 characters. It is right for building a filesystem path
# and wrong for deciding whether two principals are the same person: it maps
# genuinely distinct identities onto one string. PR #282 established this for
# the browser lease (`browserlease.canonical` strips whitespace and nothing
# else, and documents why). The job key must hold the same line — otherwise a
# name-collision defect is merely traded for an identity-collision one.

_SAFE_ID_COLLIDING = ["tg-a.b", "tg-a-b", "tg-a@b", "tg-a b"]


def test_safe_id_really_does_collide_these_principals():
    """The adversarial premise, asserted rather than assumed. If `safe_id` ever
    stopped collapsing these, the tests below would silently stop testing
    anything."""
    keys = {memory.safe_id(u) for u in _SAFE_ID_COLLIDING}
    assert keys == {"tg-a-b"}, (
        "safe_id no longer collides these; the premise of the tests below is "
        f"stale: {keys}")


def test_safe_id_colliding_principals_keep_separate_jobs():
    """Four DISTINCT principals, one job name. `safe_id` maps all four onto
    "tg-a-b", so a key built on it would leave exactly one job standing and
    three tenants silently destroyed."""
    for i, user in enumerate(_SAFE_ID_COLLIDING):
        operator.schedule(user, "daily", "shop.com", "reorder", 3600 + i)

    jobs = _stored()
    assert len(jobs) == len(_SAFE_ID_COLLIDING), (
        "principals that merely COLLIDE under safe_id were treated as one "
        f"owner: {[j['user'] for j in jobs]}")
    assert {j["user"] for j in jobs} == set(_SAFE_ID_COLLIDING)
    # Each kept its own payload — not just its own row.
    by_user = {j["user"]: j for j in jobs}
    for i, user in enumerate(_SAFE_ID_COLLIDING):
        assert by_user[user]["interval"] == 3600 + i


def test_safe_id_colliding_principals_do_not_replace_each_other():
    """Re-scheduling as one of them must displace only that exact owner."""
    for user in _SAFE_ID_COLLIDING:
        operator.schedule(user, "daily", "shop.com", "reorder", 3600)
    operator.schedule("tg-a.b", "daily", "shop.com", "reorder", 1800)

    jobs = {j["user"]: j for j in _stored()}
    assert len(jobs) == len(_SAFE_ID_COLLIDING)
    assert jobs["tg-a.b"]["interval"] == 1800            # replaced
    for other in ("tg-a-b", "tg-a@b", "tg-a b"):
        assert jobs[other]["interval"] == 3600, f"{other} was disturbed"


def test_long_principals_sharing_a_64_char_safe_id_prefix_stay_distinct():
    """`safe_id` truncates at 64 characters, so two long principals that agree
    on their first 64 sanitized characters become one key. Chat uids are
    platform-supplied and can be long; this must not merge two accounts."""
    shared_prefix = "tg-" + ("x" * 70)
    long_a = shared_prefix + "AAA"
    long_b = shared_prefix + "BBB"

    # The adversarial premise: identical under safe_id, distinct in truth.
    assert memory.safe_id(long_a) == memory.safe_id(long_b)
    assert len(memory.safe_id(long_a)) == 64
    assert long_a != long_b

    operator.schedule(long_a, "daily", "shop.com", "reorder", 3600)
    operator.schedule(long_b, "daily", "shop.com", "reorder", 7200)

    jobs = _stored()
    assert len(jobs) == 2, (
        "two principals sharing a 64-char safe_id prefix were merged into one "
        "job owner")
    by_user = {j["user"]: j for j in jobs}
    assert by_user[long_a]["interval"] == 3600
    assert by_user[long_b]["interval"] == 7200


def test_same_exact_owner_still_replaces_and_a_different_one_does_not():
    """The other half of exact equality: it must not become so strict that a
    genuine re-schedule stops replacing."""
    operator.schedule("tg-a.b", "daily", "shop.com", "reorder", 3600)
    operator.schedule("tg-a.b", "daily", "shop.com", "reorder", 7200)
    assert len(_stored()) == 1
    assert _stored()[0]["interval"] == 7200

    operator.schedule("tg-zzz", "daily", "shop.com", "reorder", 3600)
    assert len(_stored()) == 2


def test_job_key_helpers_do_not_use_safe_id():
    """Structural: the identity helpers must not reach for `safe_id` again.

    A future edit that "tidies" this back into `safe_id` would silently
    reintroduce cross-principal merging, and the behavioural tests above would
    be the only thing catching it. This catches it at the source.
    """
    import inspect
    for fn in (operator._job_principal, operator._job_key):
        src = inspect.getsource(fn)
        code = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith("#"))
        # Strip the docstring so prose explaining WHY safe_id is wrong is fine.
        doc = inspect.getdoc(fn) or ""
        for line in doc.splitlines():
            code = code.replace(line, "")
        assert "safe_id" not in code, (
            f"{fn.__name__} uses safe_id for identity equality; it is lossy "
            "(punctuation collapse + 64-char truncation) and merges distinct "
            "principals")


# --- persistence ----------------------------------------------------------

def test_jobs_survive_reload_with_owner_identity_intact():
    operator.schedule(A, "daily", "shop.com", "reorder", 3600)
    operator.schedule(B, "daily", "other.com", "reorder", 7200)

    reloaded = operator._load_jobs()               # fresh read off disk
    assert len(reloaded) == 2
    # The key is the EXACT stored principal — spelled literally here rather
    # than via safe_id, which would pass by coincidence for these two ids and
    # quietly re-assert the identity model this patch removed.
    assert {operator._job_key(j) for j in reloaded} == {
        ("tg-alice", "daily"), ("tg-bob", "daily")}

    # And a post-reload replacement is still owner-scoped.
    operator.schedule(B, "daily", "other.com", "reorder", 1800)
    jobs = {j["user"]: j for j in _stored()}
    assert len(jobs) == 2
    assert jobs[A]["interval"] == 3600
    assert jobs[B]["interval"] == 1800


def test_storage_format_is_unchanged():
    """The record shape is load-bearing for the heartbeat and for any job
    written before this change — the fix must not migrate or reshape it."""
    operator.schedule(A, "daily", "shop.com", "reorder", 3600, {"k": "v"})
    job = _stored()[0]
    assert set(job) == {"name", "user", "domain", "template", "params",
                        "interval", "last_run", "enabled"}
    assert job["last_run"] == 0.0 and job["enabled"] is True


def test_legacy_job_without_a_user_field_is_addressable():
    """A record written before jobs carried an owner normalizes to the shared
    principal rather than crashing the filter or silently matching everyone."""
    path = config.MEMORY_DIR / "operator_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {"name": "daily", "domain": "shop.com", "template": "reorder",
         "params": {}, "interval": 3600, "last_run": 0.0, "enabled": True}
    ]), encoding="utf-8")

    assert operator._job_key(operator._load_jobs()[0]) == ("shared", "daily")

    # A named owner does not displace it...
    operator.schedule(A, "daily", "shop.com", "reorder", 3600)
    assert len(_stored()) == 2
    # ...but the shared principal does.
    operator.schedule("shared", "daily", "shop.com", "reorder", 1800)
    assert len(_stored()) == 2


# --- execution stays owner-bound ------------------------------------------

def test_run_due_executes_each_job_under_its_own_stored_owner(monkeypatch):
    _authorize(monkeypatch, A, B)
    browser.record_profile("shop.com", login_url="https://shop.com/login")
    browser.set_template("shop.com", "reorder", "notable",
                         [{"op": "click", "selector": "#buy"}])

    operator.schedule(A, "daily", "shop.com", "reorder", 300, {"cart": "A"})
    operator.schedule(B, "daily", "shop.com", "reorder", 300, {"cart": "B"})

    seen = []
    monkeypatch.setattr(
        operator, "run",
        lambda user, domain, template, params: seen.append(
            (user, params.get("cart")))
        or actions.Action(id="x", user=user, type="browser_operate",
                          title="t", payload={}, risk_class=actions.NOTABLE,
                          reversible=True, status=actions.EXECUTED))

    lines = operator.run_due(now=10_000.0)

    assert len(seen) == 2, lines
    assert set(seen) == {(A, "A"), (B, "B")}, \
        "a job ran under the wrong owner or with another owner's payload"


def test_run_due_still_enforces_the_operator_and_authorization_gates(
        monkeypatch):
    """The gates are unchanged: a job whose owner never authorized the domain
    is skipped, and one whose owner did runs — evaluated per job, per owner."""
    _authorize(monkeypatch, A)                     # B is NOT set up
    operator.schedule(A, "daily", "shop.com", "reorder", 300)
    operator.schedule(B, "daily", "shop.com", "reorder", 300)

    ran = []
    monkeypatch.setattr(
        operator, "run",
        lambda user, domain, template, params: ran.append(user)
        or actions.Action(id="x", user=user, type="browser_operate",
                          title="t", payload={}, risk_class=actions.NOTABLE,
                          reversible=True, status=actions.EXECUTED))

    operator.run_due(now=10_000.0)

    assert ran == [A], "the operator/authorization gate stopped applying"


def test_b_cannot_reach_a_job_through_the_tool_layer(monkeypatch):
    """End to end through the LLM-callable tool, where `name` is entirely
    caller-chosen and the owner comes from the authenticated context."""
    from olympus import tools
    _authorize(monkeypatch, A, B, domain="shop.com")
    browser.set_template("shop.com", "reorder", "notable",
                         [{"op": "click", "selector": "#buy"}])

    memory.set_user(A)
    tools._operator_schedule("daily", "shop.com", "reorder", "1h")
    memory.set_user(B)
    tools._operator_schedule("daily", "shop.com", "reorder", "2h")

    jobs = _stored()
    assert len(jobs) == 2
    assert {j["user"] for j in jobs} == {A, B}
