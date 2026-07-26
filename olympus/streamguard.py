"""Degenerate-stream defense — Wave-2 capability C8.

Colibri armors its own decode loop against degenerate sampling (one NaN logit
once produced a silent unbroken stream of token 0). Olympus does not see
logits: it sees a provider stream. The transferable insight (gap G2) is the
same — **defend the decode boundary against degenerate output** — but the
failure class here is the one an API client actually meets: empty-delta
stalls, repetition loops, never-ending whitespace, malformed event sequences,
tool-argument JSON that never closes, mid-stream encoding garbage, impossible
usage counters, output past the declared bound.

Shape of the module (W2-I8.2): **every detector is a pure function** — it
takes explicit state, returns a detail string (the reason) or None, touches no
disk, no clock, no network. `StreamMonitor` is the only stateful object: it
accumulates the counters the detectors need and raises `StreamPathology` on
the first trip. Evidence writing lives in `record_pathology()`, never inside
`feed()` — so the detectors stay testable and side-effect-free.

Contract for callers (W2-I8.1 / W2-I8.3):

1. Construct with `monitor(...)`. When `OLYMPUS_STREAMGUARD` is off this
   returns a no-op object, so wiring a stream through it is byte-identical to
   not wiring it at all.
2. Feed every delta/event. On `StreamPathology`: **abort the stream**.
3. Never execute the raw tool-argument buffer. Use `safe_tool_calls()` — it
   returns ONLY tool calls that were complete and parsed *before* the trip;
   the call in flight is deliberately absent (W2-I8.1). The raw buffers are
   private on purpose: a partial call must never reach `toolcall_repair`,
   whose ladder would happily close the braces and hand a half-formed call to
   a handler.
4. Fail loudly. A tripped stream is a structured failure, never a silently
   truncated answer (W2-I8.3).
5. Preserve evidence: `record_pathology(pathology_record(...))` appends to
   `MEMORY_DIR/streamguard/pathologies.jsonl` (proclock-serialised, never
   raises) so a rising pathology *rate* becomes provider-decay evidence for
   `modelgate`/`modelgrade` later.

Not to be confused with the *other* streamguard concern in domain 06 (the
marker/secret hold-back filter, env `OLYMPUS_STREAM_GUARD`): that is an egress
filter over the bytes; this is a pathology detector over the stream. They
share a module home by design but not a flag.

Knobs (all read per call, so they are runtime-flippable):
  OLYMPUS_STREAMGUARD           master flag, default OFF
  OLYMPUS_STREAM_LOOP_REPEATS   consecutive n-gram repeats allowed (12)
  OLYMPUS_STREAM_EMPTY_DELTAS   consecutive no-progress deltas allowed (200)
  OLYMPUS_STREAM_WS_RUN         consecutive whitespace chars allowed (4096)
  OLYMPUS_STREAM_PARTIAL_JSON   unparseable tool-arg deltas allowed (32)
  OLYMPUS_STREAM_CHARS_PER_TOKEN  chars-per-token bound factor (16)
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any

from . import config

# --- pathology kinds --------------------------------------------------------

TOKEN_LOOP = "token_loop"
EMPTY_PROGRESS = "empty_progress"
MALFORMED_EVENTS = "malformed_events"
INVALID_TOOL_DELTA = "invalid_tool_delta"
IMPOSSIBLE_USAGE = "impossible_usage"
WHITESPACE_FLOOD = "whitespace_flood"
PARTIAL_JSON_LOOP = "partial_json_loop"
INVALID_UNICODE = "invalid_unicode"
EVENT_ORDER = "event_order"
OUTPUT_BOUNDS = "output_bounds"

KINDS = (TOKEN_LOOP, EMPTY_PROGRESS, MALFORMED_EVENTS, INVALID_TOOL_DELTA,
         IMPOSSIBLE_USAGE, WHITESPACE_FLOOD, PARTIAL_JSON_LOOP,
         INVALID_UNICODE, EVENT_ORDER, OUTPUT_BOUNDS)


_DETAIL_MAX = 240          # bound on the reason text we surface and persist


class StreamPathology(Exception):
    """A detector tripped. `.kind` is one of KINDS; `.detail` is the human
    reason (bounded, so it is safe to persist as evidence)."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = str(kind)
        self.detail = str(detail)[:_DETAIL_MAX]
        super().__init__(f"{self.kind}: {self.detail}" if self.detail
                         else self.kind)


