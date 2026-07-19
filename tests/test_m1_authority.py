"""Milestone 1.1 — witness-signed contract artifacts on ABC (Plane 1 authority).

The behavioral-contract evaluator (C = Preconditions/Invariants/Governance/
Recovery) already exists; this pins the M1.1 delta:

  * a contract set can be SEALED into a witness-signed artifact; any tamper
    breaks the signature and the artifact is REFUSED (never applied);
  * an operator override is fail-closed and TIGHTEN-ONLY — it may add contracts
    or make a built-in stricter, never weaken a hard-stop;
  * the §C-12 hard-stops (cmdguard DENY, SSRF/egress) are declared invariants
    that BLOCK and trigger Recovery.
"""

import json

import pytest

from olympus import behavioral_contracts as abc, config, witness

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")

_CONTRACT = {"operation": "test.op", "recovery": "block",
             "preconditions": [], "invariants": ["risk_class_known"],
             "governance": []}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


# --- §C-12 hard-stop coverage: block + Recovery ---------------------------

@pytest.mark.parametrize("cmd", [
    ":(){ :|:& };:",                       # fork bomb
    "mkfs.ext4 /dev/sda",                   # disk wipe
    "rm -rf /",                             # recursive root delete
])
def test_denied_command_blocks_and_recovers(cmd):
    v = abc.check("command.run", {"command": cmd})
    assert v.blocking and v.recovery == abc.BLOCK and v.contract == "shell_command"


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata
    "http://127.0.0.1/",                             # loopback literal
    "http://localhost/admin",                        # loopback by name
    "http://10.0.0.1/",                              # private
])
def test_ssrf_url_blocks_and_recovers(url):
    v = abc.check("net.fetch", {"url": url})
    assert v.blocking and v.recovery == abc.BLOCK and v.contract == "network_egress"


def test_benign_command_and_url_pass():
    assert abc.check("command.run", {"command": "ls -la"}).ok
    assert abc.check("net.fetch", {"url": "http://93.184.216.34/"}).ok


def test_enforce_raises_contract_violation_on_hard_stop(monkeypatch):
    monkeypatch.delenv("OLYMPUS_ABC", raising=False)
    with pytest.raises(abc.ContractViolation) as ei:
        abc.enforce("command.run", {"command": "poweroff"})
    assert ei.value.recovery == abc.BLOCK


# --- signed artifacts: tamper refuses to load -----------------------------

@_signing
def test_seal_open_round_trip():
    art = abc.seal({"c": dict(_CONTRACT)})
    assert art["schema"] == abc.SEAL_SCHEMA
    assert abc.open_sealed(art) == {"c": _CONTRACT}


@_signing
def test_tampered_artifact_refuses_to_load():
    art = abc.seal({"c": dict(_CONTRACT)})
    art["contracts"]["c"]["recovery"] = "degrade"       # forge a weaker recovery
    with pytest.raises(abc.ContractSealError):
        abc.open_sealed(art)


@_signing
def test_unsigned_artifact_refuses():
    art = abc.seal({"c": dict(_CONTRACT)})
    art.pop("integrity")
    with pytest.raises(abc.ContractSealError):
        abc.open_sealed(art)


@_signing
def test_wrong_pin_refuses():
    art = abc.seal({"c": dict(_CONTRACT)})
    with pytest.raises(abc.ContractSealError):
        abc.open_sealed(art, pin="00" * 32)
    assert abc.open_sealed(art, pin=witness.public_key_hex()) == {"c": _CONTRACT}


@_signing
def test_default_seed_seal_is_unattested_configured_is_attested(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    assert abc.sealed_is_attested(abc.seal({"c": dict(_CONTRACT)})) is False
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m1-secret-seed")
    assert abc.sealed_is_attested(abc.seal({"c": dict(_CONTRACT)})) is True


# --- tighten-only ----------------------------------------------------------

def test_tighten_only_allows_add_and_stricter():
    base = abc._DEFAULT_CONTRACTS
    ok, _ = abc.tighten_only_ok(base, {"brand_new": dict(_CONTRACT)})
    assert ok
    # specialist_output is recovery 'reject'; tightening to 'block' is allowed
    ov = {**base["specialist_output"], "recovery": "block"}
    ok, _ = abc.tighten_only_ok(base, {"specialist_output": ov})
    assert ok


def test_tighten_only_rejects_weaker_recovery():
    base = abc._DEFAULT_CONTRACTS
    ov = {**base["shell_command"], "recovery": "degrade"}       # block -> degrade
    ok, reason = abc.tighten_only_ok(base, {"shell_command": ov})
    assert not ok and "weakens recovery" in reason


def test_tighten_only_rejects_dropped_clause():
    base = abc._DEFAULT_CONTRACTS
    ov = {**base["shell_command"], "invariants": []}            # drop command_not_denied
    ok, reason = abc.tighten_only_ok(base, {"shell_command": ov})
    assert not ok and "drops" in reason


# --- operator-override path is fail-closed --------------------------------

@_signing
def test_sealed_override_adds_a_contract():
    art = abc.seal({"custom_rule": dict(_CONTRACT)})
    (config.MEMORY_DIR / "behavioral_contracts.sealed.json").write_text(
        json.dumps(art), encoding="utf-8")
    assert "custom_rule" in abc.load()


@_signing
def test_tampered_sealed_override_is_refused_builtin_stands():
    art = abc.seal({"shell_command": {**abc._DEFAULT_CONTRACTS["shell_command"]}})
    art["contracts"]["shell_command"]["recovery"] = "degrade"   # tamper post-sign
    (config.MEMORY_DIR / "behavioral_contracts.sealed.json").write_text(
        json.dumps(art), encoding="utf-8")
    reg = abc.load()                        # tampered artifact refused, not applied
    assert reg["shell_command"].recovery == abc.BLOCK


@_signing
def test_signed_but_loosening_override_is_refused():
    # A validly-signed override that WEAKENS a built-in is still refused (tighten-only).
    weak = {**abc._DEFAULT_CONTRACTS["shell_command"], "recovery": "degrade"}
    art = abc.seal({"shell_command": weak})
    (config.MEMORY_DIR / "behavioral_contracts.sealed.json").write_text(
        json.dumps(art), encoding="utf-8")
    assert abc.load()["shell_command"].recovery == abc.BLOCK
