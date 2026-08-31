"""`olympus doctor` — a readiness check + capability summary.

Answers the question a new user actually has after setup: "is this thing
actually configured, and what can it do right now?" Runs a series of cheap,
offline checks (no model calls unless asked) and prints a ✓/⚠/✗ summary, then a
one-glance capability readiness view. Reused at the end of `olympus setup` so
the wizard ends by showing the same readiness picture.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from . import config

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✓", WARN: "⚠", FAIL: "✗"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str          # OK | WARN | FAIL
    detail: str

    def render(self) -> str:
        return f"  {_ICON.get(self.status, '•')} {self.name:22s} {self.detail}"


def _provider_checks() -> list[Check]:
    from . import firstrun
    out: list[Check] = []
    pool = config.ModelPool.from_env()
    primary = pool.primary()
    if firstrun.configured():
        model = primary.model or config.default_model()
        if primary.provider in ("anthropic", "openai") and not model:
            out.append(Check("provider", FAIL,
                             f"{primary.provider}: no model chosen — Olympus "
                             "doesn't assume one; run `olympus setup` or set "
                             "OLYMPUS_MODEL"))
        else:
            out.append(Check("provider", OK,
                             f"{primary.provider} / "
                             f"{model or '(tool login default)'}"))
    else:
        out.append(Check("provider", FAIL,
                         "no key/endpoint — run `olympus setup`"))
    # Pool composition
    if pool.is_multi():
        out.append(Check("model pool", OK,
                         f"{len(pool.members)} models composed "
                         "(best-of-each per role)"))
    # Credential rotation
    keys = primary.all_keys()
    if len(keys) > 1:
        out.append(Check("key rotation", OK,
                         f"{len(keys)} keys — rotates on rate-limit/quota"))
    return out


def _sandbox_checks() -> list[Check]:
    from . import cmdguard, sandbox
    out: list[Check] = []
    be = sandbox.backend()
    out.append(Check("sandbox backend", OK, f"{be}"
                     + ("  (host-side; commands need approval)"
                        if be == "local" else "  (isolated container)")))
    m = cmdguard.mode()
    status = OK if m in ("enforce", "paranoid") else WARN
    note = {"enforce": "blocks catastrophic commands (fail-closed)",
            "paranoid": "blocks catastrophic + risky commands",
            "audit": "classifies but does NOT block — audit only",
            "off": "DISABLED — commands are not screened"}.get(m, m)
    out.append(Check("command gate", status, f"{m} — {note}"))
    # Workspace writable
    try:
        wd = sandbox.workdir()
        writable = os.access(wd, os.W_OK)
        out.append(Check("workspace", OK if writable else WARN,
                         f"{wd}" + ("" if writable else "  (not writable!)")))
    except Exception as err:
        out.append(Check("workspace", WARN, f"unavailable: {str(err)[:60]}"))
    return out


def _memory_checks() -> list[Check]:
    out: list[Check] = []
    try:
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(config.MEMORY_DIR, os.W_OK)
        out.append(Check("memory dir", OK if writable else FAIL,
                         f"{config.MEMORY_DIR}"
                         + ("" if writable else "  (not writable!)")))
    except Exception as err:
        out.append(Check("memory dir", FAIL, f"{str(err)[:60]}"))
    return out


def _optional_checks() -> list[Check]:
    """Optional capabilities — WARN (not FAIL) when unconfigured; they're extras."""
    out: list[Check] = []
    media_key = (os.environ.get("OPENAI_API_KEY")
                 or os.environ.get("OLYMPUS_MEDIA_API_KEY"))
    out.append(Check("media / vision", OK if media_key else WARN,
                     "image gen, TTS, analyze_image ready" if media_key
                     else "no media key — image/vision tools will decline"))
    gateways = {
        "telegram": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "discord": os.environ.get("DISCORD_WEBHOOK_URL")
        or os.environ.get("DISCORD_PUBLIC_KEY"),
        "slack": os.environ.get("SLACK_BOT_TOKEN"),
        "signal": os.environ.get("SIGNAL_CLI_REST_URL"),
        "ntfy": os.environ.get("NTFY_TOPIC"),
    }
    live = [n for n, v in gateways.items() if v]
    out.append(Check("gateways", OK if live else WARN,
                     ", ".join(live) if live else "none connected (optional)"))
    # Web-search providers: DuckDuckGo always works keyless; report any keyed
    # or self-hosted providers the operator has configured so they're
    # discoverable.
    from . import websearch
    providers = websearch.configured()
    extra = [p for p in providers if p != "ddg"]
    out.append(Check("web search", OK,
                     (f"{', '.join(providers)} (order)" if extra
                      else "DuckDuckGo (keyless; add SearXNG/Brave/Tavily/"
                           "Serper/PSE for better results)")))
    # OS-level computer use: default-off, two-switch opt-in. Show whether it is
    # active and, if so, whether the platform toolchain is actually present.
    from . import computeruse
    if not computeruse.enabled():
        out.append(Check("computer use", WARN,
                         "off (OS control disabled; set OLYMPUS_COMPUTER_USE=1 "
                         "and OLYMPUS_COMPUTER_USE_ACTUATOR=native to enable)"))
    else:
        name = computeruse.actuator_name()
        if name == "disabled":
            out.append(Check("computer use", WARN,
                             "enabled but no actuator installed "
                             "(set OLYMPUS_COMPUTER_USE_ACTUATOR=native)"))
        else:
            ready, missing = computeruse.actuator_ready()
            out.append(Check("computer use", OK if ready else WARN,
                             f"active ({name} actuator)" if ready
                             else f"active ({name}) — missing tool(s): "
                                  f"{', '.join(missing)}"))
    return out