class StreamAborted(StreamPathology):
    """Raised by a *caller* (e.g. `llm.stream_text`) after it has aborted the
    stream and written the evidence record — the typed failure the user-facing
    path surfaces instead of a truncated answer (W2-I8.3). Subclasses
    StreamPathology so one `except` catches both."""

    def __init__(self, kind: str, detail: str = "",
                 record: dict[str, Any] | None = None):
        super().__init__(kind, detail)
        self.record = record or {}


# --- flag + limits ----------------------------------------------------------

_TRUE = ("1", "true", "on", "yes")


def enabled() -> bool:
    """OLYMPUS_STREAMGUARD — default OFF (A14 fail-safe default). Read per
    call so it is runtime-flippable and so tests can flip it per case."""
    return os.environ.get("OLYMPUS_STREAMGUARD", "").strip().lower() in _TRUE


DEFAULT_LOOP_REPEATS = 12
DEFAULT_EMPTY_DELTAS = 200
DEFAULT_WS_RUN = 4096
DEFAULT_PARTIAL_JSON = 32
DEFAULT_CHARS_PER_TOKEN = 16


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def limits() -> dict[str, int]:
    """The active thresholds. Pure w.r.t. the environment (a snapshot)."""
    return {
        "loop_repeats": _int_env("OLYMPUS_STREAM_LOOP_REPEATS",
                                 DEFAULT_LOOP_REPEATS, minimum=2),
        "empty_deltas": _int_env("OLYMPUS_STREAM_EMPTY_DELTAS",
                                 DEFAULT_EMPTY_DELTAS),
        "ws_run": _int_env("OLYMPUS_STREAM_WS_RUN", DEFAULT_WS_RUN),
        "partial_json": _int_env("OLYMPUS_STREAM_PARTIAL_JSON",
                                 DEFAULT_PARTIAL_JSON),
        "chars_per_token": _int_env("OLYMPUS_STREAM_CHARS_PER_TOKEN",
                                    DEFAULT_CHARS_PER_TOKEN),
    }


# --- pure detectors ---------------------------------------------------------
#
# Every function below is a pure predicate: explicit inputs, no I/O, no
# hidden state, returns the reason string or None (W2-I8.2).

_MAX_PERIOD = 128          # longest repeated block we look for, in chars
_MIN_LOOP_CHARS = 12       # a char-based n-gram must be at least this long
_MIN_LOOP_WORDS = 3        # …or carry at least this many words
# A block drawn from a ONE-character alphabet ("aaaa", "----", a wall of
# "        }\n") is the shape legitimate output takes too — rules, banners,
# closing braces. It is degeneracy only at extreme length, so it must clear a
# much higher repeat bar. This is the false-positive guard that keeps long
# real code out of TOKEN_LOOP.
_LOW_ALPHABET_FACTOR = 8


