"""Platform safety: `import olympus` must succeed where confinement does not.

Two separate obligations, and conflating them is the bug this file exists to
prevent:

1. **Importing Olympus must work on Linux, Windows and macOS.** A trading
   system that cannot be imported on a developer's laptop cannot be reviewed on
   one. Nothing Unix-only may be imported at module scope.
2. **Running generated code must fail on a host that cannot confine it**, with
   a typed, explicit refusal rather than a weaker sandbox or an `ImportError`
   from three frames down.

The tests below cover both, and the first is checked *by construction* rather
than by running on Windows: a subprocess is started with `resource`, `fcntl`,
`pwd`, `grp` and `termios` made unimportable, which is the part of Windows that
matters here. That runs on every platform, on every push, and does not need a
Windows runner to be honest — though CI runs one too, because a simulation of
Windows is not Windows.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from olympus.trading.native import isolation as ISO

#: Modules that exist on Unix and not on Windows. `resource` is the one the
#: pre-merge audit found being imported unconditionally; the rest are here so
#: the next one is caught by this test rather than by a user.
UNIX_ONLY = ("resource", "fcntl", "pwd", "grp", "termios", "posix", "tty",
             "crypt", "syslog", "nis", "spwd")

#: The subset made unimportable in the simulation. `posix` is excluded because
#: CPython's own `shutil` imports it, so blocking it tests the standard library
#: rather than Olympus — and a test that fails for a reason it is not about is
#: a test nobody will keep.
SIMULATED_ABSENT = ("resource", "fcntl", "termios", "tty", "crypt", "syslog",
                    "nis", "spwd")

#: Modules that exist on Windows and not elsewhere, for symmetry.
WINDOWS_ONLY = ("msvcrt", "winreg", "winsound", "_winapi")

PACKAGE_ROOT = Path(ISO.__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# 1. nothing platform-specific at module scope
# ---------------------------------------------------------------------------

def module_scope_imports(path: Path) -> set[str]:
    """Top-level imports only. A function-local import is the fix, not the bug.

    Walks the module body rather than the whole tree, and descends into `try`,
    `if` and `with` at module scope because those still execute on import.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError, OSError):
        return set()
    found: set[str] = set()

    def visit(body):
        for node in body:
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module:
                    found.add(node.module.split(".")[0])
            elif isinstance(node, (ast.Try, ast.If, ast.With)):
                # `try: import fcntl / except ImportError:` is the accepted
                # pattern and is caught here on purpose — the test excuses it
                # explicitly rather than by not looking.
                for attribute in ("body", "orelse", "finalbody", "handlers"):
                    inner = getattr(node, attribute, None) or []
                    visit([n for n in inner if isinstance(n, ast.stmt)])
                    for handler in inner:
                        if isinstance(handler, ast.ExceptHandler):
                            visit(handler.body)
    visit(tree.body)
    return found


#: Modules that import a Unix-only name at module scope with a guard, and the
#: guard. Enumerated with a reason so a new one is a deliberate decision.
GUARDED: dict[str, str] = {
    "olympus/proclock.py": "wrapped in try/except ImportError, with fcntl=None "
                           "and a documented degraded path",
}


def test_no_module_imports_a_unix_only_module_at_module_scope():
    """The blocker, checked over the whole package rather than one file."""
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.joinpath("olympus").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(PACKAGE_ROOT))
        if relative in GUARDED:
            continue
        found = module_scope_imports(path) & set(UNIX_ONLY + WINDOWS_ONLY)
        if found:
            offenders.append(f"{relative}: {sorted(found)}")
    assert not offenders, (
        "these modules import a platform-specific module at module scope, so "
        "`import olympus` fails on the other platform:\n  "
        + "\n  ".join(offenders))


