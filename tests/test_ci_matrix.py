"""CI must actually TEST every Python version the package claims to support.

The 2026-07-15 truth-state audit flagged that CI ran a single Python (3.12)
while pyproject declared 3.10–3.13 — a coverage gap where a 3.10-only or
3.13-only regression would ship undetected. These tests pin the fix: the
`test` job's version matrix must cover exactly the range pyproject advertises,
so the two can never silently drift apart again.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_PYPROJECT = _ROOT / "pyproject.toml"


def _declared_versions() -> set[str]:
    """The minor versions pyproject advertises via its classifiers."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    return set(re.findall(r"Programming Language :: Python :: (3\.\d+)", text))


def _ci_matrix_versions() -> set[str]:
    """The versions the CI `test` job's matrix runs."""
    text = _CI.read_text(encoding="utf-8")
    m = re.search(r"python-version:\s*\[([^\]]*)\]", text)
    assert m, "ci.yml test job has no `python-version: [...]` matrix"
    return set(re.findall(r"3\.\d+", m.group(1)))


def _job_body(name: str) -> str:
    text = _CI.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\s*\n"
        rf"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        text,
    )
    assert match, f"ci.yml has no `{name}` job"
    return match.group("body")


def test_ci_matrix_covers_every_declared_version():
    declared = _declared_versions()
    assert declared, "pyproject declares no Python classifiers"
    matrix = _ci_matrix_versions()
    missing = declared - matrix
    assert not missing, f"CI does not test declared Python version(s): {missing}"


def test_ci_matrix_has_no_undeclared_versions():
    # The matrix shouldn't test a version the package doesn't claim to support.
    extra = _ci_matrix_versions() - _declared_versions()
    assert not extra, f"CI tests undeclared Python version(s): {extra}"


def test_requires_python_floor_is_in_the_matrix():
    text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*"[>=]*\s*(3\.\d+)"', text)
    assert m, "pyproject has no requires-python floor"
    floor = m.group(1)
    assert floor in _ci_matrix_versions(), (
        f"requires-python floor {floor} is not exercised by CI")


def test_ci_still_hash_pins_dependencies():
    # The matrix must not have traded supply-chain integrity for convenience:
    # every leg still installs from the hash-locked file.
    text = _CI.read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock" in text, (
        "CI must install with --require-hashes (no unpinned installs)")



def test_ci_runs_real_search_provider_probe():
    """CI must exercise at least the keyless provider against the real network."""
    text = _CI.read_text(encoding="utf-8")

    match = re.search(
        r"(?ms)^  search-live:\s*\n"
        r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        text,
    )
    assert match, "ci.yml has no dedicated `search-live` job"

    body = match.group("body")
    assert 'OLYMPUS_SEARCH_LIVE: "1"' in body, (
        "search-live must explicitly enable real provider requests")
    assert "tests/test_websearch_live.py" in body, (
        "search-live must run the live provider end-to-end tests")
    assert "pytest" in body, (
        "search-live must execute the test through pytest")



def test_ci_runs_self_hosted_searxng_probe():
    """CI must verify Olympus against a real, locally hosted SearXNG server."""
    text = _CI.read_text(encoding="utf-8")

    match = re.search(
        r"(?ms)^  search-live:\s*\n"
        r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        text,
    )
    assert match, "ci.yml has no dedicated `search-live` job"

    body = match.group("body")
    assert "searxng/searxng:" in body, (
        "search-live must launch the official SearXNG container")
    assert "OLYMPUS_SEARXNG_URL: http://127.0.0.1:8888" in body, (
        "search-live must point Olympus at the local SearXNG instance")
    assert ".github/searxng/settings.yml" in body, (
        "search-live must mount the committed SearXNG configuration")
    assert "format=json" in body and "curl" in body, (
        "search-live must wait for the JSON search API before testing")

    settings_path = _ROOT / ".github" / "searxng" / "settings.yml"
    assert settings_path.exists(), (
        "the SearXNG CI settings file is missing")

    settings = settings_path.read_text(encoding="utf-8")
    assert "use_default_settings: true" in settings
    assert "formats:" in settings
    assert re.search(r"(?m)^\s*-\s*json\s*$", settings), (
        "SearXNG JSON output must be explicitly enabled")