def find_repetition_loop(text: str, *, repeats: int,
                         max_period: int = _MAX_PERIOD) -> str | None:
    """The block of a consecutive repetition loop at the END of `text`, or
    None. A block qualifies when it repeats > `repeats` times back to back and
    is either >= 12 chars or >= 3 words (the n-gram floor: shorter units are
    ordinary language/punctuation rhythm, not degeneracy). Whitespace-only
    blocks are ignored — `whitespace_run` owns that pathology — and
    single-character alphabets need `_LOW_ALPHABET_FACTOR`x more repeats.

    Because it reads a *tail buffer* rather than one delta, a loop split
    arbitrarily across deltas is caught exactly like an unsplit one.
    """
    n = len(text)
    for period in range(1, max_period + 1):
        if period * (repeats + 1) > n:
            break                          # every longer period needs more
        block = text[-period:]
        if block != text[-2 * period:-period]:
            continue                       # cheap reject before the full scan
        if not block.strip():
            continue                       # whitespace flood, not a loop
        if len(block) < _MIN_LOOP_CHARS and len(block.split()) < _MIN_LOOP_WORDS:
            continue                       # below the n-gram floor
        needed = repeats
        if len({c for c in block if not c.isspace()}) < 2:
            needed = repeats * _LOW_ALPHABET_FACTOR
        need = period * (needed + 1)
        if need > n:
            continue
        if text[-need:] != block * (needed + 1):
            continue
        return block
    return None


def detect_token_loop(tail: str, *, repeats: int) -> str | None:
    block = find_repetition_loop(tail, repeats=repeats)
    if block is None:
        return None
    return (f"n-gram repeated >{repeats}x consecutively: "
            f"{block[:60]!r} ({len(block)} chars)")


def detect_empty_progress(consecutive_empty: int, *, limit: int) -> str | None:
    """> `limit` consecutive deltas contributing zero non-whitespace chars."""
    if consecutive_empty <= limit:
        return None
    return (f"{consecutive_empty} consecutive deltas with no non-whitespace "
            f"content (limit {limit})")


def whitespace_run(prev_run: int, text: str) -> tuple[int, int]:
    """(run_at_end, peak_run) for `text` continuing a previous run of
    `prev_run` whitespace chars. The peak matters because a flood can be
    interior to one delta (4096 spaces then a word), which a tail-only
    counter would miss. Pure."""
    cur = peak = int(prev_run)
    for ch in text:
        if ch.isspace():
            cur += 1
            if cur > peak:
                peak = cur
        else:
            cur = 0
    return cur, peak


def detect_whitespace_flood(run: int, *, limit: int) -> str | None:
    if run <= limit:
        return None
    return f"{run} consecutive whitespace chars (limit {limit})"


_TRIM_MAX = 24             # longest incomplete literal we will trim back over


def json_prefix_viable(buffer: str) -> bool:
    """True when `buffer` is a *healthy* partial JSON value — i.e. SOME
    completion of it parses. A legitimate tool call streams through hundreds
    of viable prefixes (mid-string, mid-escape, mid-number, dangling comma);
    a degenerate one never becomes closable. This is the guard that keeps
    PARTIAL_JSON_LOOP off big healthy tool arguments, and it is pure.

    Method: neutralize the open string (replace its content with ""), close
    the open brackets, and — because a delta can land mid-literal — retry with
    up to `_TRIM_MAX` trailing chars removed. A value that is COMPLETE but
    followed by junk ("{}{}{}") is definitively broken, not partial.
    """
    s = buffer.strip()
    if not s:
        return True                        # nothing sent yet is not a loop
    try:
        _, end = json.JSONDecoder().raw_decode(s)
        return not s[end:].strip()         # trailing junk ⇒ broken, not partial
    except ValueError:
        pass
    if not s.startswith(("{", "[")):
        return False                       # not even the start of a container
    stack: list[str] = []
    in_string = False
    escaped = False
    str_start = -1
    for i, ch in enumerate(s):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            str_start = i
        elif ch in "[{":
            stack.append("]" if ch == "[" else "}")
        elif ch in "]}":
            if not stack or stack.pop() != ch:
                return False               # structurally broken, not partial
    if not stack:
        return False                       # balanced yet unparseable → garbage
    base = (s[:str_start] + '""') if in_string else s
    closers = "".join(reversed(stack))
    for cut in range(0, min(len(base), _TRIM_MAX) + 1):
        head = base[:len(base) - cut].rstrip()
        if head.endswith(":"):
            cand = head + "null" + closers
        elif head.endswith(","):
            cand = head[:-1] + closers
        else:
            cand = head + closers
        try:
            json.loads(cand)
            return True
        except ValueError:
            continue
    return False


