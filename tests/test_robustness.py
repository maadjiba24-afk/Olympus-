"""Tests for the robustness fixes: failure isolation, file bounds, concurrency."""

import threading

from olympus import config, facts, memory
from olympus import orchestrator
from olympus import trace as trace_mod
from olympus.specialists import SPECIALISTS


def test_specialist_failure_is_isolated(monkeypatch):
    """One specialist raising must not kill the whole dispatch."""
    bot = orchestrator.Olympus(user="iso")

    def fake_run_counted(self, task, settings=None, effort="high"):
        if self.key == "argus":
            raise RuntimeError("network down")
        return f"ok from {self.key}", 0

    # _run_one funnels through run_counted; stub that (failure isolation is the
    # same try/except path).
    monkeypatch.setattr(SPECIALISTS["argus"].__class__, "run_counted",
                        fake_run_counted)
    out = dict(bot._dispatch([
        {"specialist": "argus", "task": "t"},
        {"specialist": "plutus", "task": "t"},
    ], trace_mod.Trace("t")))
    assert "could not complete" in out["argus"]
    assert out["plutus"] == "ok from plutus"


def test_concurrent_user_memory_isolation():
    """Per-user memory namespaces stay isolated under real concurrent threads."""
    results: dict[str, str] = {}

    def worker(user: str):
        memory.set_user(user)
        memory.save("lessons", f"{user} private", f"secret for {user}")
        # immediately read back in the same thread/context
        results[user] = memory.search(f"{user} private")

    threads = [threading.Thread(target=worker, args=(f"user{i}",))
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # each user sees only their own private lesson
    for i in range(6):
        assert f"secret for user{i}" in results[f"user{i}"]
        for j in range(6):
            if i != j:
                assert f"secret for user{j}" not in results[f"user{i}"]


def test_facts_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(facts, "MAX_FACTS", 5)
    for i in range(25):
        facts.record(f"claim number {i}", "true", "src")
    # compaction triggers past 2x cap; stays bounded
    assert facts.count() <= facts.MAX_FACTS * 2


def test_conversation_counter_atomic_and_separate(monkeypatch):
    """The conversation counter is its own file and increments atomically under
    concurrent writers — it must not clobber, or be clobbered by, heartbeat
    state timestamps."""
    memory.save_state({"opportunity_scan": 12345.0, "daily_learning": 678.0})

    results = []

    def worker():
        for _ in range(25):
            results.append(memory.bump_conversation_count())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 4 threads * 25 increments = 100 distinct values, none lost
    assert sorted(results) == list(range(1, 101))
    # heartbeat state untouched by counter writes (separate file)
    state = memory.load_state()
    assert state["opportunity_scan"] == 12345.0
    assert state["daily_learning"] == 678.0
    memory.reset_conversation_count()
    assert memory.bump_conversation_count() == 1


def test_evolution_audit_runs_in_shared_namespace(monkeypatch):
    """System self-audit must write to shared memory even when triggered from
    a user-context thread (HIGH-1 regression)."""
    from olympus import orchestrator
    from olympus.specialists import SPECIALISTS

    memory.set_user("web-visitor-42")   # simulate a user request context

    captured = {}

    def fake_run(self, task, settings=None, effort="high"):
        captured["user"] = memory.current_user()
        return "audit done"

    monkeypatch.setattr(SPECIALISTS["prometheus"].__class__, "run", fake_run)
    orchestrator.evolution_audit()
    assert captured["user"] == "shared"   # not "web-visitor-42"


def test_sweep_dated_files(monkeypatch):
    traces = config.MEMORY_DIR / "traces"
    traces.mkdir(parents=True)
    (traces / "20200101.jsonl").write_text("old\n")
    (traces / "29991231.jsonl").write_text("future\n")
    (traces / "notadate.jsonl").write_text("keep\n")
    removed = memory.sweep_dated_files(retain_days=30)
    assert removed == 1
    assert not (traces / "20200101.jsonl").exists()
    assert (traces / "29991231.jsonl").exists()
    assert (traces / "notadate.jsonl").exists()
