"""Shadow execution mode and the single side-effect boundary (Phase 5, P5-3/P5-4).

Shadow mode runs the REAL decision path — routing, planning, the dependency
graph, verification, context budgeting, admission, the recovery ladder,
journaling, tracing, replay recording — and intercepts only what leaves the
machine. A shadow run that skipped the decision path would measure nothing;
a shadow run that could send an email would not be a shadow run.

WHY ONE MODULE, NOT TWO
PHASE5_STAGING_SHADOW_SPEC.md §31 provisionally budgeted two modules
(`shadow` + `sideeffects`). Building it showed that wrong: the classification
table's only enforcement consumer is shadow mode, so two files would always
change together — the coupling criterion inverted. One module is also literally
what Step 5 asks for ("a single, testable boundary"). The unspent slot stays
reserved rather than claimed. Deviation recorded here rather than in a later
report, so the spec and the code never silently disagree.

THE ENFORCEMENT POINTS
Recon found 40 modules capable of egress but exactly TWO sites that execute a
council tool — `agent.py` (Anthropic-native loop) and `openai_compat.py`
(OpenAI-compat loop) — and both obtain their handler from
`tools.resolve_handler`. That one function is therefore the boundary, covering
both API dialects plus plugin and MCP handlers with one testable seam. Two
further points give defence in depth:

  1. `tools.resolve_handler`  — every council tool call        (primary)
  2. `actions._execute`       — the approval spine             (defence)
  3. `guard_egress()`         — an explicit call for code that leaves the
                                tool path entirely             (defence)

DEFAULT DENY
`band()` returns PROHIBITED for any name it does not recognise. A tool added
next month is refused in shadow until somebody classifies it. That is the
reject-never-repair principle applied to the action surface: the failure mode
of forgetting is a refusal, never a leak.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from . import config

# --- the five bands --------------------------------------------------------
# Ordered from least to most dangerous. See PHASE5_STAGING_SHADOW_SPEC.md §8.

READ = "read"                    # pure read; no external mutation
STAGING_WRITE = "staging_write"  # reversible write to Olympus's OWN stores
RECORDED = "recorded"            # safe-to-simulate external effect
APPROVAL = "approval"            # needs an explicit human approval boundary
PROHIBITED = "prohibited"        # irreversible or financial — never in shadow

BANDS = (READ, STAGING_WRITE, RECORDED, APPROVAL, PROHIBITED)

#: Bands whose real handler runs untouched in shadow mode.
_ALLOWED_IN_SHADOW = frozenset({READ, STAGING_WRITE})


# --- provenance (spec §10) -------------------------------------------------

SYNTHETIC = "synthetic"
REPLAY_DERIVED = "replay-derived"
SHADOW_PROVIDER = "shadow-provider"
REAL_PROVIDER_STAGING = "real-provider-staging"
REAL_CLIENT_STAGING = "real-client-staging"
REAL_USER = "real-user"

PROVENANCE_VALUES = (SYNTHETIC, REPLAY_DERIVED, SHADOW_PROVIDER,
                     REAL_PROVIDER_STAGING, REAL_CLIENT_STAGING, REAL_USER)


class ProvenanceError(ValueError):
    """An evidence record carried no provenance, or an unknown one.

    Hard failure by design (P5-A6): a record whose origin is unknown cannot be
    told apart from operational evidence later, and rule 7 forbids mixing them.
    Defaulting would be the silent-corruption path."""


def check_provenance(value: str) -> str:
    if value not in PROVENANCE_VALUES:
        raise ProvenanceError(
            f"provenance {value!r} is not one of {PROVENANCE_VALUES}. Evidence "
            f"without a declared source cannot be told from real traffic.")
    return value


def default_provenance() -> str:
    """What this process's evidence should be stamped with, absent a caller
    that knows better.

    Deliberately pessimistic: with no provider credential configured, any
    "measurement" came from a stub, so it is `shadow-provider` and must never
    be mistaken for a real-provider number."""
    if not enabled():
        return SYNTHETIC
    return REAL_PROVIDER_STAGING if _real_credential_present() else SHADOW_PROVIDER


def _real_credential_present() -> bool:
    """Whether a provider call could actually reach a provider.

    `Settings.usable()` is deliberately NOT the test: it returns True for
    provider=anthropic with no key set, because the SDK reads the key from the
    environment at call time. That is right for `usable()` and wrong here —
    stamping stub output `real-provider-staging` because a provider NAME was
    configured is exactly the over-claim this module exists to prevent."""
    from . import config as _c
    settings = _c.Settings.from_env()
    if settings.api_key:
        return True
    return any(os.environ.get(var, "").strip() for var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
        "OPENROUTER_API_KEY", "XAI_API_KEY"))


# --- mode ------------------------------------------------------------------

def enabled() -> bool:
    """OLYMPUS_SHADOW_MODE. Default OFF — every seam below is inert unless this
    is on, so a normal deployment is byte-identically unaffected."""
    return os.environ.get("OLYMPUS_SHADOW_MODE", "").strip().lower() in (
        "1", "true", "yes", "on")


def mode() -> str:
    return "shadow" if enabled() else "live"


class ShadowRefusal(RuntimeError):
    """A shadow run attempted an effect its band forbids."""

    def __init__(self, tool: str, band_name: str):
        self.tool = tool
        self.band = band_name
        super().__init__(
            f"refused in shadow mode: '{tool}' is classified {band_name}, "
            f"which may not execute without a real approval boundary")


# --- the classification table ---------------------------------------------
# Every entry is a decision about what the tool does to the world OUTSIDE
# Olympus's own stores. Sorted by band so the dangerous end is short and
# reviewable.
#
# On network READS (web_fetch, web_search, browse_page, crawl_site, …): these
# egress but do not mutate, and blocking them would make every research task
# fail — distorting the very evidence shadow mode exists to collect. They stay
# READ. SSRF protection is unchanged and independent (`security.url_block_reason`).

_CLASSIFICATION: dict[str, str] = {}


def _band_all(band_name: str, names: str) -> None:
    for n in names.split():
        _CLASSIFICATION[n] = band_name


# ---- READ: no mutation anywhere ----
_band_all(READ, """
    current_time list_dir glob_files grep_files read_file read_source_file
    list_source_files parse_document read_document list_documents
    search_documents recall_memory recall_fact search_sessions recent_learning
    list_todos read_skill gate_prompt gate_skills list_findings
    codegraph_overview codegraph_neighbors codegraph_path codegraph_subgraph
    codegraph_impact query_codegraph verify_code_claim chart_from_data
    site_profiles operator_history operator_status operator_attestations
    operator_trust
    operator_verify_receipt operator_review assess_scope assess_selfassess
    assess_deps assess_sast assess_secrets assess_validate assess_http_audit
    assess_recon browser_observe browser_read browser_read_ax browser_exists
    browser_frames browser_frame_observe browser_tabs browser_console
    browser_skills browser_pattern browser_screenshot web_monitor_list
    web_fetch web_search web_scrape web_batch_scrape web_map web_extract
    web_diff web_llms_txt browse_page crawl_site watch_youtube ask_user
