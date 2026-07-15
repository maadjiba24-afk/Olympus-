"""Plugin lifecycle hooks: observe the agent loop, block or rewrite tool
calls — without forking Olympus. Blocking composes with (never bypasses)
the approval spine: a hook runs before execution and can only get stricter."""

import types

import pytest

from olympus import agent, connectors, tools


@pytest.fixture(autouse=True)
def _clean_hooks():
    connectors.clear_hooks()
    yield
    connectors.clear_hooks()


def _block(name="my_tool", inp=None):
    return types.SimpleNamespace(type="tool_use", id="toolu_hook",
                                 name=name, input=inp or {})


def _with_handler(monkeypatch, fn):
    monkeypatch.setattr(tools, "resolve_handler",
                        lambda name: fn if name == "my_tool" else None)


def test_unknown_event_is_rejected():
    with pytest.raises(ValueError):
        connectors.hook("on_teleport")


def test_pre_tool_can_block(monkeypatch):
    _with_handler(monkeypatch, lambda **_: "ran")
    calls = []

    @connectors.hook("pre_tool")
    def deny(name, params):
        calls.append(name)
        return {"block": "not on my watch"}

    res = agent._tool_result(_block())
    assert res["is_error"]
    assert "Blocked by plugin policy: not on my watch" in res["content"]
    assert calls == ["my_tool"]


def test_pre_tool_can_rewrite_params(monkeypatch):
    seen = {}
    _with_handler(monkeypatch, lambda **kw: seen.update(kw) or "ok")

    @connectors.hook("pre_tool")
    def redact(name, params):
        return {"params": {**params, "query": "[redacted]"}}

    res = agent._tool_result(_block(inp={"query": "secret"}))
    assert not res.get("is_error")
    assert seen == {"query": "[redacted]"}


def test_post_tool_can_rewrite_result(monkeypatch):
    _with_handler(monkeypatch, lambda **_: "raw result")

    @connectors.hook("post_tool")
    def stamp(name, params, result):
        return result + " [checked]"

    res = agent._tool_result(_block())
    # 'my_tool' is unclassified, so its output is now enveloped (M0.3 fail-closed);
    # the post_tool hook still rewrote the result inside that envelope.
    assert "raw result [checked]" in res["content"]


def test_broken_hook_is_contained(monkeypatch):
    _with_handler(monkeypatch, lambda **_: "fine")

    @connectors.hook("pre_tool")
    def boom(name, params):
        raise RuntimeError("plugin bug")

    @connectors.hook("post_tool")
    def boom2(name, params, result):
        raise RuntimeError("plugin bug")

    res = agent._tool_result(_block())
    assert "fine" in res["content"]          # the run survived both failures (enveloped, M0.3)


def test_observe_hooks_fire_in_order():
    log = []
    connectors.hook("run_start")(lambda user, msg: log.append(("start", msg)))
    connectors.hook("run_end")(lambda user, reply: log.append(("end", reply)))
    connectors.emit("run_start", "u", "question")
    connectors.emit("run_end", "u", "answer")
    assert log == [("start", "question"), ("end", "answer")]


def test_multiple_pre_tool_rewrites_chain(monkeypatch):
    seen = {}
    _with_handler(monkeypatch, lambda **kw: seen.update(kw) or "ok")
    connectors.hook("pre_tool")(
        lambda n, p: {"params": {**p, "a": 1}})
    connectors.hook("pre_tool")(
        lambda n, p: {"params": {**p, "b": 2}})
    agent._tool_result(_block(inp={}))
    assert seen == {"a": 1, "b": 2}
