"""Phase 5 P5-2 — the staging profile.

Gates proved here: P5-A2 (staging config fails closed), P5-A3 (durable stores
use persistent paths), P5-A21 (every env var documented), and the parts of
P5-A4 (restart recovery) that belong to configuration rather than to data.

Everything is offline. Nothing here claims a deployment happened — the compose
file is validated by schema (`docker compose config`) in a separate, skipped-if-
absent test, and never brought up.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from olympus import config, web

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"


@pytest.fixture
def staging_env(monkeypatch, tmp_path):
    """A COMPLETE staging profile. Each test then removes exactly one thing, so
    a failure names the missing piece rather than 'staging is broken'."""
    mem = tmp_path / "stagemem"
    mem.mkdir()
    monkeypatch.setenv("OLYMPUS_ENV", "staging")
    monkeypatch.setenv("OLYMPUS_MEMORY_DIR", str(mem))
    monkeypatch.setattr(config, "MEMORY_DIR", mem)
    monkeypatch.setenv("OLYMPUS_DAILY_BUDGET", "5.00")
    monkeypatch.setenv("OLYMPUS_API_KEYS", "stagingkey")
    monkeypatch.setattr(config, "RETAIN_DAYS", 14)
    monkeypatch.delenv("OLYMPUS_BACKUP_CMD", raising=False)
    return mem


# ── the mode itself ────────────────────────────────────────────────────────

def test_deployment_env_normalises_both_spellings(monkeypatch):
    """A profile that says `stage` must not silently fall through to dev
    rules, where none of the staging invariants apply."""
    for raw, expect in (("staging", "staging"), ("stage", "staging"),
                        ("STAGING", "staging"), (" Stage ", "staging"),
                        ("production", "production"), ("prod", "production"),
                        ("", ""), ("dev", ""), ("qa", "")):
        monkeypatch.setenv("OLYMPUS_ENV", raw)
        assert config.deployment_env() == expect, raw
    monkeypatch.setenv("OLYMPUS_ENV", "stage")
    assert config.is_staging() is True
    assert config.is_production() is False


def test_staging_and_production_are_disjoint(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ENV", "staging")
    assert config.is_staging() and not config.is_production()
    monkeypatch.setenv("OLYMPUS_ENV", "production")
    assert config.is_production() and not config.is_staging()


# ── P5-A2: fails closed ────────────────────────────────────────────────────

def test_a_complete_staging_profile_boots(staging_env):
    assert config.staging_problems() == []
    config.require_staging_config()             # must not raise


def test_dev_and_production_are_completely_unaffected(monkeypatch, tmp_path):
    """The validation must be a no-op outside staging, or this change would
    alter every existing deployment's boot behaviour."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "m")
    monkeypatch.delenv("OLYMPUS_MEMORY_DIR", raising=False)
    monkeypatch.delenv("OLYMPUS_DAILY_BUDGET", raising=False)
    for env in ("", "dev", "production"):
        monkeypatch.setenv("OLYMPUS_ENV", env)
        assert config.staging_problems() == [], env
        config.require_staging_config()         # must not raise


def test_missing_memory_dir_refuses_to_start(staging_env, monkeypatch):
    """The single most destructive misconfiguration: durable state defaults
    into the image and every journal dies with the container."""
    monkeypatch.delenv("OLYMPUS_MEMORY_DIR")
    problems = config.staging_problems()
    assert any("OLYMPUS_MEMORY_DIR is unset" in p for p in problems), problems
    with pytest.raises(config.StagingConfigError) as exc:
        config.require_staging_config()
    assert "restart" in str(exc.value)          # the reason, not just the name


def test_unwritable_memory_dir_refuses_to_start(staging_env, monkeypatch,
                                                tmp_path):
    """A read-only volume leaves the process alive while silently dropping
    every append. `os.access` lies here, so the probe must actually write."""
    ro = tmp_path / "readonly"
    ro.mkdir()
    ro.chmod(0o555)
    monkeypatch.setenv("OLYMPUS_MEMORY_DIR", str(ro))
    monkeypatch.setattr(config, "MEMORY_DIR", ro)
    try:
        if config._writable_dir(ro):
            pytest.skip("running as root — chmod cannot make a dir unwritable")
        problems = config.staging_problems()
        assert any("not writable" in p for p in problems), problems
    finally:
        ro.chmod(0o755)


