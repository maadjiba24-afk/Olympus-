"""Wave-2 C9 (optimization liveness) + C10 (configuration-skew diagnostics).

Closes review gaps G6 (silent no-op substrate detection) and G7 (startup-only
settings / config skew).

Invariants under test:
  W2-I9.1  a configured-but-inactive optimization is reported INACTIVE (WARN),
           never as a success.
  W2-I9.2  liveness is a PURE READ of existing telemetry — no new measurement,
           no model call, no network.
  W2-I10.1 config-skew diagnostics NEVER self-correct — they only report, and
           every finding names the exact operator action.
  W2-I10.2 startup-scoped settings are a declared, enumerable registry in
           `config`, surfaced as a running-process fingerprint via `health`.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket

import pytest

from olympus import config, doctor, health

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _usage_dir():
    d = config.MEMORY_DIR / "usage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_day(day: str, ledger: dict) -> None:
    (_usage_dir() / f"{day}.json").write_text(json.dumps(ledger),
                                              encoding="utf-8")


def _active_ledger() -> dict:
    """A day file where the provider reported real cache reads."""
    return {
        "model:claude-opus-4-8": {"in": 5000, "out": 500, "cost": 0.03,
                                  "cache_read": 40000, "cache_creation": 2000},
        "prefix": {"fp0000000001": {"calls": 10, "hits": 8, "cache_read": 40000}},
    }


def _inert_ledger() -> dict:
    """Many cache-carrying calls, zero cache reads — cache_control is a no-op
    on this substrate (the exact G6 analogue of fadvise on 9p)."""
    return {
        "model:claude-opus-4-8": {"in": 90000, "out": 900, "cost": 0.5,
                                  "cache_read": 0, "cache_creation": 90000},
        "prefix": {"fp0000000002": {"calls": 40, "hits": 0, "cache_read": 0}},
    }


def _row(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


def _check(checks, name: str):
    return next(c for c in checks if c.name == name)


def _fs_snapshot(root) -> dict:
    """Path → sha256 of contents for every file under `root` (missing = {})."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                out[str(path)] = "<unreadable>"
        else:
            out[str(path)] = "<dir>"
    return out


# ==========================================================================
# C9 — optimization liveness
# ==========================================================================

def test_every_row_has_exactly_the_declared_fields():
    rows = doctor.optimization_liveness()
    assert rows, "liveness must report at least one optimization"
    for r in rows:
        assert tuple(r) == doctor.LIVENESS_FIELDS, (
            f"{r.get('name')} row fields drifted from the binding contract")


def test_every_shipped_optimization_is_covered():
    """A13: every optimization Olympus ships (or reserves) has a verdict — an
    optimization without a liveness signal is visible at design time."""
    names = {r["name"] for r in doctor.optimization_liveness()}
    assert names == {
        "prompt_caching", "context_heat", "routing_substitution",
        "context_compaction", "coalescing", "provider_mirroring",
        "speculation", "prefetching"}


def test_row_names_are_unique():
    rows = doctor.optimization_liveness()
    assert len({r["name"] for r in rows}) == len(rows)


def test_prompt_cache_no_signal_without_telemetry():
    row = _row(doctor.optimization_liveness(), "prompt_caching")
    assert row["configured"] is True
    assert row["eligible"] is False and row["activated"] is False
    assert "no cache-carrying calls" in row["inactivity_reason"]


def test_prompt_cache_active_from_synthetic_day_file():
    _write_day("2024-05-01", _active_ledger())
    row = _row(doctor.optimization_liveness(), "prompt_caching")
    assert row["eligible"] is True and row["activated"] is True
    assert row["hits"] == 8 and row["misses"] == 2
    assert row["acceptance_rate"] == pytest.approx(0.8)
    assert row["savings"] > 0
    assert row["inactivity_reason"] == ""


def test_prompt_cache_inert_from_synthetic_day_file():
    _write_day("2024-05-02", _inert_ledger())
    row = _row(doctor.optimization_liveness(), "prompt_caching")
    assert row["configured"] is True and row["eligible"] is True
    assert row["activated"] is False
    assert row["hits"] == 0 and row["misses"] == 40
    assert "no-op" in row["inactivity_reason"]