def _repair_checks() -> list[Check]:
    """Tool-call repair-rate decay signal (C8): a rising rung≥3 share over the
    last 7 days means the model is emitting more malformed calls — WARN above
    OLYMPUS_REPAIR_WARN_RATE (default 0.15) once there are ≥50 calls."""
    from . import toolcall_repair
    try:
        r = toolcall_repair.repair_rate(7)
    except Exception:
        return []
    n, rate = r.get("total", 0), r.get("rate", 0.0)
    detail = f"tool-call repair: {rate * 100:.1f}% over {n} calls (7d)"
    if n >= 50 and rate > toolcall_repair.repair_warn_rate():
        return [Check("tool-call repair", WARN,
                      detail + " — rising repair rate; the model may be "
                      "degrading at tool-call emission")]
    return [Check("tool-call repair", OK, detail)]


def _cache_checks() -> list[Check]:
    """Prompt-cache liveness (C5): from recorded usage telemetry, is caching
    producing cache reads at all? Inert caching (many fingerprint-carrying
    calls, zero cache reads) means the money spent on cache writes buys
    nothing — WARN with the likely causes."""
    from . import usage
    try:
        s = usage.cache_stats(7)
    except Exception:
        return []
    verdict = s.get("verdict")
    if verdict == "active":
        return [Check("prompt cache", OK,
                      f"active — {s.get('hit_rate', 0.0) * 100:.0f}% hit rate "
                      f"over {s.get('fp_calls', 0)} calls (7d), "
                      f"~${s.get('savings_usd', 0.0):.4f} saved")]
    if verdict == "inert":
        return [Check("prompt cache", WARN,
                      "prompt caching configured but producing no cache reads "
                      f"({s.get('fp_calls', 0)} calls) — check prompt prefix "
                      "stability / provider support")]
    return [Check("prompt cache", OK, "no cache telemetry yet")]


