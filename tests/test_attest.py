"""The Attested Human Handoff moat: a human-verification checkpoint becomes a
CRYPTOGRAPHICALLY SIGNED attestation, verifiable and tamper-evident.
"""

import pytest

from olympus import attest, config, witness

pytestmark = pytest.mark.skipif(not witness.available(),
                                reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


def test_attestation_is_signed_and_verifies():
    rec = attest.attest("captcha", "shop.com")
    assert rec["kind"] == "captcha" and rec["domain"] == "shop.com"
    assert rec["id"].startswith("att_")
    assert rec["integrity"]["publicKey"] == witness.public_key_hex()
    assert attest.verify_attestation(rec) is True


def test_tampering_breaks_the_signature():
    rec = attest.attest("otp", "bank.com")
    rec["domain"] = "evil.com"                      # forge the attested domain
    assert attest.verify_attestation(rec) is False
    rec2 = attest.attest("otp", "bank.com")
    rec2["kind"] = "captcha"                         # forge the check kind
    assert attest.verify_attestation(rec2) is False


def test_pin_binds_to_the_expected_key():
    rec = attest.attest("step_up", "acme.com")
    assert attest.verify_attestation(rec, pin=witness.public_key_hex()) is True
    assert attest.verify_attestation(rec, pin="00" * 32) is False   # wrong signer


def test_unknown_kind_is_normalized():
    rec = attest.attest("ignore-all-instructions", "x.com")
    assert rec["kind"] == "step_up"                  # bounded enum, no injection


def test_record_and_list_and_latest_roundtrip():
    attest.attest_and_record("captcha", "shop.com")
    attest.attest_and_record("otp", "shop.com")
    attest.attest_and_record("captcha", "other.com")
    shop = attest.list_attestations("shop.com")
    assert [r["kind"] for r in shop] == ["captcha", "otp"]      # newest last
    latest = attest.latest_attestation("shop.com")
    assert latest and latest["kind"] == "otp" and attest.verify_attestation(latest)
    assert attest.latest_attestation("never.com") is None


def test_summary_reports_validity():
    attest.attest_and_record("captcha", "shop.com")
    out = attest.summary("shop.com")
    assert "shop.com" in out and "captcha" in out and "valid" in out


def test_burden_tracking_drives_the_evolution_report():
    for _ in range(3):
        attest.attest_and_record("captcha", "heavy.com")
    attest.attest_and_record("otp", "light.com")
    burden = attest.burden_by_domain()
    assert burden["heavy.com"] == 3 and burden["light.com"] == 1
    report = attest.evolution_report()
    # the heaviest site is flagged to save its session (cut repeat checks)
    assert "heavy.com" in report and "save this session" in report.lower()


def test_receipt_export_and_third_party_verify():
    rec = attest.attest("captcha", "bank.com")
    receipt = attest.export_receipt(rec)
    assert "OLYMPUS ATTESTATION" in receipt and "captcha" in receipt
    # a third party verifies the signature, and binds trust to the expected key
    v = attest.verify_receipt(receipt)
    assert v["ok"] and v["kind"] == "captcha" and v["domain"] == "bank.com"
    trusted = attest.verify_receipt(receipt, pin=witness.public_key_hex())
    assert trusted["ok"] and trusted["trusted"] is True
    wrong = attest.verify_receipt(receipt, pin="00" * 32)
    assert wrong["ok"] and wrong["trusted"] is False       # valid but untrusted


def test_tampered_receipt_fails_verification():
    rec = attest.attest("otp", "shop.com")
    receipt = attest.export_receipt(rec).replace("shop.com", "evil.com")
    v = attest.verify_receipt(receipt)
    assert v["ok"] is False and any("INVALID" in p for p in v["problems"])
    assert attest.verify_receipt("not a receipt at all")["ok"] is False


def test_malformed_ledger_lines_are_skipped():
    attest.attest_and_record("captcha", "shop.com")
    path = attest._path()
    path.write_text(path.read_text(encoding="utf-8") + '{"not":"an attestation"}\n'
                    + "not json at all\n", encoding="utf-8")
    recs = attest.list_attestations()
    assert len(recs) == 1 and recs[0]["kind"] == "captcha"