def detect_partial_json_loop(buffer: str, attempts: int, *,
                             limit: int) -> str | None:
    """The tool-argument buffer keeps growing without ever parsing. Only a
    buffer that is ALSO non-viable (not completable into valid JSON) trips —
    a long healthy tool call streams viable prefixes all the way."""
    if attempts <= limit:
        return None
    if json_prefix_viable(buffer):
        return None
    return (f"tool arguments unparseable and unclosable after {attempts} "
            f"deltas ({len(buffer)} chars)")


def detect_invalid_unicode(delta: Any) -> str | None:
    """Unpaired surrogates in a str delta, or undecodable bytes."""
    if isinstance(delta, (bytes, bytearray)):
        try:
            bytes(delta).decode("utf-8")
        except UnicodeDecodeError as err:
            return f"undecodable bytes in delta: {err.reason}"
        return None
    if not isinstance(delta, str):
        return None
    try:
        delta.encode("utf-8")
    except UnicodeEncodeError as err:
        return (f"unencodable code point at {err.start} "
                f"(unpaired surrogate?): {err.reason}")
    return None


# Event-sequence state machine. States: init → message → block → …/done.
# `ping` is inert; `error` terminates. tool_use/tool_result are pseudo-events
# so a provider that reports them out of band still gets ordered.
_TRANSITIONS: dict[str, dict[str, str]] = {
    "init": {
        "message_start": "message",
        "ping": "init",
        "error": "done",
    },
    "message": {
        "content_block_start": "block",
        "message_delta": "message",
        "message_stop": "done",
        "tool_use": "message",
        "tool_result": "message",
        "ping": "message",
        "error": "done",
    },
    "block": {
        "content_block_delta": "block",
        "content_block_stop": "message",
        "tool_use": "block",
        "tool_result": "block",
        "ping": "block",
        "error": "done",
    },
    "done": {},
}

KNOWN_EVENTS = frozenset(
    name for row in _TRANSITIONS.values() for name in row)


def detect_malformed_event(event: Any) -> str | None:
    """Structural validity of one event — shape, not order."""
    if not isinstance(event, dict):
        return f"event is {type(event).__name__}, not a mapping"
    name = event.get("type") or event.get("event")
    if not isinstance(name, str) or not name:
        return "event has no string 'type'"
    if name not in KNOWN_EVENTS:
        return f"unknown event type {name!r}"
    if name in ("content_block_delta", "message_delta"):
        if not isinstance(event.get("delta"), dict):
            return f"{name} carries no delta mapping"
    if name == "content_block_start":
        if not isinstance(event.get("content_block", {}), dict):
            return "content_block_start carries a non-mapping content_block"
    return None


def detect_event_order(state: str, name: str, *,
                       saw_tool_use: bool) -> str | None:
    """Order validity of a structurally-valid event, given the machine state.
    Covers content_block_delta before start, anything after stop, and a
    tool_result with no preceding tool_use."""
    if name == "tool_result" and not saw_tool_use:
        return "tool_result before any tool_use"
    allowed = _TRANSITIONS.get(state, {})
    if name not in allowed:
        return f"event {name!r} not allowed in state {state!r}"
    return None


def next_state(state: str, name: str) -> str:
    """The state after a *validated* event. Pure."""
    return _TRANSITIONS.get(state, {}).get(name, state)


def detect_invalid_tool_delta(payload: Any, *, open_id: str | None,
                              delta_id: str | None) -> str | None:
    """A tool-argument delta that is not a str, or a tool id that changed
    mid-block (the provider interleaving two calls into one buffer — the
    surest way to synthesize a tool call nobody asked for)."""
    if not isinstance(payload, str):
        return (f"tool-argument delta is {type(payload).__name__}, not str")
    if delta_id and open_id and delta_id != open_id:
        return f"tool id changed mid-block: {open_id!r} → {delta_id!r}"
    if delta_id and not open_id:
        return f"tool-argument delta {delta_id!r} outside any tool block"
    return None