def test_the_guarded_list_is_not_stale():
    """An entry that no longer needs its exemption is a lie about the code."""
    for relative, reason in GUARDED.items():
        path = PACKAGE_ROOT / relative
        assert path.is_file(), f"{relative} is exempted and does not exist"
        found = module_scope_imports(path) & set(UNIX_ONLY + WINDOWS_ONLY)
        assert found, (
            f"{relative} no longer imports a platform-specific module at "
            f"module scope; remove it from GUARDED ({reason})")
        text = path.read_text(encoding="utf-8")
        assert "except ImportError" in text, (
            f"{relative} is exempted on the grounds that it guards the import, "
            f"and it does not")


# ---------------------------------------------------------------------------
# 2. the import actually survives without them
# ---------------------------------------------------------------------------

_BLOCKER = '''
import sys

BLOCKED = {blocked!r}


class _Absent:
    """Make the Unix-only modules unimportable, as they are on Windows."""

    def find_module(self, name, path=None):
        return self.find_spec(name, path)

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("No module named %r (simulated Windows)" % name)
        return None


for name in list(sys.modules):
    if name.split(".")[0] in BLOCKED:
        del sys.modules[name]
sys.meta_path.insert(0, _Absent())

import olympus
import olympus.trading
import olympus.trading.native
import olympus.trading.native.isolation as iso
import olympus.trading.native.promotion
import olympus.trading.native.challengers
import olympus.trading.native.evidence
import olympus.trading.native.improvement

assert iso.IsolationUnavailable is not None
print("IMPORT-OK", iso.ISOLATION_SCHEMA_VERSION)
'''


@pytest.mark.parametrize("blocked", [
    pytest.param(SIMULATED_ABSENT, id="windows-shaped"),
    pytest.param(("resource",), id="resource-only"),
])
def test_olympus_imports_with_unix_only_modules_unavailable(blocked):
    """The real check: import the package with those modules genuinely absent.

    A subprocess, because the blocker has to be installed before Olympus is
    first imported and this process imported it at collection time.
    """
    script = _BLOCKER.format(blocked=list(blocked))
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                          capture_output=True, text=True, cwd=str(PACKAGE_ROOT),
                          timeout=300)
    assert proc.returncode == 0, (
        f"importing Olympus without {list(blocked)} failed:\n"
        f"{proc.stdout}\n{proc.stderr}")
    assert "IMPORT-OK" in proc.stdout


