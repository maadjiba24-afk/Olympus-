"""Four complete-coverage Windows CI partitions; not a product execution API.

Each runner collects the ordinary suite, retains that full ordered universe, and
executes one deterministic whole-file partition in the same pytest process.
The required aggregate joins all four results. No result means no success.
Imports are stdlib-only; pytest is imported only by the explicit ``run`` command.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

SHARDS = 4
CASE_TIMEOUT = 300
MAX_ITEMS = 65536
MAX_BYTES = 128 * 1024 * 1024
SCHEMA = "olympus.windows-full-shard.v1"


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


# Phase reports run before monkeypatch fixtures restore JSONEncoder methods.
# Keep both bound methods on this private encoder; encode calls iterencode
# dynamically. Product fault injection remains active on the public class.
_REPORT_ENCODER = json.JSONEncoder(ensure_ascii=True, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
_REPORT_ENCODER.iterencode = _REPORT_ENCODER.iterencode
_REPORT_ENCODE = _REPORT_ENCODER.encode


def encoded(value):
    return _REPORT_ENCODE(value).encode("utf-8")


def integer(value, low, high, reason):
    require(type(value) is int and low <= value <= high, reason)
    return value


def ordinary_bytes(path, maximum=MAX_BYTES):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "evidence/source is not an ordinary file")
    require(0 <= info.st_size <= maximum, "file exceeds evidence bound")
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    require(len(data) == info.st_size and len(data) <= maximum, "file read changed or exceeds bound")
    return data


def write_new(path, value):
    data = encoded(value) + b"\n"
    require(len(data) <= MAX_BYTES, "receipt exceeds bound")
    with path.open("xb") as handle:
        handle.write(data)


def nodes_checked(nodes):
    require(type(nodes) is list and 0 < len(nodes) <= MAX_ITEMS, "empty or oversized collection")
    for node in nodes:
        require(type(node) is str and 0 < len(node) <= 4096 and "\x00" not in node,
                "invalid collected node")
        name = node.split("::", 1)[0]
        require(name.endswith(".py") and not name.startswith(("/", "\\"))
                and "\\" not in name and ":" not in name
                and all(part not in ("", ".", "..") for part in name.split("/")),
                "collected path is not repository-relative")
    require(len(nodes) == len(set(nodes)), "duplicate collected node")
    require(len({pytest_names(n) for n in nodes}) == len(nodes), "ambiguous JUnit node names")
    return nodes


def assignments(nodes):
    """Stable largest-file-first balancing by actual collected item count.

    Counts are a scheduling heuristic, never a license to omit slow cases.
    Preserve the original order inside each shard and never split a test file.
    """
    nodes_checked(nodes)
    groups = Counter(node.split("::", 1)[0] for node in nodes)
    require(len(groups) >= SHARDS, "fewer files than Windows shards")
    loads = [0] * SHARDS
    owner = {}
    for name in sorted(groups, key=lambda key: (-groups[key], key)):
        shard = min(range(SHARDS), key=lambda key: (loads[key], key))
        owner[name] = shard
        loads[shard] += groups[name]
    return [[node for node in nodes if owner[node.split("::", 1)[0]] == shard]
            for shard in range(SHARDS)]


def pytest_names(node):
    path, bracket, parameters = node.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + parameters
    def escaped(value):
        return "".join(char if (ord(char) in (9, 10, 13)
                       or 0x20 <= ord(char) <= 0x7e or 0x80 <= ord(char) <= 0xd7ff
                       or 0xe000 <= ord(char) <= 0xfffd or 0x10000 <= ord(char) <= 0x10ffff)
                       else "#x" + format(ord(char), "02X" if ord(char) <= 255 else "04X")
                       for char in value)
    return ".".join(names[:-1]), escaped(names[-1])


def phase_outcomes(selected, phases):
    """Require the actual sequence: setup/call/teardown, or failed/skipped setup/teardown."""
    require(type(phases) is list and len(phases) <= 3 * len(selected), "phase count")
    require(all(type(row) is dict for row in phases), "phase object schema")
    cursor, xml_cases, classifications = 0, [], []
    for node in selected:
        own = []
        while cursor < len(phases) and phases[cursor].get("nodeid") == node:
            own.append(phases[cursor])
            cursor += 1
        require(len(own) in (2, 3) and own[0].get("when") == "setup"
                and own[-1].get("when") == "teardown"
                and (len(own) != 3 or own[1].get("when") == "call"
                     and own[0].get("outcome") == "passed")
                and (len(own) != 2 or own[0].get("outcome") in ("failed", "skipped")),
                "missing, duplicated or reordered test phase")
        markers = []
        xml_cases.append((node, markers))
        call_failed = len(own) == 3 and own[1]["outcome"] == "failed"
        for row in own:
            require(row.get("kind") == "phase" and row.get("outcome") in ("passed", "failed", "skipped")
                    and row.get("xfail") in ("none", "reported", "strict_xpass"), "phase schema")
            integer(row.get("duration_ns"), 0, 10**15, "phase duration")
            require(row.get("reason_sha256") is None or type(row["reason_sha256"]) is str
                    and re.fullmatch(r"[0-9a-f]{64}", row["reason_sha256"]), "phase reason digest")
            outcome, when, xfail = row["outcome"], row["when"], row["xfail"]
            if outcome == "failed":
                if when == "teardown" and call_failed:
                    markers = []
                    xml_cases.append((node, markers))
                markers.append(("error" if when != "call" else
                                "skipped" if xfail == "reported" else "failure", None))
            elif outcome == "skipped":
                markers.append(("skipped", "pytest.xfail" if xfail == "reported" else "pytest.skip"))
            if outcome != "passed" or xfail != "none":
                classifications.append({key: row[key] for key in
                                        ("nodeid", "when", "outcome", "xfail", "reason_sha256")})
    require(cursor == len(phases), "unknown or out-of-order phase")
    return xml_cases, classifications


def verify_junit(raw, selected, phases, collect_skips):
    require(type(raw) is bytes and len(raw) <= MAX_BYTES
            and b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(), "JUnit bounds/DTD")
    root = ET.fromstring(raw)
    require(root.tag == "testsuites" and len(root) == 1 and root[0].tag == "testsuite", "JUnit suite shape")
    cases = [child for child in root[0] if child.tag == "testcase"]
    require(all(child.tag in ("testcase", "properties") for child in root[0])
            and sum(child.tag == "properties" for child in root[0]) <= 1, "unexpected JUnit child")
    expected, classifications = phase_outcomes(selected, phases)
    expected = [(row["nodeid"], [("skipped", None)]) for row in collect_skips] + expected
    require(len(cases) == len(expected), "JUnit case count differs from phases")
    counts = Counter()
    for case, (node, markers) in zip(cases, expected):
        require((case.get("classname", ""), case.get("name")) == pytest_names(node), "JUnit ordered node mismatch")
        actual = [(child.tag, child.get("type")) for child in case
                  if child.tag in ("failure", "error", "skipped")]
        require(actual == markers, "JUnit per-node outcome mismatch")
        counts.update(tag for tag, _ in markers)
    for name, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
        require(root[0].get(name) == str(counts[tag]), "JUnit summary differs from outcomes")
    # Match the retained pytest JUnit implementation: a passing call increments
    # its pass counter even if teardown skips/fails. A teardown error without a
    # failed call reuses its XML testcase and subtracts that double count.
    passed_calls = sum(row["when"] == "call" and row["outcome"] == "passed" for row in phases)
    failed_calls = {row["nodeid"] for row in phases if row["when"] == "call" and row["outcome"] == "failed"}
    reused_teardown_errors = sum(row["when"] == "teardown" and row["outcome"] == "failed"
                                 and row["nodeid"] not in failed_calls for row in phases)
    expected_total = passed_calls + sum(counts.values()) - reused_teardown_errors
    require(root[0].get("tests") == str(expected_total), "JUnit test total differs")
    return classifications


def read_stream(raw):
    require(type(raw) is bytes and len(raw) <= MAX_BYTES and raw.endswith(b"\n"), "partial or oversized stream")
    rows = [json.loads(line) for line in raw.splitlines()]
    require(rows and all(type(row) is dict for row in rows), "event schema")
    require(rows[-1].get("kind") == "session_end", "incomplete event stream")
    require(sum(row.get("kind") == "configuration" for row in rows) == 1
            and sum(row.get("kind") == "collection" for row in rows) == 1
            and sum(row.get("kind") == "session_end" for row in rows) == 1, "duplicate/missing session boundary")
    require(all(row.get("kind") in ("configuration", "collection", "collect_skip", "phase", "warning", "session_end")
                for row in rows), "unknown event")
    configuration = next(row for row in rows if row["kind"] == "configuration")
    configuration_index = rows.index(configuration)
    require(all(row["kind"] == "warning" for row in rows[:configuration_index]),
            "collection/runtime preceded configuration")
    require(configuration.get("standard_selection") is True
            and type(configuration.get("timeout")) is int and configuration["timeout"] == CASE_TIMEOUT,
            "suite selection or timeout changed")
    collection = next(row for row in rows if row["kind"] == "collection")
    universe = nodes_checked(collection.get("universe"))
    require(configuration_index < rows.index(collection), "collection preceded configuration")
    require(all(row["kind"] != "collect_skip" for row in rows[rows.index(collection) + 1:]),
            "collection skip occurred after collection ended")
    selected = collection.get("selected")
    shard = integer(collection.get("shard"), 0, SHARDS - 1, "shard identity")
    require(selected == assignments(universe)[shard], "assignment differs from whole-file partition")
    require(rows.index(collection) < min([i for i, row in enumerate(rows) if row["kind"] == "phase"] or [len(rows)]),
            "runtime preceded collection")
    phases = [row for row in rows if row["kind"] == "phase"]
    collect_skips = [row for row in rows if row["kind"] == "collect_skip"]
    require(len(collect_skips) <= MAX_ITEMS and len({row.get("nodeid") for row in collect_skips}) == len(collect_skips),
            "duplicate collection skips")
    require(all(type(row.get("nodeid")) is str and row["nodeid"]
                and type(row.get("reason_sha256")) is str
                and re.fullmatch(r"[0-9a-f]{64}", row["reason_sha256"]) for row in collect_skips), "collection skip schema")
    end = rows[-1]
    integer(end.get("exitstatus"), 0, 5, "pytest exit status")
    require(type(end.get("phases")) is int and end["phases"] == len(phases), "phase footer count")
    phase_outcomes(selected, phases)
    warnings = [row for row in rows if row["kind"] == "warning"]
    require(type(end.get("warnings")) is int and end["warnings"] == len(warnings), "warning footer count")
    require(all(type(row.get("nodeid")) is str and row.get("when") in ("config", "collect", "runtest")
                and type(row.get("category")) is str and row["category"]
                and type(row.get("message_sha256")) is str
                and re.fullmatch(r"[0-9a-f]{64}", row["message_sha256"]) for row in warnings), "warning schema")
    return collection, phases, collect_skips, warnings, end


def verify_bundle(receipt, stream, xml, *, expected_sha, expected_tree, expected_run, expected_attempt):
    require(type(receipt) is dict and receipt.get("schema") == SCHEMA and receipt.get("status") == "completed",
            "missing/incomplete shard result")
    shard = integer(receipt.get("shard"), 0, SHARDS - 1, "shard identity")
    require(type(receipt.get("pytest_exit_code")) is int and receipt["pytest_exit_code"] == 0, "failed shard")
    require(receipt.get("run_id") == expected_run and receipt.get("attempt") == expected_attempt,
            "different workflow run or attempt")
    binding = receipt.get("source_before")
    require(type(binding) is dict and binding == receipt.get("source_after")
            and binding.get("head") == expected_sha and binding.get("tree") == expected_tree,
            "checkout/source changed or differs from exact head/tree")
    files = binding.get("files")
    require(type(files) is dict and 0 < len(files) <= 10000
            and all(type(name) is str and type(row) is dict and row.get("mode") in ("100644", "100755")
                    and type(row.get("blob_oid")) is str and re.fullmatch(r"[0-9a-f]{40}", row["blob_oid"])
                    and type(row.get("sha256")) is str and re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                    for name, row in files.items()), "source file binding schema")
    runtime = receipt.get("runtime")
    require(type(runtime) is dict and runtime.get("platform") == "win32"
            and runtime.get("implementation") == "cpython"
            and type(runtime.get("python")) is list and len(runtime["python"]) == 3
            and all(type(value) is int and value >= 0 for value in runtime["python"])
            and runtime["python"][:2] == [3, 12]
            and type(runtime.get("packages")) is list and runtime["packages"], "runtime identity schema")
    packages = runtime["packages"]
    require(packages == metadata_packages(packages)
            and {"pytest", "pytest-timeout"}.issubset({row["name"] for row in packages}), "dependency identity schema")
    modules = receipt.get("modules")
    required = {"olympus": "olympus/__init__.py", "olympus.vault": "olympus/vault.py",
                "olympus.prefs": "olympus/prefs.py", "olympus.browser": "olympus/browser.py"}
    require(type(modules) is dict and set(modules) == set(required), "module identity schema")
    for name, relative in required.items():
        require(relative in files and modules[name] == {"relative_path": relative, "sha256": files[relative]["sha256"]},
                "module differs from checkout source binding")
    require(receipt.get("stream_sha256") == digest(stream) and receipt.get("junit_sha256") == digest(xml), "artifact digest mismatch")
    collection, phases, collect_skips, warnings, end = read_stream(stream)
    require(collection["shard"] == shard and end["exitstatus"] == 0, "shard/footer mismatch")
    require(not any(row["outcome"] == "failed" for row in phases), "failed phase in successful shard")
    classifications = verify_junit(xml, collection["selected"], phases, collect_skips)
    return {"shard": shard, "universe": collection["universe"], "selected": collection["selected"],
            "collection_skips": collect_skips, "classifications": classifications, "warnings": warnings,
            "source": binding, "runtime": receipt.get("runtime"), "modules": receipt.get("modules")}


def aggregate_bundles(bundles, *, matrix_result, expected_sha, expected_tree, expected_run, expected_attempt):
    require(matrix_result == "success", "missing, failed or cancelled Windows matrix")
    require(type(bundles) is list and len(bundles) == SHARDS, "missing or extra shard artifact")
    checked = [verify_bundle(*bundle, expected_sha=expected_sha, expected_tree=expected_tree,
                             expected_run=expected_run, expected_attempt=expected_attempt) for bundle in bundles]
    require(sorted(row["shard"] for row in checked) == list(range(SHARDS)), "duplicate or missing shard identity")
    first = checked[0]
    require(all(row["universe"] == first["universe"] and row["source"] == first["source"]
                and row["runtime"] == first["runtime"] and row["modules"] == first["modules"]
                and row["collection_skips"] == first["collection_skips"] for row in checked),
            "shards disagree on ordered universe/source/runtime/modules/collection skips")
    executed = [node for row in checked for node in row["selected"]]
    require(Counter(executed) == Counter(first["universe"]) and len(executed) == len(set(executed)),
            "Windows partitions are not a disjoint exhaustive union")
    return {"schema": "olympus.windows-full-union.v1", "status": "completed", "shards": SHARDS,
            "head": expected_sha, "tree": expected_tree, "run_id": expected_run, "attempt": expected_attempt,
            "universe_sha256": digest(encoded(first["universe"])), "collected": len(executed),
            "whole_file_assignment": True, "disjoint_exhaustive_union": True,
            "collection_skips": first["collection_skips"],
            "phase_exceptions": [item for row in sorted(checked, key=lambda value: value["shard"])
                                 for item in row["classifications"]],
            "warnings": [item for row in checked for item in row["warnings"]],
            "skip_warning_xfail_review_required": True, "milestone_acceptance": False}


def source_binding(root):
    """Fixed read-only Git metadata commands; never dispatch candidate test code."""
    def git(kind):
        commands = {"head": ("rev-parse", "HEAD"), "tree": ("rev-parse", "HEAD^{tree}"),
                    "files": ("ls-tree", "-rz", "HEAD"),
                    "clean": ("diff", "--no-ext-diff", "--no-textconv", "--exit-code", "--quiet", "HEAD", "--")}
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root),
                                 *commands[kind]], capture_output=True,
                                timeout=30, check=False)
        require(result.returncode == 0 and not result.stderr and len(result.stdout) <= 4 * 1024 * 1024,
                "read-only checkout metadata unavailable")
        return result.stdout
    head, tree = git("head").decode("ascii").strip(), git("tree").decode("ascii").strip()
    require(re.fullmatch(r"[0-9a-f]{40}", head) and re.fullmatch(r"[0-9a-f]{40}", tree), "Git object identity")
    git("clean")
    files = {}
    for record in git("files").split(b"\x00"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        name = raw_name.decode("utf-8")
        require(mode in ("100644", "100755") and kind == "blob" and name not in files
                and not name.startswith("/") and "\\" not in name and ":" not in name
                and all(part not in ("", ".", "..") for part in name.split("/")), "tracked source shape")
        path = root / name
        for parent in path.parents:
            if parent == root:
                break
            st = parent.lstat()
            require(stat.S_ISDIR(st.st_mode) and not getattr(st, "st_file_attributes", 0) & 0x400,
                    "source parent is not ordinary")
        raw = ordinary_bytes(path)
        require(re.fullmatch(r"[0-9a-f]{40}", oid), "Git blob identity")
        # Git's clean result authenticates its declared checkout conversion;
        # raw bytes are retained separately (Windows CRLF need not equal Linux).
        files[name] = {"mode": mode, "blob_oid": oid, "sha256": digest(raw)}
    require(0 < len(files) <= 10000, "tracked source count")
    git("clean")
    require(git("head").decode("ascii").strip() == head and git("tree").decode("ascii").strip() == tree,
            "checkout identity changed during source observation")
    return {"head": head, "tree": tree, "files": files}


def canonical_source(binding):
    return {"head": binding["head"], "tree": binding["tree"],
            "files": {name: {"mode": row["mode"], "blob_oid": row["blob_oid"]}
                      for name, row in binding["files"].items()}}


def metadata_packages(records):
    """Canonical metadata locations; duplicate discovery is not a new install."""
    require(type(records) is list and 0 < len(records) <= 4096, "metadata observation bound")
    origins, versions = {}, {}
    for record in records:
        require(type(record) is dict and set(record) == {"name", "version", "scope", "path"}, "metadata record schema")
        name, version, scope, path = (record[key] for key in ("name", "version", "scope", "path"))
        require(type(name) is str and 0 < len(name) <= 256
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name), "metadata project name")
        name = re.sub(r"[-_.]+", "-", name).lower()
        require(type(version) is str and 0 < len(version) <= 256
                and all(32 < ord(char) < 127 for char in version), "metadata version")
        require(scope in ("checkout", "runtime", "base-runtime") and type(scope) is str
                and type(path) is str and 0 < len(path) <= 2048
                and "\\" not in path and ":" not in path
                and all(part not in ("", ".", "..") for part in path.split("/"))
                and path.endswith((".dist-info", ".egg-info"))
                and all(ord(char) >= 32 and ord(char) != 127 for char in path), "metadata origin")
        row = dict(name=name, version=version, scope=scope, path=path)
        origin = (scope, path)
        require(origin not in origins or origins[origin] == row, "metadata origin conflict")
        require(name not in versions or versions[name] == version, "metadata version conflict")
        origins[origin], versions[name] = row, version
    return sorted(origins.values(), key=lambda row: tuple(row[key] for key in ("name", "version", "scope", "path")))


def metadata_search_roots(entries):
    require(type(entries) is list and len(entries) <= 128, "metadata search-root bound")
    roots = {}
    for entry in entries:
        require(type(entry) is str and len(entry) <= 4096, "metadata search-root schema")
        path = Path(entry or os.curdir).resolve()
        roots.setdefault(os.path.normcase(str(path)), path)
    return [roots[key] for key in sorted(roots)]


def metadata_origin(path, root):
    # CPython PathDistribution retains its actual metadata location in _path.
    # Unsupported resolver/zip metadata refuses; never invent an ambient origin.
    require(isinstance(path, Path), "unsupported metadata location")
    path = path.resolve(strict=True)
    for scope, base in (("checkout", root), ("runtime", Path(sys.prefix)),
                        ("base-runtime", Path(sys.base_prefix))):
        try:
            relative = path.relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
        return scope, relative
    raise ValueError("metadata outside checkout/runtime roots")


def runtime_binding(root):
    records = []
    # Do not mutate sys.path. Repeated checkout entries (including conftest's
    # insertion) discover once; distinct egg-info/dist-info locations remain.
    for search_root in metadata_search_roots(list(sys.path)):
        for dist in importlib.metadata.distributions(path=[str(search_root)]):
            require(len(records) < 4096, "metadata observation bound")
            scope, relative = metadata_origin(getattr(dist, "_path", None), root)
            records.append(dict(name=dist.metadata["Name"], version=dist.version, scope=scope, path=relative))
    return {"python": list(sys.version_info[:3]), "implementation": sys.implementation.name,
            "platform": sys.platform, "packages": metadata_packages(records)}


def module_bindings(root, source):
    result = {}
    for name, relative in (("olympus", "olympus/__init__.py"), ("olympus.vault", "olympus/vault.py"),
                           ("olympus.prefs", "olympus/prefs.py"), ("olympus.browser", "olympus/browser.py")):
        module = sys.modules.get(name)
        path = getattr(module, "__file__", None)
        require(type(path) is str and Path(path).resolve() == (root / relative).resolve(), "candidate module binding")
        require(digest(ordinary_bytes(Path(path))) == source["files"][relative]["sha256"], "loaded module source hash")
        result[name] = {"relative_path": relative, "sha256": source["files"][relative]["sha256"]}
    return result


def make_plugin(pytest, root, shard, append):
    class Reporter:
        selected = None
        collection_error = None
        phases = 0
        warnings = 0
        partitioning = False
        finished = False
        exitstatus = None

        @pytest.hookimpl(trylast=True)
        def pytest_configure(self, config):
            opt = config.option
            require(Path(config.rootpath).resolve() == root and len(config.args) == 1
                    and Path(config.args[0]).resolve() == root, "ordinary full-suite root changed")
            maximum = getattr(opt, "maxfail", None)
            require(maximum is None or type(maximum) is int and maximum == 0, "early test stop configured")
            require(not any(getattr(opt, key, None) for key in
                            ("keyword", "markexpr", "deselect", "ignore", "ignore_glob", "collectonly",
                             "continue_on_collection_errors", "numprocesses", "lf", "ff", "stepwise")), "suite filtering/reordering configured")
            # pytest-timeout resolves CLI/environment/INI in its configure hook.
            # Inspect that initialized effective contract, not its string INI.
            # This does not rewrite existing per-test markers (including 120s).
            timeout = getattr(config, "_env_timeout", None)
            require(type(timeout) in (int, float) and timeout == CASE_TIMEOUT
                    and getattr(config, "_env_timeout_func_only", None) is False
                    and config.pluginmanager.hasplugin("timeout"), "300-second test bound unavailable")
            append({"kind": "configuration", "standard_selection": True, "timeout": CASE_TIMEOUT})

        def pytest_deselected(self, items):
            require(self.partitioning, "unexpected ordinary-suite deselection")

        def pytest_collectreport(self, report):
            require(report.outcome in ("passed", "skipped"), "collection failure")
            if report.outcome == "skipped":
                append({"kind": "collect_skip", "nodeid": report.nodeid,
                        "reason_sha256": digest(str(report.longrepr).encode("utf-8", "backslashreplace"))})

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_collection_finish(self, session):
            try:
                yield
                universe = [item.nodeid for item in session.items]
                self.selected = assignments(universe)[shard]
                wanted = set(self.selected)
                append({"kind": "collection", "universe": universe, "selected": self.selected, "shard": shard})
                removed = [item for item in session.items if item.nodeid not in wanted]
                session.items[:] = [item for item in session.items if item.nodeid in wanted]
                self.partitioning = True
                try:
                    session.config.hook.pytest_deselected(items=removed)
                finally:
                    self.partitioning = False
            except BaseException as error:
                if self.collection_error is None:
                    self.collection_error = error
                raise


        @pytest.hookimpl(tryfirst=True)
        def pytest_runtestloop(self, session):
            require(self.selected is not None and [item.nodeid for item in session.items] == self.selected,
                    "partition changed before execution")

        def pytest_runtest_logreport(self, report):
            require(self.selected is not None and report.nodeid in self.selected
                    and report.when in ("setup", "call", "teardown")
                    and report.outcome in ("passed", "failed", "skipped"), "test report outside partition")
            require(type(report.duration) in (int, float) and math.isfinite(report.duration)
                    and report.duration >= 0, "invalid test duration")
            xfail, reason = "none", None
            if hasattr(report, "wasxfail"):
                xfail, reason = "reported", str(report.wasxfail)
            elif report.outcome == "failed" and type(report.longrepr) is str and report.longrepr.startswith("[XPASS(strict)]"):
                xfail, reason = "strict_xpass", report.longrepr
            elif report.outcome == "skipped":
                reason = str(report.longrepr)
            append({"kind": "phase", "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
                    "xfail": xfail, "reason_sha256": None if reason is None else digest(reason.encode("utf-8", "backslashreplace")),
                    "duration_ns": int(report.duration * 10**9)})
            self.phases += 1

        def pytest_warning_recorded(self, warning_message, when, nodeid, location):
            require(self.warnings < MAX_ITEMS, "warning evidence bound")
            append({"kind": "warning", "nodeid": nodeid, "when": when,
                    "category": warning_message.category.__module__ + "." + warning_message.category.__qualname__,
                    "message_sha256": digest(str(warning_message.message).encode("utf-8", "backslashreplace"))})
            self.warnings += 1

        def pytest_sessionfinish(self, session, exitstatus):
            require(not self.finished, "duplicate pytest session finish")
            self.finished = True
            self.exitstatus = int(exitstatus)
            # pytest's sessionfinish/unconfigure wrappers may still deliver
            # warnings. The caller publishes the terminal row only after main
            # has actually returned, never from this earlier hook.
    return Reporter()


def run(shard, output, expected_sha, run_id, attempt):
    integer(shard, 0, SHARDS - 1, "shard identity")
    require(os.name == "nt" and sys.version_info[:2] == (3, 12), "Windows Python 3.12 required")
    require(os.environ.get("GITHUB_ACTIONS") == "true", "explicit hosted CI execution required")
    require(not os.environ.get("PYTEST_ADDOPTS") and not os.environ.get("PYTEST_PLUGINS"), "ambient pytest selection/plugins")
    root = Path(__file__).resolve().parents[1]
    require(Path.cwd().resolve() == root and root not in output.resolve().parents, "owned evidence must be outside checkout")
    output.mkdir(exist_ok=False)
    before, runtime = source_binding(root), runtime_binding(root)
    require(before["head"] == expected_sha, "checkout is not expected workflow SHA")
    write_new(output / "started.json", {"schema": SCHEMA, "shard": shard, "head": expected_sha,
                                        "run_id": run_id, "attempt": attempt, "status": "started"})
    import pytest
    total = 0
    with (output / "events.jsonl").open("xb") as stream:
        def append(row):
            nonlocal total
            raw = encoded(row) + b"\n"
            total += len(raw)
            require(total <= MAX_BYTES, "event evidence bound")
            stream.write(raw)
            stream.flush()
        plugin = make_plugin(pytest, root, shard, append)
        code = int(pytest.main(["-q", "--junitxml=" + str(output / "junit.xml")], plugins=[plugin]))
        require(plugin.finished is True and plugin.exitstatus == code, "pytest return lacks matching session finish")
        append({"kind": "session_end", "exitstatus": code, "phases": plugin.phases, "warnings": plugin.warnings})
    after = source_binding(root)
    require(before == after and runtime == runtime_binding(root), "source/runtime changed during shard")
    # A hook refusal has no collection row. Keep its actual primary; do
    # not replace it with a secondary completed-stream boundary error.
    if plugin.collection_error is not None:
        raise plugin.collection_error
    modules = module_bindings(root, before)
    stream_raw, xml_raw = ordinary_bytes(output / "events.jsonl"), ordinary_bytes(output / "junit.xml")
    collection, phases, skips, warnings, end = read_stream(stream_raw)
    require(end["exitstatus"] == code and collection["shard"] == shard, "pytest return/footer mismatch")
    classifications = verify_junit(xml_raw, collection["selected"], phases, skips)
    receipt = {"schema": SCHEMA, "status": "completed", "shard": shard, "pytest_exit_code": code,
               "run_id": run_id, "attempt": attempt, "source_before": before, "source_after": after,
               "runtime": runtime, "modules": modules, "stream_sha256": digest(stream_raw), "junit_sha256": digest(xml_raw),
               "phase_exceptions": classifications, "warnings": len(warnings), "milestone_acceptance": False}
    write_new(output / "result.json", receipt)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "aggregate"):
        command = commands.add_parser(name)
        command.add_argument("--expected-sha", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--attempt", required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "run":
            command.add_argument("--shard", type=int, required=True)
        else:
            command.add_argument("--inputs", type=Path, required=True)
            command.add_argument("--matrix-result", required=True)
    args = parser.parse_args(argv)
    require(re.fullmatch(r"[0-9a-f]{40}", args.expected_sha)
            and re.fullmatch(r"[1-9][0-9]{0,19}", args.run_id)
            and re.fullmatch(r"[1-9][0-9]{0,8}", args.attempt), "workflow provenance arguments")
    if args.command == "run":
        return run(args.shard, args.output, args.expected_sha, args.run_id, args.attempt)
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(exist_ok=False)
    write_new(args.output / "started.json", {"schema": "olympus.windows-full-union.v1", "status": "started",
                                             "head": args.expected_sha, "run_id": args.run_id, "attempt": args.attempt})
    expected = source_binding(root)
    require(expected["head"] == args.expected_sha, "aggregate checkout differs from workflow SHA")
    require(args.inputs.is_dir(), "missing downloaded artifacts")
    directories = sorted(args.inputs.iterdir())
    require(len(directories) == SHARDS, "shard artifact directory count")
    for path in directories:
        metadata = path.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and not getattr(metadata, "st_file_attributes", 0) & 0x400,
                "shard artifact directory is not ordinary")
    bundles = [(json.loads(ordinary_bytes(path / "result.json")), ordinary_bytes(path / "events.jsonl"),
                ordinary_bytes(path / "junit.xml")) for path in directories]
    result = aggregate_bundles(bundles, matrix_result=args.matrix_result, expected_sha=args.expected_sha,
                               expected_tree=expected["tree"], expected_run=args.run_id, expected_attempt=args.attempt)
    require(all(canonical_source(bundle[0]["source_before"]) == canonical_source(expected) for bundle in bundles),
            "aggregate canonical source inventory differs")
    result["input_artifacts"] = [{"directory": path.name,
                                  "files": {name: digest(ordinary_bytes(path / name))
                                            for name in ("result.json", "events.jsonl", "junit.xml")}}
                                 for path in directories]
    write_new(args.output / "result.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
