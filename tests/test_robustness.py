"""Tests for the robustness fixes: failure isolation, file bounds, concurrency."""

import threading

from olympus import config, facts, memory
from olympus import orchestrator
from olympus.specialists import SPECIALISTS


def test_specialist_failure_is_isolated(monkeypatch):
    """One specialist raising must not kill the whole dispatch."""
    bot = orchestrator.Olympus(user="iso")

    def fake_run(self, task, settings=None, effort="high"):
        if self.key == "argus":
            raise RuntimeError("network down")
        return f"ok from {self.key}"

    monkeypatch.setattr(SPECIALISTS["argus"].__class__, "run", fake_run)
    out = dict(bot._dispatch([
        {"specialist": "argus", "task": "t"},
        {"specialist": "plutus", "task": "t"},
    ]))
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
