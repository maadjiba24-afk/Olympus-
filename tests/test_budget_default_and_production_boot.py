"""P0-7 + P0-8 — the two unsafe defaults the technical audit found.

Both are about what happens when an operator configures NOTHING:

* **P0-7** `OLYMPUS_DAILY_BUDGET` used to default to `"0"`, and `0` means
  UNLIMITED here — so a fresh install had no spend ceiling at all while the
  heartbeat spends on a cadence with no human watching. Unset must now mean
  BOUNDED, and removing the ceiling must be a deliberate act. The same line also
  used to raise `ValueError` at module scope on a typo, making `import
  olympus.config` — and therefore the whole package — fail.
* **P0-8** the fail-closed boot checklist (bounded spend, credential when bound
  off-loopback, finite retention) ran only under `OLYMPUS_ENV=staging`.
  Production got only the signing-seed invariant, so it could boot wide open on
  all three.

Everything here is offline and reads/probes config only.
"""

from __future__ import annotations

import importlib

import pytest

from olympus import config


# --------------------------------------------------------------------------
# P0-7 — an unset budget is bounded; unlimited is opt-in; typos fail safe
# --------------------------------------------------------------------------

def test_unset_budget_resolves_to_a_bounded_default():
    """The whole point: no configuration ⇒ a real ceiling, not unlimited."""
    assert config.resolve_daily_budget(None) == config.DEFAULT_DAILY_BUDGET
    assert config.resolve_daily_budget("") == config.DEFAULT_DAILY_BUDGET
    assert config.resolve_daily_budget("   ") == config.DEFAULT_DAILY_BUDGET
    assert config.DEFAULT_DAILY_BUDGET > 0


@pytest.mark.parametrize("raw", ["0", "0.0", "unlimited", "none", "off",
                                 "UNLIMITED", " Off "])
def test_unlimited_is_explicit_opt_in(raw):
    """`0` keeps meaning unlimited (an operator who typed it chose it), and the
    words are accepted because "0 means unlimited" reads like "off"."""
    assert config.resolve_daily_budget(raw) == 0.0


@pytest.mark.parametrize("raw,expected", [("5", 5.0), ("20.5", 20.5),
                                          ("0.25", 0.25)])
def test_a_positive_number_is_honoured(raw, expected):
    assert config.resolve_daily_budget(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "not-a-number", "5;rm -rf /", "-3",
                                 "-0.5", "1e", "$5"])
def test_malformed_or_negative_falls_back_to_the_bounded_default(raw):
    """Fails SAFE, two ways: it must not widen the ceiling to unlimited, and it
    must not raise — a typo in one env var used to make the package
    unimportable."""
    assert config.resolve_daily_budget(raw) == config.DEFAULT_DAILY_BUDGET


def test_a_malformed_budget_no_longer_breaks_the_import(monkeypatch):
    """Regression: `float("abc")` at module scope raised ValueError, so
    `OLYMPUS_DAILY_BUDGET=abc` made `import olympus.config` fail outright."""
    monkeypatch.setenv("OLYMPUS_DAILY_BUDGET", "abc")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.DAILY_BUDGET == reloaded.DEFAULT_DAILY_BUDGET
    finally:
        monkeypatch.delenv("OLYMPUS_DAILY_BUDGET", raising=False)
        importlib.reload(config)


def test_zero_still_disables_the_cap_for_the_consumer(monkeypatch):
    """`usage.daily_budget()` treats <=0 as "no cap"; that contract is
    unchanged, so an operator who set 0 keeps the behaviour they chose."""
    from olympus import prefs, usage
    monkeypatch.setattr(prefs, "get", lambda *a, **k: None)
    monkeypatch.setattr(config, "DAILY_BUDGET", config.resolve_daily_budget("0"))
    assert usage.daily_budget() == 0.0
    monkeypatch.setattr(config, "DAILY_BUDGET",
                        config.resolve_daily_budget(None))
    assert usage.daily_budget() == config.DEFAULT_DAILY_BUDGET


# --------------------------------------------------------------------------
# P0-8 — production gets the same fail-closed checklist as staging
# --------------------------------------------------------------------------

