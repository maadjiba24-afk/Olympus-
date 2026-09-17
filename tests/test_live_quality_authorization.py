"""Owned fixtures only: authorization precedes provider access and publication."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from olympus import config, evals, live_quality_authorization as auth

ROOT = Path(__file__).resolve().parent.parent
COMMIT = "1" * 40
REASON = "Owned fixture authorization only"


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(**overrides):
    values = dict(allow_live=True, expected_commit=COMMIT,
                  authorization_reason=REASON, provider="kimi", model="owned-model")
    values.update(overrides)
    return argparse.Namespace(**values)


def argv(**overrides):
    values = vars(arguments(**overrides))
    out = ["--allow-live"] if values.pop("allow_live") else []
    for key, value in values.items():
        out += ["--" + key.replace("_", "-"), str(value)]
    return out


@pytest.fixture
def local_authorization(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("GITHUB_", "GIT_")) or key == "CI" or key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(auth, "reviewed_head", lambda root: COMMIT)
    monkeypatch.setenv("KIMI_API_KEY", "owned-fake-kimi-key")


def dispatch(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"inputs": vars(arguments())}), encoding="utf-8")
    env = dict(CI="true", GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="workflow_dispatch",
               GITHUB_REF="refs/heads/main", GITHUB_REF_PROTECTED="true", GITHUB_SHA=COMMIT,
               GITHUB_REPOSITORY="owned/Olympus", OLYMPUS_LIVE_QUALITY_ENABLED="1",
               GITHUB_WORKFLOW_REF="owned/Olympus/.github/workflows/live-quality-gate.yml@refs/heads/main",
               GITHUB_EVENT_PATH=str(event))
    return event, env


@pytest.mark.parametrize("bad", [
    {"allow_live": False}, {"allow_live": 1}, {"expected_commit": "main"},
    {"expected_commit": "a" * 39}, {"authorization_reason": ""},
    {"authorization_reason": "reason\ninjected"}, {"provider": "unknown"},
    {"model": "model\nOPENAI_API_KEY=bad"}, {"model": "$(false)"},
])
def test_invalid_consent_refuses_before_source_or_provider_access(monkeypatch, bad):
    monkeypatch.setattr(auth, "reviewed_head", lambda root: pytest.fail("source read before consent"))
    with pytest.raises(auth.AuthorizationRequired):
        auth.authorize(arguments(**bad), ROOT, environ={})


@pytest.mark.parametrize("change", [
    {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_EVENT_NAME": "pull_request_target"},
    {"GITHUB_EVENT_NAME": "schedule"}, {"GITHUB_EVENT_NAME": "issues"},
    {"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_REF_PROTECTED": "false"},
    {"GITHUB_SHA": "2" * 40}, {"OLYMPUS_LIVE_QUALITY_ENABLED": "0"},
    {"GITHUB_WORKFLOW_REF": "owned/Olympus/.github/workflows/quality-gate.yml@refs/heads/main"},
    {"GITHUB_ACTIONS": ""},
])
def test_automatic_or_wrong_ci_context_cannot_authorize(tmp_path, monkeypatch, change):
    event, env = dispatch(tmp_path)
    env.update(change)
    monkeypatch.setattr(auth, "reviewed_head", lambda root: pytest.fail("CI context should refuse first"))
    with pytest.raises(auth.AuthorizationRequired):
        auth.authorize(arguments(), ROOT, environ=env)


@pytest.mark.parametrize("payload", [
    "{}", "[]", '{"inputs":{}}',
    # Otherwise-valid consent: without the size guard this would authorize.
    json.dumps({"inputs": vars(arguments()), "padding": "x" * 65536}),
    # Both duplicate values are individually valid: uniqueness must be checked.
    '{"inputs":' + json.dumps(vars(arguments()))
        + ',"inputs":' + json.dumps(vars(arguments())) + '}',
    "not-json",
    json.dumps({"inputs": vars(arguments(allow_live=1))}),
    json.dumps({"inputs": vars(arguments(provider="openai"))}),
    json.dumps({"inputs": vars(arguments(expected_commit="2" * 40))}),
    json.dumps({"inputs": vars(arguments(authorization_reason="Different operator decision"))}),
], ids=[
    "missing-inputs", "non-object-event", "empty-inputs", "oversized-valid-consent",
    "duplicate-valid-inputs", "invalid-json", "numeric-opt-in", "provider-mismatch",
    "commit-mismatch", "reason-mismatch",
])

def test_malformed_stale_or_ambiguous_dispatch_is_unavailable(tmp_path, local_authorization, payload):
    event, env = dispatch(tmp_path)
    event.write_text(payload, encoding="utf-8")
    with pytest.raises(auth.AuthorizationRequired):
        auth.authorize(arguments(), ROOT, environ=env)


def test_matching_local_and_manual_dispatch_authorization(tmp_path, local_authorization):
    expected = vars(arguments()).copy()
    expected.pop("allow_live")
    assert auth.authorize(arguments(), ROOT, environ={}) == expected
    event, env = dispatch(tmp_path)
    assert auth.authorize(arguments(), ROOT, environ=env) == expected
    value = json.loads(event.read_text())
    value["inputs"]["allow_live"] = "true"
    event.write_text(json.dumps(value))
    assert auth.authorize(arguments(), ROOT, environ=env) == expected


def test_ci_marker_alone_is_not_local_consent(local_authorization):
    with pytest.raises(auth.AuthorizationRequired):
        auth.authorize(arguments(), ROOT, environ={"CI": "true"})


def test_real_reviewed_checkout_rejects_dirty_and_stale_source(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "owned-repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.PIPE).decode().strip()
    git("init")
    git("config", "user.name", "Owned Fixture")
    git("config", "user.email", "fixture@example.invalid")
    source = repo / "owned.txt"
    source.write_text("reviewed")
    git("add", "owned.txt")
    git("-c", "commit.gpgsign=false", "commit", "-m", "owned fixture")
    head = git("rev-parse", "HEAD")
    assert auth.reviewed_head(repo) == head
    with pytest.raises(auth.AuthorizationRequired, match="commit changed"):
        auth.authorize(arguments(expected_commit=COMMIT), repo, environ={})
    source.write_text("unreviewed")
    with pytest.raises(auth.AuthorizationRequired, match="tracked checkout"):
        auth.reviewed_head(repo)


@pytest.mark.parametrize("tool", ["quality_gate", "ci_provider_resolve"])
def test_no_consent_with_ambient_fake_credentials_cannot_call_or_write(
    tool, monkeypatch, tmp_path, local_authorization
):
    module = script(tool)
    baseline = tmp_path / "baseline.json"
    baseline.write_text("preserve this evidence")
    monkeypatch.setattr(evals, "BASELINE_PATH", baseline)
    monkeypatch.setattr(evals, "per_specialist_scores", lambda **kw: pytest.fail("benchmark called"))
    monkeypatch.setattr(auth, "provider_configuration", lambda *a, **kw: pytest.fail("credentials read"))
    if tool == "ci_provider_resolve":
        monkeypatch.setattr(module, "list_models", lambda *a: pytest.fail("provider discovery called"))
    dest = tmp_path / "github.env"
    monkeypatch.setenv("GITHUB_ENV", str(dest))
    assert module.main(["--update-baseline"] if tool == "quality_gate" else []) == 3
    assert baseline.read_text() == "preserve this evidence"
    assert not dest.exists()


@pytest.mark.parametrize("tool", ["quality_gate", "ci_provider_resolve"])
def test_subprocess_ambient_key_never_authorizes(tool, local_authorization, tmp_path):
    env = dict(os.environ, GITHUB_ENV=str(tmp_path / "github.env"))
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / (tool + ".py"))],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 3
    assert "UNAVAILABLE" in result.stderr
    assert "owned-fake-kimi-key" not in result.stdout + result.stderr
    assert not (tmp_path / "github.env").exists()


def test_explicit_provider_does_not_fall_back_to_another_key(local_authorization):
    authorization = vars(arguments(provider="openai"))
    with pytest.raises(auth.AuthorizationRequired):
        auth.provider_configuration(authorization)
    settings = auth.provider_configuration(vars(arguments()))
    assert settings == {"provider": "openai", "base_url": "https://api.moonshot.ai/v1",
                        "model": "owned-model", "api_key": "owned-fake-kimi-key"}


def test_resolver_checks_consent_without_credentials(local_authorization, monkeypatch):
    monkeypatch.setattr(auth, "provider_configuration", lambda *a, **kw: pytest.fail("credential read"))
    assert script("ci_provider_resolve").main(argv() + ["--check-authorization"]) == 0


@pytest.mark.parametrize("inventory", [[], ["other-model"], OSError("owned-fake-kimi-key")])
def test_resolver_never_falls_back_or_exports_on_discovery_failure(
    inventory, local_authorization, monkeypatch, capsys, tmp_path
):
    module = script("ci_provider_resolve")
    def discovery(url, key):
        assert key == "owned-fake-kimi-key"
        if isinstance(inventory, Exception):
            raise inventory
        return inventory
    monkeypatch.setattr(module, "list_models", discovery)
    dest = tmp_path / "env"
    monkeypatch.setenv("GITHUB_ENV", str(dest))
    assert module.main(argv() + ["--verify-model"]) == 3
    assert not dest.exists()
    output = capsys.readouterr()
    assert "owned-fake-kimi-key" not in output.out + output.err


def test_resolver_exports_only_identifiers_for_the_authorized_model(
    local_authorization, monkeypatch, tmp_path, capsys
):
    module = script("ci_provider_resolve")
    monkeypatch.setattr(module, "list_models", lambda *args: ["other-model", "owned-model"])
    dest = tmp_path / "env"
    monkeypatch.setenv("GITHUB_ENV", str(dest))
    assert module.main(argv() + ["--verify-model"]) == 0
    assert "OLYMPUS_MODEL=owned-model" in dest.read_text()
    assert "owned-fake-kimi-key" not in dest.read_text() + capsys.readouterr().out


@pytest.mark.parametrize("baseline,meta", [
    ({}, {}), ({"argus": 8}, {}),
    ({"argus": 8}, {"model": "other-model", "endpoint": "https://api.moonshot.ai/v1"}),
    ({"argus": 8}, {"model": "owned-model", "endpoint": "https://elsewhere.invalid"}),
])
def test_missing_or_incomparable_baseline_is_not_a_pass(
    baseline, meta, local_authorization, monkeypatch
):
    monkeypatch.setattr(evals, "load_baseline", lambda: baseline)
    monkeypatch.setattr(evals, "load_baseline_meta", lambda: meta)
    monkeypatch.setattr(evals, "per_specialist_scores", lambda **kw: pytest.fail("unnecessary live call"))
    assert script("quality_gate").main(argv()) == 3


def test_authorized_comparison_and_confirmation_use_exact_provider(
    local_authorization, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unselected-key")
    monkeypatch.setattr(evals, "load_baseline", lambda: {"argus": 8})
    monkeypatch.setattr(evals, "load_baseline_meta", lambda: {
        "model": "owned-model", "endpoint": "https://api.moonshot.ai/v1"})
    calls = []
    def measure(**kw):
        calls.append(kw)
        assert kw["settings"].provider == "openai"
        assert kw["settings"].base_url == "https://api.moonshot.ai/v1"
        assert kw["settings"].api_key == "owned-fake-kimi-key"
        assert kw["settings"].model == "owned-model"
        return {"argus": 6.0}
    monkeypatch.setattr(evals, "per_specialist_scores", measure)
    assert script("quality_gate").main(argv()) == 1
    assert len(calls) == 2 and calls[1]["only_specialists"] == ["argus"]


def test_authorized_baseline_update_retains_authorization(
    local_authorization, monkeypatch, tmp_path
):
    path = tmp_path / "baseline.json"
    monkeypatch.setattr(evals, "BASELINE_PATH", path)
    monkeypatch.setattr(evals, "per_specialist_scores", lambda **kw: {"argus": 8.0})
    assert script("quality_gate").main(argv() + ["--update-baseline"]) == 0
    payload = json.loads(path.read_text())
    assert payload["scores"] == {"argus": 8.0}
    assert payload["_provenance"]["authorization"]["expected_commit"] == COMMIT


def test_automatic_workflow_has_no_live_entrypoint_or_provider_secrets():
    import yaml
    path = ROOT / ".github/workflows/quality-gate.yml"
    raw = path.read_text()
    flow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert "secrets." not in raw
    assert "scripts/quality_gate.py" not in raw and "ci_provider_resolve.py" not in raw
    steps = flow["jobs"]["quality-gate"]["steps"]
    assert any("test_live_quality_authorization.py" in step.get("run", "")
               and "test_quality_gate.py" in step.get("run", "") for step in steps)


def test_live_workflow_is_explicit_dispatch_and_rechecks_before_each_entrypoint():
    import yaml
    raw = (ROOT / ".github/workflows/live-quality-gate.yml").read_text()
    flow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"}
    inputs = flow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["allow_live"]["default"] == "false"
    job = flow["jobs"]["authorized-live-quality"]
    assert job["environment"] == "olympus-live-quality"
    for guard in ("inputs.allow_live == true", "github.ref_protected == true",
                  "github.sha == inputs.expected_commit", "OLYMPUS_LIVE_QUALITY_ENABLED == '1'"):
        assert guard in job["if"]
    steps = job["steps"]
    preflight = next(i for i, s in enumerate(steps) if "--check-authorization" in s.get("run", ""))
    first_secret = next(i for i, s in enumerate(steps) if "secrets." in str(s))
    assert preflight < first_secret
    entries = [s["run"] for s in steps if "scripts/" in s.get("run", "")]
    assert len(entries) == 3
    for command in entries:
        for arg in ("--allow-live", "--expected-commit", "--provider", "--model", "--authorization-reason"):
            assert arg in command
        assert "${{" not in command  # no untrusted expression inserted into shell source


def test_measurement_scope_pins_judge_and_prevents_pool_failover(monkeypatch):
    from olympus import backend
    settings = config.Settings(provider="anthropic", model="approved", api_key="owned")
    monkeypatch.setattr(config, "JUDGE_MODEL", "ambient-unapproved-judge")
    monkeypatch.setattr(backend, "_fallback_chain", lambda *args: pytest.fail("pool fallback attempted"))
    calls = []
    def broken(s, *args):
        calls.append(s.model)
        raise RuntimeError("owned provider failure")
    monkeypatch.setattr(backend, "_dispatch_text", broken)
    with evals.single_model_benchmark(settings):
        assert evals._judge_settings(settings) is settings
        with pytest.raises(RuntimeError, match="owned provider failure"):
            backend.complete_text(settings, "owned", [])
        other = config.Settings(provider="anthropic", model="unapproved", api_key="owned")
        with pytest.raises(ValueError, match="authorization"):
            backend.complete_text(other, "owned", [])
        with pytest.raises(ValueError, match="authorization"):
            backend.complete_json(other, "owned", [], {})
        with pytest.raises(ValueError, match="cannot widen"):
            with backend.pinned_model(other):
                pytest.fail("nested scope widened authorization")
    assert calls == ["approved"]
    assert evals._judge_settings(settings).model == "ambient-unapproved-judge"


def test_actual_benchmark_calls_answer_and_judge_on_only_selected_model(monkeypatch):
    from olympus import backend
    settings = config.Settings(provider="openai", model="owned-model", api_key="owned",
                               base_url="https://owned.invalid/v1")
    rows = [{"id": "owned", "specialist": "argus", "task": "owned task", "criteria": "owned"}]
    monkeypatch.setattr(evals, "load_benchmarks", lambda **kw: rows)
    calls = []
    def answer(s, *a, **kw):
        calls.append(("answer", s))
        return "owned mock answer with sufficient substantive evidence for fixture"
    def judge(s, *a, **kw):
        calls.append(("judge", s))
        return {"score": 8, "justification": "owned fixture only"}
    monkeypatch.setattr(backend.openai_compat, "complete_text", answer)
    monkeypatch.setattr(backend.openai_compat, "complete_json", judge)
    with evals.single_model_benchmark(settings):
        scores = evals.per_specialist_scores(settings=settings)
    assert scores == {"argus": 8.0}
    assert calls == [("answer", settings), ("judge", settings)]
