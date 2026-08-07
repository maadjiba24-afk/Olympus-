#!/usr/bin/env python3
"""Phase 4 Stage D — performance & cost validation harness (MEASURES ONLY).

What this file is
-----------------
A measurement harness for the overhead the absorption programme (Wave 1 + Wave 2)
added to Olympus. It runs entirely offline against fake providers and touches
nothing under ``olympus/``.

HONESTY CONTRACT
----------------
There are NO provider credentials and NO network in the environment this was
written for. Therefore:

* Everything printed in the MEASURED table below is a real, reproducible
  measurement of *Olympus's own code* (journal I/O, admission, watchdog,
  streamguard, context budgeting, replay, the tool-call ladder, storage, RSS).
* Anything that requires a real model call — request latency, TTFT, council
  completion latency, tool latency, verifier latency, queue time under real
  load, model-routing savings, prompt-cache savings, cost per task, recovery
  cost — is **UNMEASURABLE-OFFLINE** and is printed as such, with the reason and
  what would actually measure it. No fake number is ever substituted for one of
  those.

Run:  ``python scripts/perf_validation.py``          (full table)
      ``python scripts/perf_validation.py --quick``  (reduced sample sizes)
      ``python scripts/perf_validation.py --json``   (machine-readable)

The companion test (``tests/test_val_performance.py``) reuses these functions at
reduced sizes with deliberately LOOSE bounds: it is there to catch a ~10x
regression, not to police normal machine-to-machine variance.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path

try:
    import resource
except ImportError:
    resource = None

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from olympus import config  # noqa: E402


# --------------------------------------------------------------------------
# tiny stat helpers
# --------------------------------------------------------------------------

def pct(values, q: float) -> float:
    """Nearest-rank percentile (no interpolation) — stable for small samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


def summarize(ms: list[float]) -> dict:
    return {
        "n": len(ms),
        "p50": pct(ms, 0.50),
        "p90": pct(ms, 0.90),
        "p99": pct(ms, 0.99),
        "max": max(ms) if ms else 0.0,
        "mean": statistics.fmean(ms) if ms else 0.0,
    }


def dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


class MemoryMeasurementError(RuntimeError):
    """Raised when the host cannot provide an honest process-memory sample."""


def memory_backend() -> str:
    """Return the active process-memory measurement backend."""
    if os.name == "nt":
        return "windows-psapi"
    if sys.platform.startswith("linux"):
        return "unix-getrusage+/proc"
    return "unix-getrusage+ps"