def _ledger_checks() -> list[Check]:
    """Usage-ledger health (W2-2): is compaction keeping the journal bounded?

    The ledger is append-only, and `today_spend()` -- which feeds the budget
    guard on the per-model-call path -- replays the un-compacted journal every
    call. Its cost therefore scales with journal size. Auto-compaction bounds
    that, but a compaction failing repeatedly (disk full, permissions, a wedged
    lock) leaves the journal growing with nothing visible: the bound quietly
    not existing. `record()` captures that failure, and this SURFACES it.

    Reports the active fsync mode too, because it decides how much spend an
    unclean shutdown can lose and is otherwise invisible.
    """
    from . import usage
    try:
        mode = usage._fsync_ledger()
        limit = usage.compact_at_bytes()
        day = time.strftime("%Y-%m-%d")
        journal = (config.MEMORY_DIR / "usage" / f"{day}.jsonl")
        size = journal.stat().st_size if journal.exists() else 0
    except (OSError, AttributeError):
        # Narrow ON PURPOSE. A bare  here silently returned
        # [] when this function had a NameError -- a health check that cannot
        # fail, which is the defect class this whole program keeps meeting.
        return []
    tail = {"always": "no loss on an unclean stop",
            "auto": "tail loss bounded by the OS, not by us"}.get(
                mode, f"tail loss bounded by "
                      f"{usage.fsync_interval_ms() / 1000:.0f}s")
    if size > limit * 4:
        return [Check("usage ledger", FAIL,
                      f"journal is {size // 1024} KiB against a "
                      f"{limit // 1024} KiB threshold — compaction is NOT "
                      f"keeping up, so the budget guard's per-call cost is "
                      f"growing; check errors for usage.record.autocompact")]
    if size > limit:
        return [Check("usage ledger", WARN,
                      f"journal is {size // 1024} KiB, over the "
                      f"{limit // 1024} KiB threshold — compaction is due "
                      f"and has not run")]
    return [Check("usage ledger", OK,
                  f"journal {size // 1024} KiB / {limit // 1024} KiB, "
                  f"fsync={mode} ({tail})")]


def _drift_checks() -> list[Check]:
    """Provider-drift freeze markers (C6): a member the drift gate froze because
    the pinned model regressed or started erroring. FAIL if any member is at
    severity `block` (a verify/refusal regression); WARN if any freeze/quarantine
    marker is present; OK otherwise. Markers are files — deleting one unfreezes."""
    from . import modelgate
    try:
        frozen = modelgate.frozen_members()
    except Exception:
        return [Check("provider drift", FAIL,
                      "freeze evidence is unreadable or malformed — model "
                      "qualification must remain blocked")]
    if not frozen:
        return [Check("provider drift", OK, "no frozen members")]
    blocked = [m for m, v in frozen.items() if v.get("severity") == "block"]
    summary = ", ".join(f"{m}:{v.get('severity')}" for m, v in sorted(
        frozen.items()))
    if blocked:
        return [Check("provider drift", FAIL,
                      f"{len(frozen)} frozen ({summary}) — a pinned model "
                      "regressed on verify/refusal; investigate or unfreeze")]
    return [Check("provider drift", WARN,
                  f"{len(frozen)} frozen: {summary}")]


# ── W2-C9: optimization liveness ────────────────────────────────────────────
# Gap G6 (docs/absorption/13-review-gaps.md): an optimization that is switched
# on but provably inert on this substrate must never be reported as a success.
# Colibri detects one such substrate (fadvise on 9p) by statfs magic; Olympus
# generalises the principle into a uniform observed-effect audit over every
# optimization it ships.
#
# W2-I9.2 — PURE READ. Every row below is derived from telemetry that already
# exists on disk (usage day ledgers, the ctxbudget calibration table, a
# capability module's own counters). Nothing here issues a model call, opens a
# socket, or starts a measurement. Cross-module imports are LAZY and guarded:
# a Wave-2 module that has not landed yet degrades to configured=False, never
# an exception.

#: Exact field set of an `optimization_liveness()` row (binding contract).
LIVENESS_FIELDS = ("name", "configured", "eligible", "activated", "hits",
                   "misses", "acceptance_rate", "savings", "overhead",
                   "net_benefit", "inactivity_reason")

_WAVE3 = "not implemented (Wave 3)"


def _row(name: str, *, configured: bool = False, eligible: bool = False,
         activated: bool = False, hits: int = 0, misses: int = 0,
         acceptance_rate: float | None = None, savings: float | None = None,
         overhead: float | None = None, net_benefit: float | None = None,
         inactivity_reason: str = "") -> dict:
    """One liveness row with exactly LIVENESS_FIELDS. Unmeasurable ratios and
    dollar figures are None (`no signal`) rather than 0.0 — a fabricated zero
    would read as "measured, and it's worthless"."""
    return {"name": name, "configured": configured, "eligible": eligible,
            "activated": activated, "hits": hits, "misses": misses,
            "acceptance_rate": acceptance_rate, "savings": savings,
            "overhead": overhead, "net_benefit": net_benefit,
            "inactivity_reason": inactivity_reason}


