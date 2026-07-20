"""Fail-closed production signing-seed guard (M2).

`OLYMPUS_ENV=production` must run on a configured, non-default signing seed —
the public default is forgeable, so a production instance on it would sign every
decision log / backup / attestation with a key anyone can reproduce. The boot
refuses (nonzero exit, actionable message) on the default seed in production; a
dev instance on the default seed boots but logs a one-line posture warning.
"""
import logging
import os
import subprocess
import sys

import pytest

from olympus import witness


_SEED_ENVS = ("OLYMPUS_ENV", "OLYMPUS_SIGNING_SEED", "OLYMPUS_SIGNING_SEED_FILE")


def _clear(monkeypatch):
    for k in _SEED_ENVS:
        monkeypatch.delenv(k, raising=False)


# --- (a) production + default seed → refuse ------------------------------

def test_production_unset_seed_refuses(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    with pytest.raises(witness.WitnessError, match="refusing to boot"):
        witness.require_production_seed()


def test_production_explicit_dev_seed_refuses(monkeypatch):
    # Setting the seed to the SHIPPED dev seed is still the default → refuse.
    _clear(monkeypatch)
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", witness._DEFAULT_SEED)
    with pytest.raises(witness.WitnessError, match="PUBLIC DEFAULT"):
        witness.require_production_seed()


# --- (b) production + distinct seed → boots ------------------------------

def test_production_distinct_seed_boots(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED",
                       "a-long-distinct-production-signing-secret-9f8e7d6c")
    witness.require_production_seed()          # must not raise


# --- (c) dev + default seed → boots with a logged warning ----------------

def test_dev_default_seed_boots_with_warning(monkeypatch, caplog):
    _clear(monkeypatch)                        # OLYMPUS_ENV unset → not production
    with caplog.at_level(logging.WARNING, logger="olympus.witness"):
        witness.require_production_seed()      # must not raise
    assert any("DEV" in r.message and "default seed" in r.message
               for r in caplog.records), "expected a dev-posture warning"


# --- real boot: nonzero exit / clean boot via the CLI --------------------

def _boot(extra_env: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _SEED_ENVS}
    env["OLYMPUS_MODEL"] = "claude-opus-4-8"
    env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "olympus", "version"],
                          capture_output=True, text=True, env=env, timeout=90)


def test_boot_production_default_seed_exits_nonzero():
    r = _boot({"OLYMPUS_ENV": "production"})
    assert r.returncode != 0, f"expected nonzero exit, got 0\n{r.stdout}\n{r.stderr}"
    blob = (r.stdout + r.stderr).lower()
    assert "production" in blob and "refusing to boot" in blob


def test_boot_production_distinct_seed_exits_zero():
    r = _boot({"OLYMPUS_ENV": "production",
               "OLYMPUS_SIGNING_SEED": "prod-secret-distinct-abc123xyz-boot"})
    assert r.returncode == 0, f"expected clean boot\n{r.stdout}\n{r.stderr}"
