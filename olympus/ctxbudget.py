"""Calibrated context budgeting — Wave 1 capability C4 (one arbiter module).

Today token estimation is an uncalibrated ``chars // 4`` and no budget accounts
for the output reserve, so compaction fires late (CJK ~1.5-2 chars/token) or
early (code), and an over-full prompt is discovered only by the provider. This
module owns:

- **Estimation** (`estimate_tokens`): chars / ratio, where the ratio is a
  *calibrated* chars-per-token value for the (provider, model) pair when
  observations exist, else the cold-start prior of 4.0 (the legacy divisor).
- **Calibration** (`observe`): provider-reported input token counts ratchet the
  per-pair ratio by a clamped EMA and persist to
  ``MEMORY_DIR/ctx_calibration.json`` (proclock + tmp/os.replace). Malformed
  samples are discarded and counted, never written (bounds 0.5..20 cpt).
  A corrupt calibration file is quarantined aside
  (``ctx_calibration.corrupt.<ts>.json``) and calibration restarts from the
  prior — reject-never-repair for the artifact, fresh start for telemetry-class
  data.
- **Budgeting** (`plan`): a whole-prompt accounting (system, history, task,
  tool schemas, tool-output allowance, provider overhead, retry/repair reserve,
  output reserve) against the model's context window, returning
  fit | needs_compaction | exceeded. Invariant I-C2: never "fit" when
  estimates + reserve exceed the window.

Everything is flag-gated by ``OLYMPUS_CTX_BUDGET`` (default **off**; flag off
means callers keep their exact legacy behaviour — I-C1). The window comes from
`config.context_window` — a single source, no second table.

Knobs (all documented in .env.example):
- ``OLYMPUS_CTX_BUDGET``                    master flag, default off.
- ``OLYMPUS_CTX_OUTPUT_RESERVE_FRACTION``   output reserve as a fraction of the
  window (default 0.20); the reserve is ``min(config.MAX_TOKENS, window * f)``
  so a 1M-window model doesn't reserve 200k tokens it can never emit.
- ``OLYMPUS_CTX_REPAIR_RESERVE``            extra input-token reserve for
  retry/repair round-trips (default 0 — retries re-send the same prompt, so no
  reserve is needed unless a deployment appends repair context; the knob
  exists so that deployment can account for it without a code change).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import config

# --- constants (estimator version 1) -----------------------------------------

PRIOR_CPT = 4.0            # cold-start chars-per-token (the legacy chars//4)
EMA_ALPHA = 0.2            # EMA weight of a new observation
MAX_STEP = 0.10            # per-observation ratchet clamp: cpt moves <= +-10%
CPT_MIN, CPT_MAX = 0.5, 20.0   # sanity bounds on an observed ratio
EST_VER = 1                # estimator version recorded per calibration row
PROVIDER_OVERHEAD_TOKENS = 64  # per-request wrapper overhead (role tags,
                               # message framing, stop sequences) — a small
                               # documented constant, deliberately conservative
MIN_COMPACTED_HISTORY = 256    # allowance for the compacted state block +
                               # verbatim tail after a history compaction; used
                               # only to decide needs_compaction vs exceeded

_DISCARD_LOCK = threading.Lock()
_DISCARDED = 0             # process-lifetime count of rejected observations


# --- flag + knob readers (zero-arg, runtime-flippable) ------------------------

def enabled() -> bool:
    """Master flag for calibrated context budgeting. Default OFF: callers keep
    their exact legacy behaviour (I-C1)."""
    return os.environ.get("OLYMPUS_CTX_BUDGET", "").strip().lower() in (
        "1", "true", "yes", "on")


def output_reserve_fraction() -> float:
    """Fraction of the context window reserved for the model's output."""
    try:
        f = float(os.environ.get("OLYMPUS_CTX_OUTPUT_RESERVE_FRACTION", "0.20"))
    except (TypeError, ValueError):
        return 0.20
    return min(max(f, 0.0), 0.9)


def repair_reserve() -> int:
    """Extra input tokens reserved for retry/repair round-trips (default 0)."""
    try:
        return max(0, int(os.environ.get("OLYMPUS_CTX_REPAIR_RESERVE", "0")))
    except (TypeError, ValueError):
        return 0


# --- calibration store --------------------------------------------------------

def _cal_path():
    return config.MEMORY_DIR / "ctx_calibration.json"


def _fresh() -> dict:
    return {"v": 1, "ratios": {}}


def _quarantine(path, err: BaseException) -> None:
    """Move a corrupt calibration file aside (never patched in place) and
    report. Calibration then restarts from the prior."""
    dest = path.with_name(f"ctx_calibration.corrupt.{time.time_ns()}.json")
    try:
        os.replace(path, dest)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
    from . import errors
    errors.capture("ctxbudget.calibration", err,
                   context=f"corrupt calibration quarantined to {dest.name}")


