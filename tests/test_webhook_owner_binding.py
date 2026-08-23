"""The generic webhook secret maps to one server-owned tenant.

All identities and payloads here are synthetic; no socket or external service
is contacted.
"""

import pytest

from olympus import gateway, webhook_gateway


def test_configured_requires_secret_and_owner(monkeypatch):
    monkeypatch.delenv("OLYMPUS_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("OLYMPUS_WEBHOOK_USER", raising=False)
    assert webhook_gateway.configured() is False

    monkeypatch.setenv("OLYMPUS_WEBHOOK_SECRET", "synthetic-secret")
    assert webhook_gateway.configured() is False

    monkeypatch.setenv("OLYMPUS_WEBHOOK_USER", "alice")
    assert webhook_gateway.configured() is True


def test_payload_is_routed_only_to_server_owner(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gateway, "reply_for",
        lambda bots, user, text, prefix="ol":
        seen.update(user=user, text=text, prefix=prefix) or ["safe reply"],
    )

    out = webhook_gateway.handle_payload(
        {}, {"text": "harmless fixture"}, owner="alice")

    assert out == {"reply": "safe reply"}
    assert seen == {"user": "alice", "text": "harmless fixture",
                    "prefix": "hook"}


@pytest.mark.parametrize("forged", ["bob", "shared", "anonymous", "cli"])
def test_public_user_field_fails_closed(monkeypatch, forged):
    called = []
    monkeypatch.setattr(gateway, "reply_for",
                        lambda *args, **kwargs: called.append(True) or ["bad"])

    with pytest.raises(ValueError, match="caller-supplied 'user'"):
        webhook_gateway.handle_payload(
            {}, {"text": "harmless fixture", "user": forged}, owner="alice")

    assert called == []


def test_empty_trusted_owner_fails_before_routing(monkeypatch):
    called = []
    monkeypatch.setattr(gateway, "reply_for",
                        lambda *args, **kwargs: called.append(True) or ["bad"])
    with pytest.raises(ValueError, match="owner is not configured"):
        webhook_gateway.handle_payload({}, {"text": "fixture"}, owner="")
    assert called == []


def test_gateway_daemon_does_not_enable_partially_bound_webhook(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEBHOOK_SECRET", "synthetic-secret")
    monkeypatch.delenv("OLYMPUS_WEBHOOK_USER", raising=False)
    configured, _start = gateway._channel_registry()["webhook"]
    assert configured() is False

    monkeypatch.setenv("OLYMPUS_WEBHOOK_USER", "alice")
    assert configured() is True