def test_the_blocker_script_would_actually_catch_a_regression():
    """A test that cannot fail is not a test.

    Imports `resource` directly under the same blocker and asserts it raises —
    otherwise the test above would pass on a broken package because the blocker
    silently did nothing.
    """
    script = _BLOCKER.split("import olympus")[0] + textwrap.dedent("""
        try:
            import resource
        except ImportError:
            print("BLOCKER-WORKS")
        else:
            raise SystemExit("the blocker did not block")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script.format(blocked=list(SIMULATED_ABSENT))],
        capture_output=True, text=True, cwd=str(PACKAGE_ROOT), timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert "BLOCKER-WORKS" in proc.stdout


# ---------------------------------------------------------------------------
# 3. the refusal on an unsupported host
# ---------------------------------------------------------------------------

def test_an_unsupported_platform_refuses_with_a_typed_error(monkeypatch):
    """`IsolationUnavailable`, not `ImportError`, and not a weaker sandbox.

    The type is the contract: a caller's correct response is to refuse to run
    generated code, and a caller that catches `OSError` broadly would swallow
    a bare one and carry on.
    """
    monkeypatch.setattr(ISO.sys, "platform", "win32")
    assert ISO.platform_supported() is False
    with pytest.raises(ISO.IsolationUnavailable) as raised:
        ISO.require_platform()
    assert "Linux" in str(raised.value)
    assert raised.value.details["platform"] == "win32"

    from olympus.trading.errors import TradingError
    assert isinstance(raised.value, TradingError)

    support = ISO.host_support()
    assert support.supported_platform is False
    assert support.can_run_generated_code is False
    assert support.missing and "is not linux" in support.missing[0]


@pytest.mark.parametrize("platform_name", ["win32", "darwin", "cygwin",
                                           "aix", "sunos5"])
def test_every_unsupported_platform_refuses_rather_than_degrading(
        platform_name, monkeypatch):
    """No platform gets a quieter, weaker sandbox. They all get the refusal."""
    monkeypatch.setattr(ISO.sys, "platform", platform_name)
    spec = ISO.ExperimentSpec(experiment_id="cross-platform",
                              source="def run(inputs):\n    return {}\n",
                              generated=True)
    with pytest.raises(ISO.IsolationUnavailable):
        ISO.IsolatedWorker().run(spec)
    with pytest.raises(ISO.IsolationUnavailable):
        ISO.probe_confinement()

    manifest = ISO.run_isolated(spec)
    assert manifest.outcome is ISO.Verdict.REFUSED
    assert manifest.verdict is ISO.Verdict.REFUSED
    assert manifest.trustworthy is False
    assert manifest.result == {}


def test_the_isolation_report_is_answerable_on_any_platform(monkeypatch):
    """An operator on a laptop may still ask what would happen on this host."""
    monkeypatch.setattr(ISO.sys, "platform", "darwin")
    report = ISO.isolation_report()
    assert report["supported_platform"] is False
    assert report["can_run_generated_code"] is False
    assert report["confinement"] is None
    assert report["required_for_generated_code"], (
        "the report must still say what would be required, or a reader cannot "
        "tell an unsupported host from an unmeasured one")


def test_hand_written_code_is_also_refused_on_an_unsupported_platform(
        monkeypatch):
    """`trusted_reason` exempts code from the *host* check, not from the host.

    A hand-written experiment on Windows still has no cgroup, no network
    namespace and no bounded filesystem. The exemption says "this code is not
    generated", which is a statement about the code and not a licence to run it
    where nothing can be enforced. It is still refused; the reason recorded is
    the platform.
    """
    monkeypatch.setattr(ISO.sys, "platform", "win32")
    spec = ISO.ExperimentSpec(experiment_id="hand-written",
                              source="def run(inputs):\n    return {}\n",
                              generated=False)
    manifest = ISO.run_isolated(spec, trusted_reason="written by an engineer")
    assert manifest.verdict is ISO.Verdict.REFUSED


# ---------------------------------------------------------------------------
# 4. Linux-specific mechanisms are marked as such
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="cgroups are a Linux facility")
def test_the_cgroup_probe_names_what_is_missing_rather_than_averaging():
    """A host offering three of four controls is described as such."""
    from olympus.trading.native import cgroups

    support = cgroups.probe()
    assert isinstance(support.missing, tuple)
    if support.complete:
        assert support.missing == ()
        assert support.hierarchy in ("v1", "v2")
    else:
        assert support.missing, (
            "an incomplete cgroup probe must name what it is missing, or a "
            "reader cannot tell a refusal from a bug")


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="cgroups are a Linux facility")
def test_a_cgroup_scope_refuses_to_be_created_without_every_control():
    """`CgroupScope.create` does not silently drop a controller it lacks."""
    from olympus.trading.native import cgroups

    crippled = cgroups.CgroupSupport(available=True, hierarchy="v1",
                                     root="/sys/fs/cgroup", process_limit=True,
                                     memory_quota=False, cpu_quota=True,
                                     freeze=True)
    assert crippled.complete is False
    scope = cgroups.CgroupScope("olympus-test-refusal", support=crippled)
    with pytest.raises(cgroups.IsolationUnavailable) as raised:
        scope.create(max_processes=4, memory_bytes=1 << 20, cpu_quota_us=50_000)
    assert "memory_quota" in raised.value.details["missing"]


@pytest.mark.skipif(sys.platform.startswith("linux"),
                    reason="this asserts the behaviour off Linux")
def test_off_linux_the_module_still_imports_and_reports():   # pragma: no cover
    """Runs on the Windows and macOS CI legs. Nothing here touches a cgroup."""
    assert ISO.platform_supported() is False
    assert ISO.host_support().can_run_generated_code is False
    report = ISO.isolation_report()
    assert report["can_run_generated_code"] is False
    with pytest.raises(ISO.IsolationUnavailable):
        ISO.require_platform()