""")

# ---- STAGING_WRITE: reversible, inside Olympus's own stores ----
_band_all(STAGING_WRITE, """
    add_todo complete_todo save_lesson cache_fact record_finding
    note_knowledge_gap create_skill propose_playbook propose_site_profile
    propose_upgrade site_profile_record site_template_record export_findings
    generate_llmstxt generate_benchmark web_monitor_add set_advanced_mode
    assess_import_sarif assess_propose_fix refresh_email_style
    browser_checkpoint browser_learn browser_skill_record browser_save_auth
    operator_authorize_site operator_forget_site prepare_action
""")

# ---- RECORDED: a real external effect that is safe to simulate ----
# Provider-billed generation, and writes that land in the user's workspace
# rather than an Olympus store. In shadow these record the intent and return a
# synthetic result — the council sees a plausible outcome and keeps planning,
# which is what makes the rest of the run measurable.
_band_all(RECORDED, """
    generate_image edit_image text_to_speech transcribe_audio analyze_image
    write_document update_prompt restore_prompt browser_download
    browser_save_pdf trigger_research spawn_subagent
""")

# ---- APPROVAL: must cross a human boundary; refused in shadow ----
_band_all(APPROVAL, """
    run_python run_benchmark run_code_benchmark edit_file browser_act
    browser_operate browser_frame_act browser_login browser_open
    browser_upload browser_dialog browser_switch_tab browser_restore_auth
    operator_remember_login schedule_task operator_schedule triage_inbox
    read_email read_inbox read_calendar
""")

# ---- PROHIBITED: irreversible or financial; never in shadow ----
_band_all(PROHIBITED, """
    send_email call_webhook operator_attest_receipt browser_attest_human