def test_inert_cache_has_negative_net_benefit():
    """Cache writes were paid for and bought nothing: overhead > savings."""
    _write_day("2024-05-02", _inert_ledger())
    row = _row(doctor.optimization_liveness(), "prompt_caching")
    assert row["overhead"] is not None and row["overhead"] > 0
    assert row["net_benefit"] is not None and row["net_benefit"] < 0


# --- W2-I9.1 --------------------------------------------------------------

def test_w2_i91_configured_but_inactive_is_warn_never_ok():
    """W2-I9.1 — the whole point of G6: an optimization that is switched on and
    provably producing nothing is reported INACTIVE, not as a success."""
    _write_day("2024-05-02", _inert_ledger())
    chk = _check(doctor._liveness_checks(), "opt:prompt_caching")
    assert chk.status == doctor.WARN
    assert chk.status != doctor.OK
    assert "INACTIVE" in chk.detail


def test_w2_i91_holds_for_every_configured_inactive_row(monkeypatch):
    """The invariant is structural, not one hard-coded row: ANY row that is
    configured + eligible + not activated must come back WARN/INACTIVE."""
    def fake_rows():
        return [doctor._row("synthetic_opt", configured=True, eligible=True,
                            activated=False, hits=0, misses=17,
                            acceptance_rate=0.0,
                            inactivity_reason="never fired")]
    monkeypatch.setattr(doctor, "optimization_liveness", fake_rows)
    chk = _check(doctor._liveness_checks(), "opt:synthetic_opt")
    assert chk.status == doctor.WARN and "INACTIVE" in chk.detail


def test_active_but_not_beneficial_is_warn(monkeypatch):
    """Active yet costing more than it saves is not a success either."""
    def fake_rows():
        return [doctor._row("synthetic_opt", configured=True, eligible=True,
                            activated=True, hits=5, misses=5,
                            acceptance_rate=0.5, savings=0.01, overhead=0.05,
                            net_benefit=-0.04)]
    monkeypatch.setattr(doctor, "optimization_liveness", fake_rows)
    chk = _check(doctor._liveness_checks(), "opt:synthetic_opt")
    assert chk.status == doctor.WARN
    assert "not beneficial" in chk.detail


def test_active_and_beneficial_is_ok():
    _write_day("2024-05-01", _active_ledger())
    chk = _check(doctor._liveness_checks(), "opt:prompt_caching")
    assert chk.status == doctor.OK and "active" in chk.detail


def test_unconfigured_optimization_is_informational_ok():
    """A Wave-3 placeholder is not a warning — it is simply not enabled."""
    chk = _check(doctor._liveness_checks(), "opt:coalescing")
    assert chk.status == doctor.OK
    assert "not enabled" in chk.detail and "Wave 3" in chk.detail


# --- absent Wave-2 modules degrade, never raise ---------------------------

def test_missing_wave2_modules_degrade_to_not_configured(monkeypatch):
    """Wave-2 modules land in parallel: an absent one must produce
    configured=False with 'not built', never an exception."""
    monkeypatch.setattr(doctor, "_optional_module", lambda name: None)
    rows = doctor.optimization_liveness()
    for name in ("context_heat", "routing_substitution"):
        row = _row(rows, name)
        assert row["configured"] is False
        assert row["inactivity_reason"] == "not built"


def test_optional_module_returns_none_for_absent_module():
    assert doctor._optional_module("definitely_not_a_module_xyz") is None


def test_exploding_capability_module_does_not_break_liveness(monkeypatch):
    """A module that imports but whose telemetry raises degrades to a reason
    string — the audit is never the thing that breaks doctor."""
    class Boom:
        @staticmethod
        def enabled():
            return True

        @staticmethod
        def liveness():
            raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(doctor, "_optional_module",
                        lambda name: Boom if name == "routesub" else None)
    row = _row(doctor.optimization_liveness(), "routing_substitution")
    assert row["configured"] is True
    assert "telemetry unreadable" in row["inactivity_reason"]


