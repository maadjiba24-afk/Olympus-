"""Tests for the Phase 0 gate harness plumbing — proves the measurement
machinery (usage delta, file-read counter, flag counting, toggle, aggregation
and verdict) is correct, without needing a real model."""

import pytest

from olympus import codegraph, codegraph_gate, config, store, tools, usage


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    codegraph.set_enabled(True)
    yield
    codegraph.set_enabled(True)
    store.reset()


def test_usage_totals_reads_ledger():
    usage.record("claude-opus-4-8", 100, 20)
    usage.record("claude-opus-4-8", 50, 10)
    calls, tokens = codegraph_gate._usage_totals()
    assert calls == 2 and tokens == 180


def test_count_flags():
    assert codegraph_gate._count_flags(
        "a [unverified] b [low confidence: x]") == 2
    assert codegraph_gate._count_flags("clean answer") == 0


def test_instrument_counts_read_source(monkeypatch):
    monkeypatch.setitem(tools.HANDLERS, "read_source_file", lambda path: "ok")
    with codegraph_gate._instrument_read_source() as c:
        tools.HANDLERS["read_source_file"]("a")
        tools.HANDLERS["read_source_file"]("b")
    assert c["n"] == 2
    assert tools.HANDLERS["read_source_file"]("z") == "ok"   # restored


def test_run_task_collects_metrics(monkeypatch):
    monkeypatch.setitem(tools.HANDLERS, "read_source_file", lambda path: "ok")

    def fake_ask(task):
        tools.HANDLERS["read_source_file"]("x")
        tools.HANDLERS["read_source_file"]("y")
        usage.record("claude-opus-4-8", 1000, 200)
        return "here is the answer [unverified]"

    m = codegraph_gate.run_task("q", graph_on=True, ask=fake_ask)
    assert m["read_source_file"] == 2
    assert m["tokens"] == 1200
    assert m["calls"] == 1
    assert m["flags"] == 1


def test_run_gate_aggregates_and_decides(monkeypatch):
    monkeypatch.setitem(tools.HANDLERS, "read_source_file", lambda path: "ok")

    def fake_ask(task):
        # graph ON: 1 read, cheap, clean.  graph OFF: 6 reads, dear, flagged.
        if codegraph.enabled():
            tools.HANDLERS["read_source_file"]("x")
            usage.record("claude-opus-4-8", 800, 150)
            return "answer (all verified)"
        for _ in range(6):
            tools.HANDLERS["read_source_file"]("x")
        usage.record("claude-opus-4-8", 4000, 700)
        return "answer [unverified] [low confidence: guess]"

    res = codegraph_gate.run_gate(root="olympus", tasks=["q"], ask=fake_ask)
    assert res["passed"] is True
    assert res["arms"]["read_source_file -50%"] is True
    assert res["arms"]["tokens -25%"] is True
    assert res["arms"]["fewer hallucination flags"] is True
    assert res["aggregate"]["read_source_file"]["off"] == 6
    assert res["aggregate"]["read_source_file"]["on"] == 1
    assert res["build"]["nodes"] > 0           # the graph really got built