def test_zero_or_missing_budget_refuses_to_start(staging_env, monkeypatch):
    """OLYMPUS_DAILY_BUDGET=0 means UNLIMITED, not off — the trap is documented
    in the production compose file and must not be reachable in staging."""
    monkeypatch.delenv("OLYMPUS_DAILY_BUDGET")
    assert any("OLYMPUS_DAILY_BUDGET is unset" in p
               for p in config.staging_problems())
    for bad in ("0", "0.0", "-1"):
        monkeypatch.setenv("OLYMPUS_DAILY_BUDGET", bad)
        problems = config.staging_problems()
        assert any("UNLIMITED" in p or "positive" in p for p in problems), bad
    monkeypatch.setenv("OLYMPUS_DAILY_BUDGET", "not-a-number")
    assert any("is not a number" in p for p in config.staging_problems())


def test_off_loopback_bind_without_a_credential_refuses(staging_env,
                                                        monkeypatch):
    monkeypatch.delenv("OLYMPUS_API_KEYS")
    monkeypatch.delenv("OLYMPUS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OLYMPUS_REQUIRE_LOGIN", raising=False)
    problems = config.staging_problems(bind_host="0.0.0.0")
    assert any("no credential configured" in p for p in problems), problems
    # ...and a loopback bind is fine without one
    assert config.staging_problems(bind_host="127.0.0.1") == []
    # ...and ANY one of the three credentials satisfies it
    for var in ("OLYMPUS_API_KEYS", "OLYMPUS_ACCESS_TOKEN",
                "OLYMPUS_REQUIRE_LOGIN"):
        monkeypatch.setenv(var, "x")
        assert config.staging_problems(bind_host="0.0.0.0") == [], var
        monkeypatch.delenv(var)


def test_infinite_retention_refuses_to_start(staging_env, monkeypatch):
    monkeypatch.setattr(config, "RETAIN_DAYS", 0)
    assert any("retention sweeps" in p for p in config.staging_problems())


def test_production_backup_destination_is_refused_in_staging(staging_env,
                                                             monkeypatch):
    """A staging archive delivered to the production bucket is a real data
    incident, not a tidiness problem."""
    monkeypatch.setenv("OLYMPUS_BACKUP_CMD", "aws s3 cp {path} s3://prod/")
    assert any("production destination" in p
               for p in config.staging_problems())
    # the escape hatch is explicit, and named in the refusal
    monkeypatch.setenv("OLYMPUS_STAGING_ALLOW_BACKUP_CMD", "1")
    assert config.staging_problems() == []


def test_every_problem_is_reported_at_once(staging_env, monkeypatch):
    """One restart per typo is a bad operator experience; the error carries the
    whole list."""
    monkeypatch.delenv("OLYMPUS_MEMORY_DIR")
    monkeypatch.delenv("OLYMPUS_DAILY_BUDGET")
    monkeypatch.setattr(config, "RETAIN_DAYS", 0)
    with pytest.raises(config.StagingConfigError) as exc:
        config.require_staging_config()
    assert len(exc.value.problems) >= 3
    text = str(exc.value)
    assert "OLYMPUS_MEMORY_DIR" in text and "OLYMPUS_DAILY_BUDGET" in text


def test_no_problem_message_leaks_a_secret(staging_env, monkeypatch):
    """Boot errors are printed to stderr and land in logs."""
    monkeypatch.setenv("OLYMPUS_API_KEYS", "sup3r-s3cret-staging-key")
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "another-secret-token")
    monkeypatch.delenv("OLYMPUS_MEMORY_DIR")
    monkeypatch.setattr(config, "RETAIN_DAYS", 0)
    blob = " ".join(config.staging_problems(bind_host="0.0.0.0"))
    assert "sup3r-s3cret-staging-key" not in blob
    assert "another-secret-token" not in blob