def normalize_usage(usage: Any) -> dict[str, int]:
    """Provider usage → a flat int dict. Accepts Anthropic-style objects/dicts
    (input_tokens/output_tokens/cache_*_input_tokens) and OpenAI-style dicts
    (prompt_tokens/completion_tokens/prompt_tokens_details.cached_tokens).
    Unreadable fields are simply absent. Pure."""
    def _get(key: str) -> Any:
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    out: dict[str, int] = {}
    for src, dst in (("input_tokens", "input"),
                     ("output_tokens", "output"),
                     ("cache_read_input_tokens", "cache_read"),
                     ("cache_creation_input_tokens", "cache_creation"),
                     ("prompt_tokens", "prompt"),
                     ("completion_tokens", "completion")):
        val = _get(src)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        out[dst] = int(val)
    details = _get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, (int, float)) and not isinstance(cached, bool):
            out["cache_read"] = int(cached)
    if "output" not in out and "completion" in out:
        out["output"] = out["completion"]
    return out


def detect_impossible_usage(usage: Any, *,
                            max_output_tokens: int | None = None
                            ) -> str | None:
    """Counters that cannot be true: negatives, output past the declared max,
    or cached input exceeding the provider's own declared input total (only
    checked when the provider reports a *total*, e.g. OpenAI `prompt_tokens`
    — Anthropic's `input_tokens` is the UNCACHED remainder, so cache_read
    legitimately exceeds it)."""
    vals = normalize_usage(usage)
    for key, val in sorted(vals.items()):
        if val < 0:
            return f"negative usage counter {key}={val}"
    out = vals.get("output")
    if max_output_tokens and out is not None and out > max_output_tokens:
        return (f"output_tokens={out} exceeds declared max_tokens="
                f"{max_output_tokens}")
    prompt = vals.get("prompt")
    cached = vals.get("cache_read")
    if prompt is not None and cached is not None and cached > prompt:
        return (f"cache_read={cached} exceeds declared input total={prompt}")
    return None


def output_bound_chars(max_tokens: int | None, *,
                       chars_per_token: int) -> int | None:
    """The generous char ceiling implied by a declared max_tokens. `None` when
    nothing was declared (no bound to enforce). Pure."""
    if not max_tokens or max_tokens <= 0:
        return None
    return int(max_tokens) * max(1, int(chars_per_token))


def detect_output_bounds(total_chars: int, *,
                         max_chars: int | None) -> str | None:
    if not max_chars or total_chars <= max_chars:
        return None
    return f"{total_chars} chars streamed, bound is {max_chars}"


# --- the stateful accumulator ----------------------------------------------