def test_capability_module_liveness_is_consumed(monkeypatch):
    class Live:
        @staticmethod
        def enabled():
            return True

        @staticmethod
        def liveness():
            return {"hits": 3, "misses": 1, "savings": 0.2, "overhead": 0.05,
                    "net_benefit": 0.15}

    monkeypatch.setattr(doctor, "_optional_module",
                        lambda name: Live if name == "routesub" else None)
    row = _row(doctor.optimization_liveness(), "routing_substitution")
    assert row["activated"] is True and row["hits"] == 3
    assert row["acceptance_rate"] == pytest.approx(0.75)
    assert row["net_benefit"] == pytest.approx(0.15)


def test_context_compaction_reads_ctxbudget_calibration(monkeypatch):
    """Verdict source for compaction is the ctxbudget calibration table."""
    from olympus import ctxbudget
    monkeypatch.setenv("OLYMPUS_CTX_BUDGET", "on")
    monkeypatch.setattr(ctxbudget, "calibration_status", lambda: {
        "enabled": True, "discarded_observations": 2,
        "pairs": {"a/b": {"label": "calibrated", "n": 30},
                  "c/d": {"label": "prior", "n": 1}}})
    row = _row(doctor.optimization_liveness(), "context_compaction")
    assert row["configured"] is True and row["activated"] is True
    assert row["hits"] == 30 and row["misses"] == 2
    assert row["acceptance_rate"] == pytest.approx(0.5)


def test_context_compaction_enabled_but_all_prior_is_inactive(monkeypatch):
    from olympus import ctxbudget
    monkeypatch.setattr(ctxbudget, "calibration_status", lambda: {
        "enabled": True, "discarded_observations": 0,
        "pairs": {"a/b": {"label": "prior", "n": 3}}})
    row = _row(doctor.optimization_liveness(), "context_compaction")
    assert row["configured"] is True and row["eligible"] is True
    assert row["activated"] is False
    chk = _check(doctor._liveness_checks(), "opt:context_compaction")
    assert chk.status == doctor.WARN and "INACTIVE" in chk.detail


# --- W2-I9.2 purity -------------------------------------------------------

def test_w2_i92_liveness_is_a_pure_read_no_network_no_model_calls(monkeypatch):
    """W2-I9.2 — liveness adds NO measurement load: no socket is opened and no
    provider entry point is invoked. Everything it needs is already on disk."""
    called: list[str] = []

    def forbidden(name):
        def _boom(*a, **k):
            called.append(name)
            raise AssertionError(f"liveness must not call {name}")
        return _boom

    monkeypatch.setattr(socket, "socket", forbidden("socket.socket"))
    monkeypatch.setattr(socket, "create_connection",
                        forbidden("socket.create_connection"))
    import http.client
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", forbidden("urlopen"))
    monkeypatch.setattr(http.client.HTTPConnection, "request",
                        forbidden("HTTPConnection.request"))
    monkeypatch.setattr(http.client.HTTPSConnection, "request",
                        forbidden("HTTPSConnection.request"))

    from olympus import llm
    for entry in ("complete", "stream_text", "client", "server_web_search",
                  "server_web_fetch"):
        monkeypatch.setattr(llm, entry, forbidden(f"llm.{entry}"))

    _write_day("2024-05-01", _active_ledger())
    rows = doctor.optimization_liveness()
    checks = doctor._liveness_checks()

    assert called == []
    assert rows and checks


def test_liveness_does_not_write_to_memory_dir():
    """A read-only audit leaves no trace of its own."""
    _write_day("2024-05-01", _active_ledger())
    before = _fs_snapshot(config.MEMORY_DIR)
    doctor.optimization_liveness()
    doctor._liveness_checks()
    assert _fs_snapshot(config.MEMORY_DIR) == before


# ==========================================================================
# C10 — configuration-skew diagnostics
# ==========================================================================

# --- W2-I10.2 registry + fingerprint --------------------------------------

def test_startup_scoped_registry_is_enumerable_and_well_formed():
    """W2-I10.2 — the startup-scoped set is declared, not folklore."""
    assert isinstance(config.STARTUP_SCOPED, tuple)
    assert config.STARTUP_SCOPED
    assert all(n.startswith("OLYMPUS_") for n in config.STARTUP_SCOPED)
    assert len(set(config.STARTUP_SCOPED)) == len(config.STARTUP_SCOPED)


