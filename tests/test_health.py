"""Gap 5 — runtime health / degraded-state reporting. A live view of the moving
parts (models, memory, gateway, search, push, connections). `ok` is False only
when something is genuinely DOWN; an absent optional piece is DEGRADED.
"""

import pytest

from olympus import config, health


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    for k in ("BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY",
              "SERPAPI_API_KEY", "GOOGLE_PSE_KEY", "OLYMPUS_SEARXNG_URL",
              "TELEGRAM_BOT_TOKEN",
              "DISCORD_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SIGNAL_CLI_REST_URL",
              "NTFY_TOPIC"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return tmp_path


# --- structure -------------------------------------------------------------

def test_report_shape(env):
    rep = health.report()
    assert set(rep) == {"ok", "down", "degraded", "components"}
    assert set(rep["components"]) == {
        "models", "memory", "gateway", "search", "notifications",
        "connections", "config"}


def test_healthy_minimal_install_is_ok(env):
    # a model key + writable memory, no optional extras -> ok overall
    rep = health.report()
    assert rep["ok"] is True
    assert rep["components"]["memory"]["status"] == "ok"
    assert rep["components"]["models"]["status"] == "ok"


def test_missing_optional_is_degraded_not_down(env):
    rep = health.report()
    # no push channel, no Google connection -> degraded, but still ok overall
    assert "notifications" in rep["degraded"]
    assert "connections" in rep["degraded"]
    assert rep["ok"] is True


def test_no_model_is_down(env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Dead:
        def usable(self): return False
        provider, model = "anthropic", ""

    class Pool:
        members = [Dead()]
        def primary(self): return Dead()

    monkeypatch.setattr(config.ModelPool, "from_env", classmethod(lambda cls: Pool()))
    rep = health.report()
    assert rep["components"]["models"]["status"] == "down"
    assert rep["ok"] is False and "models" in rep["down"]


def test_memory_probe_is_unique_and_cleans_up(env):
    # concurrent /api/health polls must not collide on a shared probe filename,
    # and a probe must not leave a leftover file behind.
    import glob
    for _ in range(5):
        assert health._memory()["status"] == "ok"
    leftovers = glob.glob(str(env / ".health_probe*"))
    assert leftovers == []


def test_unwritable_memory_is_down(env, monkeypatch):
    class Bad:
        def mkdir(self, *a, **k): raise OSError("read-only fs")
    monkeypatch.setattr(config, "MEMORY_DIR", Bad())
    c = health._memory()
    assert c["status"] == "down"


def test_keyed_search_reported_as_unverified(env, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tv")
    c = health._search()
    assert c["status"] == "degraded"
    assert "tavily" in c["detail"]
    assert "unverified" in c["detail"]
    assert c["providers"]["tavily"]["status"] == "unverified"


def test_bing_search_reported_as_unverified(env, monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "secret")
    c = health._search()
    assert c["status"] == "degraded"
    assert "bing" in c["detail"]
    assert "unverified" in c["detail"]
    assert c["providers"]["bing"]["status"] == "unverified"


def test_ntfy_counts_as_notification_channel(env, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "alerts")
    c = health._notifications()
    assert c["status"] == "ok" and "ntfy" in c["detail"]


# --- gateway view ----------------------------------------------------------

def test_gateway_no_daemon_is_ok(env, monkeypatch):
    from olympus import gateway
    monkeypatch.setattr(gateway, "read_status",
                        lambda *a, **k: {"running": False})
    c = health._gateway()
    assert c["status"] == "ok" and "no gateway" in c["detail"]


def test_gateway_impaired_channel_is_degraded(env, monkeypatch):
    from olympus import gateway
    monkeypatch.setattr(gateway, "read_status", lambda *a, **k: {
        "channels": {"telegram": {"status": "running"},
                     "slack": {"status": "failed"}}})
    c = health._gateway()
    assert c["status"] == "degraded" and "slack" in c["detail"]


def test_probe_error_never_crashes_report(env, monkeypatch):
    monkeypatch.setattr(health, "_search",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    rep = health.report()
    assert rep["components"]["search"]["status"] == "degraded"


# --- render ----------------------------------------------------------------

def test_render_healthy(env):
    out = health.render()
    assert "Olympus health: Healthy" in out and "models" in out


def test_render_degraded_header(env, monkeypatch):
    monkeypatch.setattr(health, "report", lambda: {
        "ok": False, "down": ["models"], "degraded": [],
        "components": {"models": {"status": "down", "detail": "x"}}})
    assert "DEGRADED" in health.render()


# --- live search diagnostics -----------------------------------------------


def test_search_health_config_only_is_unverified(env, monkeypatch):
    from olympus import websearch

    monkeypatch.setattr(websearch, "diagnostics", lambda live=False: {
        "live": False,
        "ok": None,
        "providers": {
            "ddg": {
                "configured": True,
                "status": "unverified",
                "result_count": 0,
            },
        },
    })

    component = health._search(live=False)

    assert component["status"] == "degraded"
    assert "unverified" in component["detail"]
    assert component["providers"]["ddg"]["status"] == "unverified"


def test_search_health_live_is_ok_when_a_provider_answers(env, monkeypatch):
    from olympus import websearch

    monkeypatch.setattr(websearch, "diagnostics", lambda live=False: {
        "live": True,
        "ok": True,
        "providers": {
            "tavily": {
                "configured": True,
                "status": "ok",
                "result_count": 1,
                "latency_ms": 20.0,
            },
            "ddg": {
                "configured": True,
                "status": "down",
                "result_count": 0,
                "latency_ms": 10.0,
                "error_type": "RuntimeError",
            },
        },
    })

    component = health._search(live=True)

    assert component["status"] == "degraded"
    assert "tavily" in component["detail"]
    assert "ddg" in component["detail"]


def test_search_health_live_is_down_when_no_provider_answers(env, monkeypatch):
    from olympus import websearch

    monkeypatch.setattr(websearch, "diagnostics", lambda live=False: {
        "live": True,
        "ok": False,
        "providers": {
            "ddg": {
                "configured": True,
                "status": "down",
                "result_count": 0,
                "latency_ms": 10.0,
                "error_type": "RuntimeError",
            },
        },
    })

    component = health._search(live=True)

    assert component["status"] == "down"
    assert "no search provider answered" in component["detail"]


def test_report_can_request_live_search_probe(env, monkeypatch):
    seen = []

    monkeypatch.setattr(
        health,
        "_search",
        lambda live=False: seen.append(live) or {
            "status": "ok",
            "detail": "live",
        },
    )

    health.report(live_search=True)

    assert seen == [True]


# --- health CLI live-search flag -------------------------------------------


def test_health_parser_accepts_live_search():
    from olympus import cli

    args = cli.build_parser().parse_args(["health", "--live-search"])

    assert args.command == "health"
    assert args.live_search is True


def test_health_cli_requests_live_search(monkeypatch, capsys):
    from olympus import cli

    seen = []

    monkeypatch.setattr(
        health,
        "report",
        lambda *, live_search=False: seen.append(live_search) or {
            "ok": True,
            "down": [],
            "degraded": [],
            "components": {},
        },
    )
    monkeypatch.setattr(health, "render", lambda report: "health rendered")

    result = cli.main(["health", "--live-search"])

    assert result == 0
    assert seen == [True]
    assert "health rendered" in capsys.readouterr().out