def _cache_write_overhead_usd(days: int = 7) -> float | None:
    """Estimated USD spent on cache CREATION over the recorded day ledgers —
    the cost side of prompt caching, which `usage.cache_stats` reports only as
    a token count. Pure read of the same files, priced per-model exactly as
    `usage.estimate_cost` does. None when nothing was written."""
    import json
    from . import usage
    base = config.MEMORY_DIR / "usage"
    if not base.exists():
        return None
    premium = max(0.0, usage.cache_write_mult() - 1.0)
    total, seen = 0.0, False
    # Days are enumerated from BOTH the snapshots and the journals (W2-2): a
    # day that has recorded spend but has not been compacted yet has only a
    # .jsonl, and globbing *.json alone would skip it entirely.
    days_seen = {q.stem for q in base.glob("*.json")}
    days_seen |= {q.stem for q in base.glob("*.jsonl")}
    for day in sorted(days_seen, reverse=True)[:days]:
        ledger = usage.ledger(day)
        if not isinstance(ledger, dict) or not ledger:
            continue
        for key, row in ledger.items():
            if not (key.startswith("model:") and isinstance(row, dict)):
                continue
            try:
                cc = int(row.get("cache_creation", 0) or 0)
            except (TypeError, ValueError):
                continue
            if cc <= 0:
                continue
            seen = True
            price_in, _ = usage.PRICES.get(key[len("model:"):],
                                           usage.DEFAULT_PRICE)
            total += cc * price_in * premium / 1_000_000
    return round(total, 6) if seen else None


def _liveness_prompt_caching() -> dict:
    """Verdict source: `usage.cache_stats` (active / inert / no_signal)."""
    from . import usage
    try:
        s = usage.cache_stats(7)
    except Exception as err:
        return _row("prompt_caching", configured=True,
                    inactivity_reason=f"telemetry unreadable: {str(err)[:60]}")
    verdict = s.get("verdict")
    calls = int(s.get("fp_calls", 0) or 0)
    hits = int(s.get("hits", 0) or 0)
    savings = float(s.get("savings_usd", 0.0) or 0.0)
    overhead = _cache_write_overhead_usd(7)
    net = round(savings - (overhead or 0.0), 6)
    common = dict(configured=True, hits=hits, misses=max(0, calls - hits),
                  acceptance_rate=(s.get("hit_rate") if calls else None),
                  savings=savings, overhead=overhead)
    if verdict == "active":
        return _row("prompt_caching", eligible=True, activated=True,
                    net_benefit=net, **common)
    if verdict == "inert":
        return _row("prompt_caching", eligible=True, activated=False,
                    net_benefit=net, **common,
                    inactivity_reason=(
                        f"{calls} cache-carrying calls, zero cache reads — the "
                        "provider is treating cache_control as a no-op, or the "
                        "prompt prefix is unstable (check OLYMPUS_CACHE_TTL and "
                        "prefix stability)"))
    return _row("prompt_caching", eligible=False, activated=False,
                net_benefit=None, **common,
                inactivity_reason="no cache-carrying calls recorded yet")


def _optional_module(name: str):
    """Import a Wave-2 capability module if this build has it, else None.

    LAZY and total: Wave-2 modules land in parallel PRs, so a module that is
    absent (or that raises on import) must degrade the row to
    configured=False — the audit never becomes the thing that breaks doctor."""
    try:
        import importlib
        return importlib.import_module(f".{name}", __package__)
    except Exception:
        return None