def test_ci_runs_complete_windows_python_312_suite():
    body = _job_body("windows-test")
    assert "runs-on: windows-latest" in body
    assert 'python-version: "3.12"' in body
    assert "--require-hashes -r requirements.lock" in body
    assert 'python -m pip install -e ".[test]"' in body

    for command in (
        "python scripts/check_no_prerelease.py requirements.lock",
        "python -m compileall -q olympus",
        "python -m olympus capabilities --check",
        "python scripts/check_threat_model.py",
        "python scripts/noninterference_gate.py",
        "python -m pytest -q",
    ):
        assert command in body, f"Windows CI is missing `{command}`"

    forbidden = (
        "continue-on-error",
        "--ignore",
        "--continue-on-collection-errors",
    )
    for token in forbidden:
        assert token not in body, (
            f"Windows CI must not suppress full-suite failures with `{token}`")
    assert not re.search(r"(?m)(?:^|\s)-k(?:\s|$)", body), (
        "Windows CI must not filter the complete suite with -k")


def test_aggregate_gate_requires_windows_suite():
    body = _job_body("test-gate")
    match = re.search(r"needs:\s*\[([^\]]*)\]", body)
    assert match, "aggregate test gate has no inline needs list"
    needs = {item.strip() for item in match.group(1).split(",")}
    assert "windows-test" in needs, (
        "aggregate test gate does not require the Windows suite")
    assert "needs.windows-test.result" in body, (
        "aggregate test gate does not enforce the Windows result")


# --- session-journal latency telemetry: red-capable, but never PR-blocking ---
#
# The absolute p50/p99 append bounds moved out of the required suite because a
# shared GitHub runner cannot tell an Olympus regression from host load. They
# are enforced unchanged by a dedicated workflow instead. That relocation is
# only honest if the workflow really can go red AND really cannot block a PR,
# so both halves are pinned here rather than trusted.

_PERF_WF = _ROOT / ".github" / "workflows" / "sessionlog-performance.yml"


def test_sessionlog_latency_telemetry_workflow_exists():
    assert _PERF_WF.is_file(), (
        "the absolute append-latency contract was removed from the required "
        "suite, so its dedicated workflow must exist to still enforce it")


def test_sessionlog_latency_telemetry_never_runs_on_push_or_pull_request():
    """The whole point: a breach must not hold an unrelated PR shut."""
    text = _PERF_WF.read_text(encoding="utf-8")
    trigger = re.search(r"(?ms)^on:\s*\n(?P<body>.*?)(?=^jobs:\s*$)", text)
    assert trigger, "telemetry workflow has no parsable `on:` block"
    body = trigger.group("body")

    assert re.search(r"(?m)^\s{2}schedule:\s*$", body), (
        "telemetry must run on a schedule so a regression is still caught")
    assert re.search(r"(?m)^\s{2}workflow_dispatch:", body), (
        "telemetry must be runnable on demand")
    assert not re.search(r"(?m)^\s{2}push:", body), (
        "telemetry must NOT run on push")
    assert not re.search(r"(?m)^\s{2}pull_request:", body), (
        "telemetry must NOT run on pull_request — that would recreate the "
        "PR-blocking gate this design removed")


def test_sessionlog_latency_telemetry_is_red_capable():
    """Relocating the contract must not have quietly disarmed it."""
    text = _PERF_WF.read_text(encoding="utf-8")

    # Directive-level, not word-level: the hazard is a YAML key or a shell
    # escape hatch, and the workflow is allowed to EXPLAIN in prose why it has
    # none of them.
    assert not re.search(r"(?mi)^\s*continue-on-error\s*:", text), (
        "telemetry must stay red on a breach; `continue-on-error:` disarms it")
    assert not re.search(r"(?mi)^\s*uses:.*retry", text), (
        "telemetry must never retry a timing measurement")
    for token in ("|| true", "exit 0", "if: failure()"):
        assert token not in text, (
            f"telemetry must stay red on a breach; `{token}` would weaken it")
    assert "scripts/sessionlog_latency_telemetry.py" in text, (
        "telemetry workflow does not run the telemetry script")
    assert "runs-on: windows-latest" in text, (
        "the defect was exposed on Windows; measure it there")
    assert 'python-version: "3.12"' in text
    assert "pip install --require-hashes -r requirements.lock" in text, (
        "telemetry must install through the repository's pinned procedure")

    # Exactly one measurement per run. Counts INVOCATIONS, not mentions — the
    # header comment names the script when explaining the design, and that
    # must not read as a second execution.
    assert text.count("python scripts/sessionlog_latency_telemetry.py") == 1, (
        "the telemetry script must be executed exactly once per run")

    # The artifact must survive a red run — that is when it is worth reading.
    assert "actions/upload-artifact@v4" in text
    upload = text.index("actions/upload-artifact@v4")
    assert "if: always()" in text[upload:upload + 200], (
        "the telemetry artifact must upload with `if: always()`")