@pytest.fixture
def prod_env(monkeypatch, tmp_path):
    """A COMPLETE production profile. Each test then breaks exactly one thing."""
    mem = tmp_path / "prodmem"
    mem.mkdir()
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    monkeypatch.setattr(config, "MEMORY_DIR", mem)
    monkeypatch.setattr(config, "RETAIN_DAYS", 30)
    monkeypatch.delenv("OLYMPUS_DAILY_BUDGET", raising=False)
    for k in ("OLYMPUS_API_KEYS", "OLYMPUS_ACCESS_TOKEN",
              "OLYMPUS_REQUIRE_LOGIN", "OLYMPUS_BIND_HOST",
              "OLYMPUS_MEMORY_DIR"):
        monkeypatch.delenv(k, raising=False)
    return mem


def test_a_complete_production_profile_boots(prod_env):
    assert config.production_problems() == []
    config.require_production_config()          # must not raise


def test_production_refuses_an_explicitly_unlimited_budget(prod_env,
                                                           monkeypatch):
    """The actual hazard: someone wrote 0 (or "off") thinking it disabled the
    guard, and production then spends without a ceiling."""
    for raw in ("0", "0.0", "unlimited", "off", "none"):
        monkeypatch.setenv("OLYMPUS_DAILY_BUDGET", raw)
        problems = config.production_problems()
        assert any("UNLIMITED" in p for p in problems), raw
        with pytest.raises(config.ProductionConfigError):
            config.require_production_config()


def test_production_accepts_an_unset_budget_because_it_is_now_bounded(prod_env):
    """Unlike staging, production does not demand an explicit number — unset
    resolves to DEFAULT_DAILY_BUDGET, which is already a real ceiling."""
    assert not any("OLYMPUS_DAILY_BUDGET" in p
                   for p in config.production_problems())


def test_production_refuses_infinite_retention(prod_env, monkeypatch):
    monkeypatch.setattr(config, "RETAIN_DAYS", 0)
    assert any("OLYMPUS_RETAIN_DAYS" in p for p in config.production_problems())
    with pytest.raises(config.ProductionConfigError):
        config.require_production_config()


def test_production_refuses_off_loopback_without_a_credential(prod_env):
    problems = config.production_problems(bind_host="0.0.0.0")
    assert any("no credential configured" in p for p in problems)
    with pytest.raises(config.ProductionConfigError):
        config.require_production_config(bind_host="0.0.0.0")


@pytest.mark.parametrize("var", ["OLYMPUS_ACCESS_TOKEN", "OLYMPUS_REQUIRE_LOGIN"])
def test_a_credential_permits_off_loopback_binding(prod_env, monkeypatch, var):
    monkeypatch.setenv(var, "x")
    assert config.production_problems(bind_host="0.0.0.0") == []


def test_production_refuses_an_unwritable_memory_dir(prod_env, monkeypatch):
    """The failure that leaves a process happily alive while dropping every
    journal append. Probed by writing, because os.access(W_OK) lies on some
    volume setups."""
    ro = prod_env / "readonly"
    ro.mkdir()
    ro.chmod(0o555)
    monkeypatch.setattr(config, "MEMORY_DIR", ro)
    try:
        if config._writable_dir(ro):
            pytest.skip("running as root — chmod cannot make a dir unwritable")
        assert any("not writable" in p for p in config.production_problems())
        with pytest.raises(config.ProductionConfigError):
            config.require_production_config()
    finally:
        ro.chmod(0o755)


def test_production_does_not_require_an_explicit_memory_dir(prod_env):
    """Relaxed on purpose: the shipped Dockerfile declares `VOLUME
    /app/memory` and compose mounts the named volume at that DEFAULT path, so
    demanding the env var would refuse the project's own documented deploy."""
    assert not any("OLYMPUS_MEMORY_DIR is unset" in p
                   for p in config.production_problems())


# --------------------------------------------------------------------------
# Isolation — other modes must be byte-identically unaffected
# --------------------------------------------------------------------------

def test_dev_and_staging_boots_are_unaffected_by_the_production_gate(
        monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_ENV", raising=False)
    assert config.production_problems() == []       # dev: no-op
    config.require_production_config()
    monkeypatch.setenv("OLYMPUS_ENV", "staging")
    assert config.production_problems() == []       # staging: not our gate
    config.require_production_config()


def test_staging_error_is_still_its_own_type_and_names_its_mode():
    """`StagingConfigError` is what cli.py and existing tests catch; it must
    keep working while sharing the new base."""
    assert issubclass(config.StagingConfigError, config.DeploymentConfigError)
    assert issubclass(config.ProductionConfigError, config.DeploymentConfigError)
    assert "staging configuration is incomplete" in str(
        config.StagingConfigError(["x"]))
    assert "production configuration is incomplete" in str(
        config.ProductionConfigError(["x"]))
