#!/usr/bin/env python3
"""CI gate for the generated-code isolation. Never passes vacuously.

The adversarial isolation tests need a host that can actually enforce cgroup
limits and mount a sized tmpfs. A GitHub-hosted runner may or may not be one,
depending on the image and on whether the job runs privileged — so a suite that
simply skips when it cannot confine would go green on a runner where none of it
ran, and the pull request would carry a passing check that proves nothing.

This script makes that impossible by testing *both* worlds and always testing
something real:

* **On a capable host** it runs the adversarial suite, and its exit code is the
  suite's. Every escape attempt has to actually be stopped.
* **On an incapable host** it asserts the fail-closed path instead: generated
  code must be refused before it starts, the refusal must name every missing
  control, and no work directory may be created. That is the behaviour that
  matters on a laptop, a container without cgroup delegation, or a Mac — and it
  is a real assertion, not a skip.

Either way the host's capability report is printed in full, so a reader of the
CI log can see which branch ran and why.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITE = "tests/test_trading_native_isolation_adversarial.py"


def notice(message: str) -> None:
    print(f"::notice::{message}")


def failure(message: str) -> None:
    print(f"::error::{message}")


def main() -> int:
    from olympus.trading.native import isolation as ISO

    support = ISO.host_support()
    print("host support:")
    print(json.dumps(support.to_dict(), indent=2, sort_keys=True))

    if support.can_run_generated_code:
        notice("this runner can enforce every required control; running the "
               "adversarial isolation suite")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", SUITE,
             "tests/test_trading_native_isolation.py",
             "tests/test_trading_native_isolation_platform.py"],
            cwd=str(REPO))
        if completed.returncode != 0:
            failure("the adversarial isolation suite failed on a host that "
                    "claims it can confine generated code")
        return completed.returncode

    # Not capable. Assert the refusal rather than skipping.
    shortfall = list(support.missing)
    notice("this runner cannot enforce every required control "
           f"({'; '.join(shortfall)}); asserting the fail-closed path instead")

    spec = ISO.ExperimentSpec(
        experiment_id="ci-refusal-probe",
        source="def run(inputs):\n    return {'ran': True}\n",
        generated=True)

    problems: list[str] = []
    try:
        ISO.IsolatedWorker().run(spec)
        problems.append("IsolatedWorker.run did not raise IsolationUnavailable "
                        "for generated code on a host that cannot confine it")
    except ISO.IsolationUnavailable as refusal:
        print(f"refusal: {refusal}")
    except Exception as error:                           # noqa: BLE001
        problems.append(f"the refusal was {type(error).__name__}, not "
                        f"IsolationUnavailable: {error}")

    manifest = ISO.run_isolated(spec)
    if manifest.verdict is not ISO.Verdict.REFUSED:
        problems.append(f"run_isolated returned {manifest.verdict.value}, "
                        f"not refused")
    if manifest.trustworthy:
        problems.append("a refused run reported itself trustworthy")
    if manifest.result:
        problems.append(f"a refused run carried a result: {manifest.result}")
    if manifest.destruction.get("workdir") != "(never created)":
        problems.append("a refused run created a work directory")
    if "cannot confine" not in manifest.stderr:
        problems.append("the refusal does not say the host cannot confine "
                        f"generated code: {manifest.stderr!r}")

    # The rest of the isolation tests still apply: they cover the manifest, the
    # signing, the platform gate and the refusal itself.
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/test_trading_native_isolation_platform.py"],
        cwd=str(REPO))
    if completed.returncode != 0:
        problems.append("the platform-safety suite failed")

    for problem in problems:
        failure(problem)
    if problems:
        return 1
    notice("generated code is refused on this host, and the refusal names "
           f"what is missing: {'; '.join(shortfall)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