def _load() -> dict:
    """Load the calibration table; corrupt file → quarantine + fresh start."""
    path = _cal_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return _fresh()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(
                data.get("ratios"), dict):
            raise ValueError("ctx_calibration.json: unexpected schema")
    except (ValueError, json.JSONDecodeError) as err:
        _quarantine(path, err)
        return _fresh()
    return data


def _save(data: dict) -> None:
    path = _cal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def _key(provider: str, model: str) -> str:
    return f"{provider or ''}/{model or ''}"


def _row_cpt(row: Any) -> float | None:
    """A calibrated ratio from a stored row, or None if the row is unusable
    (defensive: a hand-edited file must never poison estimates)."""
    if not isinstance(row, dict):
        return None
    try:
        cpt = float(row.get("cpt"))
    except (TypeError, ValueError):
        return None
    if not (CPT_MIN <= cpt <= CPT_MAX):
        return None
    try:
        if int(row.get("n", 0)) < 1:
            return None
    except (TypeError, ValueError):
        return None
    return cpt


def _ratio_for(provider: str, model: str) -> tuple[float, str]:
    """(chars-per-token, source-label) for a pair: calibrated when observations
    exist and are sane, else the prior."""
    row = _load().get("ratios", {}).get(_key(provider, model))
    cpt = _row_cpt(row)
    if cpt is None:
        return PRIOR_CPT, "prior"
    return cpt, "calibrated"


def _discard(reason: str, context: str) -> None:
    global _DISCARDED
    with _DISCARD_LOCK:
        _DISCARDED += 1
    from . import errors
    errors.capture("ctxbudget.observe", ValueError(reason), context=context)


def observe(provider: str, model: str, prompt_chars: Any,
            reported_input_tokens: Any) -> bool:
    """Feed one provider-reported usage sample into calibration.

    Validation (poisoning guard): both counts must be genuine positive ints and
    the implied ratio must fall in [0.5, 20] chars/token — anything else is
    discarded and counted via errors.capture, never written. Accepted samples
    ratchet the pair's EMA (alpha 0.2) with the per-observation step clamped to
    +-10% of the current ratio, so no single sample (or short burst of
    poisoned ones) can yank the estimator far (I-C3 convergence).
    Returns True when the sample was accepted."""
    ctx = f"{_key(provider, model)} chars={prompt_chars!r} " \
          f"tokens={reported_input_tokens!r}"
    for v in (prompt_chars, reported_input_tokens):
        if isinstance(v, bool) or not isinstance(v, int):
            _discard("non-int usage sample", ctx)
            return False
        if v <= 0:
            _discard("non-positive usage sample", ctx)
            return False
    sample = prompt_chars / reported_input_tokens
    if not (CPT_MIN <= sample <= CPT_MAX):
        _discard(f"implausible chars-per-token {sample:.3f}", ctx)
        return False

    from . import proclock
    now = time.time()
    with proclock.lock("ctx-calibration"):
        data = _load()
        ratios = data.setdefault("ratios", {})
        row = ratios.get(_key(provider, model))
        prev = _row_cpt(row)
        if prev is None:               # first (or unusable) row: start at prior
            prev = PRIOR_CPT
            row = {"cpt": prev, "n": 0, "first": now, "last": now,
                   "est_ver": EST_VER}
        new = prev + EMA_ALPHA * (sample - prev)           # EMA pull...
        lo, hi = prev * (1 - MAX_STEP), prev * (1 + MAX_STEP)
        new = min(max(new, lo), hi)                        # ...ratchet-clamped
        new = min(max(new, CPT_MIN), CPT_MAX)
        row["cpt"] = new
        row["n"] = int(row.get("n", 0)) + 1
        row.setdefault("first", now)
        row["last"] = now
        row["est_ver"] = EST_VER
        ratios[_key(provider, model)] = row
        _save(data)
    return True


# --- estimation ---------------------------------------------------------------