def test_startup_scoped_settings_are_actually_read_at_import():
    """Every registered name must appear in config.py — a knob nobody reads
    cannot be startup-scoped, and the registry must not rot."""
    src = (config.PROJECT_ROOT / "olympus" / "config.py").read_text(
        encoding="utf-8")
    for name in config.STARTUP_SCOPED:
        assert src.count(name) >= 2, f"{name} is registered but not read"


def test_fingerprint_is_stable_for_the_same_environment():
    assert config.config_fingerprint() == config.config_fingerprint()
    assert len(config.config_fingerprint()) == 12


def test_fingerprint_is_sensitive_to_a_startup_scoped_change(monkeypatch):
    before = config.config_fingerprint()
    monkeypatch.setenv("OLYMPUS_MAX_CONCURRENT_CALLS", "99")
    assert config.config_fingerprint() != before


def test_fingerprint_ignores_a_live_read_setting(monkeypatch):
    """OLYMPUS_CONTRACTS is read live, so it is NOT startup-scoped and must not
    move the fingerprint — otherwise every runtime flip looks like stale state."""
    before = config.config_fingerprint()
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "off")
    assert config.config_fingerprint() == before


def test_boot_fingerprint_is_frozen_at_import(monkeypatch):
    before = config.boot_fingerprint()
    monkeypatch.setenv("OLYMPUS_MAX_CONCURRENT_CALLS", "99")
    assert config.boot_fingerprint() == before


def test_health_report_publishes_the_running_fingerprint():
    """W2-I10.2 — the running process surfaces its fingerprint via health."""
    rep = health.report()
    assert "config" in rep["components"]
    comp = rep["components"]["config"]
    assert comp["config_fingerprint"] == config.boot_fingerprint()
    assert comp["config_fingerprint"] in comp["detail"]
    assert comp["startup_scoped"] == list(config.STARTUP_SCOPED)
    assert comp["status"] == health.OK


def test_health_stays_ok_with_the_config_component():
    rep = health.report()
    assert "config" not in rep["down"] and "config" not in rep["degraded"]


# --- skew classes ---------------------------------------------------------

def test_skew_report_declares_every_class():
    skew = doctor.config_skew()
    for cls in doctor.SKEW_CLASSES:
        assert cls in skew and isinstance(skew[cls], list)
    assert skew["fingerprint"] == config.boot_fingerprint()
    assert skew["current_fingerprint"] == config.config_fingerprint()


def test_clean_environment_reports_no_skew_check():
    chk = _check(doctor._config_skew_checks(), "config skew")
    assert chk.status == doctor.OK and "none" in chk.detail


def test_startup_only_change_reports_restart_required(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MAX_CONCURRENT_CALLS", "512")
    findings = doctor.config_skew()["restart_required"]
    names = [f["setting"] for f in findings]
    assert "OLYMPUS_MAX_CONCURRENT_CALLS" in names
    action = next(f["action"] for f in findings
                  if f["setting"] == "OLYMPUS_MAX_CONCURRENT_CALLS")
    assert "restart required for: OLYMPUS_MAX_CONCURRENT_CALLS" in action
    chk = _check(doctor._config_skew_checks(), "config restart")
    assert chk.status == doctor.WARN


def test_unknown_setting_is_detected(monkeypatch):
    monkeypatch.setenv("OLYMPUS_TOTALY_MADE_UP_KNOB", "1")
    findings = doctor.config_skew()["unknown"]
    assert [f["setting"] for f in findings] == ["OLYMPUS_TOTALY_MADE_UP_KNOB"]
    assert "unset" in findings[0]["action"]
    assert _check(doctor._config_skew_checks(), "config unknown").status \
        == doctor.WARN


def test_a_real_knob_is_never_reported_unknown(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "on")
    unknown = {f["setting"] for f in doctor.config_skew()["unknown"]}
    assert "OLYMPUS_CONTRACTS" not in unknown


def _an_undocumented_knob() -> str:
    """A knob the code genuinely reads but .env.example does not document, and
    that no declarative registry reacts to (so the test isolates one class)."""
    reactive = set(config.RUNTIME_DEPENDENT) | set(config.DEPRECATED)
    for entry in config.CONFLICTING + config.UNSAFE_COMBINATIONS:
        reactive.update({entry["a"][0], entry["b"][0]})
    documented = set(config.documented_settings())
    for name in config.known_settings():
        if (name not in documented and name not in reactive
                and name not in config.STARTUP_SCOPED
                and name not in os.environ):
            return name
    return ""


def test_undocumented_setting_is_detected(monkeypatch):
    name = _an_undocumented_knob()
    assert name, "expected at least one undocumented knob in this build"
    monkeypatch.setenv(name, "1")
    findings = doctor.config_skew()["undocumented"]
    assert name in [f["setting"] for f in findings]
    assert ".env.example" in findings[0]["action"]


def test_documented_knob_is_not_reported_undocumented(monkeypatch):
    monkeypatch.setenv("OLYMPUS_DAILY_BUDGET", "5")
    undoc = {f["setting"] for f in doctor.config_skew()["undocumented"]}
    assert "OLYMPUS_DAILY_BUDGET" not in undoc


def test_documented_settings_absent_file_cannot_judge(monkeypatch):
    """No checkout ⇒ no .env.example ⇒ report nothing, never 'all undocumented'."""
    monkeypatch.setattr(config, "documented_settings", lambda: ())
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "on")
    assert doctor.config_skew()["undocumented"] == []