class StreamMonitor:
    """Incremental pathology monitor over one provider stream.

    `feed(x)` takes a text delta (str/bytes) or a provider event (dict) and
    raises `StreamPathology` on the first trip. It performs NO I/O: evidence
    is the caller's job via `pathology_record()` / `record_pathology()`.
    """

    enabled = True

    def __init__(self, *, provider: str = "", model: str = "",
                 max_tokens: int | None = None,
                 thresholds: dict[str, int] | None = None):
        self.provider = provider or ""
        self.model = model or ""
        self.max_tokens = max_tokens
        self.limits = dict(thresholds or limits())
        self.max_chars = output_bound_chars(
            max_tokens, chars_per_token=self.limits["chars_per_token"])
        # counters
        self.deltas = 0
        self.events = 0
        self.chars = 0
        self.empty_run = 0
        self.ws_run = 0
        self.state = "init"
        self.saw_tool_use = False
        self.tripped: StreamPathology | None = None
        self._tail = ""
        self._tail_cap = _MAX_PERIOD * (self.limits["loop_repeats"] + 2)
        # tool-call accumulation — PRIVATE: the in-flight buffer must never
        # leave this object (W2-I8.1).
        self._tool_id: str | None = None
        self._tool_name: str = ""
        self._tool_buf = ""
        self._tool_attempts = 0
        self._complete_calls: list[dict[str, Any]] = []

    # -- public API ---------------------------------------------------------

    def feed(self, delta: Any) -> None:
        """Feed one text delta (str/bytes) or one provider event (dict)."""
        if isinstance(delta, (str, bytes, bytearray)):
            self.feed_text(delta)
            return
        if isinstance(delta, dict):
            self.feed_event(delta)
            return
        self._trip(MALFORMED_EVENTS, detect_malformed_event(delta) or "")

    def feed_text(self, delta: Any) -> None:
        """A text delta: unicode, whitespace, progress, loop and bound checks."""
        self.deltas += 1
        reason = detect_invalid_unicode(delta)
        if reason:
            self._trip(INVALID_UNICODE, reason)
        if isinstance(delta, (bytes, bytearray)):
            text = bytes(delta).decode("utf-8", "replace")
        elif isinstance(delta, str):
            text = delta
        else:
            text = ""
            self._trip(MALFORMED_EVENTS,
                       f"text delta is {type(delta).__name__}, not str")
        peak_ws = self._account(text)
        # progress: a delta contributing zero non-whitespace chars
        if text.strip():
            self.empty_run = 0
        else:
            self.empty_run += 1
            reason = detect_empty_progress(
                self.empty_run, limit=self.limits["empty_deltas"])
            if reason:
                self._trip(EMPTY_PROGRESS, reason)
        reason = detect_whitespace_flood(peak_ws,
                                         limit=self.limits["ws_run"])
        if reason:
            self._trip(WHITESPACE_FLOOD, reason)
        reason = detect_output_bounds(self.chars, max_chars=self.max_chars)
        if reason:
            self._trip(OUTPUT_BOUNDS, reason)
        reason = detect_token_loop(self._tail,
                                   repeats=self.limits["loop_repeats"])
        if reason:
            self._trip(TOKEN_LOOP, reason)

    def feed_event(self, event: Any) -> None:
        """A provider event: shape, order, tool-delta and usage checks."""
        self.events += 1
        reason = detect_malformed_event(event)
        if reason:
            self._trip(MALFORMED_EVENTS, reason)
        name = event.get("type") or event.get("event")
        reason = detect_event_order(self.state, name,
                                    saw_tool_use=self.saw_tool_use)
        if reason:
            self._trip(EVENT_ORDER, reason)
        self.state = next_state(self.state, name)
        if name == "content_block_start":
            self._open_block(event.get("content_block") or {})
        elif name == "content_block_delta":
            self._block_delta(event)
        elif name == "content_block_stop":
            self._close_block()
        elif name == "tool_use":
            self.saw_tool_use = True
        elif name == "message_delta":
            usage = event.get("usage") or (event.get("delta") or {}).get("usage")
            if usage is not None:
                self.feed_usage(usage)

    def feed_usage(self, usage: Any) -> None:
        """Provider usage counters (mid-stream or final)."""
        reason = detect_impossible_usage(usage,
                                         max_output_tokens=self.max_tokens)
        if reason:
            self._trip(IMPOSSIBLE_USAGE, reason)

    def safe_tool_calls(self) -> list[dict[str, Any]]:
        """The ONLY safe view of this stream's tool calls (W2-I8.1): the calls
        that were fully received AND parsed before the trip. The call in
        flight when a detector fires is deliberately excluded — callers must
        use this instead of any raw argument buffer, so a partial call can
        never reach the repair ladder or a handler."""
        return [copy.deepcopy(c) for c in self._complete_calls]

    def stats(self) -> dict[str, Any]:
        """Counter snapshot for the evidence record. No content."""
        return {
            "deltas": self.deltas,
            "events": self.events,
            "chars": self.chars,
            "state": self.state,
            "empty_run": self.empty_run,
            "ws_run": self.ws_run,
            "tool_calls_complete": len(self._complete_calls),
            "tool_call_in_flight": bool(self._tool_id),
            "tool_arg_chars": len(self._tool_buf),
            "max_tokens": self.max_tokens,
        }

    # -- internals ----------------------------------------------------------

    def _trip(self, kind: str, detail: str) -> None:
        exc = StreamPathology(kind, detail)
        self.tripped = exc
        raise exc

    def _account(self, text: str) -> int:
        """Char counters + the loop tail. Shared by text and tool-arg deltas —
        one stream, one output budget. Returns the PEAK whitespace run seen
        (which may be interior to this delta, not just its tail)."""
        self.chars += len(text)
        self.ws_run, peak = whitespace_run(self.ws_run, text)
        self._extend_tail(text)
        return peak

    def _extend_tail(self, text: str) -> None:
        self._tail = (self._tail + text)[-self._tail_cap:]

    def _open_block(self, block: dict[str, Any]) -> None:
        if block.get("type") != "tool_use":
            self._tool_id = None
            self._tool_name = ""
            self._tool_buf = ""
            self._tool_attempts = 0
            return
        self.saw_tool_use = True
        self._tool_id = str(block.get("id") or "") or "tool"
        self._tool_name = str(block.get("name") or "")
        self._tool_buf = ""
        self._tool_attempts = 0

    def _block_delta(self, event: dict[str, Any]) -> None:
        delta = event.get("delta") or {}
        kind = delta.get("type")
        if kind == "input_json_delta" or "partial_json" in delta:
            payload = delta.get("partial_json")
            delta_id = delta.get("id") or event.get("id")
            reason = detect_invalid_tool_delta(
                payload, open_id=self._tool_id,
                delta_id=str(delta_id) if delta_id else None)
            if reason:
                self._trip(INVALID_TOOL_DELTA, reason)
            reason = detect_invalid_unicode(payload)
            if reason:
                self._trip(INVALID_UNICODE, reason)
            self._tool_buf += payload
            self._tool_attempts += 1
            peak_ws = self._account(payload)
            reason = detect_whitespace_flood(peak_ws,
                                             limit=self.limits["ws_run"])
            if reason:
                self._trip(WHITESPACE_FLOOD, reason)
            reason = detect_output_bounds(self.chars, max_chars=self.max_chars)
            if reason:
                self._trip(OUTPUT_BOUNDS, reason)
            reason = detect_token_loop(self._tail,
                                       repeats=self.limits["loop_repeats"])
            if reason:
                self._trip(TOKEN_LOOP, reason)
            reason = detect_partial_json_loop(
                self._tool_buf, self._tool_attempts,
                limit=self.limits["partial_json"])
            if reason:
                self._trip(PARTIAL_JSON_LOOP, reason)
            return
        text = delta.get("text")
        if text is None:
            return
        if not isinstance(text, str):
            self._trip(MALFORMED_EVENTS,
                       f"text delta is {type(text).__name__}, not str")
        self.feed_text(text)

    def _close_block(self) -> None:
        """Promote the in-flight tool call to the safe list — ONLY if its
        arguments parse. An unparseable buffer is dropped, never repaired
        here: repair is the ladder's job on a healthy stream."""
        if self._tool_id:
            raw = self._tool_buf.strip()
            args: Any = None
            if not raw:
                args = {}
            else:
                try:
                    args = json.loads(raw)
                except (ValueError, TypeError):
                    args = None
            if isinstance(args, dict):
                self._complete_calls.append({"id": self._tool_id,
                                             "name": self._tool_name,
                                             "input": args})
        self._tool_id = None
        self._tool_name = ""
        self._tool_buf = ""
        self._tool_attempts = 0