def _windows_process_memory_kb() -> tuple[int, int]:
    """Return peak/current working-set memory for this process in KiB."""
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), counters.cb
    ):
        code = ctypes.get_last_error()
        message = ctypes.FormatError(code).strip()
        raise MemoryMeasurementError(
            f"GetProcessMemoryInfo failed with WinError {code}: {message}"
        )

    peak_kb = int(counters.PeakWorkingSetSize // 1024)
    current_kb = int(counters.WorkingSetSize // 1024)
    if peak_kb < 0 or current_kb < 0:
        raise MemoryMeasurementError(
            "Windows returned negative working-set values: "
            f"peak={peak_kb}, current={current_kb}"
        )
    return peak_kb, current_kb


def _unix_peak_rss_kb() -> int:
    if resource is None:
        raise MemoryMeasurementError(
            "resource.getrusage is unavailable on this non-Windows platform"
        )
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # CPython exposes bytes on macOS and KiB on Linux/other supported Unix.
    if sys.platform == "darwin":
        value //= 1024
    if value < 0:
        raise MemoryMeasurementError(
            f"resource.getrusage returned negative RSS: {value}"
        )
    return value


def _proc_current_rss_kb() -> int:
    with open("/proc/self/statm", encoding="ascii") as fh:
        fields = fh.read().split()
    if len(fields) < 2:
        raise MemoryMeasurementError("/proc/self/statm did not contain RSS")
    pages = int(fields[1])
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    if pages < 0 or page_size <= 0:
        raise MemoryMeasurementError(
            f"invalid /proc RSS sample: pages={pages}, page_size={page_size}"
        )
    return int((pages * page_size) // 1024)


def _ps_current_rss_kb() -> int:
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            check=True,
            capture_output=True,
            text=True,
        )
        value = int(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise MemoryMeasurementError(
            "current RSS is unavailable: both /proc and `ps` measurement failed"
        ) from exc
    if value < 0:
        raise MemoryMeasurementError(f"`ps` returned negative RSS: {value}")
    return value


def rss_kb() -> int:
    """Peak resident working set for this process in KiB."""
    if os.name == "nt":
        return _windows_process_memory_kb()[0]
    return _unix_peak_rss_kb()


def current_rss_kb() -> int:
    """Current resident working set for this process in KiB."""
    if os.name == "nt":
        return _windows_process_memory_kb()[1]
    try:
        return _proc_current_rss_kb()
    except (OSError, ValueError, MemoryMeasurementError):
        return _ps_current_rss_kb()


class env:
    """Context manager: set/unset env vars and restore them exactly."""

    def __init__(self, **kw):
        self.kw = {k: v for k, v in kw.items()}
        self.old: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class patched:
    """Context manager: temporarily setattr on modules/objects, then restore."""

    def __init__(self, *triples):
        self.triples = triples
        self.old: list = []

    def __enter__(self):
        for obj, name, value in self.triples:
            self.old.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        return self

    def __exit__(self, *a):
        for obj, name, value in reversed(self.old):
            setattr(obj, name, value)
        return False


_SCRATCH: list[Path] = []


def isolate(tag: str) -> Path:
    """Point MEMORY_DIR at a fresh temp dir and return it.

    The previous scratch dir is deleted as we move on, so a full harness run
    leaves at most one temp tree behind instead of ~30 (and `cleanup()` removes
    that one too).
    """
    d = Path(tempfile.mkdtemp(prefix=f"olymperf-{tag}-"))
    while _SCRATCH:
        shutil.rmtree(_SCRATCH.pop(), ignore_errors=True)
    _SCRATCH.append(d)
    config.MEMORY_DIR = d
    return d


def cleanup() -> None:
    while _SCRATCH:
        shutil.rmtree(_SCRATCH.pop(), ignore_errors=True)


def ab_compare(run_a, run_b, iters: int) -> tuple[list[float], list[float]]:
    """Interleave two timed callables A,B,A,B... so machine drift (CPU
    frequency, page cache, a noisy neighbour) hits both arms equally. A
    sequential 'all of A then all of B' comparison attributes drift to B."""
    a_lat: list[float] = []
    b_lat: list[float] = []
    run_a()                                   # warm both paths before timing
    run_b()
    for _ in range(iters):
        t0 = time.perf_counter()
        run_a()
        a_lat.append((time.perf_counter() - t0) * 1000.0)
        t0 = time.perf_counter()
        run_b()
        b_lat.append((time.perf_counter() - t0) * 1000.0)
    return a_lat, b_lat


def ab_summary(a_lat: list[float], b_lat: list[float]) -> dict:
    """Paired comparison of two interleaved arms. The PAIRED median delta
    (median of b_i - a_i) is far more robust than the difference of medians
    when the absolute times are noisy."""
    paired = [b - a for a, b in zip(a_lat, b_lat)]
    a50, b50 = pct(a_lat, 0.5), pct(b_lat, 0.5)
    delta = pct(paired, 0.5)
    # Robust noise floor: 1.4826 * MAD of the paired deltas ~ 1 sigma. A signal
    # smaller than this is not distinguishable from run-to-run jitter, and this
    # harness says so instead of reporting it as an overhead.
    noise = 1.4826 * pct([abs(p - delta) for p in paired], 0.5) if paired else 0.0
    return {"a_p50_ms": a50, "b_p50_ms": b50,
            "paired_delta_ms": delta,
            "paired_delta_p90_ms": pct(paired, 0.9),
            "noise_ms": noise,
            "significant": abs(delta) > noise,
            "overhead_pct": (delta / a50 * 100.0) if a50 else 0.0,
            "n": len(paired)}


# --------------------------------------------------------------------------
# fake provider harness (reused from tests/test_decisionlog.py's shape)
# --------------------------------------------------------------------------

def _message(text: str):
    import anthropic
    return anthropic.types.Message.model_validate({
        "id": "msg_perf", "type": "message", "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 300,
                  "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    })


class _FakeStream:
    def __init__(self, message):
        self._m = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._m


class _FakeMessages:
    def __init__(self, counter, responder):
        self.counter = counter
        self.responder = responder

    def stream(self, **params):
        self.counter[0] += 1
        return _FakeStream(self.responder(params))


def fake_client(counter, responder):
    msgs = _FakeMessages(counter, responder)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def _responder(params):
    sysblk = params["system"][0]["text"]
    return _message(json.dumps({"k": sysblk.split("-")[0]}))


_SCHEMA = {"type": "object", "properties": {"k": {"type": "string"}},
           "required": ["k"]}
_USER_TEXT = ("Analyse the deployment plan and identify every risk. " * 20)


def provider_pipeline(user_text: str = _USER_TEXT, stages: int = 3,
                      flush: bool = True, metrics_hook=None):
    """One fake-LLM 'pipeline' run through the REAL provider call path.

    route -> plan -> review, each via ``backend.complete_json`` -> ``llm.complete``
    -> the fake anthropic client. That path is where every piece of provider-side
    instrumentation lives: ``usage.slot`` (admission), ``usage.record`` (ledger +
    cost), ``llm._observe_ctx`` (ctxbudget calibration), ``streamguard.
    check_usage_evidence``, ``replaystore.put``, and the decision log.
    """
    from olympus import backend, replaystore, trace as trace_mod
    settings = config.Settings(provider="anthropic", model="claude-test")
    model = {"provider": "anthropic", "model": "claude-test", "version": None}
    tr = trace_mod.Trace("chat", "perf")
    tr.meta = {"input": user_text}
    parent = None
    for stage in ("route", "plan", "review")[:stages]:
        out = backend.complete_json(settings, f"{stage}-system",
                                    [{"role": "user", "content": user_text}],
                                    _SCHEMA, effort="medium")
        rec = tr.decision(stage, model, out, status="ok", model=model,
                          parent_record_id=parent,
                          request_hash=replaystore.last_ref(),
                          response_ref=replaystore.last_ref())
        parent = rec["record_id"]
        if metrics_hook is not None:
            metrics_hook(f"/{stage}", 200)
    if flush:
        tr.flush()
    return tr


# --------------------------------------------------------------------------
# 1. sessionlog append latency
# --------------------------------------------------------------------------

def bench_sessionlog_append(n: int = 500, fsync: str = "auto") -> dict:
    from olympus import sessionlog
    isolate(f"jappend-{fsync}")
    cid = f"perf-{fsync}-{uuid.uuid4().hex[:8]}"
    msgs = [{"role": "user", "content": "u" * 200},
            {"role": "assistant", "content": "a" * 400}]
    lat: list[float] = []
    with env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC=fsync):
        for _ in range(n):
            t0 = time.perf_counter()
            seq = sessionlog.append_turn(cid, msgs)
            lat.append((time.perf_counter() - t0) * 1000.0)
            if seq == 0:
                raise RuntimeError("journal append failed — measurement void")
    k = max(5, n // 10)
    head = statistics.fmean(lat[:k])
    tail = statistics.fmean(lat[-k:])
    # append_turn re-scans and re-verifies the WHOLE journal before each
    # append, so per-append cost rises linearly with journal depth (the series
    # as a whole is O(n^2)). Fit the slope from the first/last decile means and
    # project the per-append cost at deeper journals.
    span = max(1.0, float(n) - k)
    slope_ms_per_record = (tail - head) / span
    out = summarize(lat)
    out.update({"fsync": fsync, "first_decile_mean": head,
                "last_decile_mean": tail,
                "growth_ratio": (tail / head) if head else 0.0,
                "slope_ms_per_record": slope_ms_per_record,
                "projected_ms_at_10k": head + slope_ms_per_record * 10000,
                "projected_ms_at_50k": head + slope_ms_per_record * 50000})
    return out


def bench_sessionlog_sync(turns: int = 300, *, cache: bool = True) -> dict:
    """The LIVE per-turn journal path.

    ``memory.save_conversation`` calls ``sessionlog.sync(cid, history)`` on
    every turn — not ``append_turn``. ``sync`` used to do strictly MORE work
    than an append: re-scan and re-verify the whole journal, then ``_replay()``
    every record back into a message list and compare it to the caller's
    history before sealing the delta. That made a long-lived conversation's
    per-turn cost grow with its own depth — Stage-D defect D1.

    ``cache=False`` disables the `_cache_get` hot path and reproduces exactly
    that pre-fix behaviour, so the two arms are a controlled A/B on one machine
    rather than a comparison against a remembered number.
    """
    from olympus import memory, sessionlog
    isolate("jsync")
    memory.set_user("perf")
    cid = f"sync-{uuid.uuid4().hex[:8]}"
    history: list[dict] = []
    lat: list[float] = []
    real_cache_get = sessionlog._cache_get
    if not cache:
        sessionlog._cache_get = lambda sid, path: None
    try:
        with env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC="auto"):
            for i in range(turns):
                history.append({"role": "user",
                                "content": f"q{i} " + "u" * 200})
                history.append({"role": "assistant",
                                "content": f"a{i} " + "a" * 400})
                t0 = time.perf_counter()
                sessionlog.sync(cid, history)
                lat.append((time.perf_counter() - t0) * 1000.0)
    finally:
        sessionlog._cache_get = real_cache_get
    k = max(5, turns // 10)
    head = statistics.fmean(lat[:k])
    tail = statistics.fmean(lat[-k:])
    span = max(1.0, float(turns) - k)
    slope = (tail - head) / span
    out = summarize(lat)
    # A projection is only meaningful when the series actually grows; a flat
    # (or noise-negative) slope must not be reported as a negative latency.
    out.update({"turns": turns, "cache": cache, "first_decile_mean": head,
                "last_decile_mean": tail,
                "growth_ratio": (tail / head) if head else 0.0,
                "slope_ms_per_turn": slope,
                "projected_ms_at_1k": head + max(0.0, slope) * 1000,
                "projected_ms_at_10k": head + max(0.0, slope) * 10000})
    return out


# --------------------------------------------------------------------------
# 2. journal recovery (read_verified) at depth
# --------------------------------------------------------------------------

def _seed_journal(cid: str, records: int) -> None:
    """Build a journal of `records` sealed turn records in ONE append pass."""
    from olympus import sessionlog
    sid = sessionlog._sid(cid)
    entries = [("turn", {"messages": [{"role": "user", "content": "u" * 120},
                                      {"role": "assistant",
                                       "content": "a" * 240}]})
               for _ in range(records)]
    with sessionlog._locked(sid):
        sessionlog._append_records(sid, entries, (0, ""))    # empty-chain tail


def bench_sync_depth_scaling(depths=(100, 1500), samples: int = 25) -> dict:
    """How steeply per-turn journaling cost grows with session depth — the
    property Stage-D defect D1 was actually about.

    `bench_sessionlog_sync` fits a slope from the first/last decile of ONE
    growing run. That conflates the signal with warm-up and page-cache effects,
    and at small `turns` the noise swamps it (it produced a genuinely flaky
    assertion). This measures the thing directly instead: grow a session to a
    given depth, then time further syncs AT that depth, for two depths and both
    cache arms. The slope between the two depths is the depth-scaling
    coefficient, in microseconds of added cost per turn already in the session.

    Reported for both arms so the comparison is a controlled A/B on one machine
    rather than a ratio against a remembered constant.
    """
    from olympus import memory, sessionlog
    real_cache_get = sessionlog._cache_get
    out: dict = {"depths": list(depths), "samples": samples}

    def at(depth: int, cached: bool) -> float:
        isolate(f"depthscale-{depth}-{int(cached)}")
        memory.set_user("perf")
        sessionlog._cache_get = (real_cache_get if cached
                                 else (lambda sid, path: None))
        try:
            cid = f"depth-{depth}"
            history: list[dict] = []
            with env(OLYMPUS_SESSION_JOURNAL="on",
                     OLYMPUS_SESSION_FSYNC="auto"):
                for i in range(depth):
                    history += [{"role": "user", "content": f"q{i} " + "u" * 200},
                                {"role": "assistant",
                                 "content": f"a{i} " + "a" * 400}]
                    sessionlog.sync(cid, history)
                lat: list[float] = []
                for i in range(samples):
                    history += [{"role": "user", "content": f"x{i} " + "u" * 200},
                                {"role": "assistant",
                                 "content": f"y{i} " + "a" * 400}]
                    t0 = time.perf_counter()
                    sessionlog.sync(cid, history)
                    lat.append((time.perf_counter() - t0) * 1000.0)
            return pct(lat, 0.5)
        finally:
            sessionlog._cache_get = real_cache_get

    lo, hi = min(depths), max(depths)
    span = max(1, hi - lo)
    for arm, cached in (("on", True), ("off", False)):
        c_lo, c_hi = at(lo, cached), at(hi, cached)
        out[f"{arm}_ms_at_{lo}"] = c_lo
        out[f"{arm}_ms_at_{hi}"] = c_hi
        out[f"{arm}_us_per_turn_of_depth"] = (c_hi - c_lo) / span * 1000.0
    on_slope = out["on_us_per_turn_of_depth"]
    off_slope = out["off_us_per_turn_of_depth"]
    out["slope_reduction_x"] = (off_slope / on_slope) if on_slope > 0 else None
    out["speedup_at_max_depth"] = (out[f"off_ms_at_{hi}"] / out[f"on_ms_at_{hi}"]
                                   if out[f"on_ms_at_{hi}"] > 0 else None)
    return out


def bench_journal_recovery(sizes=(100, 1000, 5000), repeats: int = 5) -> list[dict]:
    from olympus import sessionlog
    rows = []
    with env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC="auto"):
        for size in sizes:
            isolate(f"jread-{size}")
            cid = f"recover-{size}"
            _seed_journal(cid, size)
            path = sessionlog._journal_path(sessionlog._sid(cid))
            nbytes = path.stat().st_size
            lat = []
            for _ in range(repeats):
                t0 = time.perf_counter()
                recs, status = sessionlog.read_verified(cid)
                lat.append((time.perf_counter() - t0) * 1000.0)
                if len(recs) != size or status != "ok":
                    raise RuntimeError(
                        f"recovery void: {len(recs)}/{size} status={status}")
            rows.append({"records": size, "bytes": nbytes,
                         "p50_ms": pct(lat, 0.5), "max_ms": max(lat),
                         "us_per_record": pct(lat, 0.5) * 1000.0 / size})
    return rows


# --------------------------------------------------------------------------
# 3. observability overhead on a full fake-LLM pipeline run
# --------------------------------------------------------------------------

def _obs_runner(*, usage_on: bool, ctx_on: bool, otel_on: bool,
                metrics_on: bool, guard_on: bool):
    """A (enter, run, exit) triple for one instrumentation configuration.

    ``usage``   -> usage.record: cost estimate + per-day ledger read/modify/write
                   under a cross-process lock.
    ``ctx``     -> llm._observe_ctx -> ctxbudget.observe: estimator calibration
                   (also a locked read/modify/write).
    ``otel``    -> trace.flush -> otel.export_run with a STUB poster (payload
                   build + serialize; nothing leaves the box).
    ``metrics`` -> metrics.record_response per stage.
    ``guard``   -> streamguard.check_usage_evidence (pure, no I/O).
    """
    from olympus import llm, metrics, otel, streamguard, usage
    counter = [0]
    posts = [0]

    def _stub_post(url, payload, timeout=5.0):
        json.dumps(payload)
        posts[0] += 1
        return 200

    patches = [(llm, "client",
                lambda settings=None: fake_client(counter, _responder)),
               (otel, "_post", _stub_post)]
    if not usage_on:
        patches.append((usage, "record", lambda *a, **kw: None))
    if not ctx_on:
        patches.append((llm, "_observe_ctx", lambda *a, **kw: None))
    if not guard_on:
        patches.append((streamguard, "check_usage_evidence",
                        lambda *a, **kw: None))
    hook = metrics.record_response if metrics_on else None
    otlp = "http://127.0.0.1:9/v1/traces" if otel_on else None

    ctx = env(OLYMPUS_OTLP_ENDPOINT=otlp, OLYMPUS_ADMISSION=None,
              OLYMPUS_STREAMGUARD=None, OLYMPUS_CTX_BUDGET=None)
    pat = patched(*patches)

    def enter():
        ctx.__enter__()
        pat.__enter__()

    def run():
        provider_pipeline(metrics_hook=hook)

    def leave():
        pat.__exit__()
        ctx.__exit__()

    return enter, run, leave


_ALL_OFF = dict(usage_on=False, ctx_on=False, otel_on=False,
                metrics_on=False, guard_on=False)
_ALL_ON = dict(usage_on=True, ctx_on=True, otel_on=True,
               metrics_on=True, guard_on=True)


def _timed_config(iters: int, tag: str, **flags) -> list[float]:
    isolate(f"obs-{tag}")
    enter, run, leave = _obs_runner(**flags)
    enter()
    try:
        run()                                     # warm
        lat = []
        for _ in range(iters):
            t0 = time.perf_counter()
            run()
            lat.append((time.perf_counter() - t0) * 1000.0)
        return lat
    finally:
        leave()


def bench_observability_overhead(iters: int = 20) -> dict:
    """Full fake-LLM pipeline run with ALL instrumentation off vs on, plus a
    per-component attribution (each component removed from the fully-ON
    configuration, so the delta is that component's marginal cost).

    HONESTY NOTE on the percentage: the denominator is a pipeline whose
    provider calls take ~0 ms. Under real traffic the same absolute overhead
    sits against seconds of inference time, so the PERCENTAGE here is an upper
    bound that will collapse by orders of magnitude. The absolute ms/run is the
    portable number.
    """
    def _interleaved(flags_a: dict, flags_b: dict, tag: str) -> dict:
        """Interleave two instrumentation configurations run-for-run."""
        isolate(f"obs-{tag}")
        e_a, r_a, l_a = _obs_runner(**flags_a)
        e_b, r_b, l_b = _obs_runner(**flags_b)
        a_lat: list[float] = []
        b_lat: list[float] = []
        e_a(); r_a(); l_a()                       # warm both arms
        e_b(); r_b(); l_b()
        for _ in range(iters):
            e_a()
            t0 = time.perf_counter(); r_a()
            a_lat.append((time.perf_counter() - t0) * 1000.0)
            l_a()
            e_b()
            t0 = time.perf_counter(); r_b()
            b_lat.append((time.perf_counter() - t0) * 1000.0)
            l_b()
        return ab_summary(a_lat, b_lat)

    s = _interleaved(_ALL_OFF, _ALL_ON, "ab")
    off_p50, on_p50 = s["a_p50_ms"], s["b_p50_ms"]

    # Per-component attribution: each component is removed from the fully-ON
    # configuration and the two are interleaved, so the paired median delta is
    # that component's marginal cost. Components contend on the same locks and
    # filesystem, so they do NOT sum to the total; a value at or below its own
    # noise floor is reported as not-resolvable rather than as a number.
    components = {}
    for name in ("usage", "ctx", "otel", "metrics", "guard"):
        flags = dict(_ALL_ON)
        flags[f"{name}_on"] = False
        cs = _interleaved(flags, _ALL_ON, f"no-{name}")
        components[name] = {"ms": cs["paired_delta_ms"],
                            "noise_ms": cs["noise_ms"],
                            "resolved": cs["significant"]}
    return {"off_p50_ms": off_p50, "on_p50_ms": on_p50,
            "abs_overhead_ms": s["paired_delta_ms"],
            "noise_ms": s["noise_ms"], "significant": s["significant"],
            "overhead_pct": s["overhead_pct"],
            "components_ms": components, "iters": iters}


# --------------------------------------------------------------------------
# 4. context-budget overhead
# --------------------------------------------------------------------------

def _history(turns: int = 40) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"turn {i}: " + ("context payload " * 40)}
            for i in range(turns)]


def bench_ctxbudget(calls: int = 2000) -> dict:
    from olympus import ctxbudget, orchestrator
    isolate("ctxbudget")
    hist = _history()
    blocks = {"system": "You are Olympus. " * 40,
              "history": "\n".join(str(m["content"]) for m in hist),
              "task": "Do the thing. " * 20,
              "tool_schemas": 3000, "tool_output_allowance": 4000}

    t0 = time.perf_counter()
    for _ in range(calls):
        ctxbudget.estimate_tokens(hist, provider="anthropic",
                                  model="claude-opus-4-8")
    est_us = (time.perf_counter() - t0) * 1e6 / calls

    t0 = time.perf_counter()
    for _ in range(calls):
        ctxbudget.plan(blocks, provider="anthropic", model="claude-opus-4-8")
    plan_us = (time.perf_counter() - t0) * 1e6 / calls

    # The LIVE seam: orchestrator._estimate_tokens (called by _maybe_compact
    # once per turn). Flag off == legacy chars//4; flag on == calibrated.
    seam = {}
    for label, flag in (("off", None), ("on", "1")):
        with env(OLYMPUS_CTX_BUDGET=flag):
            t0 = time.perf_counter()
            for _ in range(calls):
                orchestrator.Olympus._estimate_tokens(hist, "anthropic",
                                                      "claude-opus-4-8")
            seam[label] = (time.perf_counter() - t0) * 1e6 / calls
    return {"estimate_tokens_us": est_us, "plan_us": plan_us,
            "seam_off_us": seam["off"], "seam_on_us": seam["on"],
            "seam_delta_us": seam["on"] - seam["off"],
            "history_turns": len(hist)}


def bench_ctxbudget_pipeline(iters: int = 12) -> dict:
    """Per-TURN delta with OLYMPUS_CTX_BUDGET off vs on (interleaved).

    A turn is the real orchestrator ``_pipeline`` followed by ``_maybe_compact``
    — the latter is the flag's only live seam (it calls ``_estimate_tokens``,
    which is calibrated when the flag is on and legacy ``chars//4`` when off).
    History is sized under the compaction budget so the measurement is the
    estimator seam and not a model-backed compaction.
    """
    from olympus import trace as trace_mod
    isolate("ctxpipe")
    bot, bp = _fake_bot()
    hist = _history(40)

    def _once(flag):
        def _run():
            with env(OLYMPUS_CTX_BUDGET=flag):
                tr = trace_mod.Trace("chat", "perf-tester")
                tr.meta = {"input": "a real user message"}
                bot._pipeline("a real user message", tr)
                bot.history = list(hist)
                bot._maybe_compact()
        return _run

    with env(OLYMPUS_WATCHDOG=None), patched(bp):
        off_lat, on_lat = ab_compare(_once(None), _once("1"), iters)
    s = ab_summary(off_lat, on_lat)
    return {"off": s["a_p50_ms"], "on": s["b_p50_ms"], "iters": iters,
            "abs_overhead_ms": s["paired_delta_ms"],
            "noise_ms": s["noise_ms"], "significant": s["significant"],
            "overhead_pct": s["overhead_pct"]}


# --------------------------------------------------------------------------
# 5. streamguard overhead
# --------------------------------------------------------------------------

def bench_streamguard(deltas: int = 2000, repeats: int = 5) -> dict:
    from olympus import streamguard
    isolate("streamguard")
    chunks = [f"token{i % 97} of the streamed answer " for i in range(deltas)]
    out = {}
    for label, flag in (("off", None), ("on", "1")):
        with env(OLYMPUS_STREAMGUARD=flag):
            totals = []
            for _ in range(repeats):
                mon = streamguard.monitor(provider="anthropic",
                                          model="claude-test",
                                          max_tokens=64000)
                t0 = time.perf_counter()
                for c in chunks:
                    mon.feed(c)
                totals.append((time.perf_counter() - t0) * 1000.0)
            out[f"{label}_total_ms"] = pct(totals, 0.5)
            out[f"{label}_per_delta_us"] = pct(totals, 0.5) * 1000.0 / deltas
    out["deltas"] = deltas
    out["delta_overhead_us"] = out["on_per_delta_us"] - out["off_per_delta_us"]
    out["total_overhead_ms"] = out["on_total_ms"] - out["off_total_ms"]
    return out


# --------------------------------------------------------------------------
# 6. watchdog overhead
# --------------------------------------------------------------------------

def bench_watchdog_calls(calls: int = 5000) -> dict:
    from olympus import watchdog
    isolate("watchdog-calls")
    out = {}
    for label, flag in (("off", None), ("observe", "observe")):
        with env(OLYMPUS_WATCHDOG=flag):
            lease = watchdog.ProgressLease(f"perf-{label}", kind="run",
                                           deadline_secs=3600)
            t0 = time.perf_counter()
            for i in range(calls):
                lease.beat("plan_node_completed", spend=0.0001)
            out[f"{label}_beat_us"] = (time.perf_counter() - t0) * 1e6 / calls
            t0 = time.perf_counter()
            for _ in range(calls):
                lease.check()
            out[f"{label}_check_us"] = (time.perf_counter() - t0) * 1e6 / calls
    out["calls"] = calls
    return out


def _fake_bot(output: str = "a real specialist finding"):
    """The orchestrator run harness from tests/test_watchdog_wiring.py: the
    surrounding model stages are faked, `_pipeline`/`_run_one`/`_dispatch_dag`
    (where the watchdog lease is fed) are the REAL ones."""
    from olympus import backend, orchestrator
    bot = orchestrator.Olympus(user="perf-tester")
    bot._route = lambda msg: {
        "mode": "delegate", "direct_reply": None,
        "specialists": ["argus"], "brief": "do it",
        "needs_verification": True, "clarifying_questions": []}
    bot._plan = lambda brief, keys, **kw: [
        {"id": "s1", "specialist": "argus", "task": "t", "depends_on": []},
        {"id": "s2", "specialist": "argus", "task": "t2", "depends_on": ["s1"]}]
    bot._verify_timed = lambda brief, outs: (
        "verified findings", {"status": "pass", "unsupported_claims": [],
                              "confidence": 0.9})
    bot._review = lambda brief, verified: {
        "verdict": "approve", "feedback": "", "retry_specialists": []}
    bot._enforce_answer_verify = \
        lambda brief, assignments, outs, verified, verdict, err, tr: verified
    backend_patch = (backend, "run_agent_counted",
                     lambda *a, **kw: (output, 0))
    return bot, backend_patch


def bench_watchdog_pipeline(iters: int = 12) -> dict:
    """A full orchestrator run with OLYMPUS_WATCHDOG off vs observe.

    `off` must construct NO lease at all (the shipped default); `observe`
    constructs one, feeds it every beat and classifies it. Interleaved.
    """
    from olympus import trace as trace_mod
    isolate("wdpipe")
    bot, bp = _fake_bot()

    def _once(flag):
        def _run():
            with env(OLYMPUS_WATCHDOG=flag):
                tr = trace_mod.Trace("chat", "perf-tester")
                tr.meta = {"input": "a real user message"}
                bot._pipeline("a real user message", tr)
        return _run

    with patched(bp):
        off_lat, obs_lat = ab_compare(_once(None), _once("observe"), iters)
    s = ab_summary(off_lat, obs_lat)
    return {"off": s["a_p50_ms"], "observe": s["b_p50_ms"],
            "abs_overhead_ms": s["paired_delta_ms"],
            "noise_ms": s["noise_ms"], "significant": s["significant"],
            "overhead_pct": s["overhead_pct"], "iters": iters}


# --------------------------------------------------------------------------
# 7. admission overhead
# --------------------------------------------------------------------------

def bench_admission(calls: int = 300, threads: int = 8,
                    per_thread: int = 40) -> dict:
    from olympus import usage
    out = {}
    for label, flag in (("off", None), ("on", "1")):
        isolate(f"admission-{label}")
        with env(OLYMPUS_ADMISSION=flag,
                 OLYMPUS_ADMISSION_QUEUE_TIMEOUT="30",
                 OLYMPUS_ADMISSION_MAX_QUEUE="256"):
            usage.admission_reset()
            lat = []
            for _ in range(calls):
                t0 = time.perf_counter()
                with usage.slot():
                    pass
                lat.append((time.perf_counter() - t0) * 1000.0)
            out[f"{label}_uncontended_p50_ms"] = pct(lat, 0.5)
            out[f"{label}_uncontended_p99_ms"] = pct(lat, 0.99)

            # contended: `threads` workers hammering the same slot pool
            results: list[list[float]] = [[] for _ in range(threads)]
            barrier = threading.Barrier(threads)

            def worker(idx: int) -> None:
                barrier.wait()
                for _ in range(per_thread):
                    t0 = time.perf_counter()
                    with usage.slot(key=f"tenant{idx % 3}"):
                        pass
                    results[idx].append((time.perf_counter() - t0) * 1000.0)

            ts = [threading.Thread(target=worker, args=(i,))
                  for i in range(threads)]
            t0 = time.perf_counter()
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            wall = (time.perf_counter() - t0) * 1000.0
            flat = [v for r in results for v in r]
            out[f"{label}_contended_p50_ms"] = pct(flat, 0.5)
            out[f"{label}_contended_p99_ms"] = pct(flat, 0.99)
            out[f"{label}_contended_wall_ms"] = wall
            usage.admission_reset()
    out["threads"] = threads
    out["calls"] = calls
    out["uncontended_overhead_us"] = (out["on_uncontended_p50_ms"]
                                      - out["off_uncontended_p50_ms"]) * 1000.0
    return out


# --------------------------------------------------------------------------
# 8. replay vs record
# --------------------------------------------------------------------------

def bench_replay(iters: int = 10) -> dict:
    from olympus import llm
    isolate("replay")
    counter = [0]
    rec_lat, rep_lat = [], []
    with patched((llm, "client",
                  lambda settings=None: fake_client(counter, _responder))):
        # warm the store once so the recorded run id exists
        tr = provider_pipeline()
        run_id = tr.id
        for _ in range(iters):
            t0 = time.perf_counter()
            provider_pipeline()
            rec_lat.append((time.perf_counter() - t0) * 1000.0)
    calls_after_record = counter[0]

    def _forbidden(settings=None):
        raise AssertionError("replay must not touch the provider")

    with env(OLYMPUS_REPLAY=run_id), patched((llm, "client", _forbidden)):
        for _ in range(iters):
            t0 = time.perf_counter()
            provider_pipeline(flush=False)
            rep_lat.append((time.perf_counter() - t0) * 1000.0)
    return {"record_p50_ms": pct(rec_lat, 0.5),
            "replay_p50_ms": pct(rep_lat, 0.5),
            "speedup_x": (pct(rec_lat, 0.5) / pct(rep_lat, 0.5)
                          if pct(rep_lat, 0.5) else 0.0),
            "provider_calls_during_replay": counter[0] - calls_after_record,
            "iters": iters}


# --------------------------------------------------------------------------
# 9. tool-call ladder
# --------------------------------------------------------------------------

def bench_ladder(passes: int = 40) -> dict:
    sys.path.insert(0, str(_REPO / "tests"))
    from test_toolcall_ladder import CASES, run_ladder  # noqa: E402
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in CASES]
    isolate("ladder")
    n = 0
    t0 = time.perf_counter()
    for _ in range(passes):
        for case in cases:
            run_ladder(case["payload"], case["tools"],
                       salvage=bool(case.get("salvage")))
            n += 1
    elapsed = (time.perf_counter() - t0) * 1000.0
    return {"corpus_cases": len(cases), "calls": n,
            "per_call_ms": elapsed / n, "total_ms": elapsed}


# --------------------------------------------------------------------------
# 10. memory growth
# --------------------------------------------------------------------------

def bench_memory_growth(n: int = 1000) -> dict:
    from olympus import ctxheat, memory, sessionlog, usage
    d = isolate("memgrowth")
    memory.set_user("perf")
    import gc
    gc.collect()
    peak0, cur0 = rss_kb(), current_rss_kb()

    cid = "mem-growth"
    msgs = [{"role": "user", "content": "u" * 80},
            {"role": "assistant", "content": "a" * 160}]
    with env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC="auto"):
        t0 = time.perf_counter()
        for _ in range(n):
            sessionlog.append_turn(cid, msgs)
        journal_s = time.perf_counter() - t0
    gc.collect()
    peak1, cur1 = rss_kb(), current_rss_kb()

    with env(OLYMPUS_CTXHEAT="on"):
        t0 = time.perf_counter()
        for i in range(n):
            ctxheat.record(f"item-{i % 200}", "memory", retrieved=True,
                           provenance="recall")
        heat_s = time.perf_counter() - t0
    gc.collect()
    peak2, cur2 = rss_kb(), current_rss_kb()

    t0 = time.perf_counter()
    for i in range(n):
        usage.record("claude-test", 1200, 300, cache_read=0,
                     cache_creation=0, provider="anthropic")
    usage_s = time.perf_counter() - t0
    gc.collect()
    peak3, cur3 = rss_kb(), current_rss_kb()

    return {
        "n": n,
        "journal_peak_kb": peak1 - peak0, "journal_cur_kb": cur1 - cur0,
        "heat_peak_kb": peak2 - peak1, "heat_cur_kb": cur2 - cur1,
        "usage_peak_kb": peak3 - peak2, "usage_cur_kb": cur3 - cur2,
        "total_peak_kb": peak3 - peak0, "total_cur_kb": cur3 - cur0,
        "journal_s": journal_s, "heat_s": heat_s, "usage_s": usage_s,
        "disk_bytes": dir_bytes(d),
    }