def test_deprecated_setting_is_reported_with_its_replacement(monkeypatch):
    monkeypatch.setattr(config, "DEPRECATED",
                        {"OLYMPUS_LEGACY_TEMP": "OLYMPUS_MAX_TOKENS"})
    monkeypatch.setenv("OLYMPUS_LEGACY_TEMP", "0.7")
    findings = doctor.config_skew()["deprecated"]
    assert findings and findings[0]["setting"] == "OLYMPUS_LEGACY_TEMP"
    assert "OLYMPUS_MAX_TOKENS" in findings[0]["action"]
    assert "will not migrate it for you" in findings[0]["action"]


def test_deprecated_registry_shape():
    assert isinstance(config.DEPRECATED, dict)
    for old, new in config.DEPRECATED.items():
        assert old.startswith("OLYMPUS_") and new.startswith("OLYMPUS_")


def test_conflicting_settings_are_detected(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", "1")
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "5")
    findings = doctor.config_skew()["conflicting"]
    pairs = [tuple(f["settings"]) for f in findings]
    assert ("OLYMPUS_REQUIRE_BYOK", "OLYMPUS_FREE_CHATS") in pairs
    assert _check(doctor._config_skew_checks(), "config conflict").status \
        == doctor.WARN


def test_only_one_side_of_a_conflict_is_not_a_conflict(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", "1")
    monkeypatch.delenv("OLYMPUS_FREE_CHATS", raising=False)
    pairs = [tuple(f["settings"]) for f in doctor.config_skew()["conflicting"]]
    assert ("OLYMPUS_REQUIRE_BYOK", "OLYMPUS_FREE_CHATS") not in pairs


def test_sovereign_with_a_remote_only_pool_is_a_conflict(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setattr(config, "sovereign_status", lambda: {
        "sovereign": True, "allowlist": [],
        "members": ["anthropic/claude-opus-4-8 @ api.anthropic.com"],
        "eligible_local": []})
    findings = doctor.config_skew()["conflicting"]
    assert any("remote-only" in f["detail"] for f in findings)


def test_ctx_budget_without_calibration_is_not_a_conflict(monkeypatch):
    """An uncalibrated but enabled estimator falls back to the prior — that is
    a liveness question, not a configuration conflict."""
    monkeypatch.setenv("OLYMPUS_CTX_BUDGET", "on")
    settings = {s for f in doctor.config_skew()["conflicting"]
                for s in f["settings"]}
    assert "OLYMPUS_CTX_BUDGET" not in settings


def test_setting_ignored_by_this_runtime_is_detected(monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_DEPENDENT", {
        "OLYMPUS_NEEDS_MISSING_MODULE": (
            "olympus._absolutely_not_a_module",
            "unset OLYMPUS_NEEDS_MISSING_MODULE — this build cannot honour it")})
    monkeypatch.setenv("OLYMPUS_NEEDS_MISSING_MODULE", "1")
    findings = doctor.config_skew()["ignored"]
    assert findings and findings[0]["setting"] == "OLYMPUS_NEEDS_MISSING_MODULE"
    assert "not importable" in findings[0]["detail"]
    assert _check(doctor._config_skew_checks(), "config ignored").status \
        == doctor.WARN


def test_ignored_class_is_silent_when_the_module_is_present(monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_DEPENDENT", {
        "OLYMPUS_NEEDS_PRESENT_MODULE": ("olympus.config", "unset it")})
    monkeypatch.setenv("OLYMPUS_NEEDS_PRESENT_MODULE", "1")
    assert doctor.config_skew()["ignored"] == []