class NullMonitor:
    """The flag-off monitor: every method is inert, so a wired stream behaves
    byte-identically to an unwired one (A17 rollback)."""

    enabled = False
    provider = ""
    model = ""
    max_tokens = None
    tripped = None

    def feed(self, delta: Any) -> None:
        return None

    def feed_text(self, delta: Any) -> None:
        return None

    def feed_event(self, event: Any) -> None:
        return None

    def feed_usage(self, usage: Any) -> None:
        return None

    def safe_tool_calls(self) -> list[dict[str, Any]]:
        return []

    def stats(self) -> dict[str, Any]:
        return {}


def monitor(*, provider: str = "", model: str = "",
            max_tokens: int | None = None) -> StreamMonitor | NullMonitor:
    """A monitor for one stream — a real one when OLYMPUS_STREAMGUARD is on,
    an inert NullMonitor otherwise."""
    if not enabled():
        return NullMonitor()
    return StreamMonitor(provider=provider, model=model, max_tokens=max_tokens)


# --- evidence (the only I/O in this module) ---------------------------------

_STATE_DIR = "streamguard"
_LEDGER = "pathologies.jsonl"


def _dir():
    d = config.MEMORY_DIR / _STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def pathology_record(mon: Any, exc: BaseException, *, provider: str = "",
                     model: str = "") -> dict[str, Any]:
    """Build the structured failure record. PURE — builds, never writes."""
    kind = getattr(exc, "kind", "") or type(exc).__name__
    detail = getattr(exc, "detail", "") or str(exc)
    stats = {}
    try:
        stats = mon.stats() if mon is not None else {}
    except Exception:                       # a caller's stand-in monitor
        stats = {}
    safe: list[dict[str, Any]] = []
    try:
        safe = mon.safe_tool_calls() if mon is not None else []
    except Exception:
        safe = []
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": str(kind),
        "detail": str(detail)[:_DETAIL_MAX],
        "provider": provider or getattr(mon, "provider", "") or "",
        "model": model or getattr(mon, "model", "") or "",
        "stats": stats,
        "safe_tool_calls": [c.get("name", "") for c in safe],
    }


