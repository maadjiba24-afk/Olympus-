"""The release pipeline must be fail-closed before it may ever run again.

Step 1L v4. The publish workflow is the only pipeline holding the OIDC PyPI
credential (`id-token: write`) and the only one that ever sees the release
signing seed. These contracts parse the workflow SEMANTICALLY — job graph,
permission maps, step shapes, artifact wiring — rather than trusting fragile
text matching, so a regression must change meaning, not merely wording, to
slip through. `actionlint` at a pinned, checksum-verified version then
proves GitHub would actually accept what the parser approved.

The workflow itself is `disabled_manually` on GitHub and must stay so until
the activation preconditions recorded in RELEASING.md are met; these tests
gate the CONTENT, not the activation.

GitHub-compatible parsing note: YAML 1.1 loads a bare `on` key as the
boolean True. `_on()` handles both spellings so the trigger contract can
never be silently skipped because of that misparse.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WF_PATH = _ROOT / ".github" / "workflows" / "publish.yml"
_LOCK_PATH = _ROOT / "requirements-publish.lock"
_LOCK_IN_PATH = _ROOT / "requirements-publish.in"
_SIGN_LOCK_PATH = _ROOT / "requirements-signing.lock"
_SIGN_LOCK_IN_PATH = _ROOT / "requirements-signing.in"
_RUNTIME_LOCK_PATH = _ROOT / "requirements.lock"
_RELEASING = _ROOT / "RELEASING.md"
_HELPER = _ROOT / "scripts" / "release_pipeline.py"

# The immutable pins this pipeline may execute, resolved from the upstream
# repositories. The comment beside each `uses:` must name the exact release
# the SHA was audited from — `# v4` would hide which code was reviewed.
_PINS = {
    "actions/checkout": (
        "11d5960a326750d5838078e36cf38b85af677262", "v4.4.0"),
    "actions/setup-python": (
        "a26af69be951a213d495a4c3e4e4022e16d87065", "v5.6.0"),
    "actions/upload-artifact": (
        "ea165f8d65b6e75b540449e92b4886f43607fa02", "v4.6.2"),
    "actions/download-artifact": (
        "d3f86a106a0bac45b974a628896c90dbdf5c8093", "v4.3.0"),
    "pypa/gh-action-pypi-publish": (
        "dc37677b2e1c63e2034f94d8a5b11f265b73ba33", "v1.14.2"),
}

_SHA_RE = re.compile(r"\A[^@\s]+@([0-9a-f]{40})\Z")

# actionlint is fetched at an EXACT version whose release archive checksum
# is verified before the binary is executed.
ACTIONLINT_VERSION = "1.7.7"
ACTIONLINT_SHA256 = {
    "linux_amd64":
        "023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757",
}

_JOBS = ("verify", "sign", "build", "inspect", "publish")
_SOURCE_JOBS = ("verify", "sign", "build", "inspect")
_PYTHON_JOBS = _SOURCE_JOBS


def _raw() -> str:
    return _WF_PATH.read_text(encoding="utf-8")


def _wf() -> dict:
    data = yaml.safe_load(_raw())
    assert isinstance(data, dict), "publish.yml did not parse to a mapping"
    return data


def _on(wf: dict):
    if "on" in wf:
        return wf["on"]
    return wf.get(True)


def _jobs(wf: dict) -> dict:
    jobs = wf.get("jobs")
    assert isinstance(jobs, dict), "publish.yml has no jobs mapping"
    return jobs


def _steps(job: dict) -> list:
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, "job has no steps"
    return steps


def _run_text(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in _steps(job)
                     if isinstance(s, dict))


def _uses_of(job: dict) -> list[str]:
    return [s["uses"] for s in _steps(job)
            if isinstance(s, dict) and "uses" in s]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# --- trigger: dispatch only ----------------------------------------------------

def test_the_on_key_is_parsed_even_when_yaml_reads_it_as_boolean():
    wf = _wf()
    trigger = _on(wf)
    assert trigger is not None, (
        "no trigger found under either 'on' or the YAML-1.1 boolean key")
    assert isinstance(trigger, dict)


def test_the_workflow_is_dispatch_only_with_a_required_tag_input():
    """A tag-push trigger would let an OLD tag execute an OBSOLETE copy of
    this workflow; dispatch always runs the definition on the ref it was
    launched from."""
    trigger = _on(_wf())
    assert set(trigger) == {"workflow_dispatch"}, (
        f"publish must be dispatch-only, got {sorted(map(str, trigger))}")
    inputs = (trigger["workflow_dispatch"] or {}).get("inputs")
    assert isinstance(inputs, dict) and "tag" in inputs
    tag = inputs["tag"]
    assert tag.get("required") is True, "the tag input must be required"
    assert tag.get("type") == "string"


def test_there_is_no_tag_push_trigger_anywhere():
    trigger = _on(_wf())
    assert "push" not in trigger, "the tag-push trigger must be removed"
    assert "tags:" not in _raw().split("jobs:")[0], (
        "no tag filter may remain in the trigger block")


def test_top_level_permissions_are_contents_read_only():
    assert _wf().get("permissions") == {"contents": "read"}


def test_concurrency_is_scoped_to_the_immutable_release_tag():
    concurrency = _wf().get("concurrency") or {}
    assert concurrency.get("group") == "publish-${{ inputs.tag }}", (
        "each release tag must have one concurrency group")
    assert concurrency.get("cancel-in-progress") is False, (
        "a newer dispatch must not cancel a release already in flight")


# --- the five-job graph --------------------------------------------------------

def test_the_job_graph_is_exactly_verify_sign_build_inspect_publish():
    """Post-build inspection is a fresh trust boundary, not another step on
    the build runner that produced the untrusted distributions."""
    jobs = _jobs(_wf())
    assert set(jobs) == set(_JOBS), f"unexpected job set: {sorted(jobs)}"
    assert "needs" not in jobs["verify"], "verify must be the root job"

    def needs(job):
        value = jobs[job].get("needs")
        return value if isinstance(value, list) else [value]

    assert needs("sign") == ["verify"]
    assert needs("build") == ["sign"]
    assert needs("inspect") == ["sign", "build"], (
        "inspect needs both direct dependencies so both immutable artifact "
        "IDs are available in the needs context")
    assert needs("publish") == ["inspect"]


def test_every_job_pins_the_runner_and_has_a_finite_timeout():
    for name, job in _jobs(_wf()).items():
        assert job.get("runs-on") == "ubuntu-24.04", (
            f"{name} uses a drifting runner selector")
        timeout = job.get("timeout-minutes")
        assert type(timeout) is int and 1 <= timeout <= 120, (
            f"{name} needs a finite timeout of at most two hours")


def test_every_python_job_pins_the_exact_approved_interpreter():
    jobs = _jobs(_wf())
    for name in _PYTHON_JOBS:
        setup = [s for s in _steps(jobs[name])
                 if "uses" in s
                 and s["uses"].startswith("actions/setup-python@")]
        assert len(setup) == 1, f"{name} must set Python up exactly once"
        assert (setup[0].get("with") or {}).get("python-version") == "3.12.3", (
            f"{name} must use the exact interpreter used to prove the locks")
    assert not any(use.startswith("actions/setup-python@")
                   for use in _uses_of(jobs["publish"])), (
        "the credentialed publish job must not add a Python toolchain")


def test_per_job_permissions_are_least_privilege():
    jobs = _jobs(_wf())
    for name in _SOURCE_JOBS:
        assert jobs[name].get("permissions") == {"contents": "read"}, (
            f"{name} must hold no OIDC permission")
    assert jobs["publish"].get("permissions") == {"contents": "read",
                                                  "id-token": "write"}


def test_oidc_permission_exists_only_in_the_publish_job():
    wf = _wf()
    for name, job in _jobs(wf).items():
        perms = job.get("permissions") or {}
        if name == "publish":
            assert perms.get("id-token") == "write"
        else:
            assert "id-token" not in perms, (
                f"job {name} must never hold the publish credential")
    assert "id-token" not in (wf.get("permissions") or {})


def test_the_signing_seed_and_the_pypi_credential_never_share_an_environment():
    jobs = _jobs(_wf())
    assert "environment" not in jobs["verify"]
    for name in ("build", "inspect"):
        assert "environment" not in jobs[name], (
            f"{name} must stay outside every credentialed environment")
    assert jobs["sign"].get("environment") == "release-signing"
    assert jobs["publish"].get("environment") == "pypi"
    assert jobs["sign"]["environment"] != jobs["publish"]["environment"]


# --- no source transfer: every job checks out the release commit ---------------

def test_every_source_job_checks_out_the_exact_release_commit():
    """v2 shipped a source snapshot from verify to sign, so the manifest,
    the validator and the lockfile all travelled inside the blob they were
    meant to attest. Each job must derive its inputs from github.sha."""
    jobs = _jobs(_wf())
    for name in _SOURCE_JOBS:
        checkouts = [s for s in _steps(jobs[name])
                     if "uses" in s
                     and s["uses"].startswith("actions/checkout@")]
        assert len(checkouts) == 1, (
            f"{name} must check out exactly once, got {len(checkouts)}")
        with_ = checkouts[0].get("with") or {}
        assert with_.get("ref") == "${{ github.sha }}", (
            f"{name} must check out the exact release commit")
        assert with_.get("persist-credentials") is False, (
            f"{name} must not leave a credential in the checkout")


def test_the_verify_job_uses_full_history_for_the_commit_gate():
    checkout = next(s for s in _steps(_jobs(_wf())["verify"])
                    if "uses" in s
                    and s["uses"].startswith("actions/checkout@"))
    assert (checkout.get("with") or {}).get("fetch-depth") == 0


def test_no_source_snapshot_artifact_exists():
    """The only artifacts are the signed manifest and the distributions."""
    jobs = _jobs(_wf())
    uploaded = []
    for job in jobs.values():
        for step in _steps(job):
            if "uses" in step and step["uses"].startswith(
                    "actions/upload-artifact@"):
                uploaded.append((step.get("with") or {}).get("name"))
    assert sorted(uploaded) == ["dist", "signed-manifest"], (
        f"unexpected artifacts: {uploaded}")


def test_the_sign_job_downloads_nothing():
    """It checks out the commit itself; taking any artifact would reopen the
    self-attestation hole."""
    downloads = [s for s in _steps(_jobs(_wf())["sign"])
                 if "uses" in s
                 and s["uses"].startswith("actions/download-artifact@")]
    assert downloads == [], "the signing job must not consume artifacts"


def test_the_build_job_downloads_only_the_signed_manifest():
    downloads = [s for s in _steps(_jobs(_wf())["build"])
                 if "uses" in s
                 and s["uses"].startswith("actions/download-artifact@")]
    assert len(downloads) == 1
    with_ = downloads[0].get("with") or {}
    assert with_.get("artifact-ids") == \
        "${{ needs.sign.outputs.artifact-id }}"
    assert "name" not in with_, "the signed manifest must be fetched by ID"
    assert with_.get("path") == "${{ runner.temp }}/signed", (
        "the downloaded manifest must not overwrite checkout content")


def test_the_inspect_job_downloads_the_manifest_and_dist_by_artifact_id():
    downloads = [s for s in _steps(_jobs(_wf())["inspect"])
                 if "uses" in s
                 and s["uses"].startswith("actions/download-artifact@")]
    assert len(downloads) == 2, (
        "inspect must independently fetch exactly the manifest and dist")
    ids = {(step.get("with") or {}).get("artifact-ids")
           for step in downloads}
    assert ids == {"${{ needs.sign.outputs.artifact-id }}",
                   "${{ needs.build.outputs.artifact-id }}"}
    for step in downloads:
        with_ = step.get("with") or {}
        assert "name" not in with_, "inspect artifacts must be fetched by ID"
        assert str(with_.get("path", "")).startswith("${{ runner.temp }}/"), (
            "inspect downloads must live outside the checkout")


# --- secrets containment -------------------------------------------------------

def test_only_the_sign_job_references_a_secret():
    jobs = _jobs(_wf())
    for name, job in jobs.items():
        occurrences = yaml.safe_dump(job).count("secrets.")
        expected = 1 if name == "sign" else 0
        assert occurrences == expected, (
            f"job {name} references {occurrences} secret(s)")


def test_the_signing_seed_is_step_scoped_to_the_signing_step_only():
    raw = _raw()
    assert raw.count("secrets.OLYMPUS_SIGNING_SEED") == 1

    jobs = _jobs(_wf())
    for name, job in jobs.items():
        assert "env" not in job, (
            f"job {name} carries a job-level env; secrets must be scoped to "
            f"single steps")

    holders = []
    for name, job in jobs.items():
        for step in _steps(job):
            env = step.get("env") or {}
            for value in env.values():
                if "OLYMPUS_SIGNING_SEED" in str(value):
                    holders.append((name, step.get("name", "?"),
                                    "run" in step))
    assert len(holders) == 1, f"seed visible to {holders!r}"
    job_name, step_name, is_run = holders[0]
    assert job_name == "sign"
    assert "sign" in step_name.lower()
    assert is_run


# --- action pinning ------------------------------------------------------------

def test_every_action_reference_is_a_full_forty_hex_sha():
    for name, job in _jobs(_wf()).items():
        for uses in _uses_of(job):
            assert _SHA_RE.match(uses), (
                f"job {name}: {uses!r} is not pinned to a 40-hex commit")


def test_every_pin_matches_the_recorded_immutable_sha():
    seen = set()
    for job in _jobs(_wf()).values():
        for uses in _uses_of(job):
            action, sha = uses.split("@", 1)
            assert action in _PINS, f"unexpected action {action!r}"
            assert sha == _PINS[action][0], (
                f"{action} pinned to {sha}, expected {_PINS[action][0]}")
            seen.add(action)
    assert seen == set(_PINS), f"missing actions: {set(_PINS) - seen}"


def test_every_uses_line_names_the_exact_release_in_a_comment():
    for line in _raw().splitlines():
        if "uses:" not in line:
            continue
        action = line.split("uses:", 1)[1].strip().split("@", 1)[0]
        _sha, release = _PINS[action]
        comment = line.split("#", 1)
        assert len(comment) == 2, f"no release comment on: {line.strip()}"
        assert comment[1].strip() == release, (
            f"comment on {action} must be exactly {release!r}, "
            f"got {comment[1].strip()!r}")


# --- installation hygiene ------------------------------------------------------

def _install_lines(job: dict) -> list[str]:
    return [line.strip() for line in _run_text(job).splitlines()
            if "pip install" in line]


def test_every_pip_install_is_hash_locked_or_the_editable_no_deps_form():
    """Checks the FULL install line: v2's version skipped any line
    containing --require-hashes, so `--require-hashes ... -U requests`
    would have passed."""
    for name, job in _jobs(_wf()).items():
        for line in _install_lines(job):
            editable = ("-e ." in line and "--no-deps" in line
                        and "--no-build-isolation" in line)
            hashed = "--require-hashes" in line and " -r " in line
            assert editable or hashed, (
                f"job {name}: unpinned install: {line!r}")
            if hashed:
                assert "-e " not in line and "-U" not in line \
                    and "--upgrade" not in line, (
                    f"job {name}: hash-locked install carries extra "
                    f"unpinned arguments: {line!r}")


def test_no_floating_upgrades_and_no_build_isolation():
    raw = _raw()
    assert "--upgrade" not in raw and " -U " not in raw
    build_lines = [line for line in _run_text(_jobs(_wf())["build"]).splitlines()
                   if "-m build" in line]
    assert build_lines, "the build job must build the distributions"
    for line in build_lines:
        assert "--no-isolation" in line, (
            "build isolation would resolve the backend from the network")
        assert '--outdir "$RUNNER_TEMP/dist"' in line, (
            "build outputs must be staged outside the source checkout")


def test_the_unified_lock_is_the_only_lock_verify_build_and_inspect_install():
    """v2 installed requirements.lock then a publish lock sequentially; the
    two disagreed on 9 pins and the second silently replaced the first."""
    jobs = _jobs(_wf())
    for name in ("verify", "build", "inspect"):
        locks = re.findall(r"-r (requirements[\w.-]*\.lock)",
                           _run_text(jobs[name]))
        assert locks == ["requirements-publish.lock"], (
            f"{name} must install exactly the unified release lock, "
            f"got {locks}")


def test_the_sign_job_installs_only_the_minimal_signing_lock():
    text = _run_text(_jobs(_wf())["sign"])
    locks = re.findall(r"-r (requirements[\w.-]*\.lock)", text)
    assert locks == ["requirements-signing.lock"]
    for forbidden in ("-m build", "twine", "-e ."):
        assert forbidden not in text, (
            f"the signing job must not use {forbidden!r}")


def test_the_test_toolchain_is_hash_locked_not_an_unpinned_extra():
    raw = _raw()
    assert ".[test]" not in raw and "'.[test]'" not in raw
    names = _lock_names(_LOCK_PATH)
    for required in ("pytest", "pytest-timeout", "pyyaml"):
        assert required in names, (
            f"{required} must be hash-locked for the verify job's suite")


def test_every_python_job_enforces_the_approved_pip_version():
    for source_path in (_LOCK_IN_PATH, _SIGN_LOCK_IN_PATH):
        source = source_path.read_text(encoding="utf-8")
        assert re.search(r"(?m)^pip==24\.0\s*$", source), (
            f"{source_path.name} must make pip 24.0 part of the hashed lock")
    for lock_path in (_LOCK_PATH, _SIGN_LOCK_PATH):
        assert _lock_pins(lock_path).get("pip") == "24.0", (
            f"{lock_path.name} must hash-pin the pip it executes")
    for name in _PYTHON_JOBS:
        job = _jobs(_wf())[name]
        text = _run_text(job)
        assert re.search(
            r"release_pipeline\.py check-toolchain \\\s*\n\s*"
            r'--python-version "3\.12\.3" '
            r'--pip-version "24\.0"', text), (
                f"{name} must prove the interpreter and installed pip")
        toolchain_index = next(
            i for i, step in enumerate(_steps(job))
            if "check-toolchain" in step.get("run", ""))
        setup_index = next(
            i for i, step in enumerate(_steps(job))
            if step.get("uses", "").startswith("actions/setup-python@"))
        pip_indexes = [
            i for i, step in enumerate(_steps(job))
            if "pip install" in step.get("run", "")]
        assert setup_index < toolchain_index, (
            f"{name} must check the Python that setup-python selected")
        if pip_indexes:
            assert toolchain_index < min(pip_indexes), (
                f"{name} must prove pip before pip installs anything")


# --- runtime gates -------------------------------------------------------------

def _step_named(job: dict, needle: str) -> dict:
    for step in _steps(job):
        if needle in (step.get("name") or "").lower():
            return step
    raise AssertionError(f"no step matching {needle!r}")


def _run_containing(job: dict, needle: str) -> str:
    matches = [step.get("run", "") for step in _steps(job)
               if needle in step.get("run", "")]
    assert len(matches) == 1, (
        f"expected exactly one run step containing {needle!r}, "
        f"found {len(matches)}")
    return matches[0]


def test_verify_runs_every_dispatch_gate_through_the_tested_helper():
    text = _run_text(_jobs(_wf())["verify"])
    for command, why in (
        ("release_pipeline.py check-dispatch-ref", "protected main ref"),
        ("release_pipeline.py check-commit", "protected-tip equality"),
        ("release_pipeline.py check-tag-points-at", "tag peels to the sha"),
        ("release_pipeline.py check-tag", "tag/source/runtime identity"),
    ):
        assert command in text, f"verify is missing the {why} gate"
    assert "${GITHUB_SHA}" in text
    assert "${RELEASE_TAG}" in text


def test_the_dispatch_gates_bind_their_arguments_not_merely_appear():
    """A bare substring would pass even if the command were invoked with
    the wrong argument."""
    text = _run_text(_jobs(_wf())["verify"])
    assert re.search(
        r'release_pipeline\.py check-dispatch-ref "\$\{GITHUB_REF\}" '
        r'"\$\{GITHUB_REF_TYPE\}"', text)
    assert re.search(
        r'release_pipeline\.py check-commit "\$\{GITHUB_SHA\}"', text)
    assert re.search(
        r'release_pipeline\.py check-tag-points-at \\\s*\n\s*'
        r'"\$\{RELEASE_TAG\}" "\$\{GITHUB_SHA\}"', text)
    assert re.search(
        r'release_pipeline\.py check-tag "\$\{RELEASE_TAG\}"', text)


def test_the_tag_input_reaches_the_gates_only_through_an_env_binding():
    """`${{ inputs.tag }}` interpolated directly into a shell line would be
    an injection point; it must arrive as an environment value."""
    for job in _jobs(_wf()).values():
        for step in _steps(job):
            assert "${{ inputs.tag }}" not in step.get("run", ""), (
                "the untrusted dispatch input must not be interpolated into "
                "a shell program")
            for key, value in (step.get("env") or {}).items():
                if value == "${{ inputs.tag }}":
                    assert key == "RELEASE_TAG"


def test_verify_runs_the_established_gates_and_the_full_suite():
    text = _run_text(_jobs(_wf())["verify"])
    for command in (
        "python scripts/check_no_prerelease.py requirements.lock",
        "python scripts/check_no_prerelease.py requirements-publish.lock",
        "python scripts/check_no_prerelease.py requirements-signing.lock",
        "python -m compileall -q olympus",
        "python -m olympus capabilities --check",
        "python scripts/check_threat_model.py",
        "python scripts/noninterference_gate.py",
        "python -m pytest -q",
    ):
        assert command in text, f"verify is missing the gate: {command}"


# --- signing, building, and independent inspection -----------------------------

def test_the_sign_job_uses_the_narrow_signer_not_the_olympus_cli():
    """`python -m olympus sign` imports the whole CLI graph and dies on a
    missing anthropic in the minimal signing environment."""
    text = _run_text(_jobs(_wf())["sign"])
    assert "olympus sign" not in text, (
        "the CLI entry point cannot run in the signing environment")
    assert "release_pipeline.py sign-manifest" in text
    assert '--manifest "$RUNNER_TEMP/signed/verification.json"' in text
    assert '--expected-commit "${GITHUB_SHA}"' in text
    assert "python -B " in text, "signing must not write bytecode"
    step = _step_named(_jobs(_wf())["sign"], "sign the release manifest")
    assert (step.get("env") or {}).get("PYTHONDONTWRITEBYTECODE") == "1"


def test_the_build_job_verifies_before_it_builds():
    build_job = _jobs(_wf())["build"]
    steps = _steps(build_job)
    order = []
    for step in steps:
        run = step.get("run", "")
        if "verify-manifest" in run:
            order.append("verify")
        if "cp --" in run and "olympus/verification.json" in run:
            order.append("place-manifest")
        if "-m build" in run:
            order.append("build")
    for required in ("verify", "place-manifest", "build"):
        assert required in order, f"the build job never runs {required}"
    assert order == ["verify", "place-manifest", "build"], (
        "the external manifest must be verified, then placed, then built")

    text = _run_text(build_job)
    verify = _run_containing(build_job, "verify-manifest")
    assert '--manifest "$RUNNER_TEMP/signed/verification.json"' in verify
    assert '--expected-commit "${GITHUB_SHA}"' in verify
    assert "olympus/verification.json" not in verify, (
        "build must verify the downloaded file without copying it into source")

    placement = _run_containing(build_job, "olympus/verification.json")
    assert placement.splitlines() == [
        "set -euo pipefail",
        'cp -- "$RUNNER_TEMP/signed/verification.json" '
        "olympus/verification.json",
    ], "the placement step must copy only the already-verified artifact"
    assert "check-dists" not in text and "twine check" not in text, (
        "post-build approval belongs on the fresh inspect runner")


def test_build_does_not_dirty_source_with_an_editable_install():
    """Editable setuptools installs create untracked egg-info before verify."""
    text = _run_text(_jobs(_wf())["build"])
    assert "pip install -e" not in text and "pip install --editable" not in text


def test_inspect_reauthenticates_the_manifest_then_checks_the_exact_dist():
    steps = _steps(_jobs(_wf())["inspect"])
    order = []
    for step in steps:
        run = step.get("run", "")
        if "verify-manifest" in run:
            order.append("verify")
        if "check-dists" in run:
            order.append("check-dists")
        if "twine check" in run:
            order.append("twine")
    assert order == ["verify", "check-dists", "twine"], (
        "inspect must cryptographically verify before approving either dist")

    inspect = _jobs(_wf())["inspect"]
    text = _run_text(inspect)
    manifest = '--manifest "$RUNNER_TEMP/signed/verification.json"'
    commit = '--expected-commit "${GITHUB_SHA}"'
    verify = _run_containing(inspect, "verify-manifest")
    check = _run_containing(inspect, "check-dists")
    for command in (verify, check):
        assert manifest in command, (
            "both inspectors must use the immutable downloaded manifest")
        assert commit in command, (
            "both inspectors must bind the immutable release commit")
    assert '--dist "$RUNNER_TEMP/dist"' in check
    assert '--version "${RELEASE_TAG#v}"' in check
    twine = _run_containing(inspect, "twine check")
    assert re.search(r'python -m twine check --strict '
                     r'"\$RUNNER_TEMP"/dist/\*', twine), (
        "quote the variable but leave the distribution glob expandable")


def test_inspect_has_no_seed_oidc_or_environment():
    inspect = _jobs(_wf())["inspect"]
    assert "environment" not in inspect
    assert inspect.get("permissions") == {"contents": "read"}
    dumped = yaml.safe_dump(inspect)
    assert "secrets." not in dumped
    assert "OLYMPUS_SIGNING_SEED" not in dumped
    assert "id-token" not in (inspect.get("permissions") or {})


def test_the_helper_enforces_signer_identity_without_an_optional_argument():
    """v2 called check-manifest without --expected-key, so the trust branch
    never fired. Identity must be structurally unskippable."""
    source = _HELPER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    verifier = functions["verify_manifest_native"]
    keyword_defaults = dict(zip(
        (arg.arg for arg in verifier.args.kwonlyargs),
        verifier.args.kw_defaults,
    ))
    assert "expected_key" in keyword_defaults
    assert keyword_defaults["expected_key"] is None, (
        "expected_key must be a REQUIRED keyword-only argument, so a caller "
        "cannot silently skip signer identity by omitting it")
    assert keyword_defaults.get("expected_commit", object()) is None, (
        "expected_commit must also be structurally unskippable")
    assert "expected_key=pinned_key(root)" in source, (
        "the CLI must pass the pinned key")


# --- artifacts -----------------------------------------------------------------

def test_every_artifact_upload_is_fail_closed_and_short_lived():
    jobs = _jobs(_wf())
    expected = {"sign": "signed-manifest", "build": "dist"}
    for job_name, artifact in expected.items():
        uploads = [s for s in _steps(jobs[job_name])
                   if "uses" in s
                   and s["uses"].startswith("actions/upload-artifact@")]
        assert len(uploads) == 1, f"{job_name} must upload exactly once"
        with_ = uploads[0].get("with") or {}
        assert with_.get("name") == artifact
        assert with_.get("if-no-files-found") == "error"
        retention = with_.get("retention-days")
        assert isinstance(retention, int) and 1 <= retention <= 7


def test_the_sign_job_uploads_only_the_manifest_file():
    upload = next(s for s in _steps(_jobs(_wf())["sign"])
                  if "uses" in s
                  and s["uses"].startswith("actions/upload-artifact@"))
    assert upload["with"]["path"] == \
        "${{ runner.temp }}/signed/verification.json", (
        "uploading the post-signing workspace would let anything that "
        "runner did travel onward")


def test_the_build_job_uploads_dist_from_outside_the_checkout():
    upload = next(s for s in _steps(_jobs(_wf())["build"])
                  if "uses" in s
                  and s["uses"].startswith("actions/upload-artifact@"))
    assert upload["with"]["path"] == "${{ runner.temp }}/dist/*"


@pytest.mark.parametrize("job_name", ["sign", "build"])
def test_artifact_producers_expose_the_immutable_artifact_id(job_name):
    jobs = _jobs(_wf())
    outputs = jobs[job_name].get("outputs") or {}
    assert outputs.get("artifact-id") == \
        "${{ steps.upload.outputs.artifact-id }}"
    upload = next(s for s in _steps(jobs[job_name])
                  if "uses" in s
                  and s["uses"].startswith("actions/upload-artifact@"))
    assert upload.get("id") == "upload"


def test_inspect_forwards_exactly_the_dist_artifact_id_it_approved():
    outputs = _jobs(_wf())["inspect"].get("outputs") or {}
    assert outputs == {
        "artifact-id": "${{ needs.build.outputs.artifact-id }}",
    }


def test_publish_downloads_by_artifact_id_not_merely_by_name():
    """A name is ambiguous within a run; an id names the exact upload."""
    download = next(s for s in _steps(_jobs(_wf())["publish"])
                    if s["uses"].startswith("actions/download-artifact@"))
    with_ = download.get("with") or {}
    assert with_.get("artifact-ids") == \
        "${{ needs.inspect.outputs.artifact-id }}"
    assert "name" not in with_, (
        "downloading by name alongside an id would reintroduce ambiguity")
    assert with_.get("path") == "${{ runner.temp }}/dist"


def test_publish_contains_exactly_the_two_allowed_actions_and_no_shell():
    steps = _steps(_jobs(_wf())["publish"])
    assert len(steps) == 2, f"publish must hold two steps, got {len(steps)}"
    for step in steps:
        assert "run" not in step, "the credentialed job must execute no shell"
        assert "env" not in step
    download, publish = steps
    assert download["uses"].startswith("actions/download-artifact@")
    assert publish["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert (publish.get("with") or {}).get("packages-dir") == \
        "${{ runner.temp }}/dist"


# --- no fail-open anywhere -----------------------------------------------------

def test_no_fail_open_keys_or_shell_fragments():
    for mapping in _walk(_wf()):
        assert "continue-on-error" not in mapping
        condition = str(mapping.get("if", ""))
        assert "failure()" not in condition and "always()" not in condition
    raw = _raw()
    for fragment in ("|| true", "exit 0", "set +e"):
        assert fragment not in raw, f"fail-open shell fragment: {fragment!r}"


def test_every_multiline_run_fails_fast():
    for name, job in _jobs(_wf()).items():
        for step in _steps(job):
            run = step.get("run")
            if run and "\n" in run.strip():
                assert run.splitlines()[0].strip() == "set -euo pipefail", (
                    f"job {name} step {step.get('name', '?')!r}: multi-line "
                    f"shell must start with set -euo pipefail")


# --- actionlint ----------------------------------------------------------------

def _actionlint_binary(tmp_path: Path):
    """Fetch actionlint at an EXACT version and verify its checksum.

    Skipped only when the platform has no recorded checksum or the download
    is unavailable — an unverified binary must never be executed just to
    make a test go green.
    """
    import hashlib
    import platform
    import tarfile
    import urllib.error
    import urllib.request

    system = platform.system().lower()
    machine = platform.machine().lower()
    key = None
    if system == "linux" and machine in ("x86_64", "amd64"):
        key = "linux_amd64"
    if key is None or key not in ACTIONLINT_SHA256:
        pytest.skip(f"no verified actionlint checksum for {system}/{machine}")

    url = (f"https://github.com/rhysd/actionlint/releases/download/"
           f"v{ACTIONLINT_VERSION}/actionlint_{ACTIONLINT_VERSION}_{key}"
           f".tar.gz")
    archive = tmp_path / "actionlint.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            archive.write_bytes(response.read())
    except (urllib.error.URLError, OSError) as err:
        pytest.skip(f"actionlint download unavailable ({type(err).__name__})")

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest == ACTIONLINT_SHA256[key], (
        f"actionlint archive checksum mismatch: {digest}")

    with tarfile.open(archive) as tar:
        tar.extract(tar.getmember("actionlint"), path=tmp_path)
    binary = tmp_path / "actionlint"
    binary.chmod(0o755)
    return binary


def test_actionlint_accepts_the_publish_workflow(tmp_path):
    """YAML parsing proves well-formedness; actionlint proves GitHub would
    accept the expressions, contexts, and action inputs."""
    binary = _actionlint_binary(tmp_path)
    proc = subprocess.run(
        [str(binary), "-no-color", "-oneline", str(_WF_PATH)],
        capture_output=True, text=True, cwd=str(_ROOT))
    assert proc.returncode == 0, (
        f"actionlint rejected publish.yml:\n{proc.stdout}\n{proc.stderr}")


# --- the lockfiles -------------------------------------------------------------

def _lock_requirements(path: Path) -> list[str]:
    assert path.is_file(), f"{path.name} is missing"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") \
                or stripped.startswith("--hash"):
            continue
        out.append(stripped)
    return out


def _lock_names(path: Path) -> set[str]:
    return {re.split(r"[=<>!~\[; ]", req, 1)[0].lower()
            for req in _lock_requirements(path)}


def _lock_pins(path: Path) -> dict[str, str]:
    pins = {}
    for req in _lock_requirements(path):
        if "==" not in req:
            continue
        name, _, rest = req.partition("==")
        pins[name.strip().lower()] = rest.split()[0].strip().rstrip("\\").strip()
    return pins


@pytest.mark.parametrize("lock_path", [_LOCK_PATH, _SIGN_LOCK_PATH],
                         ids=["release", "signing"])
def test_every_lock_pins_everything_with_hashes(lock_path):
    text = lock_path.read_text(encoding="utf-8")
    requirements = _lock_requirements(lock_path)
    assert requirements, f"{lock_path.name} pins nothing"
    for req in requirements:
        name = re.split(r"[=<>!~\[; ]", req, 1)[0]
        assert "==" in req, f"unpinned requirement: {req!r}"
        assert re.search(
            rf"(?m)^{re.escape(name)}==[^\n]*(\\\n\s+--hash=sha256:|"
            rf"\s+--hash=sha256:)", text), (
            f"{name} carries no sha256 hash in {lock_path.name}")


def test_the_unified_lock_covers_runtime_test_and_build():
    names = _lock_names(_LOCK_PATH)
    for required in ("build", "twine", "setuptools", "pytest", "anthropic"):
        assert required in names, (
            f"{required} missing from the unified release lock")


def test_conflicting_locks_are_never_installed_into_one_environment():
    """AUDIT (medium): requirements.lock and the publish lock disagreed on
    ten pins, and v2 installed them SEQUENTIALLY — so the second install
    silently replaced pins from the first and the suite ran in an
    environment neither lock described.

    `requirements.lock` is the repository-wide runtime lock used by CI and
    is deliberately NOT modified by this change; the fix is that the release
    path never installs it. This test proves both halves: the conflict is
    real, and no job installs a second, conflicting lock.
    """
    runtime = _lock_pins(_RUNTIME_LOCK_PATH)
    unified = _lock_pins(_LOCK_PATH)
    conflicts = {name for name in set(runtime) & set(unified)
                 if runtime[name] != unified[name]}
    assert conflicts, (
        "if the two locks now agree, this guard is vacuous — re-check "
        "whether the release path still needs a single unified lock")

    jobs = _jobs(_wf())
    for name, job in jobs.items():
        installed = set(re.findall(r"-r (requirements[\w.-]*\.lock)",
                                   _run_text(job)))
        assert len(installed) <= 1, (
            f"job {name} installs {sorted(installed)} — two locks in one "
            f"environment is the silent-replacement defect")
        assert "requirements.lock" not in installed, (
            f"job {name} installs the runtime lock, which conflicts with "
            f"the unified release lock on {len(conflicts)} pin(s)")


def test_the_signing_lock_is_minimal_and_excludes_the_build_toolchain():
    names = _lock_names(_SIGN_LOCK_PATH)
    assert "cryptography" in names
    for forbidden in ("build", "twine", "pytest", "anthropic", "setuptools"):
        assert forbidden not in names, (
            f"{forbidden} must not be installed beside the signing seed")
    signing_source = _SIGN_LOCK_IN_PATH.read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*setuptools(?:\s|[<>=!~])", signing_source), (
        "the narrow signer does not need a build backend")


@pytest.mark.parametrize("spec_path, required", [
    (_LOCK_IN_PATH, ("build", "twine", "setuptools", "pip==24.0")),
    (_SIGN_LOCK_IN_PATH, ("cryptography", "pip==24.0")),
])
def test_every_lock_has_a_reproducible_pinned_source_spec(spec_path,
                                                          required):
    assert spec_path.is_file()
    text = spec_path.read_text(encoding="utf-8")
    for name in required:
        assert name in text
    assert "3.12.3" in text and "pip-compile" in text, (
        "lock evidence must name the exact approved interpreter")
    assert "Ubuntu 24.04" in text
    assert "pip==24.0" in text and "pip-tools==7.6.1" in text, (
        "the regeneration procedure must pin its own toolchain")


# --- documentation must match the pipeline ------------------------------------

def test_releasing_doc_records_every_activation_blocker():
    text = _RELEASING.read_text(encoding="utf-8").lower()
    for needle, why in (
        ("disabled", "the workflow freeze"),
        ("release-signing", "the separate signing environment"),
        ("protection", "environment protection"),
        ("admin bypass", "admin bypass must be addressed"),
        ("ruleset", "the v* tag ruleset"),
        ("olympus_signing_seed", "the seed-scope migration"),
        ("rotation", "seed rotation"),
        ("trusted publisher", "the PyPI binding verification"),
        ("mutable_publish_container=blocked", "the publisher container"),
        ("pypi_trust_binding=unverified", "the unverified PyPI binding"),
        ("activation authorization", "a separate reviewed authorization"),
    ):
        assert needle in text, f"RELEASING.md missing: {why}"


def test_releasing_doc_describes_the_dispatch_flow_not_a_tag_push():
    """AUDIT (high): the checklist still described the pre-1L flow, telling
    an operator to push a tag and expect a publish."""
    text = _RELEASING.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "workflow_dispatch" in text and "run workflow" in lowered, (
        "the doc must describe the dispatch flow")
    # Pushing a tag is still part of the flow — it creates the pointer the
    # run is checked against — so the literal command may remain. What must
    # be gone is any claim that pushing it PUBLISHES.
    assert "publishes nothing" in lowered, (
        "the doc must state plainly that a pushed tag publishes nothing")
    assert "pushing a version tag" not in lowered, (
        "the old 'a release is cut by pushing a tag' framing must be gone")
    tag_push = lowered.index("git push origin v0.16.0")
    dispatch = lowered.index("dispatch the release run")
    assert tag_push < dispatch, (
        "tagging must be described before, and separately from, dispatch")


def test_releasing_doc_protects_environments_for_the_dispatch_branch():
    text = _RELEASING.read_text(encoding="utf-8")
    lowered = text.lower()
    for environment in ("release-signing", "pypi"):
        start = lowered.index(environment)
        nearby = lowered[start:start + 700]
        assert "protected `main` branch" in nearby or \
            "protected main branch" in nearby, (
                f"{environment} must admit the branch that dispatches the run")
    assert "restrict deployments to `v*` tags" not in lowered, (
        "workflow_dispatch runs on main, not on the tag supplied as input")


def test_releasing_doc_documents_the_proven_two_ruleset_tag_design():
    """AUDIT (high): the v6 instruction specified ONE `v*` ruleset carrying
    creation + update + deletion with an empty bypass list. Because ruleset
    bypass is explicit opt-in — repository admins are NOT exempt by default —
    that configuration blocks ALL future `v*` tag creation, including by the
    release operator, making a release impossible. The correct composition is
    two rulesets, and the split was proven behaviourally before adoption."""
    text = _RELEASING.read_text(encoding="utf-8")
    lowered = text.lower()

    assert "immutable-tags" in lowered, "the immutability ruleset must be named"
    assert "controlled-tag-creation" in lowered, (
        "the creation-control ruleset must be named")

    immutable = lowered[lowered.index("immutable-tags"):]
    assert "update" in immutable and "deletion" in immutable
    assert "no bypass" in immutable or "empty bypass" in immutable, (
        "immutable-tags must record that nobody — admins included — bypasses")

    creation = lowered[lowered.index("controlled-tag-creation"):]
    assert "creation" in creation
    assert "admin" in creation, (
        "controlled-tag-creation must record the RepositoryRole admin bypass")


def test_releasing_doc_does_not_keep_the_self_defeating_single_ruleset():
    """The defective instruction must be GONE, not merely supplemented."""
    lowered = _RELEASING.read_text(encoding="utf-8").lower()
    assert "restrict creation; deny updates and deletions" not in lowered, (
        "the single-ruleset instruction would make releasing impossible")


def test_releasing_doc_records_that_the_tag_design_was_behaviourally_proven():
    lowered = _RELEASING.read_text(encoding="utf-8").lower()
    assert "ztest" in lowered, (
        "the doc must record the temporary pattern the split was proven on")
    for needle in ("creation", "bypass"):
        assert needle in lowered


def test_releasing_doc_does_not_instruct_the_forbidden_seed_placement():
    """AUDIT (high): 'One-time setup' told the operator to set the seed as a
    REPOSITORY secret — exactly what activation blocker 4 forbids."""
    text = _RELEASING.read_text(encoding="utf-8")
    assert "as a repository secret" not in text.lower(), (
        "the doc must not instruct repository-scoped seed placement")


def test_the_mutable_publisher_container_is_an_enforced_blocker():
    """The blocker stands; only its MECHANISM was recorded wrongly.

    v6 claimed a consumer run builds the action's Dockerfile and resolves
    `FROM python:3.13-slim` at run time. It does not: `create-docker-action.py`
    takes the Dockerfile branch only when `github.repository_id` equals the
    ACTION's own repo id, so every external consumer pulls a prebuilt GHCR
    image instead. The pipeline is still not fully content-addressed, because
    that image is addressed by a SHA-named — and therefore mutable — tag."""
    text = _RELEASING.read_text(encoding="utf-8")
    assert "MUTABLE_PUBLISH_CONTAINER=BLOCKED" in text
    workflow = _raw()
    assert "MUTABLE_PUBLISH_CONTAINER=BLOCKED" in workflow
    lowered = workflow.lower()
    assert "not fully" in lowered or "not content-addressed" in lowered, (
        "the workflow must not claim full content addressing while the "
        "publisher container is unresolved")


def test_no_document_still_claims_the_dockerfile_is_built_at_consumer_run_time():
    """AUDIT (high): the obsolete claim must be gone from BOTH files, not
    softened. Leaving it means a reviewer validates the wrong mechanism."""
    for path in (_RELEASING, _WF_PATH):
        flowed = _flowed(path)
        assert "python:3.13-slim" not in flowed, (
            f"{path.name} still names the base image as a run-time risk")
        for obsolete in (
            "resolved at run time",
            "resolved at runtime",
            "builds and runs a docker action from its own",
            "its own checked-out `dockerfile`",
        ):
            assert obsolete not in flowed, (
                f"{path.name} still carries the obsolete claim: {obsolete!r}")


def test_both_documents_state_that_consumers_pull_a_prebuilt_ghcr_image():
    for path in (_RELEASING, _WF_PATH):
        flowed = _flowed(path)
        assert "ghcr.io/pypa/gh-action-pypi-publish" in flowed, (
            f"{path.name} must name the image a consumer actually pulls")
        assert "prebuilt" in flowed, (
            f"{path.name} must say the image is prebuilt, not built here")
        assert "tag" in flowed and "digest" in flowed, (
            f"{path.name} must distinguish the mutable tag from the digest")


def _flowed(path: Path) -> str:
    """Lowercased text with wrapping and comment markers collapsed, so a
    claim split across two lines cannot evade a phrase assertion."""
    text = path.read_text(encoding="utf-8").lower()
    return re.sub(r"\s+", " ", text.replace("\n#", " "))


def test_the_documents_do_not_overclaim_what_the_digest_gate_guarantees():
    """AUDIT (v8): v7 said the gate ran "before publish becomes eligible"
    and that `publish` "never becomes eligible" against an unaudited
    manifest. Both overstate it. The check reads a MUTABLE name at one
    instant; a repoint landing afterwards is still pulled. The gate blocks
    only a mismatch observable while `inspect` runs."""
    for path in (_RELEASING, _WF_PATH):
        flowed = _flowed(path)
        for overclaim in (
            "never becomes eligible",
            "before publish becomes eligible",
            "before `publish` becomes eligible",
            "always detected",
            "detects every repoint",
        ):
            assert overclaim not in flowed, (
                f"{path.name} overclaims the gate: {overclaim!r}")
        assert "observable" in flowed, (
            f"{path.name} must scope the guarantee to what inspect can see")


def test_the_documents_record_digest_verification_without_overclaiming():
    """Detection is not prevention: the residual TOCTOU window must be
    stated, and full content addressing must NOT be claimed."""
    for path in (_RELEASING, _WF_PATH):
        flowed = _flowed(path)
        assert "toctou" in flowed or "window" in flowed, (
            f"{path.name} must disclose the inspect-to-publish window")
        # The phrase may legitimately appear negated ("NOT fully
        # content-addressed") or quoted in the prohibition against making
        # the claim. What must never appear is an AFFIRMATIVE use.
        claim = "fully content-addressed"
        for match in re.finditer(re.escape(claim), flowed):
            before = flowed[max(0, match.start() - 32):match.start()]
            assert before.rstrip().endswith("not") or before.endswith('"'), (
                f"{path.name} claims full content addressing at "
                f"...{flowed[max(0, match.start() - 60):match.end()]!r}")


def test_the_documents_record_that_direct_digest_invocation_is_not_adopted():
    lowered = _RELEASING.read_text(encoding="utf-8").lower()
    assert "unsupported" in lowered, (
        "the doc must record that direct digest invocation is unsupported")


# --- the GHCR digest gate lives in the uncredentialed inspect job -------------

_DIGEST_COMMAND = "check-runtime-image"


def test_inspect_verifies_the_publisher_image_digest():
    """The gate must run in `inspect` — the job that holds neither the signing
    seed nor the OIDC credential — so a repointed tag is caught by a runner
    that could not itself publish."""
    steps = _steps(_jobs(_wf())["inspect"])
    runs = "\n".join(s.get("run", "") for s in steps if isinstance(s, dict))
    assert _DIGEST_COMMAND in runs, (
        "inspect must resolve and verify the pinned publisher image digest")


def test_the_digest_gate_is_not_placed_in_a_credentialed_job():
    jobs = _jobs(_wf())
    for name in ("sign", "publish"):
        runs = "\n".join(s.get("run", "") for s in _steps(jobs[name])
                         if isinstance(s, dict))
        assert _DIGEST_COMMAND not in runs, (
            f"the digest gate must not run in the credentialed {name} job")


def test_inspect_holds_neither_the_signing_seed_nor_oidc():
    """Unchanged by v7: adding the digest gate must not have promoted
    inspect into a credentialed job."""
    inspect = _jobs(_wf())["inspect"]
    assert "environment" not in inspect, (
        "inspect must remain outside every protected environment")
    perms = inspect.get("permissions") or {}
    assert perms == {"contents": "read"}, (
        f"inspect must stay contents:read only, got {perms}")
    assert "id-token" not in perms
    assert "secrets." not in yaml.safe_dump(inspect), (
        "inspect must reference no secret whatsoever")


def test_publish_still_depends_on_inspect_and_keeps_the_exact_action_pin():
    jobs = _jobs(_wf())
    raw_needs = jobs["publish"].get("needs")
    needs = [raw_needs] if isinstance(raw_needs, str) else list(raw_needs or [])
    assert needs == ["inspect"], (
        "publish must remain gated behind the full inspection")
    sha, release = _PINS["pypa/gh-action-pypi-publish"]
    uses = _uses_of(jobs["publish"])
    assert any(u == f"pypa/gh-action-pypi-publish@{sha}" for u in uses), (
        "publish must keep the exact audited action commit")
    assert release == "v1.14.2"


def test_publish_gains_no_shell_and_no_custom_credential_exchange():
    """The credentialed job must stay two pinned actions and nothing else."""
    steps = _steps(_jobs(_wf())["publish"])
    assert all("run" not in s for s in steps if isinstance(s, dict)), (
        "the publish job must contain no shell step")
    assert len(steps) == 2, f"publish must remain two steps, got {len(steps)}"


def test_the_helper_pins_the_digest_the_workflow_will_enforce():
    """The enforced digest is source-controlled, not fetched from anywhere
    the attacker who repointed the tag also controls."""
    source = _HELPER.read_text(encoding="utf-8")
    # Adjacent string literals are how a 71-character digest fits the line
    # limit; join them before matching so wrapping is not load-bearing.
    joined = re.sub(r'"\s*\n\s*"', "", source)
    assert "sha256:a68d05519f6d7e47372aeaddab80b851b69afa89be179ec41775c72c" \
           "4e3ab2d5" in joined, "the audited digest must be pinned in source"
    assert "dc37677b2e1c63e2034f94d8a5b11f265b73ba33" in joined


def test_release_validation_is_executable_and_unit_tested():
    assert _HELPER.is_file()
    assert (_ROOT / "tests" / "test_release_pipeline_helper.py").is_file()
    raw = _raw()
    for command in ("check-dispatch-ref", "check-toolchain", "check-tag",
                    "check-commit", "check-tag-points-at", "sign-manifest",
                    "verify-manifest", "check-dists"):
        assert f"release_pipeline.py {command}" in raw or \
            f"release_pipeline.py \\\n" in raw and command in raw, (
            f"the workflow never invokes {command}")