def test_runtime_dependent_registry_shape():
    for name, (target, action) in config.RUNTIME_DEPENDENT.items():
        assert name.startswith("OLYMPUS_") and target and action


def test_unsafe_combination_is_flagged(monkeypatch):
    """Salvage reshapes a malformed tool call while validation is off."""
    monkeypatch.setenv("OLYMPUS_TOOL_VALIDATE", "off")
    monkeypatch.setenv("OLYMPUS_TOOL_SALVAGE", "on")
    findings = doctor.config_skew()["unsafe"]
    pairs = [tuple(f["settings"]) for f in findings]
    assert ("OLYMPUS_TOOL_VALIDATE", "OLYMPUS_TOOL_SALVAGE") in pairs
    entry = next(f for f in findings
                 if f["settings"] == ["OLYMPUS_TOOL_VALIDATE",
                                      "OLYMPUS_TOOL_SALVAGE"])
    assert "UNSAFE" in entry["detail"]
    assert "OLYMPUS_TOOL_VALIDATE=on" in entry["action"]
    assert _check(doctor._config_skew_checks(), "config unsafe").status \
        == doctor.WARN


def test_sovereign_on_the_public_dev_seed_is_unsafe(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED", "1")
    pairs = [tuple(f["settings"]) for f in doctor.config_skew()["unsafe"]]
    assert ("OLYMPUS_SOVEREIGN",
            "OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED") in pairs


def test_validate_on_with_salvage_on_is_safe(monkeypatch):
    monkeypatch.setenv("OLYMPUS_TOOL_VALIDATE", "on")
    monkeypatch.setenv("OLYMPUS_TOOL_SALVAGE", "on")
    pairs = [tuple(f["settings"]) for f in doctor.config_skew()["unsafe"]]
    assert ("OLYMPUS_TOOL_VALIDATE", "OLYMPUS_TOOL_SALVAGE") not in pairs


def test_version_skew_between_processes_is_reported():
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.version_marker_path().write_text("0.0.1-ancient", encoding="utf-8")
    findings = doctor.config_skew()["version_skew"]
    assert findings
    assert "0.0.1-ancient" in findings[0]["detail"]
    assert "restart" in findings[0]["action"]
    assert _check(doctor._config_skew_checks(), "version skew").status \
        == doctor.WARN


def test_matching_version_marker_is_not_skew():
    from olympus import __version__
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.version_marker_path().write_text(__version__, encoding="utf-8")
    assert doctor.config_skew()["version_skew"] == []


def test_absent_version_marker_is_not_skew():
    assert not config.version_marker_path().exists()
    assert doctor.config_skew()["version_skew"] == []


def test_health_records_the_version_marker():
    """The RUNNING process stamps its version; the diagnostic only reads it."""
    assert not config.version_marker_path().exists()
    health.report()
    from olympus import __version__
    assert config.read_version_marker() == __version__


def test_every_finding_names_an_operator_action(monkeypatch):
    """W2-I10.1 — reporting without an action is how skew becomes folklore."""
    _arm_every_skew_class(monkeypatch)
    skew = doctor.config_skew()
    populated = [c for c in doctor.SKEW_CLASSES if skew[c]]
    assert len(populated) >= 7, f"only armed {populated}"
    for cls in populated:
        for finding in skew[cls]:
            assert finding["action"].strip(), f"{cls} finding has no action"
            assert finding["detail"].strip()
            assert finding.get("setting") or finding.get("settings")


# --- W2-I10.1 no auto-correction ------------------------------------------

def _arm_every_skew_class(monkeypatch) -> None:
    """Craft an environment that trips every skew class at once."""
    monkeypatch.setenv("OLYMPUS_MAX_CONCURRENT_CALLS", "512")   # restart
    monkeypatch.setenv("OLYMPUS_TOTALY_MADE_UP_KNOB", "1")      # unknown
    undocumented = _an_undocumented_knob()
    if undocumented:
        monkeypatch.setenv(undocumented, "1")                   # undocumented
    monkeypatch.setattr(config, "DEPRECATED",
                        {"OLYMPUS_LEGACY_TEMP": "OLYMPUS_MAX_TOKENS"})
    monkeypatch.setenv("OLYMPUS_LEGACY_TEMP", "0.7")            # deprecated
    monkeypatch.setenv("OLYMPUS_REQUIRE_BYOK", "1")             # conflicting
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "5")
    monkeypatch.setattr(config, "RUNTIME_DEPENDENT", {
        "OLYMPUS_NEEDS_MISSING_MODULE": ("olympus._absolutely_not_a_module",
                                         "unset OLYMPUS_NEEDS_MISSING_MODULE")})
    monkeypatch.setenv("OLYMPUS_NEEDS_MISSING_MODULE", "1")     # ignored
    monkeypatch.setenv("OLYMPUS_TOOL_VALIDATE", "off")          # unsafe
    monkeypatch.setenv("OLYMPUS_TOOL_SALVAGE", "on")
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.version_marker_path().write_text("0.0.1-ancient",    # version skew
                                            encoding="utf-8")


