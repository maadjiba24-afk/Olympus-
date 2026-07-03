"""Execution-scored coding benchmarks.

Instead of asking a judge "does this code look good?", these benchmarks extract
the code Hephaestus wrote, run it against real tests, and score it on whether
it actually passes — objective truth, the closest thing to genuinely training a
coding agent.

Security: this runs model-generated code locally, so it is for the benchmark
harness only (Olympus's own output, in a dev/eval context) — never user input.
Each run is isolated in a temp dir with a wall-clock timeout. Only languages
whose runtime is installed are tested; the rest are skipped, not failed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import backend, config, memory

TIMEOUT = 30  # seconds per code run


def extract_code(text: str, language: str) -> str:
    """Pull the most relevant fenced code block out of a model answer."""
    blocks = re.findall(r"```([a-zA-Z0-9+#]*)\n(.*?)```", text, re.DOTALL)
    if not blocks:
        return text.strip()  # maybe the whole answer is code
    aliases = {
        "python": {"python", "py"}, "javascript": {"javascript", "js"},
        "typescript": {"typescript", "ts"}, "go": {"go", "golang"},
        "ruby": {"ruby", "rb"}, "bash": {"bash", "sh", "shell"},
    }.get(language, {language})
    # prefer a block tagged with the target language; else the longest block
    tagged = [code for tag, code in blocks if tag.lower() in aliases]
    if tagged:
        return max(tagged, key=len).strip()
    return max((code for _, code in blocks), key=len).strip()


def _run(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=TIMEOUT)
        ok = proc.returncode == 0
        out = (proc.stdout + proc.stderr)[-3000:]
        return ok, out
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT}s"
    except FileNotFoundError as err:
        return False, f"runtime not found: {err}"
    except Exception as err:
        return False, f"run error: {err}"


# Per-language: the runtime to check for, and how to assemble + run solution+test.
def _runner_python(sol: str, test: str, d: Path) -> tuple[bool, str]:
    (d / "solution.py").write_text(sol, encoding="utf-8")
    (d / "test_it.py").write_text(
        "from solution import *\n" + test, encoding="utf-8")
    return _run(["python", "test_it.py"], d)


def _runner_javascript(sol: str, test: str, d: Path) -> tuple[bool, str]:
    (d / "solution.js").write_text(sol, encoding="utf-8")
    (d / "test_it.js").write_text(test, encoding="utf-8")
    return _run(["node", "test_it.js"], d)


def _runner_go(sol: str, test: str, d: Path) -> tuple[bool, str]:
    (d / "main.go").write_text(sol, encoding="utf-8")
    (d / "main_test.go").write_text(test, encoding="utf-8")
    ok, out = _run(["go", "mod", "init", "bench"], d)
    if not ok:  # environment failure, not a wrong answer — don't mis-score
        return False, f"go mod init failed (environment): {out}"
    return _run(["go", "test", "./..."], d)


def _runner_ruby(sol: str, test: str, d: Path) -> tuple[bool, str]:
    (d / "solution.rb").write_text(sol, encoding="utf-8")
    (d / "test_it.rb").write_text(
        "require_relative 'solution'\n" + test, encoding="utf-8")
    return _run(["ruby", "test_it.rb"], d)


_RUNTIMES = {
    "python": ("python", _runner_python),
    "javascript": ("node", _runner_javascript),
    "go": ("go", _runner_go),
    "ruby": ("ruby", _runner_ruby),
}


def runtime_available(language: str) -> bool:
    entry = _RUNTIMES.get(language)
    return bool(entry and shutil.which(entry[0]))


def run_code(language: str, solution: str, test: str) -> tuple[bool, str]:
    """Assemble solution + test in a temp dir and run; return (passed, output)."""
    entry = _RUNTIMES.get(language)
    if not entry:
        return False, f"unsupported language: {language}"
    if not shutil.which(entry[0]):
        return False, f"{entry[0]} not installed"
    d = Path(tempfile.mkdtemp(prefix="olympus_codeeval_"))
    try:
        return entry[1](solution, test, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def load_tasks() -> list[dict]:
    path = Path(__file__).resolve().parent / "code_benchmarks.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    extra = config.MEMORY_DIR / "code_benchmarks_extra.json"
    if extra.exists():
        try:
            items += json.loads(extra.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return items


def run(settings: config.Settings | None = None,
        only: list[str] | None = None) -> dict:
    """Have Hephaestus solve each coding task; score by whether tests pass.

    Returns {avg(0-10), passed, total, skipped, items:[...]}.
    """
    from . import specialists
    settings = settings or config.Settings.from_env()
    heph = specialists.SPECIALISTS["hephaestus"]
    items, passed, ran = [], 0, 0
    for task in load_tasks():
        if only and task["id"] not in only:
            continue
        lang = task["language"]
        if not runtime_available(lang):
            items.append({"id": task["id"], "language": lang,
                          "status": "skipped (no runtime)"})
            continue
        answer = heph.run(
            task["prompt"] + f"\n\nProvide a complete {lang} solution. The "
            "following will be run against your code, so match the required "
            f"names/signatures exactly:\n{task.get('contract', '')}",
            settings=settings)
        code = extract_code(answer, lang)
        ok, out = run_code(lang, code, task["test"])
        ran += 1
        passed += 1 if ok else 0
        items.append({"id": task["id"], "language": lang,
                      "status": "pass" if ok else "fail",
                      "output": out[:500]})
    avg = round((passed / ran) * 10, 2) if ran else 0.0
    return {"avg": avg, "passed": passed, "total": ran,
            "skipped": len(items) - ran, "items": items}


def run_and_save(settings: config.Settings | None = None) -> str:
    result = run(settings)
    memory.set_user("shared")
    lines = [f"Code-execution benchmark: {result['passed']}/{result['total']} "
             f"passed ({result['avg']}/10)  {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for it in result["items"]:
        line = f"- {it['id']} [{it['language']}]: {it['status']}"
        if it.get("status") == "fail":
            line += f"\n    {it.get('output', '')[:200]}"
        lines.append(line)
    summary = "\n".join(lines)
    memory.save("evals", f"code-exec {result['passed']}/{result['total']}", summary)
    return summary