# --------------------------------------------------------------------------
# 11. storage growth per turn
# --------------------------------------------------------------------------

def bench_storage_per_turn(turns: int = 25) -> dict:
    from olympus import llm, memory, sessionlog
    d = isolate("storage")
    memory.set_user("perf")
    counter = [0]
    msgs = [{"role": "user", "content": _USER_TEXT[:400]},
            {"role": "assistant", "content": "the answer " * 40}]
    parts: dict[str, int] = {}
    with env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC="auto"), \
            patched((llm, "client",
                     lambda settings=None: fake_client(counter, _responder))):
        for i in range(turns):
            provider_pipeline()
            sessionlog.append_turn("storage-turn", msgs)
    for name in ("sessions", "traces", "usage", "responses", "locks"):
        parts[name] = dir_bytes(d / name)
    total = dir_bytes(d)
    per_turn = total / turns
    return {"turns": turns, "total_bytes": total,
            "per_turn_bytes": per_turn,
            "parts": parts,
            "per_turn_parts": {k: v / turns for k, v in parts.items()},
            "extrapolated_10k_bytes": per_turn * 10000,
            "extrapolated_10k_mb": per_turn * 10000 / (1024 * 1024)}


# --------------------------------------------------------------------------
# 12. concurrency limits
# --------------------------------------------------------------------------