def _content_chars(content: Any) -> int:
    """Character count of a message's content — str, or a list of content
    blocks (dicts with "text", or plain strings), defensively stringified
    otherwise."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, dict):
                text = block.get("text", "")
                total += len(text) if isinstance(text, str) \
                    else len(str(text))
            else:
                total += len(str(block))
        return total
    return len(str(content))


def _chars(text_or_messages: Any) -> int:
    if isinstance(text_or_messages, str):
        return len(text_or_messages)
    if isinstance(text_or_messages, list):
        total = 0
        for m in text_or_messages:
            if isinstance(m, dict):
                total += _content_chars(m.get("content", ""))
            else:
                total += len(str(m))
        return total
    return len(str(text_or_messages))


def estimate_tokens(text_or_messages: Any, *, provider: str = "",
                    model: str = "") -> int:
    """Estimated token count: chars / calibrated chars-per-token for the pair,
    or chars / 4.0 (the prior) when uncalibrated. Accepts a string or a list of
    {"role", "content"} messages (content may be a list of blocks)."""
    chars = _chars(text_or_messages)
    ratio, _ = _ratio_for(provider, model)
    return int(chars / ratio)


def window_for(provider: str, model: str) -> int:
    """The pair's context window — rides `config.context_window` (single
    source of truth; this module keeps NO second window table)."""
    return config.context_window(model)


# --- budgeting ----------------------------------------------------------------

class ContextExceeded(Exception):
    """A prompt cannot fit the context window even after history compaction.
    Carries the full block accounting so callers can refuse loudly (never
    silently truncate — I-C5)."""

    def __init__(self, message: str = "context window exceeded",
                 accounting: dict | None = None):
        super().__init__(message)
        self.accounting: dict = dict(accounting or {})


@dataclass
class BudgetPlan:
    """The arbiter's verdict for one prospective request."""
    verdict: str                       # "fit" | "needs_compaction" | "exceeded"
    accounting: dict = field(default_factory=dict)
    output_reserve: int = 0
    window: int = 0


def plan(blocks: dict[str, Any], *, provider: str = "",
         model: str = "") -> BudgetPlan:
    """Account every prompt block against the model's context window.

    `blocks` maps block names (system, history, task, tool_schemas,
    tool_output_allowance, other, ...) to either text (estimated with the
    calibrated ratio) or a pre-estimated int token count. The verdict:

    - "fit":              total input + output reserve fits the window.
    - "needs_compaction": it doesn't fit, but shrinking the "history" block to
                          a compacted floor would make it fit.
    - "exceeded":         it can't fit even with history fully compacted.

    Invariant I-C2: never "fit" when the estimated total + reserve exceeds the
    window. The output reserve is min(config.MAX_TOKENS, window * fraction)
    and never negative."""
    window = config.context_window(model)
    ratio, ratio_source = _ratio_for(provider, model)

    est: dict[str, int] = {}
    for name, val in blocks.items():
        if isinstance(val, bool):
            raise TypeError(f"block {name!r}: bool is not a token count")
        if isinstance(val, int):
            est[name] = max(0, val)
        else:
            est[name] = int(_chars(val) / ratio)

    reserve = max(0, min(config.MAX_TOKENS,
                         int(window * output_reserve_fraction())))
    repair = repair_reserve()
    overhead = PROVIDER_OVERHEAD_TOKENS + repair
    total_input = sum(est.values()) + overhead
    total = total_input + reserve

    history_est = est.get("history", 0)
    minimal_history = min(history_est, MIN_COMPACTED_HISTORY)
    compacted_total = total - history_est + minimal_history

    if total <= window:
        verdict = "fit"
    elif compacted_total <= window:
        verdict = "needs_compaction"
    else:
        verdict = "exceeded"

    accounting = {
        "blocks": est,
        "provider_overhead": PROVIDER_OVERHEAD_TOKENS,
        "repair_reserve": repair,
        "output_reserve": reserve,
        "total_input": total_input,
        "total_with_reserve": total,
        "window": window,
        "headroom": window - total,
        "cpt": ratio,
        "ratio_source": ratio_source,
        "provider": provider,
        "model": model,
        "verdict": None,               # filled below (self-describing dict)
    }
    accounting["verdict"] = verdict
    return BudgetPlan(verdict=verdict, accounting=accounting,
                      output_reserve=reserve, window=window)


# --- reporting (doctor / status) ----------------------------------------------

def calibration_status() -> dict:
    """Calibration table summary for doctor/reporting: every observed pair with
    its ratio, sample count and label, plus the prior and the process-lifetime
    discard count."""
    data = _load()
    pairs: dict[str, dict] = {}
    for key, row in data.get("ratios", {}).items():
        cpt = _row_cpt(row)
        pairs[key] = {
            "cpt": cpt if cpt is not None else PRIOR_CPT,
            "n": int(row.get("n", 0)) if isinstance(row, dict) else 0,
            "label": "calibrated" if cpt is not None else "prior",
            "first": row.get("first") if isinstance(row, dict) else None,
            "last": row.get("last") if isinstance(row, dict) else None,
        }
    return {"v": 1, "enabled": enabled(), "prior_cpt": PRIOR_CPT,
            "pairs": pairs, "discarded_observations": _DISCARDED}
