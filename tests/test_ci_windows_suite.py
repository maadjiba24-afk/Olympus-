"""Offline controls for exhaustive Windows CI admission, not hosted-run evidence."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from scripts import ci_windows_suite as suite


SHA, TREE = "1" * 40, "2" * 40
CONTEXT = dict(expected_sha=SHA, expected_tree=TREE, expected_run="123", expected_attempt="1")
NODES = [f"tests/test_{letter}.py::test_{number}" for letter in "abcd" for number in range(2)]


def _phase(node, when, outcome="passed", xfail="none"):
    return dict(kind="phase", nodeid=node, when=when, outcome=outcome, xfail=xfail,
                reason_sha256=None if outcome == "passed" and xfail == "none" else "5" * 64,
                duration_ns=1000)


def _xml(selected, *, markers=None, collect_skips=()):
    root = ET.Element("testsuites")
    tests = ET.SubElement(root, "testsuite", tests=str(len(selected) + len(collect_skips)),
                          errors="0", failures="0", skipped="0")
    for node in [*collect_skips, *selected]:
        cls, name = suite.pytest_names(node)
        case = ET.SubElement(tests, "testcase", classname=cls, name=name)
        if node in collect_skips:
            ET.SubElement(case, "skipped")
        for tag, attributes in (markers or {}).get(node, []):
            ET.SubElement(case, tag, **attributes)
    for key, tag in (("errors", "error"), ("failures", "failure"), ("skipped", "skipped")):
        tests.set(key, str(len(list(tests.iter(tag)))))
    return ET.tostring(root)


def _bundle(shard):
    selected = NODES[2 * shard:2 * shard + 2]
    rows = [dict(kind="configuration", standard_selection=True, timeout=300),
            dict(kind="collection", universe=list(NODES), selected=selected, shard=shard)]
    rows += [_phase(node, when) for node in selected for when in ("setup", "call", "teardown")]
    rows.append(dict(kind="session_end", exitstatus=0, phases=6, warnings=0))
    files = {relative: dict(mode="100644", blob_oid="4" * 40, sha256="3" * 64) for relative in
             ("olympus/__init__.py", "olympus/vault.py", "olympus/prefs.py", "olympus/browser.py")}
    source = dict(head=SHA, tree=TREE, files=files)
    modules = {name: dict(relative_path=relative, sha256="3" * 64) for name, relative in
               (("olympus", "olympus/__init__.py"), ("olympus.vault", "olympus/vault.py"),
                ("olympus.prefs", "olympus/prefs.py"), ("olympus.browser", "olympus/browser.py"))}
    receipt = dict(schema=suite.SCHEMA, status="completed", shard=shard, pytest_exit_code=0,
                   run_id="123", attempt="1", source_before=source, source_after=copy.deepcopy(source),
                   runtime=dict(platform="win32", implementation="cpython", python=[3, 12, 10],
                                packages=[dict(name="pytest", version="9.1.1", scope="runtime", path="Lib/site-packages/pytest-9.1.1.dist-info"),
                                          dict(name="pytest-timeout", version="2.4.0", scope="runtime", path="Lib/site-packages/pytest_timeout-2.4.0.dist-info")]),
                   modules=modules)
    return _repack(receipt, rows, _xml(selected))


def _repack(receipt, rows, xml):
    stream = b"".join(suite.encoded(row) + b"\n" for row in rows)
    receipt = copy.deepcopy(receipt)
    receipt.update(stream_sha256=suite.digest(stream), junit_sha256=suite.digest(xml))
    return receipt, stream, xml


def _rows(bundle):
    return [json.loads(line) for line in bundle[1].splitlines()]


def _aggregate(bundles, matrix_result="success"):
    return suite.aggregate_bundles(bundles, matrix_result=matrix_result, **CONTEXT)


def test_count_balancing_is_deterministic_and_preserves_whole_files_and_order():
    assert suite.assignments(NODES) == [NODES[i:i + 2] for i in range(0, 8, 2)]
    nodes = NODES + ["tests/test_a.py::TestClass::test_third", "tests/test_d.py::test_third"]
    result = suite.assignments(nodes)
    assert result == suite.assignments(list(nodes))
    assert sorted(node for shard in result for node in shard) == sorted(nodes)
    for letter in "abcd":
        own = [node for node in nodes if node.startswith(f"tests/test_{letter}.py::")]
        assert sum(all(node in shard for node in own) for shard in result) == 1
    for shard in result:
        assert shard == [node for node in nodes if node in shard]


@pytest.mark.parametrize("nodes", [[], [*NODES, NODES[0]], ["tests/test_a.py::only"],
                                    [*NODES[:-1], "../outside.py::test"],
                                    [*NODES[:-1], "C:/outside.py::test"], [*NODES[:-1], True]])
def test_invalid_collection_never_becomes_a_partition(nodes):
    with pytest.raises(ValueError):
        suite.assignments(nodes)


def test_complete_union_admits_only_same_run_source_and_classified_outcomes():
    result = _aggregate([_bundle(shard) for shard in (3, 0, 2, 1)])
    assert result["collected"] == 8
    assert result["disjoint_exhaustive_union"] is True
    assert result["whole_file_assignment"] is True
    assert result["phase_exceptions"] == []
    assert result["milestone_acceptance"] is False


@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped", "pending", "", None, True])
def test_successful_files_do_not_override_matrix_status(status):
    with pytest.raises(ValueError, match="matrix"):
        _aggregate([_bundle(i) for i in range(4)], status)


@pytest.mark.parametrize("ids", [[0, 1, 2], [0, 1, 2, 2], [0, 1, 2, 3, 3], []])
def test_missing_duplicate_and_extra_shards_refuse(ids):
    with pytest.raises(ValueError):
        _aggregate([_bundle(i) for i in ids])


@pytest.mark.parametrize("field,value", [("status", "started"), ("pytest_exit_code", 1),
                                        ("pytest_exit_code", False), ("shard", True),
                                        ("shard", 4), ("run_id", "124"), ("attempt", "2"),
                                        ("runtime", None), ("modules", {})])
def test_receipt_identity_and_completion_are_not_inferred(field, value):
    bundles = [_bundle(i) for i in range(4)]
    bundles[0][0][field] = value
    with pytest.raises(ValueError):
        _aggregate(bundles)


@pytest.mark.parametrize("change", ["order", "missing", "duplicate", "unknown", "split_file", "boolean_count"])
def test_collection_and_partition_corruption_refuses(change):
    bundles = [_bundle(i) for i in range(4)]
    receipt, _, xml = bundles[0]
    rows = _rows(bundles[0])
    collection = rows[1]
    if change == "order":
        collection["universe"][2:4] = list(reversed(collection["universe"][2:4]))
    elif change == "missing":
        collection["universe"].pop()
    elif change == "duplicate":
        collection["universe"].append(collection["universe"][0])
    elif change == "unknown":
        collection["universe"][-1] = "tests/test_z.py::unknown"
    elif change == "split_file":
        collection["selected"][-1] = NODES[2]
    else:
        rows[-1]["phases"] = True
    bundles[0] = _repack(receipt, rows, xml)
    with pytest.raises(ValueError):
        _aggregate(bundles)


@pytest.mark.parametrize("change", ["missing_call", "missing_teardown", "duplicate_setup", "unknown_node",
                                    "reordered", "failed", "boolean_duration", "false_success_junit"])
def test_missing_or_inconsistent_per_node_evidence_refuses(change):
    bundles = [_bundle(i) for i in range(4)]
    receipt, _, xml = bundles[0]
    rows = _rows(bundles[0])
    if change == "missing_call":
        del rows[3]
    elif change == "missing_teardown":
        del rows[4]
    elif change == "duplicate_setup":
        rows.insert(3, copy.deepcopy(rows[2]))
    elif change == "unknown_node":
        rows[3]["nodeid"] = "tests/test_unknown.py::test_unknown"
    elif change == "reordered":
        rows[2], rows[3] = rows[3], rows[2]
    elif change == "failed":
        rows[3]["outcome"] = "failed"
    elif change == "boolean_duration":
        rows[3]["duration_ns"] = True
    else:
        xml = _xml(NODES[:2], markers={NODES[0]: [("skipped", {"type": "pytest.skip"})]})
    rows[-1]["phases"] = sum(row["kind"] == "phase" for row in rows)
    bundles[0] = _repack(receipt, rows, xml)
    with pytest.raises(ValueError):
        _aggregate(bundles)


def test_same_aggregate_counts_cannot_hide_swapped_node_outcomes():
    receipt, _, _ = _bundle(0)
    rows = _rows(_bundle(0))
    rows[3].update(outcome="skipped", reason_sha256="5" * 64)
    wrong = _xml(NODES[:2], markers={NODES[1]: [("skipped", {"type": "pytest.skip"})]})
    bundle = _repack(receipt, rows, wrong)
    with pytest.raises(ValueError, match="per-node"):
        suite.verify_bundle(*bundle, **CONTEXT)


@pytest.mark.parametrize("kind", ["setup_skip", "xfail", "xpass"])
def test_setup_skip_xfail_and_nonstrict_xpass_remain_explicit(kind):
    receipt, _, _ = _bundle(0)
    rows = _rows(_bundle(0))
    markers = {}
    if kind == "setup_skip":
        rows[2].update(outcome="skipped", reason_sha256="5" * 64)
        del rows[3]
        rows[-1]["phases"] = 5
        markers[NODES[0]] = [("skipped", {"type": "pytest.skip"})]
    else:
        rows[3].update(xfail="reported", reason_sha256="5" * 64)
        if kind == "xfail":
            rows[3]["outcome"] = "skipped"
            markers[NODES[0]] = [("skipped", {"type": "pytest.xfail"})]
    bundle = _repack(receipt, rows, _xml(NODES[:2], markers=markers))
    checked = suite.verify_bundle(*bundle, **CONTEXT)
    assert len(checked["classifications"]) == 1
    assert checked["classifications"][0]["nodeid"] == NODES[0]
    assert checked["classifications"][0]["when"] == ("setup" if kind == "setup_skip" else "call")


def test_call_failure_and_teardown_error_have_two_junit_rows_for_one_node():
    phases = [_phase(NODES[0], "setup"), _phase(NODES[0], "call", "failed"),
              _phase(NODES[0], "teardown", "failed")]
    root = ET.Element("testsuites")
    tests = ET.SubElement(root, "testsuite", tests="2", errors="1", failures="1", skipped="0")
    cls, name = suite.pytest_names(NODES[0])
    ET.SubElement(ET.SubElement(tests, "testcase", classname=cls, name=name), "failure")
    ET.SubElement(ET.SubElement(tests, "testcase", classname=cls, name=name), "error")
    result = suite.verify_junit(ET.tostring(root), NODES[:1], phases, [])
    assert [(row["when"], row["outcome"]) for row in result] == [("call", "failed"), ("teardown", "failed")]


def test_class_and_parameter_names_are_joined_without_confusing_file_assignment():
    node = "tests/test_a.py::TestOwned::test_read[param.with.dot::colon]"
    assert suite.pytest_names(node) == ("tests.test_a.TestOwned", "test_read[param.with.dot::colon]")
    phases = [_phase(node, when) for when in ("setup", "call", "teardown")]
    assert suite.verify_junit(_xml([node]), [node], phases, []) == []


@pytest.mark.parametrize("part", ["stream", "xml", "footer"])
def test_partial_or_tampered_artifacts_are_not_completed(part):
    receipt, stream, xml = _bundle(0)
    if part == "stream":
        stream += b" "
    elif part == "xml":
        xml += b" "
    else:
        rows = _rows((receipt, stream, xml))[:-1]
        receipt, stream, xml = _repack(receipt, rows, xml)
    with pytest.raises(ValueError):
        suite.verify_bundle(receipt, stream, xml, **CONTEXT)


def test_collection_skips_are_preserved_separately_from_executed_union():
    bundles = []
    for shard in range(4):
        receipt, _, _ = _bundle(shard)
        rows = _rows(_bundle(shard))
        rows.insert(1, dict(kind="collect_skip", nodeid="tests/test_optional.py", reason_sha256="6" * 64))
        bundles.append(_repack(receipt, rows, _xml(NODES[2 * shard:2 * shard + 2],
                                                  collect_skips=["tests/test_optional.py"])))
    result = _aggregate(bundles)
    assert result["collected"] == 8
    assert len(result["collection_skips"]) == 1


def test_receipt_writes_are_exclusive_and_never_replace_prior_failure(tmp_path):
    path = tmp_path / "result.json"
    suite.write_new(path, {"status": "failed"})
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        suite.write_new(path, {"status": "completed"})
    assert path.read_bytes() == original


def test_owned_reporter_retains_full_universe_then_executes_only_exact_partition(tmp_path):
    rows = []
    reporter = suite.make_plugin(pytest, tmp_path, 0, rows.append)
    items = [SimpleNamespace(nodeid=node) for node in NODES]
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=reporter.pytest_deselected))
    session = SimpleNamespace(items=items, config=config)
    hook = reporter.pytest_collection_finish(session)
    next(hook)
    with pytest.raises(StopIteration):
        next(hook)
    assert rows == [dict(kind="collection", universe=NODES, selected=NODES[:2], shard=0)]
    assert [item.nodeid for item in session.items] == NODES[:2]
    reporter.pytest_runtestloop(session)
    session.items.append(SimpleNamespace(nodeid=NODES[2]))
    with pytest.raises(ValueError, match="partition"):
        reporter.pytest_runtestloop(session)
    with pytest.raises(ValueError, match="deselection"):
        reporter.pytest_deselected(items=[])


@pytest.mark.parametrize("field,value", [("keyword", "selected"), ("markexpr", "slow"), ("maxfail", 1),
                                        ("maxfail", False), ("ignore", ["tests/test_a.py"]), ("numprocesses", 2),
                                        ("collectonly", True), ("timeout", 301)])
def test_reporter_refuses_selection_or_resource_contract_changes(tmp_path, field, value):
    reporter = suite.make_plugin(pytest, tmp_path, 0, lambda row: None)
    options = SimpleNamespace(maxfail=None, timeout=300)
    setattr(options, field, value)
    config = SimpleNamespace(rootpath=tmp_path, args=[str(tmp_path)], option=options,
                             _env_timeout=options.timeout, _env_timeout_func_only=False,
                             getini=lambda name: "300",
                             pluginmanager=SimpleNamespace(hasplugin=lambda name: name == "timeout"))
    with pytest.raises(ValueError):
        reporter.pytest_configure(config)



def test_passing_call_with_teardown_skip_retains_both_junit_counters():
    node = NODES[0]
    phases = [_phase(node, "setup"), _phase(node, "call"), _phase(node, "teardown", "skipped")]
    xml = ET.fromstring(_xml([node], markers={node: [("skipped", {"type": "pytest.skip"})]}))
    xml[0].set("tests", "2")
    result = suite.verify_junit(ET.tostring(xml), [node], phases, [])
    assert len(result) == 1 and result[0]["when"] == "teardown"
    xml[0].set("tests", "1")
    with pytest.raises(ValueError, match="total"):
        suite.verify_junit(ET.tostring(xml), [node], phases, [])


def test_skipped_call_with_teardown_error_is_one_junit_case_with_both_markers():
    node = NODES[0]
    phases = [_phase(node, "setup"), _phase(node, "call", "skipped"), _phase(node, "teardown", "failed")]
    xml = _xml([node], markers={node: [("skipped", {"type": "pytest.skip"}), ("error", {})]})
    result = suite.verify_junit(xml, [node], phases, [])
    assert [(row["when"], row["outcome"]) for row in result] == [("call", "skipped"), ("teardown", "failed")]


def test_late_warning_callback_does_not_follow_a_premature_completed_event(tmp_path):
    rows = []
    reporter = suite.make_plugin(pytest, tmp_path, 0, rows.append)
    reporter.pytest_sessionfinish(None, 0)
    reporter.pytest_warning_recorded(SimpleNamespace(category=UserWarning, message=UserWarning("owned warning")),
                                     "config", "", None)
    assert reporter.finished is True and reporter.exitstatus == 0
    assert len(rows) == 1 and rows[0]["kind"] == "warning"
    assert not any(row["kind"] == "session_end" for row in rows)
    with pytest.raises(ValueError, match="duplicate"):
        reporter.pytest_sessionfinish(None, 0)


def test_canonical_git_identity_is_distinct_from_platform_checkout_bytes():
    windows = _bundle(0)[0]["source_before"]
    linux = copy.deepcopy(windows)
    linux["files"]["olympus/vault.py"]["sha256"] = "9" * 64
    assert windows != linux
    assert suite.canonical_source(windows) == suite.canonical_source(linux)
    linux["files"]["olympus/vault.py"]["blob_oid"] = "8" * 40
    assert suite.canonical_source(windows) != suite.canonical_source(linux)


def test_one_windows_raw_manifest_cannot_drift_even_with_same_canonical_tree():
    bundles = [_bundle(i) for i in range(4)]
    receipt = bundles[0][0]
    for field in ("source_before", "source_after"):
        receipt[field]["files"]["olympus/vault.py"]["sha256"] = "9" * 64
    receipt["modules"]["olympus.vault"]["sha256"] = "9" * 64
    with pytest.raises(ValueError, match="disagree"):
        _aggregate(bundles)



def test_normal_configuration_and_reporter_lifecycle_keep_late_warning(tmp_path):
    rows = []
    reporter = suite.make_plugin(pytest, tmp_path, 0, rows.append)
    config = SimpleNamespace(rootpath=tmp_path, args=[str(tmp_path)],
                             option=SimpleNamespace(maxfail=None, timeout=None),
                             getini=lambda name: "300", _env_timeout=300.0, _env_timeout_func_only=False,
                             pluginmanager=SimpleNamespace(hasplugin=lambda name: name == "timeout"),
                             hook=SimpleNamespace(pytest_deselected=reporter.pytest_deselected))
    reporter.pytest_configure(config)
    session = SimpleNamespace(items=[SimpleNamespace(nodeid=node) for node in NODES], config=config)
    hook = reporter.pytest_collection_finish(session)
    next(hook)
    with pytest.raises(StopIteration):
        next(hook)
    reporter.pytest_runtestloop(session)
    for node in NODES[:2]:
        for when in ("setup", "call", "teardown"):
            reporter.pytest_runtest_logreport(SimpleNamespace(nodeid=node, when=when, outcome="passed",
                                                              duration=0.001, longrepr=None))
    reporter.pytest_sessionfinish(session, 0)
    reporter.pytest_warning_recorded(SimpleNamespace(category=UserWarning, message=UserWarning("owned warning")),
                                     "config", "", None)
    assert reporter.phases == 6 and reporter.warnings == 1
    raw = b"".join(suite.encoded(row) + b"\n" for row in rows)
    with pytest.raises(ValueError, match="incomplete"):
        suite.read_stream(raw)
    footer = dict(kind="session_end", exitstatus=0, phases=6, warnings=1)
    collection, phases, skips, warnings, end = suite.read_stream(raw + suite.encoded(footer) + b"\n")
    assert collection["universe"] == NODES and collection["selected"] == NODES[:2]
    assert len(phases) == 6 and skips == [] and len(warnings) == 1 and end == footer


def _fake_checkout(tmp_path, monkeypatch, fault=None):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "owned.txt").write_bytes(b"owned\r\n")
    calls = []
    head_reads = 0
    def fake_run(argv, **kwargs):
        nonlocal head_reads
        calls.append(argv)
        assert argv[:6] == ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root)]
        command = argv[6:]
        assert kwargs == dict(capture_output=True, timeout=30, check=False)
        if command == ["rev-parse", "HEAD"]:
            head_reads += 1
            stdout = (("9" * 40 if fault == "head_changed" and head_reads > 1 else SHA) + "\n").encode()
        elif command == ["rev-parse", "HEAD^{tree}"]:
            stdout = (TREE + "\n").encode()
        elif command == ["ls-tree", "-rz", "HEAD"]:
            mode = "120000" if fault == "nonregular" else "100644"
            name = "../outside.txt" if fault == "escape" else "owned.txt"
            stdout = (mode + " blob " + "4" * 40 + "\t" + name + "\x00").encode()
        else:
            assert command == ["diff", "--no-ext-diff", "--no-textconv", "--exit-code", "--quiet", "HEAD", "--"]
            stdout = b""
            if fault == "dirty":
                return SimpleNamespace(returncode=1, stdout=stdout, stderr=b"")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"owned diagnostic" if fault == "stderr" else b"")
    monkeypatch.setattr(suite.subprocess, "run", fake_run)
    return root, calls


def test_source_binding_records_canonical_objects_and_actual_checkout_bytes(tmp_path, monkeypatch):
    root, calls = _fake_checkout(tmp_path, monkeypatch)
    result = suite.source_binding(root)
    assert result == dict(head=SHA, tree=TREE,
                         files={"owned.txt": dict(mode="100644", blob_oid="4" * 40,
                                                  sha256=suite.digest(b"owned\r\n"))})
    assert sum(call[6] == "diff" for call in calls) == 2
    assert len(calls) == 7


@pytest.mark.parametrize("fault", ["dirty", "head_changed", "nonregular", "escape", "stderr"])
def test_source_binding_refuses_failed_or_changed_git_observation(tmp_path, monkeypatch, fault):
    root, _ = _fake_checkout(tmp_path, monkeypatch, fault)
    with pytest.raises(ValueError):
        suite.source_binding(root)



@pytest.mark.parametrize("effective,func_only,plugin", [
    (None, False, True), ("300", False, True), (True, False, True),
    (299.0, False, True), (301, False, True), (float("nan"), False, True),
    (300.0, True, True), (300.0, None, True), (300.0, 0, True), (300.0, False, False),
])
def test_effective_timeout_not_ini_or_cli_controls_the_guard(tmp_path, effective, func_only, plugin):
    reporter = suite.make_plugin(pytest, tmp_path, 0, lambda row: None)
    config = SimpleNamespace(rootpath=tmp_path, args=[str(tmp_path)],
                             option=SimpleNamespace(maxfail=None, timeout=300),
                             getini=lambda name: "300", _env_timeout=effective,
                             _env_timeout_func_only=func_only,
                             pluginmanager=SimpleNamespace(hasplugin=lambda name: plugin))
    with pytest.raises(ValueError, match="300-second"):
        reporter.pytest_configure(config)


def test_reporter_checks_initialized_timeout_late_without_rewriting_test_markers(tmp_path):
    reporter = suite.make_plugin(pytest, tmp_path, 0, lambda row: None)
    assert reporter.pytest_configure.pytest_impl["trylast"] is True
    marker = pytest.mark.timeout(120)
    config = SimpleNamespace(rootpath=tmp_path, args=[str(tmp_path)],
                             option=SimpleNamespace(maxfail=0, timeout=None),
                             getini=lambda name: "300", _env_timeout=300.0, _env_timeout_func_only=False,
                             pluginmanager=SimpleNamespace(hasplugin=lambda name: name == "timeout"))
    reporter.pytest_configure(config)
    assert config._env_timeout == 300.0 and config._env_timeout_func_only is False
    assert marker.args == (120,)


def _metadata(name="owned-project", version="1.0", scope="runtime", path="Lib/site-packages/owned_project-1.0.dist-info"):
    return dict(name=name, version=version, scope=scope, path=path)


def test_duplicate_discovery_deduplicates_origin_but_preserves_editable_locations():
    installed = _metadata()
    editable = _metadata("Owned.Project", scope="checkout", path="owned_project.egg-info")
    rows = suite.metadata_packages([installed, editable, dict(installed)])
    assert len(rows) == 2
    assert {row["scope"] for row in rows} == {"checkout", "runtime"}
    assert {row["name"] for row in rows} == {"owned-project"}
    assert suite.metadata_packages(list(reversed(rows))) == rows


@pytest.mark.parametrize("other", [
    _metadata(version="2.0", scope="checkout", path="owned_project.egg-info"),
    _metadata(version="2.0"), _metadata(name="different-project"),
])
def test_conflicting_versions_or_one_origin_claiming_another_package_refuse(other):
    with pytest.raises(ValueError, match="conflict"):
        suite.metadata_packages([_metadata(), other])


@pytest.mark.parametrize("field,value", [
    ("name", None), ("name", "bad/name"), ("version", True), ("version", "bad version"),
    ("scope", "ambient"), ("path", "../owned.dist-info"), ("path", "/owned.dist-info"),
    ("path", "C:/owned.dist-info"), ("path", "owned.txt"), ("path", "owned\\bad.dist-info"),
])
def test_malformed_metadata_identity_refuses(field, value):
    row = _metadata()
    row[field] = value
    with pytest.raises(ValueError):
        suite.metadata_packages([row])


def test_metadata_search_roots_are_resolved_unique_and_do_not_mutate_input(tmp_path):
    entries = [str(tmp_path), str(tmp_path / "."), str(tmp_path / "child" / "..")]
    prior = list(entries)
    assert suite.metadata_search_roots(entries) == [tmp_path.resolve()]
    assert entries == prior
    with pytest.raises(ValueError, match="bound"):
        suite.metadata_search_roots(entries * 43)


def test_runtime_metadata_keeps_scoped_origins_and_searches_each_resolved_root_once(tmp_path, monkeypatch):
    checkout, runtime, base = [tmp_path / name for name in ("checkout", "runtime", "base")]
    for root in (checkout, runtime, base):
        root.mkdir()
    editable = checkout / "owned_project.egg-info"
    installed = runtime / "Lib" / "site-packages" / "owned_project-1.0.dist-info"
    base_info = base / "Lib" / "site-packages" / "base_package-2.0.dist-info"
    for origin in (editable, installed, base_info):
        origin.mkdir(parents=True)
    observed = []
    def distributions(*, path):
        assert len(path) == 1
        observed.append(path[0])
        by_path = {
            str(checkout): [SimpleNamespace(_path=editable, metadata={"Name": "Owned.Project"}, version="1.0")],
            str(runtime): [SimpleNamespace(_path=installed, metadata={"Name": "owned_project"}, version="1.0")] * 2,
            str(base): [SimpleNamespace(_path=base_info, metadata={"Name": "base-package"}, version="2.0")],
        }
        return iter(by_path[path[0]])
    entries = [str(runtime), str(checkout), str(checkout / "."), str(base)]
    monkeypatch.setattr(suite.sys, "path", entries)
    monkeypatch.setattr(suite.sys, "prefix", str(runtime))
    monkeypatch.setattr(suite.sys, "base_prefix", str(base))
    monkeypatch.setattr(suite.importlib.metadata, "distributions", distributions)
    before = suite.runtime_binding(checkout)
    assert len(observed) == len(set(observed)) == 3
    assert len(before["packages"]) == 3
    assert {row["scope"] for row in before["packages"]} == {"checkout", "runtime", "base-runtime"}
    assert str(tmp_path) not in json.dumps(before)
    assert suite.sys.path is entries and len(entries) == 4
    monkeypatch.setattr(suite.sys, "path", [str(checkout), *entries])
    assert suite.runtime_binding(checkout) == before


def test_metadata_outside_known_roots_or_unsupported_resolver_refuses(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "ambient.dist-info"
    outside.mkdir()
    monkeypatch.setattr(suite.sys, "prefix", str(checkout))
    monkeypatch.setattr(suite.sys, "base_prefix", str(checkout))
    with pytest.raises(ValueError, match="outside"):
        suite.metadata_origin(outside, checkout)
    with pytest.raises(ValueError, match="unsupported"):
        suite.metadata_origin(str(outside), checkout)


def test_aggregate_accepts_distinct_same_version_metadata_but_refuses_noncanonical_duplicates():
    bundles = [_bundle(i) for i in range(4)]
    for receipt, stream, xml in bundles:
        receipt["runtime"]["packages"] = suite.metadata_packages([
            *receipt["runtime"]["packages"],
            _metadata("pytest", "9.1.1", "checkout", "pytest.egg-info"),
        ])
    assert _aggregate(bundles)["status"] == "completed"
    bundles[0][0]["runtime"]["packages"].append(dict(bundles[0][0]["runtime"]["packages"][0]))
    with pytest.raises(ValueError, match="dependency identity"):
        _aggregate(bundles)


def _owned_character_node(size, fill="x", file="bound"):
    prefix, suffix = "tests/test_" + file + ".py::test_owned[", "]"
    assert len(fill) == 1
    node = prefix + fill * (size - len(prefix + suffix)) + suffix
    assert len(node) == size
    return node


@pytest.mark.parametrize("size,fill", [(4096, "x"), (4096, "\u20ac"),
                                      (4097, "x"), (4097, "\u20ac")],
                         ids=["ascii-at-limit", "unicode-at-character-limit",
                              "ascii-over-limit", "unicode-over-character-limit"])
def test_collection_keeps_existing_character_limit(size, fill):
    node = _owned_character_node(size, fill)
    if size == 4096:
        assert suite.nodes_checked([node]) == [node]
    else:
        with pytest.raises(ValueError, match="invalid collected node"):
            suite.nodes_checked([node])


def test_maximum_admitted_ids_keep_exact_partition_phase_and_junit_join():
    nodes = [_owned_character_node(4096, fill, file)
             for file, fill in [("a", "x"), ("b", "\u20ac"), ("c", "y"), ("d", "z")]]
    assert suite.assignments(nodes) == [[node] for node in nodes]
    for shard, node in enumerate(nodes):
        phases = [_phase(node, when) for when in ("setup", "call", "teardown")]
        assert suite.verify_junit(_xml([node]), [node], phases, []) == []
        rows = [dict(kind="configuration", standard_selection=True, timeout=300),
                dict(kind="collection", universe=nodes, selected=[node], shard=shard),
                *phases, dict(kind="session_end", exitstatus=0, phases=3, warnings=0)]
        observed = suite.read_stream(b"".join(suite.encoded(row) + b"\n" for row in rows))
        assert observed[0]["selected"] == [node] and observed[1] == phases
        with pytest.raises(ValueError, match="node mismatch"):
            suite.verify_junit(_xml([node[:-2] + "q]"]), [node], phases, [])
        with pytest.raises(ValueError, match="phase"):
            suite.verify_junit(_xml([node]), [node], phases[:-1], [])


@pytest.mark.parametrize("failure_type", [ValueError, OSError, KeyboardInterrupt, SystemExit])
def test_run_preserves_actual_collection_hook_failure_without_completed_receipt(tmp_path, monkeypatch, failure_type):
    root, output = tmp_path / "checkout", tmp_path / "evidence"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(suite, "__file__", str(root / "scripts" / "ci_windows_suite.py"))
    monkeypatch.setattr(suite, "os", SimpleNamespace(name="nt", environ={"GITHUB_ACTIONS": "true"}))
    monkeypatch.setattr(suite, "sys", SimpleNamespace(version_info=(3, 12)))
    observed = []
    source = {"head": SHA, "tree": TREE, "files": {}}
    monkeypatch.setattr(suite, "source_binding", lambda selected: observed.append(("source", selected)) or source)
    monkeypatch.setattr(suite, "runtime_binding", lambda selected: observed.append(("runtime", selected)) or {"owned": True})
    primary = failure_type("owned collection primary")
    def fail_assignment(nodes):
        assert nodes == NODES
        raise primary
    monkeypatch.setattr(suite, "assignments", fail_assignment)
    def forbidden(*args, **kwargs):
        pytest.fail("incomplete collection reached completed-bundle validation")
    monkeypatch.setattr(suite, "module_bindings", forbidden)
    original_read_stream = suite.read_stream
    monkeypatch.setattr(suite, "read_stream", forbidden)
    def fake_main(args, *, plugins):
        assert args == ["-q", "--junitxml=" + str(output / "junit.xml")]
        assert len(plugins) == 1
        reporter = plugins[0]
        config = SimpleNamespace(rootpath=root, args=[str(root)],
            option=SimpleNamespace(maxfail=None), _env_timeout=300.0, _env_timeout_func_only=False,
            pluginmanager=SimpleNamespace(hasplugin=lambda name: name == "timeout"))
        reporter.pytest_configure(config)
        session = SimpleNamespace(items=[SimpleNamespace(nodeid=node) for node in NODES], config=config)
        hook = reporter.pytest_collection_finish(session)
        next(hook)
        with pytest.raises(failure_type) as caught:
            next(hook)
        assert caught.value is reporter.collection_error is primary
        assert reporter.selected is None and reporter.phases == 0
        reporter.pytest_sessionfinish(session, 3)
        return 3
    monkeypatch.setattr(pytest, "main", fake_main)
    with pytest.raises(failure_type) as caught:
        suite.run(0, output, SHA, "123", "1")
    assert caught.value is primary
    assert observed == [("source", root), ("runtime", root), ("source", root), ("runtime", root)]
    assert json.loads((output / "started.json").read_bytes())["status"] == "started"
    assert not (output / "result.json").exists()
    raw = (output / "events.jsonl").read_bytes()
    assert [row["kind"] for row in map(json.loads, raw.splitlines())] == ["configuration", "session_end"]
    with pytest.raises(ValueError, match="duplicate/missing session boundary"):
        original_read_stream(raw)


def test_collection_failure_retains_long_node_refusal_and_never_partitions(tmp_path):
    rows = []
    reporter = suite.make_plugin(pytest, tmp_path, 0, rows.append)
    node = _owned_character_node(4097)
    session = SimpleNamespace(items=[SimpleNamespace(nodeid=node)], config=None)
    hook = reporter.pytest_collection_finish(session)
    next(hook)
    with pytest.raises(ValueError, match="invalid collected node") as caught:
        next(hook)
    assert reporter.collection_error is caught.value and reporter.selected is None
    assert reporter.phases == 0 and rows == []
    assert [item.nodeid for item in session.items] == [node]



@pytest.mark.parametrize("target", ["dumps", "encode", "iterencode"])
def test_reporter_serializes_while_json_fault_patch_is_active(tmp_path, monkeypatch, target):
    node = "tests/test_owned.py::test_json_fault"
    rows = []
    reporter = suite.make_plugin(pytest, tmp_path, 0,
                                 lambda row: rows.append(suite.encoded(row)))
    reporter.selected = [node]
    owner = json if target == "dumps" else json.JSONEncoder
    def forbidden(*args, **kwargs):
        raise RuntimeError("owned JSON fault remains active")
    monkeypatch.setattr(owner, target, forbidden)
    with pytest.raises(RuntimeError, match="owned JSON fault remains active"):
        json.dumps({"ordinary": True})
    reporter.pytest_runtest_logreport(SimpleNamespace(
        nodeid=node, when="call", outcome="passed", duration=0.000001, longrepr=None))
    assert reporter.phases == 1
    assert rows == [
        b'{"duration_ns":1000,"kind":"phase","nodeid":"tests/test_owned.py::test_json_fault",'
        b'"outcome":"passed","reason_sha256":null,"when":"call","xfail":"none"}'
    ]
    assert suite.encoded({"z": "\u20ac", "a": [True, None, 1.5]}) == (
        b'{"a":[true,null,1.5],"z":"\\u20ac"}')
    with pytest.raises(ValueError):
        suite.encoded({"nonfinite": float("nan")})
    # The reporter must not restore globals and thereby neutralize the product fault.
    assert getattr(owner, target) is forbidden
    with pytest.raises(RuntimeError, match="owned JSON fault remains active"):
        json.dumps({"ordinary": True})