def test_sessionlog_latency_telemetry_is_not_in_the_required_gate():
    """It must not be wired into the required `CI / test` aggregate context."""
    ci = _CI.read_text(encoding="utf-8")
    assert "sessionlog-performance" not in ci
    assert "sessionlog_latency_telemetry" not in ci
    needs = re.search(r"needs:\s*\[([^\]]*)\]", _job_body("test-gate"))
    assert needs and "sessionlog" not in needs.group(1), (
        "telemetry must not be a dependency of the required aggregate gate")


def test_required_suite_no_longer_asserts_absolute_append_latency():
    """The moved contract must not still be collected by the default suite.

    Guards against the old assertions surviving by accident, which would leave
    the PR gate exposed to host load exactly as before.
    """
    perf = (_ROOT / "tests" / "test_val_performance.py").read_text(
        encoding="utf-8")
    for gone in ("def test_sessionlog_append_latency_within_loose_bounds",
                 "def test_sessionlog_append_fsync_always_within_loose_bounds"):
        assert gone not in perf, (
            f"{gone} still exists in the required suite; its absolute "
            f"wall-clock contract belongs to the telemetry runner now")

    # ...and the numbers themselves must still be enforced somewhere.
    telemetry = (_ROOT / "scripts" / "sessionlog_latency_telemetry.py"
                 ).read_text(encoding="utf-8")
    for preserved in ("60.0", "200.0", "100.0", "400.0", "120"):
        assert preserved in telemetry, (
            f"preserved threshold {preserved} is missing from the telemetry "
            f"contract — the bound must be moved, never dropped")


# --- usage-contention telemetry: red-capable, but never PR-blocking ----------
#
# The wall-clock verdicts on `usage.record` contention moved out of the required
# suite after the v0.27.3 Linux 3.11 leg failed on p99 outliers while every
# deterministic guarantee held. That relocation is only honest if the workflow
# really can go red, really cannot block a PR, and really still covers every
# platform that used to enforce the contract — so all three are pinned rather
# than trusted.

_UCT_WF = _ROOT / ".github" / "workflows" / "usage-contention-performance.yml"
_UCT_SCRIPT = _ROOT / "scripts" / "usage_contention_telemetry.py"


def test_usage_contention_telemetry_workflow_exists():
    assert _UCT_WF.is_file(), (
        "the contention wall-clock verdicts were removed from the required "
        "suite, so their dedicated workflow must exist to still enforce them")
    assert _UCT_SCRIPT.is_file()


def test_usage_contention_telemetry_never_runs_on_push_or_pull_request():
    """The whole point: a breach must not hold an unrelated PR shut."""
    text = _UCT_WF.read_text(encoding="utf-8")
    trigger = re.search(r"(?ms)^on:\s*\n(?P<body>.*?)(?=^jobs:\s*$)", text)
    assert trigger, "telemetry workflow has no parsable `on:` block"
    body = trigger.group("body")

    assert re.search(r"(?m)^\s{2}schedule:\s*$", body)
    assert re.search(r"(?m)^\s{2}workflow_dispatch:", body)
    assert not re.search(r"(?m)^\s{2}push:", body), (
        "telemetry must NOT run on push")
    assert not re.search(r"(?m)^\s{2}pull_request:", body), (
        "telemetry must NOT run on pull_request — that would recreate the "
        "PR-blocking gate this design removed")


def test_usage_contention_telemetry_does_not_collide_with_sessionlog_schedule():
    """Two weekly telemetry jobs must not contend for the same runners."""
    def _crons(path):
        return set(re.findall(r'cron:\s*"([^"]+)"',
                              path.read_text(encoding="utf-8")))

    mine = _crons(_UCT_WF)
    assert mine == {"0 9 * * 1"}, f"expected Monday 09:00 UTC, got {mine}"
    assert not (mine & _crons(_PERF_WF)), (
        "usage-contention telemetry shares a cron slot with the sessionlog "
        "telemetry; they must not overlap")


def test_usage_contention_telemetry_covers_every_previously_enforcing_platform():
    """Relocating the contract must not silently drop a matrix leg."""
    text = _UCT_WF.read_text(encoding="utf-8")
    body = re.search(r"(?ms)^  usage-contention:\s*\n(?P<b>.*)", text)
    assert body, "no `usage-contention` job"
    legs = set(re.findall(
        r'os:\s*(\S+)\s*\n\s*python-version:\s*"(\d+\.\d+)"', body.group("b")))
    assert legs == {
        ("ubuntu-latest", "3.10"),
        ("ubuntu-latest", "3.11"),      # the leg that exposed the defect
        ("ubuntu-latest", "3.12"),
        ("ubuntu-latest", "3.13"),
        ("windows-latest", "3.12"),
    }, legs
    assert "fail-fast: false" in body.group("b"), (
        "one red leg must not cancel the others; every platform's evidence "
        "matters")


