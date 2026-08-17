"""Prompt-cache usage telemetry (Wave 1 capability C5) + the C4 observe hooks.

What these tests prove:

1. The Anthropic cache-token split survives end-to-end: a Message with
   cache_read/cache_creation usage fields lands in the day ledger with the
   split preserved (in = UNCACHED input), and I-U2 holds — the recorded
   in + cache_read + cache_creation equals the provider-reported input total.
2. Cost math applies the configurable multipliers (OLYMPUS_CACHE_READ_MULT /
   OLYMPUS_CACHE_WRITE_MULT) to the input price — and no TTL constant exists
   anywhere in usage.py (I-U3).
3. Old-format day files (no cache keys) load, report, and accept new records
   (I-U5, migration-free); the footer stays byte-identical for legacy
   positional record() calls (I-U1).
4. cache_stats liveness verdicts: active / inert / no_signal on synthetic
   recorded days, with a per-fingerprint breakdown.
5. The prefix fingerprint is stable across identical system texts and changes
   on a layout edit; it never alters request params (telemetry-only, I-U4).
6. openai_compat reads prompt_tokens_details.cached_tokens when present.
7. The C4 calibration hook fires: ctxbudget.observe receives (provider, model,
   prompt_chars, provider-reported input total) on both provider seams, and a
   raising observe never breaks the call.
"""

import hashlib
import json
import time
import types
from pathlib import Path

import anthropic
import pytest

from olympus import config, ctxbudget, doctor, llm, openai_compat, usage


# --- helpers ---------------------------------------------------------------

def _message(in_toks=100, out_toks=5, cache_read=400, cache_creation=300,
             text="ok") -> anthropic.types.Message:
    """A minimal but real anthropic Message carrying a full cache split."""
    return anthropic.types.Message.model_validate({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": in_toks, "output_tokens": out_toks,
                  "cache_read_input_tokens": cache_read,
                  "cache_creation_input_tokens": cache_creation},
    })


class _FakeStream:
    def __init__(self, message):
        self._m = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter([b.text for b in self._m.content if b.type == "text"])

    def get_final_message(self):
        return self._m


class _FakeMessages:
    def __init__(self, responder):
        self.responder = responder

    def stream(self, **params):
        return _FakeStream(self.responder(params))


def _fake_client(responder):
    msgs = _FakeMessages(responder)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


_SETTINGS = config.Settings(provider="anthropic", model="claude-test")


def _day_ledger() -> dict:
    """Today's aggregate, through the ONE reader (W2-2).

    This used to parse `usage/<day>.json` directly. After W2-2 that file is a
    compacted SNAPSHOT with an append journal beside it, so reading it alone
    reports only what has already been compacted — for a fresh test process
    that is nothing at all, and every assertion below would see zeros or a
    FileNotFoundError.

    PROPERTIES PRESERVED, not merely the format. Six tests share this helper
    and each keeps what it asserted:

      * per-user and per-model aggregation (`user:` / `model:` rows) —
        `ledger()` reconstructs exactly the same row shape the snapshot had, so
        these assertions are unchanged;
      * the cache-split fields (cache_read / cache_creation / prefix hit
        counts) — same;
      * `test_record_onto_old_format_today_file` is the MIGRATION SEAM: it
        seeds a pre-C5 snapshot and records on top. That property is now
        stronger, not weaker — the seeded file is the snapshot base and the new
        records are journal appends, so it exercises exactly the old-format +
        new-format boundary W2-2 introduced.

    What no test here asserted, and so is NOT dropped by this change: the
    tmp+replace atomicity of the old write path. That property moved to the
    append (whole newline-terminated lines) and is asserted in
    tests/test_durable_fsync.py, not here.
    """
    return usage.ledger(time.strftime("%Y-%m-%d"))


def _write_day(name: str, ledger: dict) -> None:
    base = config.MEMORY_DIR / "usage"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}.json").write_text(json.dumps(ledger), encoding="utf-8")


# --- 1. split preserved end-to-end (complete + stream_text) -----------------

