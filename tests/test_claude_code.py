"""The Claude Code provider drives the `claude` CLI as a single-turn completion
engine (subscription, no API credits). These mock the subprocess so the prompt
construction, output parsing, and error handling are verified without the CLI;
the real end-to-end run is operator-tested where Claude Code is logged in."""

import json

import pytest

from olympus import claude_code, config

_S = config.Settings(provider="claude-code", model="")


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _fake(stdout, returncode=0, stderr=""):
    cap = {}

    def run(cmd, **kw):
        cap["cmd"], cap["kw"] = cmd, kw
        return _Proc(stdout, returncode, stderr)
    return run, cap


def _ok(result):
    return json.dumps({"type": "result", "subtype": "success",
                       "is_error": False, "result": result})


def test_complete_text_parses_result(monkeypatch):
    run, cap = _fake(_ok("hello from max"))
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    out = claude_code.complete_text(_S, "be brief", [{"role": "user", "content": "hi"}])
    assert out == "hello from max"
    cmd = cap["cmd"]
    assert cmd[0] == claude_code._BIN
    assert "--output-format" in cmd and "json" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "1"        # single-turn safety
    assert "--append-system-prompt" in cmd                  # system carried


def test_complete_json_extracts_from_fenced(monkeypatch):
    run, _ = _fake(_ok('```json\n{"mode": "direct"}\n```'))
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    d = claude_code.complete_json(_S, "sys", [{"role": "user", "content": "x"}],
                                  {"type": "object"})
    assert d == {"mode": "direct"}


def test_run_agent_is_single_completion(monkeypatch):
    run, _ = _fake(_ok("specialist answer"))
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    out = claude_code.run_agent(_S, "sys", "do the task",
                                tool_defs=[{"name": "web_search"}])
    assert out == "specialist answer"


def test_model_flag_passed_when_set(monkeypatch):
    run, cap = _fake(_ok("x"))
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    claude_code.complete_text(config.Settings(provider="claude-code", model="sonnet"),
                              "s", [{"role": "user", "content": "q"}])
    assert "--model" in cap["cmd"] and "sonnet" in cap["cmd"]


def test_no_model_flag_when_empty(monkeypatch):
    run, cap = _fake(_ok("x"))
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    claude_code.complete_text(_S, "s", [{"role": "user", "content": "q"}])
    assert "--model" not in cap["cmd"]               # let Claude Code default (Opus on Max)


def test_nonzero_exit_raises(monkeypatch):
    run, _ = _fake("", returncode=1, stderr="boom")
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    with pytest.raises(claude_code.ClaudeCodeError, match="exit 1"):
        claude_code.complete_text(_S, "s", [{"role": "user", "content": "q"}])


def test_is_error_payload_raises(monkeypatch):
    run, _ = _fake(json.dumps({"type": "result", "subtype": "error_max_turns",
                               "is_error": True, "result": "hit the cap"}))
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    with pytest.raises(claude_code.ClaudeCodeError):
        claude_code.complete_text(_S, "s", [{"role": "user", "content": "q"}])


def test_flatten_handles_history_and_block_content():
    msgs = [{"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": [{"type": "text", "text": "block text"}]}]
    out = claude_code._flatten(msgs)
    assert "first" in out and "Earlier assistant reply" in out and "block text" in out


def test_provider_is_usable_without_key_or_url():
    s = config.Settings(provider="claude-code", model="")
    assert s.validate() is None
    assert s.usable() is True                        # auth is via the CLI, not a key


def test_backend_routes_claude_code(monkeypatch):
    from olympus import backend
    run, _ = _fake(_ok("routed"))
    monkeypatch.setattr(claude_code.subprocess, "run", run)
    out = backend.complete_text(_S, "sys", [{"role": "user", "content": "hi"}])
    assert out == "routed"                           # backend dispatched to claude_code