def _module_liveness(mod, row_name: str, *, no_signal: str, all_missed: str,
                     fallback=None) -> dict:
    """Shared shape for a capability module that owns its own telemetry.

    Contract with the module: an optional zero-arg `liveness()` returning
    `{hits, misses, savings, overhead, net_benefit}`. When it doesn't publish
    one, `fallback(mod)` derives the same dict from the module's own ledger.
    Both are pure reads (W2-I9.2)."""
    try:
        if hasattr(mod, "enabled") and not mod.enabled():
            return _row(row_name, inactivity_reason="disabled")
        if hasattr(mod, "liveness"):
            st = mod.liveness() or {}
        elif fallback is not None:
            st = fallback(mod) or {}
        else:
            st = {}
    except Exception as err:
        return _row(row_name, configured=True,
                    inactivity_reason=f"telemetry unreadable: {str(err)[:60]}")
    hits = int(st.get("hits", 0) or 0)
    misses = int(st.get("misses", 0) or 0)
    total = hits + misses
    note = st.get("note", "")
    return _row(row_name, configured=True, eligible=total > 0,
                activated=hits > 0, hits=hits, misses=misses,
                acceptance_rate=(round(hits / total, 4) if total else None),
                savings=st.get("savings"), overhead=st.get("overhead"),
                net_benefit=st.get("net_benefit"),
                inactivity_reason=("" if hits else
                                   ((no_signal if total == 0 else all_missed)
                                    + (f" [{note}]" if note else ""))))


def _ctxheat_fallback(mod) -> dict:
    """Derive heat liveness from the ctxheat ledger when it publishes no
    `liveness()`: a `reuse` is the pinned item actually avoiding work (the
    activation signal); a retrieval that never got reused is a miss."""
    entries = mod.entries() or {}
    reuse = sum(int(e.get("reuse", 0) or 0) for e in entries.values())
    seen = sum(int(e.get("hits", 0) or 0) for e in entries.values())
    saved = round(sum(float(e.get("avoided_cost", 0.0) or 0.0)
                      for e in entries.values()), 6)
    note = ""
    if hasattr(mod, "mode") and mod.mode() != "on":
        note = "shadow mode — records what it WOULD pin, changes nothing"
    return {"hits": reuse, "misses": max(0, seen - reuse),
            "savings": saved if entries else None,
            "overhead": None,
            "net_benefit": saved if entries else None, "note": note}


def _liveness_context_heat() -> dict:
    """Verdict source: `olympus.ctxheat` (Wave-2 C2) — LAZY, optional."""
    mod = _optional_module("ctxheat")
    if mod is None:
        return _row("context_heat", inactivity_reason="not built")
    return _module_liveness(
        mod, "context_heat",
        no_signal="enabled but no context item has been recorded yet",
        all_missed="enabled but no pinned item was ever reused",
        fallback=_ctxheat_fallback)


def _liveness_routing_substitution() -> dict:
    """Verdict source: `olympus.routesub` (Wave-2 C3) — LAZY, optional."""
    mod = _optional_module("routesub")
    if mod is None:
        return _row("routing_substitution", inactivity_reason="not built")
    return _module_liveness(
        mod, "routing_substitution",
        no_signal="enabled but no substitution candidate has been offered yet",
        all_missed="enabled but every substitution was refused by a "
                   "precondition")


def _liveness_context_compaction() -> dict:
    """Verdict source: the `ctxbudget` calibration table — the only compaction
    telemetry that exists. `enabled()` off ⇒ the estimator is the byte-identical
    chars//4 path, i.e. not configured."""
    try:
        from . import ctxbudget
        st = ctxbudget.calibration_status()
    except Exception:
        return _row("context_compaction", inactivity_reason="not built")
    if not st.get("enabled"):
        return _row("context_compaction",
                    inactivity_reason="disabled (OLYMPUS_CTX_BUDGET=off — the "
                                      "uncalibrated chars//4 estimator is in use)")
    pairs = st.get("pairs") or {}
    calibrated = [p for p in pairs.values() if p.get("label") == "calibrated"]
    hits = sum(int(p.get("n", 0) or 0) for p in calibrated)
    misses = int(st.get("discarded_observations", 0) or 0)
    if not pairs:
        return _row("context_compaction", configured=True, eligible=False,
                    misses=misses,
                    inactivity_reason="enabled but no provider/model pair has "
                                      "been observed yet — the prior is still "
                                      "in force")
    return _row("context_compaction", configured=True, eligible=True,
                activated=bool(calibrated), hits=hits, misses=misses,
                acceptance_rate=round(len(calibrated) / len(pairs), 4),
                inactivity_reason=("" if calibrated else
                                   "enabled but every observed pair is still on "
                                   "the prior — no calibrated ratio in force"))


