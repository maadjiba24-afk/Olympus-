"""Actual E19 card consumers and E31 HTTP behavior, using owned local state."""

import io
import json
from pathlib import Path

import pytest

from olympus import config, metrics, store, usage, usermem, web


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "state")
    store.reset()
    metrics.reset()
    yield
    store.reset()
    metrics.reset()


def request(path, headers=""):
    """Exercise HTTP parsing, dispatch and serialization without an IP socket."""
    class Connection:
        def __init__(self):
            self.response = bytearray()

        def makefile(self, *args, **kwargs):
            return io.BytesIO(
                f"GET {path} HTTP/1.0\r\nHost: localhost\r\n{headers}\r\n".encode())

        def sendall(self, data):
            self.response.extend(data)

    connection = Connection()
    web.Handler(connection, ("127.0.0.1", 12345), object())
    headers, body = bytes(connection.response).split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), json.loads(body)


def test_healthz_serves_when_storage_and_spend_are_unavailable(monkeypatch):
    # Count attempted calls as well as raising: the old metrics snapshot
    # swallowed budget errors, so checking status 200 alone missed the defect.
    attempted = []

    def unavailable(*args, **kwargs):
        attempted.append(True)
        raise OSError("owned fixture: state disk unavailable")

    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "owned-test-token")
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "1")
    for name in ("budget_status", "today_spend"):
        monkeypatch.setattr(usage, name, unavailable)
    for name in ("read_bytes", "read_text", "write_bytes", "write_text", "mkdir"):
        monkeypatch.setattr(Path, name, unavailable)
    monkeypatch.setattr(config, "_writable_dir", unavailable)
    monkeypatch.setattr(config, "production_problems", unavailable)

    for _ in range(3):
        code, body = request("/healthz")
        assert code == 200
        assert set(body) == {"status", "uptime_seconds"}
        assert body["status"] == "ok"
        assert body["uptime_seconds"] >= 0
    assert attempted == []


def test_readiness_still_checks_storage_without_spend_reads(monkeypatch):
    checked = []
    attempted_spend = []

    def unwritable(path):
        checked.append(path)
        return False

    def unavailable_spend():
        attempted_spend.append(True)
        raise OSError("owned fixture: ledger unavailable")

    monkeypatch.setenv("OLYMPUS_ENV", "development")
    monkeypatch.setattr(config, "_writable_dir", unwritable)
    monkeypatch.setattr(usage, "budget_status", unavailable_spend)
    monkeypatch.setattr(usage, "today_spend", unavailable_spend)
    code, body = request("/readyz")
    assert code == 503
    assert body["status"] == "not_ready"
    assert body["memory_dir_writable"] is False
    assert checked and set(checked) == {config.MEMORY_DIR}
    assert attempted_spend == []


def test_operational_metrics_retain_spend_evidence(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "owned-test-token")
    monkeypatch.setattr(usage, "budget_status", lambda: "owned budget status")
    monkeypatch.setattr(usage, "today_spend", lambda: 12.34567)
    code, body = request("/api/metrics", "X-Olympus-Token: owned-test-token\r\n")
    assert code == 200
    assert body["budget"] == "owned budget status"
    assert body["spend_today"] == 12.3457


def test_uptime_is_independent_of_wall_clock_adjustments(monkeypatch):
    monotonic = [100.0]
    monkeypatch.setattr(metrics.time, "monotonic", lambda: monotonic[0])
    metrics.reset()
    monkeypatch.setattr(metrics.time, "time", lambda: -10000000.0)
    monotonic[0] += 12.3
    assert metrics.uptime_seconds() == 12.3
    monkeypatch.setattr(metrics.time, "time", lambda: 10000000000.0)
    monotonic[0] += 0.2
    assert metrics.uptime_seconds() == 12.5


@pytest.mark.parametrize("days", [0, 1, 12, 365])
def test_memory_card_reports_creation_age_after_reinforcement(monkeypatch, days):
    now = 1800000000.0
    monkeypatch.setattr(usermem.time, "time", lambda: now - days * 86400)
    mem = usermem.add_memory("card-owner", type="preference", content="concise",
                             confidence=0.8)
    monkeypatch.setattr(usermem.time, "time", lambda: now)
    usermem.reinforce("card-owner", mem["id"])
    before = usermem.all_memories("card-owner")
    card = usermem.render_card("card-owner")
    assert f"{days}d old · id {mem['id']}" in card
    assert usermem.all_memories("card-owner") == before


@pytest.mark.parametrize("created", [None, "unknown", False, float("nan"),
                                     float("inf"), float("-inf"), 10 ** 400])
def test_memory_card_does_not_invent_age_for_bad_creation_evidence(monkeypatch, created):
    now = 1800000000.0
    monkeypatch.setattr(usermem.time, "time", lambda: now)
    mem = usermem.add_memory("card-owner", type="preference", content="concise",
                             confidence=0.8)
    from olympus.owner_evidence import OwnerEvidenceStateError
    if created is None:
        usermem._mutate("card-owner", mem["id"], lambda row: row.update(created_at=None))
    else:
        before = usermem.all_memories("card-owner")
        with pytest.raises(OwnerEvidenceStateError):
            usermem._mutate("card-owner", mem["id"], lambda row: row.update(created_at=created))
        assert usermem.all_memories("card-owner") == before
    # Defensive UI rendering still must not invent age for an unusable input.
    monkeypatch.setattr(usermem, "active_memories", lambda owner: [{**mem, "created_at": created}])
    card = usermem.render_card("card-owner")
    assert f"age unavailable · id {mem['id']}" in card
    assert "0d old" not in card


def test_card_missing_creation_age_with_known_last_use(monkeypatch):
    mem = usermem.add_memory("card-owner", type="preference", content="concise",
                             confidence=0.8)
    usermem._mutate("card-owner", mem["id"], lambda row: row.pop("created_at"))
    assert "age unavailable" in usermem.render_card("card-owner")


def test_card_future_creation_due_to_clock_skew_is_not_negative(monkeypatch):
    now = 1800000000.0
    monkeypatch.setattr(usermem.time, "time", lambda: now)
    usermem.add_memory("card-owner", type="preference", content="concise",
                       confidence=0.8)
    monkeypatch.setattr(usermem.time, "time", lambda: now - 86400)
    card = usermem.render_card("card-owner")
    assert "0d old" in card and "-1d old" not in card


def test_memory_card_cli_uses_durable_creation_age(monkeypatch, capsys):
    from olympus import cli
    now = 1800000000.0
    monkeypatch.setattr(usermem.time, "time", lambda: now - 7 * 86400)
    usermem.add_memory("card-owner", type="preference", content="concise",
                       confidence=0.8)
    monkeypatch.setattr(usermem.time, "time", lambda: now)
    assert cli.main(["memory", "card", "--user", "card-owner"]) == 0
    assert "7d old" in capsys.readouterr().out