def test_split_preserved_end_to_end_complete(monkeypatch):
    msg = _message()
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(lambda p: msg))
    out = llm.complete("SYSTEM PREFIX", [{"role": "user", "content": "hi"}],
                       settings=_SETTINGS)
    assert llm.text_of(out) == "ok"

    ledger = _day_ledger()
    row = ledger["model:claude-test"]
    assert row["in"] == 100 and row["out"] == 5          # in = UNCACHED
    assert row["cache_read"] == 400
    assert row["cache_creation"] == 300
    # I-U2: uncached + cache_read + cache_creation == provider input total.
    u = out.usage
    assert (row["in"] + row["cache_read"] + row["cache_creation"]
            == u.input_tokens + u.cache_read_input_tokens
            + u.cache_creation_input_tokens)
    # The __all__ row carries the same additive split.
    assert ledger["__all__"]["cache_read"] == 400
    # Fingerprint aggregation: 1 call, 1 hit (cache_read > 0).
    fp = llm._prefix_fp("SYSTEM PREFIX")
    assert ledger["prefix"][fp] == {"calls": 1, "hits": 1, "cache_read": 400}


def test_split_preserved_stream_text(monkeypatch):
    msg = _message(in_toks=10, cache_read=50, cache_creation=0)
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(lambda p: msg))
    chunks = list(llm.stream_text("SYS-B", [{"role": "user", "content": "x"}],
                                  settings=_SETTINGS))
    assert chunks == ["ok"]
    ledger = _day_ledger()
    row = ledger["model:claude-test"]
    assert row["in"] == 10 and row["cache_read"] == 50
    fp = llm._prefix_fp("SYS-B")
    assert ledger["prefix"][fp]["hits"] == 1


def test_fingerprint_never_alters_params(monkeypatch):
    """I-U4: the request params the client sees are byte-identical to what the
    pre-C5 code built — no fingerprint, no telemetry fields."""
    seen = {}

    def responder(params):
        seen.update(params)
        return _message()

    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(responder))
    llm.complete("SYSTEM PREFIX", [{"role": "user", "content": "hi"}],
                 settings=_SETTINGS)
    assert "prefix_fp" not in json.dumps({k: v for k, v in seen.items()
                                          if k != "messages"})
    assert seen["system"] == [{"type": "text", "text": "SYSTEM PREFIX",
                               "cache_control": {"type": "ephemeral"}}]


# --- 2. cost math ------------------------------------------------------------

def test_cost_math_with_default_multipliers():
    price_in = usage.PRICES["claude-fable-5"][0]
    assert usage.estimate_cost("claude-fable-5", 1000, 0) == pytest.approx(
        1000 * price_in / 1e6)
    assert usage.estimate_cost(
        "claude-fable-5", 0, 0, cache_read=1000) == pytest.approx(
        1000 * price_in * 0.1 / 1e6)
    assert usage.estimate_cost(
        "claude-fable-5", 0, 0, cache_creation=1000) == pytest.approx(
        1000 * price_in * 1.25 / 1e6)
    # Legacy positional call: identical arithmetic to before (I-U1).
    assert usage.estimate_cost("claude-fable-5", 500, 100) == pytest.approx(
        (500 * 10.0 + 100 * 50.0) / 1e6)