def optimization_liveness() -> list[dict]:
    """One row per optimization Olympus ships, each with exactly
    LIVENESS_FIELDS. A PURE READ of existing telemetry (W2-I9.2): no model
    call, no network, no new measurement. Never raises — an unreadable or
    absent source degrades to configured=False with the reason."""
    rows = [
        _liveness_prompt_caching(),
        _liveness_context_heat(),
        _liveness_routing_substitution(),
        _liveness_context_compaction(),
        # Wave-3 capabilities: declared here so the audit is exhaustive and a
        # future implementation cannot ship without a liveness verdict.
        _row("coalescing", inactivity_reason=_WAVE3),
        _row("provider_mirroring", inactivity_reason=_WAVE3),
        _row("speculation", inactivity_reason=_WAVE3),
        _row("prefetching", inactivity_reason=(
            _WAVE3 + " — activation is gated on the coupling predictability "
            "report")),
    ]
    return rows


def _liveness_checks() -> list[Check]:
    """W2-I9.1 — a configured optimization that is eligible but not activated
    is reported WARN as INACTIVE, never OK. Configured + activated but with a
    measured net benefit <= 0 is WARN too ("active but not beneficial"): an
    optimization that costs more than it saves is not a success either."""
    try:
        rows = optimization_liveness()
    except Exception:
        return []
    out: list[Check] = []
    for r in rows:
        name = f"opt:{r['name']}"
        if not r["configured"]:
            out.append(Check(name, OK,
                             f"not enabled — {r['inactivity_reason']}"
                             if r["inactivity_reason"] else "not enabled"))
            continue
        if not r["activated"]:
            if r["eligible"]:
                out.append(Check(name, WARN,
                                 "INACTIVE — configured but producing no "
                                 f"effect: {r['inactivity_reason']}"))
            else:
                out.append(Check(name, OK,
                                 "configured — no liveness signal yet "
                                 f"({r['inactivity_reason']})"))
            continue
        net = r["net_benefit"]
        rate = r["acceptance_rate"]
        share = f"{rate * 100:.0f}% acceptance" if rate is not None else \
            f"{r['hits']} hit(s)"
        if net is not None and net <= 0:
            out.append(Check(name, WARN,
                             f"active but not beneficial — {share}, net "
                             f"${net:.4f} (savings ${r['savings'] or 0.0:.4f} "
                             f"vs overhead ${r['overhead'] or 0.0:.4f})"))
        else:
            gain = f", net ${net:.4f}" if net is not None else ""
            out.append(Check(name, OK, f"active — {share}{gain}"))
    return out


# ── W2-C10: configuration-skew diagnostics ──────────────────────────────────
# Gap G7. Report, never self-correct (W2-I10.1): every finding carries the
# exact operator action. The startup-scoped set is a declared registry in
# `config` (W2-I10.2), and the running process's fingerprint is published by
# `health`.

#: The skew classes this diagnostic reports, in report order.
SKEW_CLASSES = ("restart_required", "unknown", "undocumented", "deprecated",
                "conflicting", "ignored", "unsafe", "version_skew")


