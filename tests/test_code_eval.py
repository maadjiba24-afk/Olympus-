"""Tests for execution-scored coding benchmarks (real code runs against tests)."""

from olympus import code_eval
from olympus.specialists import SPECIALISTS


# --- code extraction -----------------------------------------------------

def test_extract_prefers_tagged_block():
    answer = ("Here's the fix:\n\n```python\ndef f(): return 1\n```\n\n"
              "And some bash:\n```bash\necho hi\n```")
    assert code_eval.extract_code(answer, "python") == "def f(): return 1"


def test_extract_falls_back_to_longest_block():
    answer = "```\nshort\n```\n```\nmuch much longer block here\n```"
    assert "longer block" in code_eval.extract_code(answer, "python")


def test_extract_bare_code_no_fence():
    assert code_eval.extract_code("def f(): return 2", "python") == "def f(): return 2"


# --- real Python execution (python is always available) -----------------

def test_run_code_passes_correct_solution():
    ok, out = code_eval.run_code(
        "python",
        "def add(a, b):\n    return a + b",
        "assert add(2, 3) == 5\nprint('ok')")
    assert ok, out


def test_run_code_fails_wrong_solution():
    ok, out = code_eval.run_code(
        "python",
        "def add(a, b):\n    return a - b",
        "assert add(2, 3) == 5")
    assert not ok


def test_run_code_handles_runtime_error():
    ok, out = code_eval.run_code("python", "def add(a, b):\n    return a + b",
                                 "raise ValueError('boom')")
    assert not ok and "boom" in out


def test_runtime_available_python():
    assert code_eval.runtime_available("python") is True
    assert code_eval.runtime_available("nonexistent-lang") is False


# --- full benchmark run with a stubbed Hephaestus -----------------------

def test_full_run_scores_by_execution(monkeypatch):
    # Hephaestus "solves" the merge-intervals task correctly
    solution = (
        "```python\n"
        "def merge_intervals(intervals):\n"
        "    if not intervals:\n        return []\n"
        "    s = sorted(intervals)\n    out = [s[0][:]]\n"
        "    for a, b in s[1:]:\n"
        "        if a <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], b)\n"
        "        else:\n            out.append([a, b])\n"
        "    return out\n```")
    monkeypatch.setattr(SPECIALISTS["hephaestus"].__class__, "run",
                        lambda self, task, settings=None, effort="high": solution)
    result = code_eval.run(only=["py-merge-intervals"])
    assert result["total"] == 1 and result["passed"] == 1
    assert result["avg"] == 10.0
    assert result["items"][0]["status"] == "pass"


def test_full_run_marks_failing_solution(monkeypatch):
    monkeypatch.setattr(SPECIALISTS["hephaestus"].__class__, "run",
                        lambda self, task, settings=None, effort="high":
                        "```python\ndef merge_intervals(x):\n    return x\n```")
    result = code_eval.run(only=["py-merge-intervals"])
    assert result["passed"] == 0
    assert result["items"][0]["status"] == "fail"


def test_skips_uninstalled_runtime(monkeypatch):
    monkeypatch.setattr(code_eval, "runtime_available",
                        lambda lang: lang == "python")
    # go task should be skipped, not failed, when go isn't available
    result = code_eval.run(only=["go-wordcount"])
    assert result["total"] == 0 and result["skipped"] == 1
    assert "skipped" in result["items"][0]["status"]


def test_code_benchmarks_valid():
    tasks = code_eval.load_tasks()
    assert len(tasks) >= 6
    langs = {t["language"] for t in tasks}
    assert {"python", "javascript", "go"} <= langs
    for t in tasks:
        assert t["id"] and t["prompt"] and t["test"] and t["language"]