# ── build identification ───────────────────────────────────────────────────

def test_build_info_reports_version_commit_and_mode(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ENV", "staging")
    monkeypatch.setenv("OLYMPUS_BUILD_COMMIT", "deadbeefcafe")
    info = config.build_info()
    assert info["commit"] == "deadbeefcafe"
    assert info["env"] == "staging"
    assert re.match(r"^\d+\.\d+", info["version"])


def test_build_info_says_unknown_rather_than_guessing(monkeypatch):
    """A measurement correlated to the wrong commit is worse than one with no
    commit at all."""
    monkeypatch.setenv("OLYMPUS_BUILD_COMMIT", "")
    monkeypatch.setattr(config, "__name__", config.__name__)
    import subprocess

    def no_git(*a, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert config.build_info()["commit"] == "unknown"


# ── readiness vs liveness ──────────────────────────────────────────────────

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_readyz_is_distinct_from_healthz(server):
    """Liveness and readiness must not be the same probe: a config-incomplete
    instance should be pulled from rotation, not killed and restarted forever."""
    live_code, live = _get(server + "/healthz")
    ready_code, ready = _get(server + "/readyz")
    assert live_code == 200 and live["status"] == "ok"
    assert ready_code == 200 and ready["status"] == "ready"
    assert set(ready) > set(live), "readiness carries strictly more information"
    assert {"env", "version", "commit", "memory_dir_writable"} <= set(ready)


def test_readyz_needs_no_auth_and_returns_no_user_data(server):
    _, ready = _get(server + "/readyz")
    blob = json.dumps(ready).lower()
    for forbidden in ("key", "token", "password", "secret", "messages",
                      "content"):
        assert forbidden not in blob, f"/readyz leaked {forbidden!r}: {ready}"


def test_readyz_reports_503_when_staging_config_is_incomplete(
        server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_ENV", "staging")
    monkeypatch.delenv("OLYMPUS_MEMORY_DIR", raising=False)
    monkeypatch.delenv("OLYMPUS_DAILY_BUDGET", raising=False)
    code, body = _get(server + "/readyz")
    assert code == 503, body
    assert body["status"] == "not_ready"
    assert body["problems"], "a not-ready probe must say why"
    # liveness must still be 200 — the process is alive, just not serving
    assert _get(server + "/healthz")[0] == 200


def test_metrics_carries_build_and_mode(server):
    _, snap = _get(server + "/api/metrics")
    assert snap["build"]["version"] and snap["build"]["env"] in (
        "dev", "staging", "production")


# ── graceful shutdown ──────────────────────────────────────────────────────

def test_serve_installs_a_sigterm_handler():
    """`docker stop` sends SIGTERM then SIGKILLs after the grace period.
    Python's default terminates immediately, tearing an in-flight journal
    append in half."""
    import inspect
    src = inspect.getsource(web.serve)
    assert "_install_shutdown(server)" in src
    assert "server.server_close()" in src
    assert "daemon_threads = False" in src
    handler_src = inspect.getsource(web._install_shutdown)
    assert "SIGTERM" in handler_src and "SIGINT" in handler_src
    # shutdown() must not run on the handler stack — that self-deadlocks
    assert "threading.Thread(target=server.shutdown" in handler_src


def test_install_shutdown_survives_a_non_main_thread():
    """serve() also runs from worker threads (tests, TUI), where signal
    handlers cannot be installed. That must degrade, not crash."""
    class _Srv:
        def shutdown(self):
            pass

    box = {}

    def run():
        try:
            web._install_shutdown(_Srv())
            box["ok"] = True
        except Exception as err:                 # pragma: no cover - defect
            box["err"] = err

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert box.get("ok") is True, box.get("err")


def test_serve_refuses_an_incomplete_staging_profile(monkeypatch, tmp_path):
    """The boot invariant is on the serve path too, not only the CLI — a
    process started any other way must fail the same."""
    monkeypatch.setenv("OLYMPUS_ENV", "staging")
    monkeypatch.delenv("OLYMPUS_MEMORY_DIR", raising=False)
    monkeypatch.delenv("OLYMPUS_DAILY_BUDGET", raising=False)
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "m")
    with pytest.raises(config.StagingConfigError):
        web.serve(host="0.0.0.0", port=0)


def test_cli_boot_invariant_is_wired():
    import inspect
    from olympus import cli
    src = inspect.getsource(cli)
    assert "require_staging_config()" in src
    assert "StagingConfigError" in src


# ── P5-A3 / P5-A21: the profile files themselves ───────────────────────────

def test_staging_compose_profile_exists_and_is_structurally_safe():
    """Read as data: the staging profile must differ from production
    STRUCTURALLY, not just in values."""
    import yaml
    text = (_DEPLOY / "docker-compose.staging.yml").read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    services = spec["services"]
    assert set(services) == {"olympus-staging"}, (
        "staging must not carry the heartbeat (it spends money unattended) "
        "or Caddy (it publishes to the internet)")
    svc = services["olympus-staging"]
    assert "ports" not in svc, "staging must never publish a host port"
    assert svc["expose"] == ["8484"]
    assert svc["environment"]["OLYMPUS_ENV"] == "staging"
    vols = spec["volumes"]
    assert "olympus-staging-memory" in vols
    assert "olympus-memory" not in vols, (
        "staging must not mount the production volume — that contaminates "
        "production evidence with synthetic staging records")
    assert "/app/memory" in svc["volumes"][0]
    assert "readyz" in json.dumps(svc["healthcheck"]), (
        "the healthcheck must probe READINESS, not liveness")
    assert svc["stop_grace_period"], "no drain window for SIGTERM"


def test_production_compose_is_untouched_by_the_staging_profile():
    """P5-28 migration note: the staging unit is purely additive."""
    import yaml
    prod = yaml.safe_load(
        (_DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert {"olympus", "caddy", "heartbeat"} <= set(prod["services"])
    assert "olympus-memory" in prod["volumes"]


def test_staging_env_example_ships_no_real_secret():
    text = (_DEPLOY / ".env.staging.example").read_text(encoding="utf-8")
    assert "REPLACE_ME" in text
    for line in text.splitlines():
        if line.startswith("ANTHROPIC_API_KEY=") or \
                line.startswith("OPENAI_API_KEY="):
            pytest.fail(f"the example ships a live credential slot: {line}")
    # the dangerous defaults are called out where an operator will read them
    assert "0 means UNLIMITED" in text
    assert "never enter source control" in text.lower() or \
        "NEVER enter source control" in text


def test_staging_env_file_is_gitignored():
    ignored = (_REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/.env.staging" in ignored


def test_every_staging_env_var_is_documented(staging_env):
    """P5-A21. Derived from the code that reads them, not a hand-kept list —
    a hand-kept list is exactly what drifted in Wave 1."""
    import inspect
    src = inspect.getsource(config.staging_problems) + \
        inspect.getsource(config.build_info) + \
        inspect.getsource(config.deployment_env)
    referenced = set(re.findall(r'"(OLYMPUS_[A-Z_]+)"', src))
    example = (_DEPLOY / ".env.staging.example").read_text(encoding="utf-8")
    root_example = (_REPO / ".env.example").read_text(encoding="utf-8")
    undocumented = [v for v in sorted(referenced)
                    if v not in example and v not in root_example]
    assert not undocumented, (
        f"staging reads these but no example file documents them: "
        f"{undocumented}")


@pytest.mark.skipif(
    os.system("docker compose version >/dev/null 2>&1") != 0,
    reason="docker compose CLI not available")
def test_staging_compose_passes_schema_validation(tmp_path):
    """Schema validation only. This does NOT bring the stack up and makes no
    claim that it runs — the authoring environment has no Docker daemon."""
    import shutil
    import subprocess
    env_file = _DEPLOY / ".env.staging"
    created = False
    if not env_file.exists():
        shutil.copy(_DEPLOY / ".env.staging.example", env_file)
        created = True
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", "docker-compose.staging.yml", "config"],
            cwd=str(_DEPLOY), capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
    finally:
        if created:
            env_file.unlink()