def config_skew() -> dict:
    """Classified configuration skew for the running process.

    Returns `{"fingerprint": running fp, "current_fingerprint": live fp,
    <class>: [finding, ...]}` where every finding is
    `{"setting"/"settings", "detail", "action"}`. PURE READ: this function
    mutates no environment variable and writes no file (W2-I10.1) — it only
    reports, and every finding names what the operator must do."""
    out: dict = {
        "fingerprint": config.boot_fingerprint(),
        "current_fingerprint": config.config_fingerprint(),
        "version": "",
        "marker_version": "",
    }
    for cls in SKEW_CLASSES:
        out[cls] = []

    # 1. startup-only settings changed since this process booted.
    try:
        for name, (was, now) in sorted(config.stale_startup_settings().items()):
            out["restart_required"].append({
                "setting": name,
                "detail": f"startup-scoped: running on "
                          f"{was or '(unset)'!r}, environment now says "
                          f"{now or '(unset)'!r}",
                "action": f"restart required for: {name} — restart the Olympus "
                          "process(es) (gateway/heartbeat/web) to pick it up; "
                          "editing it in place changes nothing",
            })
    except Exception:
        pass

    # 2/3. unknown (set but never read) and undocumented (read but not in
    # .env.example). Both derive from the generated knob sets, never a list.
    try:
        known = set(config.known_settings())
        documented = set(config.documented_settings())
        present = sorted(k for k in os.environ if k.startswith("OLYMPUS_"))
        for name in present:
            if name in known:
                continue
            out["unknown"].append({
                "setting": name,
                "detail": "set in the environment but no Olympus code reads it "
                          "(typo, or a knob from another version)",
                "action": f"unset {name}, or correct the spelling — it is "
                          "having no effect whatsoever",
            })
        if documented:                      # empty ⇒ no checkout, cannot judge
            for name in present:
                if name in known and name not in documented:
                    out["undocumented"].append({
                        "setting": name,
                        "detail": "read by the code but absent from "
                                  ".env.example",
                        "action": f"document {name} in .env.example so "
                                  "operators can discover it",
                    })
    except Exception:
        pass

    # 4. deprecated settings — reported with the replacement, never rewritten.
    try:
        for name, replacement in sorted(config.DEPRECATED.items()):
            if os.environ.get(name):
                out["deprecated"].append({
                    "setting": name,
                    "detail": f"deprecated — superseded by {replacement}",
                    "action": f"set {replacement} and unset {name}; Olympus "
                              "will not migrate it for you",
                })
    except Exception:
        pass

    # 5. conflicting settings — declared mutually-exclusive pairs, plus the one
    # composition conflict that cannot be expressed as an env pair.
    try:
        for entry in config.CONFLICTING:
            (a, ka), (b, kb) = entry["a"], entry["b"]
            if config.setting_state(a, ka) and config.setting_state(b, kb):
                out["conflicting"].append({
                    "settings": [a, b],
                    "detail": f"{a} + {b}: {entry['why']}",
                    "action": entry["action"],
                })
    except Exception:
        pass
    try:
        if config.sovereign_mode():
            st = config.sovereign_status()
            if st.get("members") and not st.get("eligible_local"):
                out["conflicting"].append({
                    "settings": ["OLYMPUS_SOVEREIGN", "OLYMPUS_MODELS"],
                    "detail": "sovereign mode is on but every configured pool "
                              "member egresses to a non-allowlisted host — the "
                              "pool is remote-only, so every request fails "
                              "closed",
                    "action": "add a local/allowlisted member (or list its host "
                              "in OLYMPUS_EGRESS_ALLOWLIST), or turn "
                              "OLYMPUS_SOVEREIGN off",
                })
    except Exception:
        pass

    # 6. settings this runtime cannot honour (module/extra absent).
    try:
        import importlib.util
        for name, (target, action) in sorted(config.RUNTIME_DEPENDENT.items()):
            if not os.environ.get(name):
                continue
            try:
                spec = importlib.util.find_spec(target)
            except (ImportError, ValueError):
                spec = None
            if spec is None:
                out["ignored"].append({
                    "setting": name,
                    "detail": f"set, but `{target}` is not importable in this "
                              "runtime — the setting is ignored",
                    "action": action,
                })
    except Exception:
        pass

    # 7. unsafe combinations.
    try:
        for entry in config.UNSAFE_COMBINATIONS:
            (a, ka), (b, kb) = entry["a"], entry["b"]
            if config.setting_state(a, ka) and config.setting_state(b, kb):
                out["unsafe"].append({
                    "settings": [a, b],
                    "detail": f"UNSAFE: {a} + {b} — {entry['why']}",
                    "action": entry["action"],
                })
    except Exception:
        pass

    # 8. version skew between processes sharing this MEMORY_DIR.
    try:
        from . import __version__
        recorded = config.read_version_marker()
        out["version"] = __version__
        out["marker_version"] = recorded
        if recorded and recorded != __version__:
            out["version_skew"].append({
                "setting": str(config.version_marker_path()),
                "detail": f"this process is {__version__} but the last process "
                          f"to run against this memory dir was {recorded}",
                "action": "processes may be running mixed versions; restart "
                          "workers (gateway/heartbeat/web) so they all run "
                          f"{__version__}",
            })
    except Exception:
        pass
    return out