def record_pathology(record: dict[str, Any]) -> None:
    """Append one pathology record to MEMORY_DIR/streamguard/pathologies.jsonl.
    Serialised with proclock (the heartbeat and web processes share the file).
    NEVER raises — losing evidence must not turn into a second failure; a
    broken lock/disk is itself captured through `errors`."""
    try:
        from . import proclock
        path = _dir() / _LEDGER
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with proclock.lock("streamguard", timeout=5.0):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as err:                # noqa: BLE001 — evidence is best-effort
        try:
            from . import errors
            errors.capture("streamguard", err, context="record_pathology")
        except Exception:
            pass


def check_usage_evidence(usage: Any, *, provider: str = "", model: str = "",
                         max_tokens: int | None = None) -> str | None:
    """Non-streaming seam (`llm.complete`): run the pure impossible-usage
    detector on the final counters and record the evidence. Returns the reason
    or None. No-op when the flag is off; never raises — a completed answer is
    not retracted over a counter, but the pathology still reaches the ledger
    so provider decay stays visible."""
    try:
        if not enabled():
            return None
        reason = detect_impossible_usage(usage, max_output_tokens=max_tokens)
        if not reason:
            return None
        record_pathology(pathology_record(
            None, StreamPathology(IMPOSSIBLE_USAGE, reason),
            provider=provider, model=model))
        return reason
    except Exception:                       # noqa: BLE001 — never break a call
        return None


def recent(n: int = 50) -> list[dict[str, Any]]:
    """The most recent pathology records, oldest-first — the read side
    `modelgate`/`modelgrade` consume as provider-decay evidence. Never
    raises."""
    path = config.MEMORY_DIR / _STATE_DIR / _LEDGER
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, n):]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def kind_counts(records: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Pathology counts by kind over `records` (default: the recent ledger)."""
    rows = recent(500) if records is None else records
    counts: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("kind", "")) or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return counts
