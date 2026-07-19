"""Channel pairing: one-time codes, expiry, single-use, unpair."""

import time

from olympus import pairing


def test_issue_and_redeem_pairs_sender():
    code = pairing.issue_code("telegram")
    assert len(code) == 8
    assert pairing.is_paired("telegram", 42) is False
    assert pairing.redeem("telegram", code, 42) is True
    assert pairing.is_paired("telegram", 42) is True
    assert pairing.paired("telegram") == ["42"]


def test_code_is_single_use():
    code = pairing.issue_code("telegram")
    assert pairing.redeem("telegram", code, 1) is True
    assert pairing.redeem("telegram", code, 2) is False
    assert pairing.is_paired("telegram", 2) is False


def test_wrong_and_empty_codes_fail():
    pairing.issue_code("telegram")
    assert pairing.redeem("telegram", "NOPE1234", 7) is False
    assert pairing.redeem("telegram", "", 7) is False
    assert pairing.redeem("telegram", "   ", 7) is False
    assert pairing.is_paired("telegram", 7) is False


def test_code_is_case_insensitive():
    code = pairing.issue_code("telegram")
    assert pairing.redeem("telegram", code.lower(), 9) is True


def test_expired_code_fails(monkeypatch):
    code = pairing.issue_code("telegram")
    real_time = time.time
    monkeypatch.setattr(time, "time",
                        lambda: real_time() + pairing.PAIR_TTL + 1)
    assert pairing.redeem("telegram", code, 5) is False


def test_channels_are_isolated():
    code = pairing.issue_code("telegram")
    assert pairing.redeem("discord", code, 3) is False
    assert pairing.redeem("telegram", code, 3) is True
    assert pairing.is_paired("discord", 3) is False


def test_unpair_revokes_access():
    code = pairing.issue_code("telegram")
    pairing.redeem("telegram", code, 42)
    assert pairing.unpair("telegram", 42) is True
    assert pairing.is_paired("telegram", 42) is False
    assert pairing.unpair("telegram", 42) is False


def test_codes_are_stored_hashed():
    code = pairing.issue_code("telegram")
    raw = pairing._path().read_text(encoding="utf-8")
    assert code not in raw