def test_w2_i101_config_skew_never_mutates_environment_or_disk(monkeypatch):
    """W2-I10.1 — the diagnostic REPORTS. It never silently self-corrects: not
    one env var is rewritten and not one byte under MEMORY_DIR changes."""
    _arm_every_skew_class(monkeypatch)
    env_before = dict(os.environ)
    fs_before = _fs_snapshot(config.MEMORY_DIR)

    skew = doctor.config_skew()
    checks = doctor._config_skew_checks()

    assert dict(os.environ) == env_before, "config_skew() mutated the environment"
    assert _fs_snapshot(config.MEMORY_DIR) == fs_before, \
        "config_skew() wrote to MEMORY_DIR"
    assert skew and checks


def test_w2_i101_offending_settings_survive_the_diagnostic(monkeypatch):
    """The unsafe pair is still exactly as the operator set it afterwards."""
    monkeypatch.setenv("OLYMPUS_TOOL_VALIDATE", "off")
    monkeypatch.setenv("OLYMPUS_TOOL_SALVAGE", "on")
    doctor.config_skew()
    doctor._config_skew_checks()
    assert os.environ["OLYMPUS_TOOL_VALIDATE"] == "off"
    assert os.environ["OLYMPUS_TOOL_SALVAGE"] == "on"


def test_config_skew_does_not_create_the_version_marker():
    doctor.config_skew()
    assert not config.version_marker_path().exists()


# ==========================================================================
# integration — doctor stays intact
# ==========================================================================

def test_run_checks_still_returns_checks(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    checks = doctor.run_checks()
    assert isinstance(checks, list)
    assert all(isinstance(c, doctor.Check) for c in checks)
    assert all(c.status in (doctor.OK, doctor.WARN, doctor.FAIL)
               for c in checks)


def test_existing_checks_are_unaffected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    names = {c.name for c in doctor.run_checks()}
    for pre_existing in ("provider", "memory dir", "sandbox backend",
                         "command gate", "workspace", "tool-call repair",
                         "prompt cache", "provider drift"):
        assert pre_existing in names
    assert doctor.is_ready()


def test_new_sections_appear_in_run_checks(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    names = {c.name for c in doctor.run_checks()}
    assert "opt:prompt_caching" in names
    assert any(n.startswith("config ") for n in names)


def test_skew_never_blocks_readiness(monkeypatch):
    """Skew is an operator ACTION item, not an inability to serve — refusing to
    start would itself be a form of silently overriding the operator."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _arm_every_skew_class(monkeypatch)
    assert doctor.is_ready()


def test_render_includes_the_new_sections(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    out = doctor.render(with_caps=False)
    assert "opt:prompt_caching" in out
    assert "config skew" in out or "config " in out