""")


def band(tool_name: str) -> str:
    """The band for `tool_name`. DEFAULT DENY: an unclassified tool is
    PROHIBITED, so a newly added tool cannot leak by being forgotten."""
    return _CLASSIFICATION.get(tool_name, PROHIBITED)


def classified() -> dict[str, str]:
    """A copy of the table — for the coverage gate and the reports."""
    return dict(_CLASSIFICATION)


def allowed_in_shadow(tool_name: str) -> bool:
    return band(tool_name) in _ALLOWED_IN_SHADOW


# --- the shadow sink -------------------------------------------------------
# Append-only, same discipline as every other evidence store: one JSON object
# per line, never rewritten. Content-minimised — argument VALUES are hashed,
# not stored, because a tool argument can carry anything the user typed.

_SINK_LOCK = threading.Lock()


def sink_path():
    d = config.MEMORY_DIR / "shadow"
    d.mkdir(parents=True, exist_ok=True)
    return d / "intents.jsonl"


def _fingerprint(value: Any) -> str:
    import hashlib
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except Exception:                                    # noqa: BLE001
        blob = repr(value)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def record_intent(tool: str, band_name: str, outcome: str, *,
                  params: dict | None = None,
                  provenance: str | None = None,
                  detail: str = "") -> dict:
    """Append one shadow intent. Returns the record (also when the write fails,
    so a caller can still see what was attempted).

    Never raises: a sink failure must not become the reason a shadow run
    behaves differently from a live one — that would break non-interference
    (P5-A18). The failure is captured to the errors ledger instead.

    Argument NAMES are recorded; argument VALUES are only fingerprinted. An
    operator debugging a blocked action needs to know which tool wanted to run
    with which shape of input, not the input itself."""
    rec = {
        "ts": time.time(),
        "tool": tool,
        "band": band_name,
        "outcome": outcome,                 # allowed | recorded | refused
        "provenance": check_provenance(provenance or default_provenance()),
        "arg_names": sorted(params or {}),
        "arg_fingerprint": _fingerprint(params or {}),
        "detail": detail,
    }
    try:
        line = json.dumps(rec, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False) + "\n"
        with _SINK_LOCK:
            with open(sink_path(), "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except (OSError, ProvenanceError) as err:
        from . import errors
        errors.capture("shadow.record_intent", err, context=tool)
    return rec


def read_intents(limit: int = 0) -> list[dict]:
    """Every recorded intent, oldest first. Unparseable lines are SKIPPED, not
    repaired — the sink is append-only evidence."""
    out: list[dict] = []
    path = sink_path()
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return out
    return out[-limit:] if limit else out


# --- enforcement -----------------------------------------------------------

def _synthetic_result(tool: str) -> str:
    """What a RECORDED tool returns instead of acting.

    Marked unmistakably. A synthetic result that read like a real one would
    contaminate every downstream judgement in the run."""
    return (f"[shadow] '{tool}' was not executed. Olympus is running in shadow "
            f"mode, where this tool's external effect is recorded instead of "
            f"performed. Treat this as a successful no-op and continue.")


def _refusal_result(tool: str, band_name: str) -> str:
    return (f"Error: [shadow] '{tool}' is classified '{band_name}' and may not "
            f"run in shadow mode. This is a policy refusal, not a failure — do "
            f"not retry it and do not look for another tool that achieves the "
            f"same external effect.")


def wrap_handler(tool_name: str, handler: Callable | None) -> Callable | None:
    """The boundary. Returns the handler the council may actually call.

    Inert when shadow mode is off — the SAME object is returned, so a live
    deployment's dispatch is byte-identical (asserted by an identity test).

    A refusal is returned as an error STRING rather than raised, because both
    tool loops already render an error result back to the model. Raising would
    kill the run and destroy the evidence the shadow pass exists to collect,
    and the refusal text tells the model not to route around it."""
    if not enabled() or handler is None:
        return handler

    band_name = band(tool_name)

    if band_name in _ALLOWED_IN_SHADOW:
        def _allowed(**params):
            record_intent(tool_name, band_name, "allowed", params=params)
            return handler(**params)
        return _allowed

    if band_name == RECORDED:
        def _recorded(**params):
            record_intent(tool_name, band_name, "recorded", params=params)
            return _synthetic_result(tool_name)
        return _recorded

    def _refused(**params):
        record_intent(tool_name, band_name, "refused", params=params)
        return _refusal_result(tool_name, band_name)
    return _refused


def guard_egress(what: str, *, band_name: str = PROHIBITED) -> None:
    """Explicit choke for code that leaves the tool path entirely (a heartbeat
    job, a connector, a scheduler callback). Raises in shadow mode.

    Unlike `wrap_handler` this RAISES: there is no model waiting for a rendered
    error, and a background job that silently continued past a blocked effect
    would be worse than one that stops."""
    if enabled() and band_name not in _ALLOWED_IN_SHADOW:
        record_intent(what, band_name, "refused", detail="guard_egress")
        raise ShadowRefusal(what, band_name)


def status() -> dict:
    """For /readyz and the reports."""
    intents = read_intents()
    return {
        "enabled": enabled(),
        "mode": mode(),
        "provenance": default_provenance(),
        "classified_tools": len(_CLASSIFICATION),
        "intents_recorded": len(intents),
        "refusals": sum(1 for i in intents if i.get("outcome") == "refused"),
    }
