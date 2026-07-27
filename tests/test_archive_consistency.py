"""Programme A — archival consistency guard.

`archive-status.json` is the machine-readable closure marker. A marker that
disagrees with the prose it points at is worse than no marker: an automated
reader trusts it, and a human reads the report. These tests make the two
inseparable.

They also enforce the closure rules themselves — no deleted history, no
production over-claim, the required classification vocabulary, and the
distinction between programme closure and production approval.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_ABS = _REPO / "docs" / "absorption"
_MARKER = _ABS / "archive-status.json"
_REPORT = _ABS / "COLIBRI_ABSORPTION_FINAL_REPORT.md"
_INDEX = _ABS / "COLIBRI_ARCHIVE.md"

VALID_VERDICTS = ("COLIBRI ABSORPTION COMPLETE", "COLIBRI ABSORPTION INCOMPLETE")

#: The only permitted capability classifications. "mostly complete" and friends
#: are banned by the programme rules — a vague classification is how a gap
#: survives a review.
VALID_CLASSIFICATIONS = (
    "ABSORBED", "ABSORBED_AND_REDESIGNED",
    "REPLACED_BY_OLYMPUS_NATIVE_DESIGN", "REJECTED",
    "DEFERRED_PENDING_EVIDENCE", "OUT_OF_SCOPE", "INCOMPLETE",
)


@pytest.fixture(scope="module")
def marker() -> dict:
    return json.loads(_MARKER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def report() -> str:
    return _REPORT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index() -> str:
    return _INDEX.read_text(encoding="utf-8")


# ── the three artifacts exist and agree ────────────────────────────────────

def test_all_three_closure_artifacts_exist():
    for path in (_MARKER, _REPORT, _INDEX):
        assert path.exists(), f"missing closure artifact: {path}"


def test_the_marker_points_at_files_that_exist(marker):
    for key in ("final_report", "archive_index"):
        target = _REPO / marker[key]
        assert target.exists(), f"{key} points at a missing file: {marker[key]}"


def test_the_verdict_is_one_of_the_two_permitted_labels(marker):
    """The programme rules forbid inventing an intermediate label."""
    assert marker["final_verdict"] in VALID_VERDICTS, marker["final_verdict"]


def test_the_marker_verdict_matches_the_report_verdict(marker, report):
    """The load-bearing consistency check: an automated reader trusts the JSON,
    a human reads the Markdown. They must not be able to disagree."""
    verdict = marker["final_verdict"]
    assert f"# {verdict}" in report, (
        f"archive-status.json says {verdict!r} but the final report does not "
        f"declare it as a heading")
    other = [v for v in VALID_VERDICTS if v != verdict][0]
    assert f"# {other}" not in report, (
        f"the final report declares BOTH verdicts as headings")


def test_the_index_verdict_matches_the_marker(marker, index):
    assert marker["final_verdict"] in index
    other = [v for v in VALID_VERDICTS if v != marker["final_verdict"]][0]
    assert other not in index, "the archive index states both verdicts"


def test_status_and_verdict_cannot_contradict(marker):
    if marker["status"] == "closed":
        assert marker["final_verdict"] == "COLIBRI ABSORPTION COMPLETE", (
            "a programme cannot be 'closed' while its verdict is INCOMPLETE — "
            "an incomplete programme is still open")
    else:
        assert marker["final_verdict"] == "COLIBRI ABSORPTION INCOMPLETE"


# ── the dependency claim is verified, not just asserted ────────────────────

def test_no_colibri_import_or_identifier_in_runtime_code(marker):
    """§16's load-bearing claim. Checked by AST, so a comment mentioning
    Colibri (design provenance, deliberately preserved) never trips it, but an
    actual dependency always would."""
    import ast

    offences = []
    for path in sorted((_REPO / "olympus").glob("*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        if "colibri" not in src.lower():
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import) and any(
                    "colibri" in a.name.lower() for a in node.names):
                offences.append(f"{path.name}: import")
            if isinstance(node, ast.ImportFrom) and "colibri" in (
                    node.module or "").lower():
                offences.append(f"{path.name}: from-import")
            if isinstance(node, ast.Name) and "colibri" in node.id.lower():
                offences.append(f"{path.name}: identifier {node.id}")
            if isinstance(node, ast.Attribute) and "colibri" in node.attr.lower():
                offences.append(f"{path.name}: attribute {node.attr}")
    assert not offences, (
        "the archive claims no Colibri runtime dependency, but found: "
        + "; ".join(offences))
    assert marker["runtime_dependency"] is False


def test_no_colibri_dependency_in_build_files(marker):
    for name in ("requirements.txt", "requirements.lock", "pyproject.toml"):
        path = _REPO / name
        if path.exists():
            assert "colibri" not in path.read_text(
                encoding="utf-8", errors="replace").lower(), name
    assert marker["build_dependency"] is False


def test_no_colibri_dependency_in_the_active_roadmap(marker):
    """A closed programme must not still be steering the roadmap."""
    for name in ("ROADMAP.md", "NORTH_STAR.md"):
        path = _REPO / "docs" / name
        if path.exists():
            assert "colibri" not in path.read_text(
                encoding="utf-8", errors="replace").lower(), (
                f"docs/{name} still references Colibri")
    assert marker["roadmap_dependency"] is False


# ── the classification vocabulary is closed ────────────────────────────────

def test_the_report_uses_only_permitted_classifications(report):
    """Vague labels are how a gap survives a review."""
    for banned in ("mostly complete", "largely complete", "mostly done",
                   "partially absorbed", "nearly complete"):
        assert banned not in report.lower(), (
            f"the final report uses the vague classification {banned!r}")


def test_marker_classification_counts_are_internally_consistent(marker):
    counts = marker["classification_counts"]
    assert set(counts) == set(VALID_CLASSIFICATIONS), (
        f"unknown classification in the marker: "
        f"{set(counts) ^ set(VALID_CLASSIFICATIONS)}")
    assert sum(counts.values()) == marker["capabilities_dispositioned"], (
        f"classification counts sum to {sum(counts.values())} but the marker "
        f"claims {marker['capabilities_dispositioned']} capabilities")


def test_deferred_count_matches_the_deferred_list(marker):
    assert len(marker["deferred_capabilities"]) == \
        marker["classification_counts"]["DEFERRED_PENDING_EVIDENCE"]


def test_every_deferred_capability_is_registered_as_an_experiment(marker):
    """Rule: a deferred capability must have an evidence gate, and the
    registry is where gates live. A deferral that is not registered is just a
    capability somebody forgot."""
    registry = json.loads(
        (_REPO / "olympus" / "experiments.json").read_text(encoding="utf-8"))
    ids = {e["id"] for e in registry["entries"]}
    expected = {
        "speculative-execution": "wave3-speculation-draftverify",
        "predictive-prefetch": "wave3-prefetch-activation",
        "provider-mirroring": "wave3-provider-mirror",
        "local-inference-tier": "wave3-local-tier",
    }
    for cap in marker["deferred_capabilities"]:
        entry_id = expected.get(cap)
        assert entry_id, f"no registry mapping known for deferred {cap!r}"
        assert entry_id in ids, (
            f"deferred capability {cap!r} has no entry in experiments.json")
        entry = next(e for e in registry["entries"] if e["id"] == entry_id)
        assert entry["status"] == "proposed", (
            f"{entry_id} is deferred in the archive but {entry['status']!r} "
            f"in the registry")
        assert entry.get("activation_condition"), (
            f"{entry_id} is deferred with no activation condition — a deferral "
            f"without an evidence gate is not a deferral")


def test_the_modules_that_deferred_capabilities_would_need_were_never_built():
    """The module budget must not have been spent on speculative code."""
    for module in ("localtier", "draftverify", "coalesce"):
        assert not (_REPO / "olympus" / f"{module}.py").exists(), (
            f"olympus/{module}.py exists, but its capability is recorded as "
            f"DEFERRED_PENDING_EVIDENCE — either it was built (update the "
            f"classification) or it is dead speculative code")


def test_the_module_budget_was_respected(marker):
    assert marker["new_modules"] <= marker["module_budget"]
    for module in ("sessionlog", "ctxbudget", "modelgate", "coupling",
                   "modelgrade", "ctxheat", "routesub", "experiments",
                   "ingestgate", "watchdog", "streamguard", "shadow",
                   "retention"):
        assert (_REPO / "olympus" / f"{module}.py").exists(), (
            f"the archive counts olympus/{module}.py among its new modules, "
            f"but the file does not exist")


# ── closure is not production approval ─────────────────────────────────────

def test_the_marker_separates_closure_from_production_approval(marker):
    pr = marker["production_readiness"]
    assert pr["verdict"] != "PRODUCTION APPROVED"
    assert "not production approval" in pr["note"].lower()
    assert (_REPO / pr["source"]).exists()


def test_the_report_states_the_boundary_explicitly(report):
    # Normalised for markdown decoration AND line wrapping: the sentence lives
    # in a bold blockquote and wraps mid-phrase, so a raw substring check would
    # pass or fail on layout rather than on meaning.
    flat = " ".join(report.lower().replace(">", " ").replace("*", "").split())
    assert "does not equal production approval" in flat, (
        "the final report must state explicitly that programme closure is not "
        "production approval")


def test_no_closure_document_claims_a_deployment_happened():
    """The single most tempting over-claim at closure time."""
    for doc in (_REPORT, _INDEX):
        text = doc.read_text(encoding="utf-8").lower()
        for phrase in ("successfully deployed", "running in production",
                       "deployed to staging and verified",
                       "live traffic confirmed"):
            assert phrase not in text, f"{doc.name} claims {phrase!r}"


# ── history is preserved ───────────────────────────────────────────────────

def test_no_historical_absorption_document_was_deleted():
    """Rule 9: do not delete Colibri documentation or historical evidence."""
    required = [
        "00-SYNTHESIS.md", "13-review-gaps.md",
        "WAVE1_IMPLEMENTATION_SPEC.md", "WAVE1_COMPLETION_REPORT.md",
        "WAVE1_INDEPENDENT_AUDIT.md",
        "WAVE2_IMPLEMENTATION_SPEC.md", "WAVE2_COMPLETION_REPORT.md",
        "WAVE3_EVIDENCE_REVIEW.md", "WAVE3_IMPLEMENTATION_SPEC.md",
        "WAVE3_COMPLETION_REPORT.md", "WAVE3_REVIEW_AFTER_SHADOW.md",
        "PRODUCTION_READINESS_REPORT.md", "PRIVACY_RETENTION_REVIEW.md",
        "PHASE5_STAGING_SHADOW_SPEC.md", "PHASE5_STAGING_REPORT.md",
        "PHASE5_SHADOW_EVIDENCE_REPORT.md",
        "PHASE5_CLIENT_COMPATIBILITY_REPORT.md",
        "PHASE5_PROVIDER_QUALIFICATION_REPORT.md",
        "PHASE5_BACKUP_RECOVERY_REPORT.md",
        "PHASE5_RETENTION_DELETION_REPORT.md",
        "PHASE5_COMPLETION_REPORT.md",
    ]
    for i in range(1, 13):
        matches = list(_ABS.glob(f"{i:02d}-*.md"))
        assert matches, f"domain analysis {i:02d}-*.md is missing"
    missing = [n for n in required if not (_ABS / n).exists()]
    assert not missing, f"historical documents deleted: {missing}"


def test_every_indexed_file_exists(index):
    """A broken archive index is a broken archive.

    Every link INSIDE the archive must resolve. Links that point outside
    `docs/absorption/` are cross-programme pointers whose targets belong to a
    different commit, so they are reported separately rather than failing this
    guard — but they are still checked, so a permanently dangling pointer is
    visible."""
    broken_internal, dangling_external = [], []
    for link in re.findall(r"\]\(([^)]+\.(?:md|json))\)", index):
        if link.startswith(("http://", "https://")):
            continue
        target = (_ABS / link).resolve()
        if target.exists():
            continue
        (dangling_external if link.startswith("../") else
         broken_internal).append(link)
    assert not broken_internal, (
        f"archive index links to missing files: {broken_internal}")
    assert not dangling_external, (
        f"archive index has dangling cross-programme pointers "
        f"(expected once the referenced programme lands): {dangling_external}")


def test_the_index_declares_colibri_is_no_longer_the_roadmap(index):
    assert "no longer the active architectural roadmap" in index.lower()
    assert "programme charter" in index.lower(), (
        "the index must state that reopening requires a new charter")


def test_the_report_carries_the_required_structure(report):
    """Nineteen sections were required. A report missing one has an
    unanswered question, not a shorter format."""
    required_headings = [
        "## 1. Document control", "## 2. Executive summary",
        "## 3. Original objectives", "## 4. Programme timeline",
        "## 5. Final capability matrix",
        "## 6. Olympus architecture, before and after",
        "## 7. Olympus-native inventions and redesigns",
        "## 8. Rejected Colibri ideas", "## 9. Deferred capabilities",
        "## 10. Independent audit findings",
        "## 11. False claims and corrected claims",
        "## 12. Security and privacy outcome", "## 13. Validation outcome",
        "## 14. Production-readiness boundary",
        "## 15. Remaining technical debt",
        "## 16. Why Olympus no longer depends on Colibri",
        "## 17. Programme closure criteria", "## 18. Final verdict",
        "## 19. Signed evidence statement",
    ]
    missing = [h for h in required_headings if h not in report]
    assert not missing, f"final report is missing sections: {missing}"