# Calls issued by EACH worker. Fixed rather than scaled by --quick: the
# median-versus-threads curve only resolves once every worker has queued on the
# ledger lock many times over, and a scaled-down run would publish a different
# curve under the same name.
CONTENTION_PER_THREAD = 50

# NO WARM-UP, BY DECISION. An earlier revision issued untimed calls before each
# level and then wiped the day ledger, which made the 1-thread baseline look
# tidy. It also deleted a real number: the first `usage.record` against a FRESH
# SCRATCH TREE costs far more than a steady-state one (creating the tree, the
# OS's first touch of a new directory, and — the first time it happens in a
# given process — the one-time proclock "fcntl unavailable" capture on Windows).
# Measured at 592-710 ms on the reference box. So the harness times it instead
# of hiding it; it lands in the 1-thread row's `max`, guarded on its own terms
# by the pinning test rather than folded into the contention percentiles.
#
# SCOPE, precisely. What this measures is a fresh-scratch-tree first touch, NOT
# necessarily the first `usage.record` of the Python process: whether the
# process is genuinely cold depends on what ran before, and under a full pytest
# session earlier tests will already have imported and exercised
# `usage.record`/`proclock`. The authoritative process-restart guarantee is
# owned by a separate fresh-interpreter test
# (test_val_performance.py::test_first_usage_record_after_process_start_is_bounded),
# which launches its own `sys.executable` against its own MEMORY_DIR.

# One model string across every level, so `estimate_cost` does identical work
# and the only variable between rows is the thread count.
_CONTENTION_MODEL = "claude-opus-4-8"

# The published operating policy (finding F1): council fan-out stays at or below
# 16 concurrent provider calls per host. The pinning test asserts
# `MAX_CONCURRENT_CALLS <= 16` so raising the cap past the bound the docs commit
# to cannot pass silently.
CONTENTION_OPERATING_BOUND = 16

# The full report's concurrency curve. The powers of two are what make the shape
# legible as a CURVE rather than three points; `MAX_CONCURRENT_CALLS` is folded
# in so the cap this process actually enforces is always one of the measured
# points. With the default cap of 6 that is 1 / 2 / 4 / 6 / 8 / 16.
REPORT_LEVELS = (1, 2, 4, 8, 16)


def contention_levels() -> tuple[int, ...]:
    """The three levels the FOCUSED CI gate measures.

    1 thread is the uncontended baseline every other level is judged against,
    `config.MAX_CONCURRENT_CALLS` is the cap this process actually enforces, and
    `CONTENTION_OPERATING_BOUND` is the fan-out ceiling finding F1 publishes.
    With the default cap that is 1 / 6 / 16.

    This is the gate's set, deliberately narrower than `report_levels()`: the
    gate has to answer "does the guarantee still hold at the three levels the
    documentation commits to", not "what is the shape of the curve".
    """
    return tuple(sorted({1, max(1, int(config.MAX_CONCURRENT_CALLS)),
                         CONTENTION_OPERATING_BOUND}))


def report_levels() -> tuple[int, ...]:
    """The full report's levels — the published curve, plus the enforced cap.

    Never a subset of the historical 1/2/4/8/16: the report's job is to show
    scaling, and dropping intermediate points would flatten a curve into a
    slope nobody can check.
    """
    return tuple(sorted(set(REPORT_LEVELS)
                        | {max(1, int(config.MAX_CONCURRENT_CALLS))}))


