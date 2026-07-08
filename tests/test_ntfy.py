"""Gap 4 — ntfy push channel. A lightweight publish-to-a-topic delivery target,
best-effort (never raises), wired into the scheduler and the fan-out notifier.
"""

import urllib.error

import pytest

from olympus import ntfy


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN"):
        monkeypatch.delenv(k, raising=False)


# --- configuration ---------------------------------------------------------

def test_unconfigured_without_topic():
    assert ntfy.configured() is False
    assert ntfy.notify("hi") is False        # no-op, no raise


def test_configured_with_topic(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "olympus-alerts")
    assert ntfy.configured() is True


def test_default_server_and_override(monkeypatch):
    assert ntfy._server() == "https://ntfy.sh"
    monkeypatch.setenv("NTFY_SERVER", "https://push.example.com/")
    assert ntfy._server() == "https://push.example.com"   # trailing / trimmed


# --- publish ---------------------------------------------------------------

def _capture(monkeypatch):
    sent = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=30):
        sent["url"] = req.full_url
        sent["data"] = req.data
        sent["headers"] = {k.lower(): v for k, v in req.headers.items()}
        sent["method"] = req.get_method()
        return Resp()

    monkeypatch.setattr(ntfy.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_notify_posts_to_topic_url(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "alerts")
    sent = _capture(monkeypatch)
    assert ntfy.notify("build done", title="CI") is True
    assert sent["url"] == "https://ntfy.sh/alerts"
    assert sent["method"] == "POST"
    assert sent["data"] == b"build done"
    assert sent["headers"]["title"] == "CI"


def test_notify_adds_bearer_when_token_set(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "alerts")
    monkeypatch.setenv("NTFY_TOKEN", "tk_secret")
    sent = _capture(monkeypatch)
    ntfy.notify("hi")
    assert sent["headers"]["authorization"] == "Bearer tk_secret"


def test_notify_unicode_title_does_not_crash(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "alerts")
    sent = _capture(monkeypatch)
    assert ntfy.notify("done ✅", title="Olympus ⚡") is True
    assert sent["data"] == "done ✅".encode("utf-8")   # body keeps unicode


def test_notify_returns_false_on_network_error(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "alerts")

    def boom(req, timeout=30):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(ntfy.urllib.request, "urlopen", boom)
    assert ntfy.notify("hi") is False        # best-effort, swallowed


# --- wiring ----------------------------------------------------------------

def test_ntfy_in_gateway_fanout(monkeypatch):
    from olympus import gateway
    assert "ntfy" in gateway.NOTIFY_CHANNELS
    calls = {}
    monkeypatch.setattr(ntfy, "notify",
                        lambda text, *a, **k: calls.setdefault("t", text) or True)
    for mod in ("telegram", "discord", "slack"):
        m = __import__(f"olympus.{mod}", fromlist=[mod])
        monkeypatch.setattr(m, "notify", lambda text: False)
    from olympus import signal as signal_gw
    monkeypatch.setattr(signal_gw, "notify", lambda text: False)
    delivered = gateway.notify_all("hello")
    assert "ntfy" in delivered and calls["t"] == "hello"


def test_scheduler_delivers_to_ntfy(monkeypatch):
    from olympus import scheduler
    got = {}
    monkeypatch.setattr(ntfy, "notify",
                        lambda msg, title="": got.setdefault("msg", msg) or True)
    job = scheduler.Job(name="scan", interval=3600, prompt="p",
                        deliver_to="ntfy")
    scheduler._deliver(job, "the answer")
    assert "the answer" in got["msg"]