def test_cost_math_env_overrides(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CACHE_READ_MULT", "0.5")
    monkeypatch.setenv("OLYMPUS_CACHE_WRITE_MULT", "2.0")
    price_in = usage.PRICES["claude-fable-5"][0]
    assert usage.estimate_cost(
        "claude-fable-5", 0, 0, cache_read=1000) == pytest.approx(
        1000 * price_in * 0.5 / 1e6)
    assert usage.estimate_cost(
        "claude-fable-5", 0, 0, cache_creation=1000) == pytest.approx(
        1000 * price_in * 2.0 / 1e6)
    monkeypatch.setenv("OLYMPUS_CACHE_READ_MULT", "garbage")
    assert usage.cache_read_mult() == 0.1          # bad value -> default


def test_no_ttl_constants_in_usage_module():
    """I-U3: no TTL constants anywhere in usage.py — pricing multipliers are
    never tied to a cache-lifetime value."""
    src = Path(usage.__file__).read_text(encoding="utf-8")
    assert "ttl" not in src.lower()


# --- 3. old files + legacy callers -------------------------------------------

def test_old_format_day_files_load_and_report():
    _write_day("2026-07-20", {
        "__all__": {"calls": 3, "in": 100, "out": 10, "cost": 0.01},
        "model:claude-old": {"calls": 3, "in": 100, "out": 10, "cost": 0.01},
    })
    rep = usage.report(7)
    assert "2026-07-20" in rep and "$0.0100" in rep
    s = usage.cache_stats(7)
    assert s["verdict"] == "no_signal"
    assert s["cache_read"] == 0 and s["by_fp"] == {}


def test_record_onto_old_format_today_file():
    """I-U5: a pre-C5 ledger (rows without cache keys) accepts new cache-aware
    records without migration."""
    day = time.strftime("%Y-%m-%d")
    _write_day(day, {"__all__": {"calls": 1, "in": 10, "out": 1, "cost": 0.0}})
    usage.record("m2", 5, 1, cache_read=7, provider="x",
                 prefix_fp="deadbeef0123")
    ledger = _day_ledger()
    assert ledger["__all__"]["calls"] == 2
    assert ledger["__all__"]["in"] == 15
    assert ledger["__all__"]["cache_read"] == 7
    assert ledger["prefix"]["deadbeef0123"] == {
        "calls": 1, "hits": 1, "cache_read": 7}


def test_footer_byte_identical_for_positional_calls():
    from olympus import memory
    memory.set_user("fp-user")
    try:
        usage.record("claude-x", 1000, 200)        # legacy positional form
    finally:
        memory.set_user("shared")
    cost = (1000 * 1.0 + 200 * 3.0) / 1e6          # DEFAULT_PRICE model
    line = usage.footer(
        {"calls": 1, "in": 1000, "out": 200, "cost": cost}, "fp-user")
    assert line == (f"⏱ 1.0k in / 200 out · ~${cost:.4f} this reply · "
                    f"${cost:.2f} session · ${cost:.2f} today")


def test_session_totals_keep_legacy_keys():
    from olympus import memory
    memory.set_user("legacy-keys")
    try:
        usage.record("claude-x", 100, 10)
    finally:
        memory.set_user("shared")
    totals = usage.session_totals("legacy-keys")
    assert totals["calls"] == 1 and totals["in"] == 100 and totals["out"] == 10
    assert totals.get("cache_read", 0) == 0


# --- 4. liveness verdicts on synthetic recorded days --------------------------

def test_verdict_active_with_savings_and_fp_breakdown():
    _write_day("2026-07-21", {
        "model:m": {"calls": 5, "in": 50, "out": 5, "cost": 0.0,
                    "cache_read": 500, "cache_creation": 100},
        "prefix": {"aaa111aaa111": {"calls": 5, "hits": 4, "cache_read": 500}},
    })
    s = usage.cache_stats(7)
    assert s["verdict"] == "active"
    assert s["fp_calls"] == 5 and s["hits"] == 4
    assert s["hit_rate"] == pytest.approx(0.8)
    assert s["cache_read"] == 500 and s["cache_creation"] == 100
    # savings = cache_read * input_price * (1 - read_mult); "m" -> DEFAULT.
    assert s["savings_usd"] == pytest.approx(500 * 1.0 * 0.9 / 1e6)
    assert s["by_fp"]["aaa111aaa111"]["hit_rate"] == pytest.approx(0.8)


def test_verdict_inert_at_threshold():
    _write_day("2026-07-21", {
        "model:m": {"calls": 25, "in": 250, "out": 25, "cost": 0.0,
                    "cache_read": 0, "cache_creation": 0},
        "prefix": {"bbb222bbb222": {"calls": 25, "hits": 0, "cache_read": 0}},
    })
    s = usage.cache_stats(7)
    assert s["verdict"] == "inert"
    assert s["fp_calls"] == 25 and s["hits"] == 0


def test_verdict_no_signal_below_threshold_and_empty():
    assert usage.cache_stats(7)["verdict"] == "no_signal"   # nothing recorded
    _write_day("2026-07-21", {
        "model:m": {"calls": 5, "in": 50, "out": 5, "cost": 0.0},
        "prefix": {"ccc333ccc333": {"calls": 5, "hits": 0, "cache_read": 0}},
    })
    s = usage.cache_stats(7)
    assert s["verdict"] == "no_signal"      # too few fp calls to call it inert


def test_layout_change_visible_as_new_fp_with_cliff():
    _write_day("2026-07-20", {
        "prefix": {"oldfp1234567": {"calls": 30, "hits": 28,
                                    "cache_read": 9000}},
        "model:m": {"calls": 30, "in": 30, "out": 3, "cost": 0.0,
                    "cache_read": 9000, "cache_creation": 0},
    })
    _write_day("2026-07-21", {
        "prefix": {"newfp7654321": {"calls": 10, "hits": 0, "cache_read": 0}},
        "model:m": {"calls": 10, "in": 100, "out": 10, "cost": 0.0,
                    "cache_read": 0, "cache_creation": 500},
    })
    s = usage.cache_stats(7)
    assert s["by_fp"]["oldfp1234567"]["hit_rate"] > 0.9
    assert s["by_fp"]["newfp7654321"]["hit_rate"] == 0.0
    assert s["by_fp"]["newfp7654321"]["last_day"] == "2026-07-21"


def test_doctor_cache_check_verdicts():
    checks = doctor._cache_checks()
    assert checks[0].status == doctor.OK
    assert "no cache telemetry" in checks[0].detail
    _write_day("2026-07-21", {
        "prefix": {"bbb222bbb222": {"calls": 25, "hits": 0, "cache_read": 0}},
    })
    checks = doctor._cache_checks()
    assert checks[0].status == doctor.WARN
    assert "producing no cache reads" in checks[0].detail
    _write_day("2026-07-22", {
        "model:m": {"calls": 1, "in": 1, "out": 1, "cost": 0.0,
                    "cache_read": 100},
        "prefix": {"bbb222bbb222": {"calls": 1, "hits": 1, "cache_read": 100}},
    })
    checks = doctor._cache_checks()
    assert checks[0].status == doctor.OK and "active" in checks[0].detail


# --- 5. fingerprint stability --------------------------------------------------

def test_fingerprint_stable_and_layout_sensitive():
    text = "You are Zeus.\n\nRules:\n- be brief"
    a = llm._prefix_fp(text)
    assert a == llm._prefix_fp(text)                       # stable
    assert a == hashlib.sha256(text.encode()).hexdigest()[:12]
    assert len(a) == 12
    assert a != llm._prefix_fp(text + " ")                 # layout edit


# --- 6. openai_compat cached_tokens path ----------------------------------------

class _FakeHTTPResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_openai_cached_tokens_path(monkeypatch):
    body = {"choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                      "prompt_tokens_details": {"cached_tokens": 900}}}
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeHTTPResp(body))
    observed = []
    monkeypatch.setattr(ctxbudget, "observe",
                        lambda *a, **k: observed.append(a))
    settings = config.Settings(provider="openai", model="gpt-test",
                               api_key="sk-test")
    out = openai_compat.complete_text(
        settings, "sys", [{"role": "user", "content": "q"}])
    assert out == "ok"
    row = _day_ledger()["model:gpt-test"]
    assert row["in"] == 100                      # uncached = prompt - cached
    assert row["cache_read"] == 900
    assert row["cache_creation"] == 0
    # C4 hook: observe fed the FULL reported prompt total, not the split.
    assert observed == [("openai-compat", "gpt-test", len("sys") + len("q"),
                         1000)]


def test_openai_without_details_records_total_as_uncached(monkeypatch):
    body = {"choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7}}
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeHTTPResp(body))
    settings = config.Settings(provider="openai", model="gpt-test",
                               api_key="sk-test")
    openai_compat.complete_text(settings, "s", [{"role": "user",
                                                 "content": "q"}])
    row = _day_ledger()["model:gpt-test"]
    assert row["in"] == 42 and row["cache_read"] == 0


# --- 7. C4 observe wiring --------------------------------------------------------

def test_observe_wiring_fires_on_anthropic_seam(monkeypatch):
    observed = []
    monkeypatch.setattr(ctxbudget, "observe",
                        lambda *a, **k: observed.append(a))
    msg = _message()                              # 100 + 400 + 300 = 800 total
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(lambda p: msg))
    llm.complete("SYS", [{"role": "user", "content": "hello"}],
                 settings=_SETTINGS)
    assert observed == [("anthropic", "claude-test",
                         len("SYS") + len("hello"), 800)]


def test_raising_observe_never_breaks_the_call(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calibration exploded")

    monkeypatch.setattr(ctxbudget, "observe", boom)
    msg = _message()
    monkeypatch.setattr(llm, "client",
                        lambda settings=None: _fake_client(lambda p: msg))
    out = llm.complete("SYS", [{"role": "user", "content": "hello"}],
                       settings=_SETTINGS)
    assert llm.text_of(out) == "ok"               # reply unaffected
    assert _day_ledger()["model:claude-test"]["cache_read"] == 400