def test_usage_contention_telemetry_is_red_capable():
    """Relocating the contract must not have quietly disarmed it."""
    text = _UCT_WF.read_text(encoding="utf-8")

    # Directive-level, not word-level: the workflow is allowed to EXPLAIN in
    # prose why it has none of these.
    assert not re.search(r"(?mi)^\s*continue-on-error\s*:", text), (
        "telemetry must stay red on a breach; `continue-on-error:` disarms it")
    assert not re.search(r"(?mi)^\s*uses:.*retry", text), (
        "telemetry must never retry a timing measurement")
    for token in ("|| true", "exit 0", "if: failure()"):
        assert token not in text, (
            f"telemetry must stay red on a breach; `{token}` would weaken it")

    assert "scripts/usage_contention_telemetry.py" in text
    assert "pip install --require-hashes -r requirements.lock" in text, (
        "telemetry must install through the repository's pinned procedure")
    assert "pip install -e . --no-deps" in text

    # Exactly one invocation per job. Counts INVOCATIONS, not mentions — the
    # header comment names the script when explaining the design.
    assert text.count("python scripts/usage_contention_telemetry.py") == 1

    assert "actions/upload-artifact@v4" in text
    upload = text.index("actions/upload-artifact@v4")
    assert "if: always()" in text[upload:upload + 200], (
        "the telemetry artifact must upload with `if: always()`")
    # Five matrix legs write the same filename, so the artifact NAME must vary.
    assert "${{ matrix.os }}" in text[upload:upload + 400], (
        "the artifact name must carry the matrix leg or the jobs collide")


def test_usage_contention_telemetry_is_not_in_the_required_gate():
    ci = _CI.read_text(encoding="utf-8")
    assert "usage-contention" not in ci
    assert "usage_contention_telemetry" not in ci
    needs = re.search(r"needs:\s*\[([^\]]*)\]", _job_body("test-gate"))
    assert needs and "usage-contention" not in needs.group(1), (
        "telemetry must not be a dependency of the required aggregate gate")


def test_usage_contention_thresholds_and_repetitions_survive_in_telemetry():
    """Every moved number must still be enforced somewhere."""
    telemetry = _UCT_SCRIPT.read_text(encoding="utf-8")
    for preserved in ('"repetitions": 5', '"quorum": 3', '"per_thread": 50',
                      '"max_start_skew_ms": 100.0', '"p50_floor_ms": 2.0',
                      '"p50_serialisation_factor": 1.5',
                      '"p99_max_ms": 500.0', '"min_calls_per_s": 100.0',
                      '"first_touch_max_ms": 2000.0'):
        assert preserved in telemetry, (
            f"preserved contract term `{preserved}` is missing — the bound "
            f"must be moved, never dropped")
    # The levels still come from the shared helper, so 1 / cap / 16 is pinned.
    assert "contention_levels()" in telemetry


def test_required_suite_no_longer_renders_contention_wall_clock_verdicts():
    """The moved verdicts must not still be decided by the required suite."""
    perf = (_ROOT / "tests" / "test_val_performance.py").read_text(
        encoding="utf-8")
    assert "def test_usage_ledger_tail_under_concurrency" not in perf, (
        "the wall-clock-named contention test still exists in the required "
        "suite; its verdicts belong to the telemetry runner now")
    assert "def test_usage_ledger_accounting_under_concurrency_is_exact" in perf, (
        "the deterministic accounting/integrity contract must remain required")
    for gone in ("_CONTENTION_QUORUM", "_MAX_START_SKEW_MS",
                 "_contention_failures"):
        assert gone not in perf, (
            f"{gone} still drives a decision in the required suite")
    # `_FIRST_TOUCH_MAX_MS` deliberately REMAINS: it is owned by the
    # fresh-interpreter subprocess test, which measures a genuinely cold first
    # `usage.record` and is out of scope here. What was removed is its use as a
    # verdict on the in-process contention sweep's 1-thread row.
    assert "_FIRST_TOUCH_MAX_MS" in perf
    assert perf.count("_FIRST_TOUCH_MAX_MS") == 3, (
        "the first-touch ceiling must be used only by the fresh-interpreter "
        "test (one definition plus its two references)")

    # The deterministic guarantees must all still be required there.
    for kept in ("ledger_read_errors", "ledger_calls", "expected_calls",
                 "expected_samples", "barrier_parties", "workers_started",
                 "_CONTENTION_REPS", "_CONTENTION_PER_THREAD"):
        assert kept in perf, (
            f"{kept} must remain a required deterministic guarantee")