def _persisted_ledger_calls(root: Path) -> tuple[int, list[str]]:
    """`__all__.calls` summed over every persisted day ledger under `root`.

    Summed across days rather than read from today's file: a run that straddles
    midnight splits its calls over two ledgers, and reading one would report a
    phantom accounting loss. Read failures are RETURNED rather than swallowed —
    an unreadable ledger is itself an accounting defect, and the caller has to
    be able to say so instead of silently under-counting.
    """
    total = 0
    problems: list[str] = []
    base = root / "usage"
    if not base.exists():
        return 0, [f"no usage ledger directory at {base}"]
    for path in sorted(base.glob("*.json")):
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            problems.append(f"{path.name}: {type(err).__name__}: {err}")
            continue
        row = blob.get("__all__")
        if not isinstance(row, dict):
            problems.append(f"{path.name}: no __all__ row")
            continue
        total += int(row.get("calls", 0) or 0)
    return total, problems


def bench_usage_contention(levels=None,
                           per_thread: int = CONTENTION_PER_THREAD,
                           join_timeout: float = 120.0) -> list[dict]:
    """The scalability shape behind Stage-D finding F1.

    F1 was first reported as a percentage: observability costs +72..90% of a
    fake pipeline's wall time. That percentage is an artifact of the harness —
    the denominator contains ~0 ms of provider time, so ANY fixed cost looks
    enormous. The absolute cost (+1.5 ms/run) is not the concern.

    What IS decision-relevant is the shape of the dominant component under
    concurrency. `usage.record` runs after EVERY provider call and takes a
    cross-process lock around a read-modify-write of the day ledger, so the
    council's parallel specialist fan-out serialises there.

    CORRECTION — this docstring, and finding F1 with it, used to say "median
    cost is unaffected by concurrency; the TAIL and the throughput ceiling are
    what move". That was wrong, and the harness's own numbers always
    contradicted it. Serialising N callers behind one lock means the MEDIAN
    caller waits out roughly N-1 other holders, so the median rises about
    LINEARLY in the thread count. Measured here (Windows, per-call medians):

        threads      1        6       16
        p50     0.63 ms  3.47 ms  9.28 ms      (1.0x, 5.5x, 14.8x)

    The tail and the ceiling move too, but the median was never flat — and the
    pinning test's own bound, single-thread p50 x threads x 1.5, has always
    encoded linear growth. The guarantee is that the median stays at the
    SERIALISATION curve and no worse, not that it stays flat.

    Reports, per level: per-call latency percentiles, achieved throughput, and
    the accounting evidence — how many samples were timed and how many calls
    actually reached the persisted ledger. The ledger count is the point of the
    exercise as much as the latency is: `usage.record` swallows a lock timeout
    by design (losing a row beats re-billing the user), so contention could in
    principle buy its latency by dropping spend, and only counting the persisted
    rows would show that.

    FIRST TOUCH IS INCLUDED, NOT HIDDEN. There is no warm-up and the ledger is
    never wiped: every call this function makes is timed and every call it times
    is in the ledger it checks. So the first call at each level absorbs that
    level's fresh-scratch-tree costs — directory creation, the OS's first touch
    of a new tree, and, if this happens to be the process's first ledger write,
    the one-time proclock `fcntl` capture on Windows. Measured at 592-710 ms.
    It lands in the 1-thread row's `max`, where the pinning test guards it on
    its own terms instead of letting it masquerade as a contention tail.

    That is a fresh-SCRATCH-TREE first touch, and this function does not and
    cannot claim it is the process's first `usage.record` — a caller that
    already exercised the ledger leaves the module, the lock table and the
    proclock warning warm. The process-restart guarantee belongs to the
    fresh-interpreter test named in the module comment above, not to this row.

    Each level runs in its own scratch tree and starts every worker from a
    `threading.Barrier` — without that barrier the early threads finish their
    run before the last one is spawned and the "contended" row measures far less
    contention than it claims. The realised skew is reported (`start_skew_ms`)
    so a caller can reject a level where synchronisation did not hold. A worker
    that raises makes the whole level VOID (RuntimeError), never a fast row: the
    cheapest way to look fast under contention is to not do the work.

    Same-process threads only — this understates the real ceiling, which is
    shared across every Olympus process on the host.
    """
    from olympus import memory, usage
    levels = (contention_levels() if levels is None
              else tuple(sorted({int(t) for t in levels})))
    if not levels or levels[0] != 1:
        raise ValueError("contention levels must include an uncontended "
                         f"1-thread baseline to judge the rest against: {levels}")
    if per_thread < 1:
        raise ValueError(f"per_thread must be >= 1, got {per_thread}")

    rows: list[dict] = []
    for threads in levels:
        root = isolate(f"usage-contention-{threads}t")
        memory.set_user("perf")

        lat: list[float] = []
        begins: list[float] = []
        ends: list[float] = []
        failures: list[str] = []
        tripped: list[float] = []
        guard = threading.Lock()
        # `action` runs once, in whichever worker trips the barrier, before any
        # of them are released — so this stamp is the true start of the
        # contended window and excludes thread spawn.
        gate = threading.Barrier(
            threads, action=lambda: tripped.append(time.perf_counter()),
            timeout=join_timeout)

        def worker() -> None:
            try:
                # Threads start with a fresh contextvar context, so the user
                # namespace has to be set here or the ledger attributes the
                # worker's calls to the default namespace.
                memory.set_user("perf")
                gate.wait()
                begin = time.perf_counter()
                local = [0.0] * per_thread
                for i in range(per_thread):
                    t0 = time.perf_counter()
                    usage.record(_CONTENTION_MODEL, 100, 50)
                    local[i] = (time.perf_counter() - t0) * 1000.0
                end = time.perf_counter()
            except BaseException as exc:                        # noqa: BLE001
                gate.abort()      # never strand the other workers on the gate
                with guard:
                    failures.append(f"{type(exc).__name__}: {exc}")
                return
            with guard:
                lat.extend(local)
                begins.append(begin)
                ends.append(end)

        pool = [threading.Thread(target=worker, name=f"contend-{threads}-{i}")
                for i in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join(join_timeout)
        stuck = [t.name for t in pool if t.is_alive()]

        if failures or stuck or not tripped or len(ends) != threads:
            raise RuntimeError(
                f"bench_usage_contention: the {threads}-thread measurement is "
                f"VOID, not slow — worker errors: {failures or 'none'}; "
                f"threads still running after {join_timeout}s: "
                f"{stuck or 'none'}; start barrier tripped {len(tripped)} "
                f"time(s), expected 1; {len(ends)}/{threads} workers finished; "
                f"{len(lat)}/{threads * per_thread} samples collected")

        wall = max(ends) - tripped[0]
        ledger_calls, ledger_problems = _persisted_ledger_calls(root)
        row = summarize(lat)
        row.update({
            "threads": threads,
            "per_thread": per_thread,
            "wall_ms": wall * 1000.0,
            "calls_per_s": len(lat) / wall if wall > 0 else 0.0,
            "expected_samples": threads * per_thread,
            "expected_calls": threads * per_thread,
            "ledger_calls": ledger_calls,
            "ledger_read_errors": ledger_problems,
            "barrier_parties": threads,
            "workers_started": len(begins),
            "start_skew_ms": (max(begins) - min(begins)) * 1000.0,
        })
        rows.append(row)
    return rows


def bench_concurrency_limit(probe_max: int = 24) -> dict:
    """How many slots can be held at once before the next acquire is refused.

    With OLYMPUS_ADMISSION OFF the slot primitive BLOCKS (there is no refusal
    path at all) — the number reported there is the hard capacity, measured by
    holding slots until an acquire no longer completes within a short window.
    With OLYMPUS_ADMISSION ON and a zero queue timeout, capacity exhaustion is a
    typed AdmissionRefused, which is what we count.
    """
    from olympus import usage
    out: dict = {"config_max_concurrent_calls": config.MAX_CONCURRENT_CALLS}

    # --- admission ON: count admitted holders until a typed refusal ---------
    isolate("conc-on")
    with env(OLYMPUS_ADMISSION="1", OLYMPUS_ADMISSION_QUEUE_TIMEOUT="0",
             OLYMPUS_ADMISSION_MAX_QUEUE="0"):
        usage.admission_reset()
        held = 0
        refusal = ""
        stop = threading.Event()
        done = threading.Semaphore(0)
        errs: list[str] = []
        threads = []

        def holder():
            try:
                with usage.slot(cls="perf", priority="interactive"):
                    done.release()
                    stop.wait(20)
            except usage.AdmissionRefused as exc:
                errs.append(exc.reason)
                done.release()
            except Exception as exc:                      # noqa: BLE001
                errs.append(type(exc).__name__)
                done.release()

        for _ in range(probe_max):
            t = threading.Thread(target=holder, daemon=True)
            threads.append(t)
            t.start()
            if not done.acquire(timeout=5):
                errs.append("timeout")
                break
            if errs:
                refusal = errs[0]
                break
            held += 1
        stop.set()
        for t in threads:
            t.join(timeout=5)
        usage.admission_reset()
        out["admission_on_max_concurrent"] = held
        out["admission_on_refusal_reason"] = refusal or "none-within-probe"

    # --- admission OFF: no refusal exists; capacity is the blocking cap -----
    isolate("conc-off")
    with env(OLYMPUS_ADMISSION=None):
        held = 0
        stop = threading.Event()
        done = threading.Semaphore(0)
        threads = []
        blocked = False

        def holder2():
            with usage.slot():
                done.release()
                stop.wait(20)

        for _ in range(config.MAX_CONCURRENT_CALLS + 3):
            t = threading.Thread(target=holder2, daemon=True)
            threads.append(t)
            t.start()
            if not done.acquire(timeout=1.5):
                blocked = True
                break
            held += 1
        stop.set()
        for t in threads:
            t.join(timeout=5)
        out["admission_off_max_concurrent"] = held
        out["admission_off_behaviour"] = ("blocks (no refusal path)" if blocked
                                          else "never saturated within probe")
    return out


# --------------------------------------------------------------------------
# UNMEASURABLE register — the honest half of this report
# --------------------------------------------------------------------------

UNMEASURABLE = [
    ("request latency p50/p90/p95/p99",
     "every sample is dominated by provider inference time; there is no "
     "provider credential and no network in this environment",
     "Phase 5 shadow traffic: mirror production requests to a real provider "
     "and histogram trace `total_secs` per run kind"),
    ("time to first token (TTFT)",
     "TTFT is a property of the provider's streaming response; the fake "
     "client returns a complete message with no wire time",
     "Phase 5 shadow traffic with streaming enabled; instrument the first "
     "delta timestamp in llm.stream_text / openai_compat"),
    ("council completion latency (multi-specialist run, end to end)",
     "the wall time is the sum of real specialist model calls; with fakes it "
     "measures only Olympus's dispatch overhead",
     "Phase 5 shadow traffic across the real DAG dispatcher, bucketed by "
     "specialist count and depth"),
    ("tool latency (per tool, p50/p99)",
     "real tools reach network/filesystem/MCP endpoints that are not "
     "reachable here; the tool corpus here is synthetic",
     "Phase 5: per-tool spans already exist in the decision log — aggregate "
     "them under shadow traffic"),
    ("verifier (Aletheia) latency",
     "verification is a model call; `_verify_timed` is faked in every offline "
     "harness",
     "Phase 5 shadow traffic; the verify decision already carries its own "
     "timing field"),
    ("queue time under real load",
     "queueing only occurs when real calls hold slots for real durations; "
     "offline holders are microseconds long, so the queue never forms",
     "Phase 5: load-test against a real provider (or a latency-injecting "
     "proxy) and read usage.admission_status() queue depth over time"),
    ("model-routing savings (routesub / modelgrade)",
     "savings = price delta x accepted substitutions on real traffic; there "
     "are no measured qualification results and no real spend",
     "Phase 5: run routesub in shadow, compare the counterfactual cost of the "
     "heuristic pick vs the substituted pick over real traffic"),
    ("prompt-cache savings",
     "cache hit rates come from provider-reported cache_read tokens; the fake "
     "provider reports zeros, so any number here would be invented",
     "Phase 5: usage ledger already aggregates per-prefix hit rates — read "
     "them after real traffic with caching enabled"),
    ("cost per successful task / cost per failed task",
     "cost needs real token counts from a real provider AND a real "
     "success/failure verdict; both are synthetic offline",
     "Phase 5: join the usage ledger's per-run cost to the run's verifier "
     "verdict in the decision log"),
    ("recovery cost (retry/repair spend as a share of total)",
     "requires real failures — provider 5xx, rate limits, malformed tool "
     "calls from a real model — none of which occur offline",
     "Phase 5: toolcall_repair.repair_rate() + watchdog forensics + the usage "
     "ledger, correlated over real traffic"),
]


# --------------------------------------------------------------------------
# DRAFT service objectives — PROPOSALS ONLY
# --------------------------------------------------------------------------

DRAFT_SLOS = [
    ("Journal append (fsync=auto, <=500 records)", "p99 <= 25 ms",
     "MEASURED offline (p99 4.2 ms). The LIVE per-turn path is "
     "sessionlog.sync, not append_turn. Since the D1 fix its depth-scaling "
     "coefficient is 1.70 us/turn (was 26.84), so the bound holds to roughly "
     "3000 turns of session depth and must be re-baselined beyond that -- it "
     "is a DEPTH-QUALIFIED bound, not a flat one. append_turn itself still "
     "does a full rescan per call, but it has no production caller"),
    ("Journal append (fsync=always, <=500 records)", "p99 <= 40 ms",
     "MEASURED offline (p99 14.5 ms); storage-dependent, re-baseline on "
     "production disks"),
    ("Journal recovery (read_verified)", "<= 50 us/record; <= 1 s at 5k records",
     "MEASURED offline (17-18 us/record; 90.8 ms at 5k)"),
    ("Observability absolute cost per run", "<= 5 ms/run added",
     "MEASURED offline (+2.10 ms/run) — expressed in ms, NOT %, because the "
     "offline denominator has no inference time"),
    ("Admission overhead (uncontended slot)", "<= 1 ms added per slot",
     "MEASURED offline (+0.068 ms)"),
    ("Tool-call ladder", "<= 0.5 ms/call",
     "MEASURED offline (0.0147 ms/call over the 25-case corpus)"),
    ("Replay vs record", "replay strictly faster; 0 provider calls",
     "MEASURED offline (11.0x faster, 0 calls)"),
    ("Storage growth", "<= 10 KiB/turn; <= 100 MiB at 10k turns",
     "MEASURED offline (4.4 KiB/turn, ~43 MiB at 10k turns)"),
    ("Request latency", "p50 <= 8 s, p95 <= 30 s (chat), p95 <= 120 s (council)",
     "PROPOSAL - CANNOT BE VALIDATED UNTIL SHADOW TRAFFIC"),
    ("Time to first token", "p50 <= 2 s, p95 <= 6 s",
     "PROPOSAL - CANNOT BE VALIDATED UNTIL SHADOW TRAFFIC"),
    ("Availability (successful reply / admitted request)", ">= 99.0% monthly",
     "PROPOSAL - CANNOT BE VALIDATED UNTIL SHADOW TRAFFIC"),
    ("Verified-answer rate (verifier pass on delegate runs)", ">= 95%",
     "PROPOSAL - CANNOT BE VALIDATED UNTIL SHADOW TRAFFIC"),
    ("Admission refusal rate", "<= 1% of requests",
     "PROPOSAL - CANNOT BE VALIDATED UNTIL SHADOW TRAFFIC"),
    ("Cost per successful task", "no target proposed",
     "PROPOSAL WITHHELD - no offline basis whatsoever; setting a number now "
     "would be fabrication"),
]


ERROR_BUDGETS = [
    ("Availability 99.0%/month", "7h 18m of failed replies per 30 days",
     "PROPOSAL — needs shadow traffic to size"),
    ("Latency p95 <= 30 s (chat)", "5% of chat requests may exceed 30 s",
     "PROPOSAL — needs shadow traffic to size"),
    ("Verified-answer rate 95%", "5% of delegate runs may fail verification",
     "PROPOSAL — needs shadow traffic to size"),
    ("Admission refusal <= 1%", "1% of requests may be refused (refusal is a "
     "SAFE outcome — it is budgeted, not an error)",
     "PROPOSAL — needs shadow traffic to size"),
]



# --------------------------------------------------------------------------
# FINDINGS — what the measurements actually revealed
# --------------------------------------------------------------------------

# NOTE: the numbers quoted inside FINDINGS are from the REFERENCE run recorded
# when this harness was written. They are illustrative of magnitude; the table
# printed above is always this run's authoritative data. The findings' CAUSE and
# IMPACT text is structural and does not depend on the exact figures.
FINDINGS = [
    ("D1", "DEFECT -- FIXED (olympus/sessionlog.py `_cache_get`)",
     "Sealed-journal write path was O(journal depth) on EVERY turn",
     "append_turn p50 rises 2.11 ms (first decile) -> 10.59 ms (last decile) "
     "over 500 appends (5.0x, slope ~19 us/record); p99 13.5 ms at 500 "
     "records. Projected ~190 ms/append at 10k records and ~950 ms at 50k. "
     "sessionlog.sync -- the path memory.save_conversation actually calls on "
     "every turn -- does the same full rescan PLUS a complete _replay() "
     "reconstruction of the history.",
     "_append_records()/sync() call _scan(), which re-reads, re-parses and "
     "re-sha256s every record in the file before each append. The sanctioned "
     "mitigation, sessionlog.compact(), has ZERO callers anywhere in olympus/ "
     "(only tests call it) -- no cadence, no size trigger, nothing in "
     "heartbeat. The journal cap is 64 MB (~95k records at ~670 B/record).",
     "Wave 1's published 'append p50 3.0 ms, inside the 5 ms gate' held only "
     "for a SHALLOW journal; at 500 turns p50 was already ~6 ms, outside it. "
     "FIXED (partially -- see the residual, which is the honest headline). "
     "sessionlog.sync now memoizes the verified replay and chain tail and "
     "reuses it when the journal is provably untouched -- the stat tuple "
     "(inode, device, size, mtime_ns) must match byte-for-byte AND the last "
     "line's seal must still equal the recorded tail sha, both checked under "
     "the session lock. Any miss falls through to the full _scan. Every READ "
     "path (read_verified, recover_history, journal_status, compact) still "
     "verifies the whole chain unconditionally, and the cache is per-process, "
     "so a fresh process re-verifies from scratch. "
     "MEASURED (medians of syncs taken AT depth, both arms on one machine): "
     "depth 100 -> 1.68 ms cached / 4.36 ms uncached; 500 -> 1.73 / 14.56; "
     "1500 -> 2.53 / 38.31; 3000 -> 6.61 / 82.20. Depth-scaling coefficient "
     "1.70 us/turn cached vs 26.84 us/turn uncached -- a 15.8x REDUCTION. "
     "RESIDUAL 1 (corrects an earlier overstatement): the path is NOT flat in "
     "depth. An O(history length) term remains -- sync must still compare the "
     "caller's history against the replayed prefix to tell an extension from a "
     "rewrite, and _cache_put takes a defensive copy so a caller mutating a "
     "message in place cannot silently de-sync the cache. Both are inherent to "
     "the contract. The term is invisible below ~1000 turns and reaches ~6.6 ms "
     "at 3000. RESIDUAL 2: sessionlog.compact() still has zero callers in "
     "olympus/, so journal SIZE remains bounded only by the 64 MB cap. "
     "Pinned by tests/test_sessionlog_cache.py (13 correctness tests) and "
     "test_val_performance.py::"
     "test_sync_depth_scaling_is_sharply_reduced_but_not_eliminated."),
    ("F1", "FINDING -- RE-CHARACTERISED, ACCEPTED WITH A MEASURED BOUND",
     "Observability roughly DOUBLES fake-pipeline wall time (+1.5 to +2.1 ms/run)",
     "Interleaved A/B over 20 iterations: OFF 1.61 ms -> ON 3.64 ms, paired "
     "delta +1.99 ms/run against a noise floor of +/-0.08 ms, so the signal is "
     "real. Interleaved per-component attribution: usage.record +0.93 ms/run "
     "and ctxbudget.observe +0.75 ms/run account for ~85% of it; the otel stub "
     "(+0.16), metrics (+0.03) and streamguard usage-evidence (+0.01) are all "
     "AT OR BELOW their own noise floors and are reported as not resolvable "
     "rather than as numbers.",
     "The two resolvable components each take a CROSS-PROCESS file lock and do "
     "a read-modify-write of a whole JSON file on EVERY provider call: "
     "usage.record on the day ledger (proclock 'usage-ledger'), "
     "ctxbudget.observe on the calibration table (proclock 'ctx-calibration'). "
     "That is 2 flocks + 2 full-file rewrites per model call. The pure/no-I/O "
     "instrumentation (otel payload build, metrics counter, streamguard "
     "usage evidence) is not even measurable above noise.",
     "RE-CHARACTERISED. The PERCENTAGE is an artifact of the harness: the "
     "denominator is a pipeline with ~0 ms of provider time, so any fixed cost "
     "looks enormous. Against a real model call (seconds) +2 ms is <0.1% -- "
     "not a user-visible latency regression, and the >20% threshold that "
     "raised this finding does not apply to a zero-latency denominator. "
     "The concurrency concern IS real and is now MEASURED rather than "
     "asserted (harness row 12, bench_usage_contention). "
     "CORRECTION: this entry used to read 'p50 flat at ~0.20 ms ... median "
     "cost does not degrade; the TAIL and the ceiling do'. That was wrong. "
     "Serialising N callers behind one lock makes the MEDIAN caller wait out "
     "roughly N-1 other holders, so the median rises about LINEARLY in the "
     "thread count -- measured at 1 / 6 (MAX_CONCURRENT_CALLS) / 16 threads: "
     "p50 0.63 -> 3.47 -> 9.28 ms, i.e. 5.5x and 14.8x the uncontended "
     "median. The tail and the ceiling move as well (p99 into the tens of ms, "
     "throughput plateauing ~1300-1700 calls/s), but 'the median is flat' was "
     "never true and should never have been published: the pinning test's own "
     "bound -- single-thread p50 x threads x 1.5 -- has always encoded linear "
     "growth, so the claim contradicted the assertion guarding it. What is "
     "guaranteed is that the median tracks the SERIALISATION curve and no "
     "worse, that the tail stays far inside a provider call, and that no call "
     "is lost from the ledger while doing so. "
     "ACCEPTED, NOT FIXED, and deliberately so: the obvious fix -- batching or "
     "deferring ledger writes -- would make spend non-durable between "
     "flushes, and the spend cap is a governance SAFETY property. Trading a "
     "correct cap for tail latency is the wrong trade. The alternative "
     "(per-writer shard files summed on read) changes the ledger's on-disk "
     "format, which every reader and CLI report depends on; that is a "
     "designed change, not Phase-4 triage. "
     "OPERATING BOUND for canary/production: council fan-out must stay at or "
     "below 16 concurrent provider calls per host, where accounting costs a "
     "~9 ms median and a tail in the tens of ms against multi-second provider "
     "calls. Above that the ledger lock, not the provider, becomes the "
     "limiter -- and because the median grows linearly, it degrades for EVERY "
     "call, not just the unlucky ones. Pinned by "
     "test_val_performance.py::test_usage_ledger_tail_under_concurrency, "
     "which measures 1 / MAX_CONCURRENT_CALLS / 16 threads at 50 calls each "
     "over 5 independent repetitions, asserts MAX_CONCURRENT_CALLS <= 16 so "
     "this bound cannot be exceeded silently, and verifies that every call "
     "reached the persisted ledger. Stated at its true strength: the LATENCY "
     "bounds carry a 3-of-5 noise tolerance, so a regression firing on fewer "
     "than three repetitions in five can pass that gate; the ACCOUNTING check "
     "carries none and must hold 5/5. The gate measures three levels, not the "
     "curve -- the curve is this report's row 12."),
    ("F2", "OBSERVATION",
     "streamguard costs 21.9 us/delta (43.7 ms on a 2000-delta stream)",
     "OFF (NullMonitor) 0.03 us/delta vs ON 21.88 us/delta.",
     "Per-delta loop detection, whitespace-run and unicode checks run on every "
     "text delta.",
     "Default is OFF. Against a real multi-second stream this is <1%; recorded "
     "because it sits on the per-delta hot path and scales with output length."),
    ("F3", "OBSERVATION",
     "Admission under contention roughly doubles per-slot latency",
     "8 threads: p50 4.75 ms (off) -> 8.55 ms (on), p99 16.1 ms, wall 252 ms "
     "-> 363 ms (+44%). Uncontended cost is only +68 us.",
     "usage.slot opens MAX_CONCURRENT_CALLS lock files per acquire and, when "
     "saturated, polls with time.sleep(0.01) -- so contended waits quantize to "
     "10 ms regardless of when a slot frees.",
     "Acceptable at the default cap of 6; revisit the poll interval before "
     "raising MAX_CONCURRENT_CALLS."),
    ("P1", "POSITIVE",
     "Replay is 11.0x faster than record and makes ZERO provider calls",
     "record p50 4.68 ms -> replay p50 0.43 ms; 0 calls counted during replay.",
     "Frozen responses short-circuit the provider call entirely.",
     "The asserted direction holds -- replay is a cheap re-execution, not a "
     "more expensive one."),
    ("P2", "POSITIVE",
     "Flag-off inertness is real, not nominal",
     "watchdog check 1.91 us (off) vs 27.56 us (observe); streamguard 0.03 vs "
     "21.88 us/delta; ctxbudget seam 5.1 vs 13.9 us; the watchdog and "
     "ctxbudget whole-run deltas are WITHIN the measured noise floor.",
     "Each feature short-circuits on its flag before doing any work.",
     "The Wave-1/Wave-2 'default off costs nothing' claim is measurably true."),
]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _rule(width: int = 100) -> str:
    return "-" * width


def _hdr(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def _row(metric: str, condition: str, value: str, note: str = "") -> None:
    print(f"{metric:<34} {condition:<20} {value:>18}  {note}")


def run_all(quick: bool = False) -> dict:
    scale = 0.2 if quick else 1.0

    def s(n: int, floor: int = 1) -> int:
        return max(floor, int(n * scale))

    t_start = time.perf_counter()
    os.environ.setdefault("OLYMPUS_MODEL", "claude-opus-4-8")
    results: dict = {}
    results["sessionlog_auto"] = bench_sessionlog_append(s(500, 50), "auto")
    results["sessionlog_always"] = bench_sessionlog_append(s(500, 50), "always")
    results["sessionlog_sync"] = bench_sessionlog_sync(s(300, 40))
    results["sync_depth_scaling"] = bench_sync_depth_scaling(
        (100, s(1500, 300)), s(25, 8))
    results["journal_recovery"] = bench_journal_recovery(
        (100, 1000, 5000) if not quick else (100, 500))
    results["observability"] = bench_observability_overhead(s(20, 4))
    results["ctxbudget"] = bench_ctxbudget(s(2000, 200))
    results["ctxbudget_pipeline"] = bench_ctxbudget_pipeline(s(12, 4))
    results["streamguard"] = bench_streamguard(s(2000, 400), s(5, 2))
    results["watchdog_calls"] = bench_watchdog_calls(s(5000, 500))
    results["watchdog_pipeline"] = bench_watchdog_pipeline(s(12, 4))
    results["admission"] = bench_admission(s(300, 40), 8, s(40, 5))
    results["replay"] = bench_replay(s(10, 3))
    results["ladder"] = bench_ladder(s(40, 5))
    results["memory"] = bench_memory_growth(s(1000, 100))
    results["storage"] = bench_storage_per_turn(s(25, 5))
    # Deliberately NOT scaled by --quick: the report publishes a CURVE, and
    # 50 calls/thread is what makes the serialisation shape resolve. The levels
    # are the historical 1/2/4/8/16 plus whatever cap this process enforces, so
    # the report never loses a point the previous revision published.
    results["usage_contention"] = bench_usage_contention(report_levels())
    results["concurrency"] = bench_concurrency_limit()
    results["harness_seconds"] = time.perf_counter() - t_start
    cleanup()
    return results


def render(r: dict) -> None:
    _hdr("PHASE 4 STAGE D — MEASURED (offline, fake providers, no network)")
    print(f"{'metric':<34} {'condition':<20} {'value':>18}  note")
    print(_rule())

    for key, label in (("sessionlog_auto", "fsync=auto"),
                       ("sessionlog_always", "fsync=always")):
        a = r[key]
        _row("1. sessionlog append p50", label, f"{a['p50']:.3f} ms",
             f"n={a['n']}")
        _row("   sessionlog append p90", label, f"{a['p90']:.3f} ms", "")
        _row("   sessionlog append p99", label, f"{a['p99']:.3f} ms", "")
        _row("   sessionlog append max", label, f"{a['max']:.3f} ms", "")
        _row("   append cost growth", label, f"{a['growth_ratio']:.2f} x",
             f"first-decile {a['first_decile_mean']:.2f} ms -> "
             f"last-decile {a['last_decile_mean']:.2f} ms (full rescan/append)")
        _row("   projected append @10k recs", label,
             f"{a['projected_ms_at_10k']:.0f} ms",
             f"slope {a['slope_ms_per_record'] * 1000:.2f} us/record; "
             f"@50k recs {a['projected_ms_at_50k']:.0f} ms  "
             f"[append_turn: still a full rescan — see D1; it is NOT the "
             f"live per-turn path, which is row 1b]")
    ds = r["sync_depth_scaling"]
    lo, hi = min(ds["depths"]), max(ds["depths"])
    _row("1c. sync cost at depth", f"cache ON, depth {lo}",
         f"{ds[f'on_ms_at_{lo}']:.3f} ms", f"median of {ds['samples']} syncs")
    _row("    sync cost at depth", f"cache ON, depth {hi}",
         f"{ds[f'on_ms_at_{hi}']:.3f} ms", "")
    _row("    sync cost at depth", f"cache OFF, depth {lo}",
         f"{ds[f'off_ms_at_{lo}']:.3f} ms", "pre-D1-fix behaviour")
    _row("    sync cost at depth", f"cache OFF, depth {hi}",
         f"{ds[f'off_ms_at_{hi}']:.3f} ms", "")
    _row("    depth-scaling coefficient", "cache ON",
         f"{ds['on_us_per_turn_of_depth']:.2f} us/turn",
         "NOT zero — an O(history) term remains (prefix compare + copy)")
    _row("    depth-scaling coefficient", "cache OFF",
         f"{ds['off_us_per_turn_of_depth']:.2f} us/turn",
         f"reduction {ds['slope_reduction_x']:.1f}x" if ds["slope_reduction_x"]
         else "")
    print(_rule())

    sy = r["sessionlog_sync"]
    _row("1b. sessionlog.sync p50", "LIVE per-turn path", f"{sy['p50']:.3f} ms",
         f"turns={sy['turns']} (memory.save_conversation calls this, "
         "not append_turn)")
    _row("    sessionlog.sync p90", "LIVE per-turn path", f"{sy['p90']:.3f} ms", "")
    _row("    sessionlog.sync p99", "LIVE per-turn path", f"{sy['p99']:.3f} ms", "")
    _row("    sessionlog.sync max", "LIVE per-turn path", f"{sy['max']:.3f} ms", "")
    _row("    sync cost growth", "LIVE per-turn path",
         f"{sy['growth_ratio']:.2f} x",
         f"turn 1 {sy['first_decile_mean']:.2f} ms -> turn {sy['turns']} "
         f"{sy['last_decile_mean']:.2f} ms")
    _row("    projected sync @1k turns", "LIVE per-turn path",
         f"{sy['projected_ms_at_1k']:.0f} ms",
         f"@10k turns {sy['projected_ms_at_10k']:.0f} ms; slope "
         f"{sy['slope_ms_per_turn']:.3f} ms/turn  "
         f"[D1 fixed; this decile fit is NOISY at small `turns` — row 1c is "
         f"the authoritative depth measurement]")
    print(_rule())

    for row in r["journal_recovery"]:
        _row("2. journal read_verified p50", f"{row['records']} records",
             f"{row['p50_ms']:.2f} ms",
             f"{row['us_per_record']:.1f} us/record, "
             f"{row['bytes'] / 1024:.0f} KiB")
    print(_rule())

    o = r["observability"]
    _row("3. pipeline run p50", "instrumentation OFF", f"{o['off_p50_ms']:.2f} ms",
         f"iters={o['iters']}, interleaved A/B")
    _row("   pipeline run p50", "instrumentation ON", f"{o['on_p50_ms']:.2f} ms",
         "otel stub + metrics + usage ledger + ctxbudget observe + guard")
    _row("   observability overhead", "ON vs OFF", f"{o['overhead_pct']:+.1f} %",
         f"= {o['abs_overhead_ms']:+.2f} ms/run absolute (paired), noise floor "
         f"+/-{o['noise_ms']:.2f} ms  [FINDING F1 if >20%]")
    for name, c in sorted(o["components_ms"].items(),
                          key=lambda x: -x[1]["ms"]):
        _row(f"   - marginal cost: {name}", "interleaved vs ON",
             f"{c['ms']:+.2f} ms/run",
             f"noise +/-{c['noise_ms']:.2f} ms"
             + ("" if c["resolved"] else "  (BELOW NOISE - not resolvable)"))
    print("   NOTE: the denominator is a pipeline with ~0 ms of provider time.")
    print("   With real inference latency the same absolute cost is a far "
          "smaller share;\n   the absolute ms/run is the portable number.")
    print(_rule())

    c = r["ctxbudget"]
    _row("4. ctxbudget.estimate_tokens", f"{c['history_turns']}-turn history",
         f"{c['estimate_tokens_us']:.1f} us/call", "")
    _row("   ctxbudget.plan", "5 blocks", f"{c['plan_us']:.1f} us/call", "")
    _row("   orchestrator estimate seam", "CTX_BUDGET off",
         f"{c['seam_off_us']:.1f} us/call", "legacy chars//4")
    _row("   orchestrator estimate seam", "CTX_BUDGET on",
         f"{c['seam_on_us']:.1f} us/call",
         f"delta {c['seam_delta_us']:+.1f} us/call (once per turn)")
    cp = r["ctxbudget_pipeline"]
    _row("   per-turn delta", "CTX_BUDGET on vs off",
         f"{cp['overhead_pct']:+.1f} %",
         f"orchestrator turn: off {cp['off']:.2f} -> on {cp['on']:.2f} ms; "
         f"paired {cp['abs_overhead_ms']:+.3f} ms/turn, noise +/-"
         f"{cp['noise_ms']:.3f} ms -> "
         f"{'SIGNIFICANT' if cp['significant'] else 'WITHIN NOISE'}")
    print(_rule())

    sg = r["streamguard"]
    _row("5. streamguard feed()", "OFF (NullMonitor)",
         f"{sg['off_per_delta_us']:.2f} us/delta", "")
    _row("   streamguard feed()", "ON (StreamMonitor)",
         f"{sg['on_per_delta_us']:.2f} us/delta",
         f"+{sg['delta_overhead_us']:.2f} us/delta")
    _row("   streamguard total", f"{sg['deltas']}-delta stream",
         f"{sg['on_total_ms']:.2f} ms",
         f"OFF {sg['off_total_ms']:.2f} ms, "
         f"added {sg['total_overhead_ms']:.2f} ms/stream")
    print(_rule())

    w = r["watchdog_calls"]
    _row("6. watchdog lease.beat()", "mode=off",
         f"{w['off_beat_us']:.2f} us/call", "")
    _row("   watchdog lease.beat()", "mode=observe",
         f"{w['observe_beat_us']:.2f} us/call", "")
    _row("   watchdog lease.check()", "mode=off",
         f"{w['off_check_us']:.2f} us/call", "inert short-circuit")
    _row("   watchdog lease.check()", "mode=observe",
         f"{w['observe_check_us']:.2f} us/call", "full classifier ladder")
    wp = r["watchdog_pipeline"]
    _row("   full run p50", "off vs observe", f"{wp['overhead_pct']:+.1f} %",
         f"off {wp['off']:.2f} -> observe {wp['observe']:.2f} ms; paired "
         f"{wp['abs_overhead_ms']:+.3f} ms/run, noise +/-{wp['noise_ms']:.3f} "
         f"ms -> {'SIGNIFICANT' if wp['significant'] else 'WITHIN NOISE'}")
    print(_rule())

    a = r["admission"]
    _row("7. usage.slot() acquire+release", "ADMISSION off",
         f"{a['off_uncontended_p50_ms']:.3f} ms", "uncontended p50")
    _row("   usage.slot() acquire+release", "ADMISSION on",
         f"{a['on_uncontended_p50_ms']:.3f} ms",
         f"uncontended p50, +{a['uncontended_overhead_us']:.0f} us")
    _row("   usage.slot() p99", "ADMISSION off",
         f"{a['off_uncontended_p99_ms']:.3f} ms", "")
    _row("   usage.slot() p99", "ADMISSION on",
         f"{a['on_uncontended_p99_ms']:.3f} ms", "")
    _row("   usage.slot() contended p50", f"{a['threads']} threads, off",
         f"{a['off_contended_p50_ms']:.3f} ms",
         f"wall {a['off_contended_wall_ms']:.0f} ms")
    _row("   usage.slot() contended p50", f"{a['threads']} threads, on",
         f"{a['on_contended_p50_ms']:.3f} ms",
         f"wall {a['on_contended_wall_ms']:.0f} ms")
    _row("   usage.slot() contended p99", f"{a['threads']} threads, on",
         f"{a['on_contended_p99_ms']:.3f} ms", "")
    print(_rule())

    rp = r["replay"]
    _row("8. fixture run p50", "record", f"{rp['record_p50_ms']:.2f} ms", "")
    _row("   fixture run p50", "replay", f"{rp['replay_p50_ms']:.2f} ms",
         f"{rp['speedup_x']:.2f}x faster; "
         f"{rp['provider_calls_during_replay']} provider calls during replay")
    print(_rule())

    lad = r["ladder"]
    _row("9. toolcall ladder", f"{lad['corpus_cases']}-case corpus",
         f"{lad['per_call_ms']:.4f} ms/call", f"{lad['calls']} calls")
    print(_rule())

    m = r["memory"]
    _row("10. RSS delta (peak)", f"{m['n']} journal appends",
         f"{m['journal_peak_kb']} KiB", f"{m['journal_s']:.1f} s")
    _row("    RSS delta (peak)", f"{m['n']} heat records",
         f"{m['heat_peak_kb']} KiB", f"{m['heat_s']:.1f} s")
    _row("    RSS delta (peak)", f"{m['n']} usage records",
         f"{m['usage_peak_kb']} KiB", f"{m['usage_s']:.1f} s")
    _row("    RSS delta (peak) TOTAL", f"3x{m['n']} records",
         f"{m['total_peak_kb']} KiB",
         f"current-RSS delta {m['total_cur_kb']} KiB "
         "(getrusage reports the PEAK, so it never shrinks)")
    print(_rule())

    st = r["storage"]
    _row("11. storage per turn", f"{st['turns']} turns",
         f"{st['per_turn_bytes'] / 1024:.1f} KiB",
         "journal + trace + usage + frozen responses + locks")
    for k, v in sorted(st["per_turn_parts"].items(), key=lambda x: -x[1]):
        if v > 0:
            _row(f"    - {k}", "per turn", f"{v / 1024:.2f} KiB", "")
    _row("    extrapolated to 10k turns", "same mix",
         f"{st['extrapolated_10k_mb']:.0f} MiB",
         f"{st['extrapolated_10k_bytes']:,.0f} bytes")
    print(_rule())

    for row in r["usage_contention"]:
        _row("12. usage.record p50" if row["threads"] == 1
             else "    usage.record p50",
             f"{row['threads']} thread" + ("" if row["threads"] == 1 else "s"),
             f"{row['p50']:.3f} ms",
             f"p99 {row['p99']:.1f} ms  max {row['max']:.1f} ms  "
             f"{row['calls_per_s']:.0f} calls/s  "
             f"ledger {row['ledger_calls']}/{row['expected_calls']} calls")
    print(_rule())

    cc = r["concurrency"]
    _row("13. max concurrent slots", "ADMISSION on",
         f"{cc['admission_on_max_concurrent']}",
         f"next acquire refused: {cc['admission_on_refusal_reason']}")
    _row("    max concurrent slots", "ADMISSION off",
         f"{cc['admission_off_max_concurrent']}",
         cc["admission_off_behaviour"])
    _row("    configured cap", "MAX_CONCURRENT_CALLS",
         f"{cc['config_max_concurrent_calls']}", "default config")
    print(_rule())
    print(f"harness wall time: {r['harness_seconds']:.1f} s")

    _hdr("FINDINGS")
    for fid, kind, title, evidence, cause, impact in FINDINGS:
        print(f"[{fid}] {kind}: {title}")
        print(f"    EVIDENCE (reference run; the table above is this run's "
              f"authoritative data): {evidence}")
        print(f"    CAUSE    : {cause}")
        print(f"    IMPACT   : {impact}")
        print()

    _hdr("UNMEASURABLE-OFFLINE — no provider credentials, no network")
    print("These are NOT estimated, NOT modelled, and NOT reported as numbers.\n")
    for name, why, how in UNMEASURABLE:
        print(f"* {name}")
        print(f"    WHY UNMEASURABLE : {why}")
        print(f"    WHAT WOULD MEASURE IT: {how}")

    _hdr("DRAFT SERVICE OBJECTIVES — PROPOSALS")
    print("Entries tagged MEASURED have an offline basis and are backed by the "
          "table above.\nEverything tagged PROPOSAL below CANNOT BE VALIDATED "
          "UNTIL SHADOW TRAFFIC and must\nnot be read as measured.\n")
    for name, target, basis in DRAFT_SLOS:
        print(f"  {name:<48} {target:<44} [{basis}]")
    print("\nDRAFT ERROR BUDGETS (all proposals):")
    for name, budget, basis in ERROR_BUDGETS:
        print(f"  {name:<40} {budget:<58} [{basis}]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true",
                    help="reduced sample sizes (smoke run)")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable results instead of the table")
    args = ap.parse_args(argv)
    results = run_all(quick=args.quick)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        render(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