def _config_skew_checks() -> list[Check]:
    """One Check per populated skew class. Reporting only — `config_skew()`
    never writes, so a green doctor run leaves the environment untouched."""
    try:
        skew = config_skew()
    except Exception:
        return []
    # Every class is WARN: skew is an operator ACTION item, not an inability to
    # run, and `is_ready()` must keep meaning "Olympus can serve". Escalation is
    # by report, not by refusing to start — refusing would be a form of silent
    # self-correction of the operator's stated configuration (W2-I10.1).
    labels = {
        "restart_required": ("config restart", WARN),
        "unsafe": ("config unsafe", WARN),
        "conflicting": ("config conflict", WARN),
        "deprecated": ("config deprecated", WARN),
        "unknown": ("config unknown", WARN),
        "undocumented": ("config undocumented", WARN),
        "ignored": ("config ignored", WARN),
        "version_skew": ("version skew", WARN),
    }
    out: list[Check] = []
    for cls in SKEW_CLASSES:
        findings = skew.get(cls) or []
        if not findings:
            continue
        name, status = labels[cls]
        first = findings[0]
        who = first.get("setting") or ", ".join(first.get("settings", []))
        more = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
        out.append(Check(name, status,
                         f"{who}{more} — {first['detail']} → {first['action']}"))
    if not out:
        out.append(Check("config skew", OK,
                         f"none (fingerprint {skew['fingerprint']})"))
    return out


def run_checks() -> list[Check]:
    checks: list[Check] = []
    checks += _provider_checks()
    checks += _memory_checks()
    checks += _sandbox_checks()
    checks += _optional_checks()
    checks += _repair_checks()
    checks += _cache_checks()
    checks += _ledger_checks()
    checks += _drift_checks()
    checks += _liveness_checks()
    checks += _config_skew_checks()
    return checks


def paths_block() -> str:
    """Where stuff lives — with editable vs hands-off labeling, so a new user
    knows what's safe to cat/edit and what the system manages itself."""
    from . import firstrun, soul
    try:
        from . import sandbox
        workspace = str(sandbox.workdir())
    except Exception:
        workspace = "(unavailable)"
    rows = [
        ("config", str(firstrun.CONFIG_ENV), "editable — keys & settings"),
        ("soul", str(soul.soul_path()), "editable — your personality directive"),
        ("memory", str(config.MEMORY_DIR), "managed — use `olympus memory`"),
        ("sessions", str(config.MEMORY_DIR / "conversations"),
         "managed — use `olympus sessions`"),
        ("workspace", workspace, "sandbox output — safe to inspect"),
    ]
    lines = ["Where stuff lives"]
    for name, path, note in rows:
        lines.append(f"  {name:9s} {path}")
        lines.append(f"            {note}")
    return "\n".join(lines)


def capability_summary() -> str:
    """A compact 'what I can do now' overview drawn from the live manifest —
    the launch capability dashboard, grouped and counted."""
    from . import capabilities
    from .specialists import SPECIALISTS
    lines = ["Capabilities"]
    try:
        m = capabilities.manifest()
        lines.append(f"  {m['agents']['count']} specialists · "
                     f"{m['tools']['count']} tools · "
                     f"{m['commands']['count']} commands")
    except Exception:
        pass
    roster = ", ".join(SPECIALISTS[k].name for k in sorted(SPECIALISTS))
    lines.append(f"  council: {roster}")
    return "\n".join(lines)


def render(checks: list[Check] | None = None, *, with_caps: bool = True) -> str:
    checks = checks if checks is not None else run_checks()
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    lines = ["OLYMPUS doctor — readiness check", ""]
    lines += [c.render() for c in checks]
    lines.append("")
    if fails:
        lines.append(f"  {_ICON[FAIL]} {fails} blocking issue(s) — "
                     "Olympus isn't ready. Fix the ✗ items above.")
    elif warns:
        lines.append(f"  {_ICON[OK]} Ready. {warns} optional item(s) "
                     "unconfigured (⚠) — extras you can add later.")
    else:
        lines.append(f"  {_ICON[OK]} All systems go.")
    if with_caps:
        lines += ["", capability_summary(), "", paths_block()]
    return "\n".join(lines)


def is_ready(checks: list[Check] | None = None) -> bool:
    checks = checks if checks is not None else run_checks()
    return not any(c.status == FAIL for c in checks)
