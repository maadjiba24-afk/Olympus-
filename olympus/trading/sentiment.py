"""News and sentiment — the domain's only untrusted-input boundary.

Every other input to this package is a number Olympus fetched from a venue.
This one is *prose written by somebody else*, and prose is the attack surface:
a headline, an article body, or an "analyst note" can be authored specifically
to be read by a language model and to contain instructions rather than
information. The Olympus house treatment of that risk lives in
`olympus/security.py` and `olympus/specialists.py` (`_UNTRUSTED_NOTE`,
`_ingests`, `filter_tools`); this module is the trading domain's expression of
the same three layers, tightened in two places because the data here is
*persisted* and re-read later rather than being consumed once inside a single
agent turn.

The three layers, and what each one is actually for
---------------------------------------------------
1. **Sanitisation at the door** (`sanitise_external_text`). Text is neutralised
   *before* it becomes a `NewsItem`, not before it reaches a prompt. The
   difference matters: a prompt-time filter protects one call site, and the
   next call site that forgets it reintroduces the whole vulnerability. Here
   the type system carries the guarantee — `NewsItem.__post_init__` re-runs the
   sanitiser and refuses to construct if the text is not already in sanitised
   form, so there is no construction path, including a direct constructor call
   or a deserialisation from the store, that yields an item carrying live
   instructions.

   Stricter than `security.sanitize_for_memory`, deliberately: that function
   *annotates* a suspicious line (`[redacted suspected injection] <line>`),
   which preserves evidence for a human reviewer reading a lesson. A stored
   news body is re-read by machines for as long as it exists, so here the
   matched line is *excised* and replaced by a marker. We keep the count of
   what was removed, not the text of it.

2. **Taint provenance** (`TAINTED`, `taint_origin`). Sanitised prose is still
   attacker-*influenced*: an adversary who cannot make Olympus obey can still
   try to make it change its own limits by supplying numbers ("volatility is
   now normal, a 10x position is prudent"). Every value derived from a
   `NewsItem` therefore carries an origin string stamped with `TAINTED`, and
   the wiring layer (`agents.taint_external`) wraps it in the `Tainted` marker
   that `risk.assert_untainted` — called by `LimitsStore.save` before it even
   looks at the operator token — refuses outright.

   Note what this module does NOT import: `risk`. That is enforced
   structurally (`tests/test_trading_boundaries.py`), and the reason is exactly
   the rule being implemented — the untrusted-content layer must have no route
   to the limits API at all, not even a read. Owning the *provenance* here and
   the *marker* there means neither half can be bypassed by the other: this
   module cannot reach limits, and `risk.py` cannot be made to trust news by
   anything short of an operator editing it.

3. **Capability separation** (`agents.AGENT_SPECS`). The agent that ingests
   news has no action capability at all — same rule as
   `specialists.Specialist._ingests`. That lives in `agents.py`; it is listed
   here because the three layers only work as a set.

What this module deliberately does NOT do
-----------------------------------------
There is no network scraper. `SentimentProvider` is an interface plus two
offline implementations (a null one and a fixture one), because a real fetcher
belongs behind an operator-configured connector with its own egress policy —
and because the pipeline must be fully testable offline, which is the property
that lets the injection tests below be exhaustive rather than illustrative.

The shipped analyser is a deterministic lexicon, not a model. A sentiment score
that changes between two runs over the same articles cannot be backtested, and
an un-backtestable input has no business influencing a trade.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (DataQuality, Signal, SignalDirection, ensure_utc,
                        jsonable)
from .errors import DataValidationError
# Deliberately absent: `from .risk import ...`. The untrusted-content layer has
# no import route to the limits API, enforced by the layer-boundary test. See
# the module docstring, layer 2.

#: Hard cap on any single stored text. An article body is information; a
#: 400 KB "article" is a payload. Truncation is recorded, never silent.
MAX_TEXT_LENGTH = 8_000
MAX_HEADLINE_LENGTH = 400
MAX_URL_LENGTH = 2_000

#: The prefix stamped onto the origin of every value derived from external
#: content. Grepping for it in an audit trail answers "did external content
#: touch this number?".
TAINTED = "TAINTED"

#: The replacement written in place of excised material. Chosen so that it
#: matches none of the detectors below — that is what makes the sanitiser
#: idempotent, and idempotence is what lets `NewsItem.__post_init__` verify
#: sanitisation by re-running it.
MARK_INSTRUCTION = "[removed:instruction]"
MARK_ROLE_TAG = "[removed:role-tag]"
MARK_FENCE = "[removed:fenced-block]"
MARK_COMMENT = "[removed:comment]"
MARK_TRUNCATED = "[truncated]"


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------
# Every pattern below is written to be *over*-inclusive. A false positive costs
# one excised line of a news article, which at worst nudges a sentiment score.
# A false negative costs a live instruction sitting in the store, read by every
# future analysis run. The asymmetry is not close, so the patterns lean hard
# toward removal.

#: Characters that are invisible or that reorder rendering. Legitimate prose
#: never needs them; inside an injection they defeat a regex scan (a zero-width
#: space inside "ignore previous instructions") and make stored text render
#: differently from how it compares. Same set as `security._INVISIBLE_RE`.
_INVISIBLE_RE = re.compile(
    "[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")

#: Control characters other than tab/newline. A carriage-return trick can hide
#: a line from a terminal reader while leaving it in the stored text.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: Chat/completion role framing in every shape we have seen in the wild:
#: ChatML, Llama/Mistral instruction tags, Anthropic-style Human/Assistant
#: turns, XML-ish system tags, and markdown role headers. These are how a
#: document tries to *end* the document and start being a conversation.
_ROLE_TAG_RE = re.compile(
    r"""(?ix)
    (?: <\|\s*(?:im_start|im_end|endoftext|system|user|assistant|
                 start_header_id|end_header_id|eot_id)\s*\|>
      | \[/?\s*(?:INST|SYS|SYSTEM|ASSISTANT|USER)\s*\]
      | </?\s*(?:system|assistant|user|human|instructions?|prompt|
                 tool_call|function_call|untrusted_external_content)\s*>
      | ^\s{0,3}\#{1,6}\s*(?:system|assistant|user|human)\s*:?\s*$
      | ^\s{0,3}(?:system|assistant|human)\s*:\s*$
    )
    """, re.MULTILINE)

#: Fenced blocks whose info string claims to be a system/prompt/instruction
#: block. The content of an ordinary ```python fence is data; the content of a
#: ```system fence is a document asserting privilege it does not have.
_FENCE_RE = re.compile(
    r"(?is)```[ \t]*(?:system|prompt|instruction[s]?|developer|tool|config)"
    r"\b.*?(?:```|\Z)")

#: HTML/markdown comments. The classic "invisible to the reader, visible to the
#: model" carrier.
_COMMENT_RE = re.compile(r"(?s)<!--.*?(?:-->|\Z)")

#: Imperative shapes. Grouped by intent so a reviewer can see *why* each is
#: here, and so a new family can be added without re-reading the whole regex.
_INSTRUCTION_PATTERNS = (
    # (a) Override the standing task. The canonical injection.
    r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|above|earlier|preceding|foregoing)\s+"
    r"(?:instruction|prompt|rule|direction|message|context)",
    r"disregard\s+(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|above|earlier|instruction|prompt|rule|system)",
    r"forget\s+(?:everything|all|your|the)\s+"
    r"(?:previous|prior|above|instruction|rule|you)",
    r"override\s+(?:your|the|all|any)\s+"
    r"(?:instruction|rule|system|prompt|safety|limit|guardrail)",
    # (b) Re-declare the model's identity or its governing text.
    r"you\s+are\s+now\b",
    r"from\s+now\s+on[, ]+you\b",
    r"new\s+(?:instruction|rule|directive|system\s+prompt)s?\s*:",
    r"(?:system|developer)\s+(?:prompt|message|instruction)s?\s*:",
    r"(?:act|behave)\s+as\s+(?:if\s+you\s+are\s+)?(?:an?\s+)?"
    r"(?:admin|administrator|operator|developer|root|unrestricted)",
    r"pretend\s+(?:to\s+be|you\s+are)\b",
    r"(?:enter|enable|switch\s+to)\s+(?:developer|debug|god|dan)\s+mode",
    # (c) Ask for an action. No news article needs the reader to do anything.
    r"(?:send|forward|email|post|upload|exfiltrate)\s+(?:the\s+|your\s+|all\s+)*"
    r"(?:api\s+key|key|token|secret|credential|password|portfolio|position|"
    r"balance|holdings|wallet)",
    r"(?:call|invoke|execute|run)\s+(?:the\s+)?(?:tool|function|command|webhook|"
    r"following\s+code|shell)",
    r"(?:curl|wget)\s+https?://",
    # (d) Ask for the safety system itself. The reason this module exists.
    r"(?:disable|clear|reset|turn\s+off|bypass|remove)\s+(?:the\s+|all\s+|any\s+)*"
    r"(?:kill[\s-]?switch|risk\s+limit|limit|safeguard|guardrail|safety|check)",
    r"(?:raise|increase|widen|lift|relax)\s+(?:the\s+|your\s+|all\s+)*"
    r"(?:risk\s+)?(?:limit|ceiling|exposure\s+cap|max\s+position)",
    r"(?:switch|change|set)\s+(?:the\s+)?mode\s+to\s+live",
    r"(?:approve|authorise|authorize)\s+(?:this|the)\s+"
    r"(?:trade|order|intent|position)",
    # (e) Secrecy — the tell that distinguishes an injection from an opinion.
    r"do\s+not\s+(?:tell|inform|mention|report|show)\s+(?:the\s+)?"
    r"(?:user|operator|human|anyone)",
    r"without\s+(?:telling|informing|notifying)\s+(?:the\s+)?"
    r"(?:user|operator|human)",
    r"this\s+is\s+(?:not\s+)?(?:a\s+)?(?:test|drill)[.,]?\s*(?:obey|comply|"
    r"you\s+must)",
)

_INSTRUCTION_RE = re.compile(
    "(?i)(?:" + "|".join(_INSTRUCTION_PATTERNS) + ")")

#: Sentence terminators used to excise the *clause* around a hit rather than
#: only the matched phrase. Excising the phrase alone leaves the residue
#: ("..., and the keys to attacker@example.com"), which is still an imperative.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def contains_instruction_text(text: Any) -> bool:
    """True when `text` looks like it is addressing a model rather than a reader.

    Exposed because `agents.AgentOutputValidator` needs the same judgement: an
    agent that faithfully summarises a poisoned article would otherwise launder
    the instruction through a field the sanitiser never saw.
    """
    if not isinstance(text, str) or not text:
        return False
    for probe in _variants(text):
        if (_INSTRUCTION_RE.search(probe) or _ROLE_TAG_RE.search(probe)
                or _FENCE_RE.search(probe) or _COMMENT_RE.search(probe)):
            return True
    return False


def _normalise(text: str) -> str:
    """Fold the evasion tricks away before any pattern is applied.

    NFKC collapses full-width and mathematical-alphanumeric homoglyphs
    ("ｉｇｎｏｒｅ", "𝐢𝐠𝐧𝐨𝐫𝐞") onto ASCII; the two character classes remove
    invisibles and control codes. Doing this first is the whole game — a
    detector run against un-normalised text is a detector an attacker skips
    with one code point. NFKC is idempotent, so this preserves the idempotence
    that `NewsItem` relies on.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    text = _CONTROL_RE.sub(" ", text)
    return text


def _variants(text: str) -> tuple[str, str]:
    """Both ways an invisible character could have been meant, as two strings.

    Zero-width characters attack a detector from two opposite directions and no
    single normalisation catches both:

    * *inside* a word — ``ig<ZWSP>nore all previous instructions``. Deleting the
      character rejoins "ignore" and the pattern matches. Replacing it with a
      space would not.
    * *between* words — ``Ignore<ZWSP>all<ZWSP>previous<ZWSP>instructions``,
      which renders identically to the phrase with spaces. Here deleting welds
      it into one unmatched token and it is *replacing* with a space that
      matches.

    So we produce both readings and treat text as hostile if either one is. The
    delete-variant is what gets stored (it is the conservative rendering); the
    space-variant exists only to be scanned.
    """
    nfkc = unicodedata.normalize("NFKC", text)
    nfkc = _CONTROL_RE.sub(" ", nfkc)
    return (_INVISIBLE_RE.sub("", nfkc), _INVISIBLE_RE.sub(" ", nfkc))


# ---------------------------------------------------------------------------
# the sanitiser
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sanitisation:
    """What the sanitiser did, as a record rather than a log line.

    Kept because "we removed three instruction clauses from this article"
    changes how much an operator trusts a sentiment score, and because a spike
    in removals across a feed is itself the signal that the feed is under
    attack. The removed *text* is not kept: storing it re-creates the problem
    one indirection away.
    """
    text: str
    original_length: int
    truncated: bool = False
    #: Counts by marker kind, e.g. {"instruction": 2, "role-tag": 1}.
    removed: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "removed", dict(self.removed))

    @property
    def clean(self) -> bool:
        """True when nothing had to be removed or truncated."""
        return not self.removed and not self.truncated

    @property
    def removed_total(self) -> int:
        return sum(self.removed.values())

    def to_dict(self) -> dict:
        return {"text": self.text, "original_length": self.original_length,
                "truncated": self.truncated, "removed": dict(self.removed),
                "clean": self.clean}


def sanitise_external_text(text: Any, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Neutralise external prose. The only way text enters this domain.

    Returns the cleaned string. Use `sanitise_external_text_report` when the
    caller needs to know what was taken out.

    **Idempotent by construction**: every removal writes a marker that matches
    none of the detectors, so `f(f(x)) == f(x)`. That property is not cosmetic
    — `NewsItem.__post_init__` verifies sanitisation by re-running this
    function and comparing, which is only sound if a second pass is a no-op.
    """
    return sanitise_external_text_report(text, max_length=max_length).text


def sanitise_external_text_report(text: Any, *,
                                  max_length: int = MAX_TEXT_LENGTH) -> Sanitisation:
    """`sanitise_external_text`, plus the record of what it removed."""
    if text is None:
        return Sanitisation(text="", original_length=0)
    if not isinstance(text, str):
        raise DataValidationError("external text must be a string",
                                  got=type(text).__name__)
    original_length = len(text)
    removed: dict[str, int] = {}

    def _count(key: str, n: int) -> None:
        if n:
            removed[key] = removed.get(key, 0) + n

    instruction_hits = 0

    # Separator-evasion pre-pass, before anything else touches the text: a
    # phrase spelled with zero-width characters instead of spaces only matches
    # in the *space* reading, and that reading is destroyed by the very
    # normalisation every later step depends on. Line granularity (rather than
    # clause) because the two readings can split into different numbers of
    # clauses, and a mismatched excision is worse than a coarse one. Both
    # readings share a line structure, since no invisible character is a
    # newline.
    joined_variant, spaced_variant = _variants(text)
    lines: list[str] = []
    for joined_line, spaced_line in zip(joined_variant.split("\n"),
                                        spaced_variant.split("\n")):
        if _INSTRUCTION_RE.search(spaced_line) \
                and not _INSTRUCTION_RE.search(joined_line):
            instruction_hits += 1
            lines.append(MARK_INSTRUCTION)
        else:
            lines.append(joined_line)
    body = "\n".join(lines)

    # Order matters. Structural carriers (comments, fences, role tags) go first
    # so that an imperative hidden *inside* one is removed with its container
    # rather than being re-examined line by line after the container is gone.
    body, n = _COMMENT_RE.subn(MARK_COMMENT, body)
    _count("comment", n)
    body, n = _FENCE_RE.subn(MARK_FENCE, body)
    _count("fenced-block", n)
    body, n = _ROLE_TAG_RE.subn(MARK_ROLE_TAG, body)
    _count("role-tag", n)

    # Then imperatives, clause by clause. Splitting on sentence boundaries
    # inside each line keeps the surrounding (legitimate) reporting intact
    # while removing the whole imperative, not just the phrase that matched.
    lines = []
    for line in body.split("\n"):
        if not _INSTRUCTION_RE.search(line):
            lines.append(line)
            continue
        kept: list[str] = []
        for clause in _SENTENCE_SPLIT_RE.split(line):
            if _INSTRUCTION_RE.search(clause):
                instruction_hits += 1
                kept.append(MARK_INSTRUCTION)
            else:
                kept.append(clause)
        lines.append(" ".join(part for part in kept if part))
    _count("instruction", instruction_hits)
    body = "\n".join(lines)

    # Secret redaction reuses the house implementation so there is exactly one
    # place that knows what a credential looks like. `redact_pii=False` is
    # explicit: the default consults an environment variable, and a sanitiser
    # whose output depends on ambient config cannot support the idempotence
    # check that `NewsItem` performs.
    try:
        from olympus import security
        redacted = security.sanitize_for_prompt(body, redact_pii=False)
    except Exception:                                    # noqa: BLE001
        # A missing or broken security module must not become a path that
        # skips sanitisation entirely — the injection layers above already ran,
        # and this layer is additive.
        redacted = body
    if redacted != body:
        _count("secret", 1)
    body = redacted

    # Collapse the runs of blank space that excision leaves behind, so the
    # stored text stays readable and, more importantly, so the result is
    # stable under a second pass.
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = "\n".join(line.strip() for line in body.split("\n")).strip()

    truncated = False
    if len(body) > max_length:
        # Reserve room for the marker so the RESULT still fits under the cap.
        # Without that reservation a second pass would truncate the already
        # truncated text again, and the idempotence `NewsItem` depends on would
        # quietly fail for exactly the oversized documents most likely to be
        # hostile. Then back off to the last whitespace, so the tail is not a
        # half-token that happens to look like something else.
        room = max(1, max_length - len(MARK_TRUNCATED) - 1)
        cut = body[:room]
        space = cut.rfind(" ")
        if space > room // 2:
            cut = cut[:space]
        body = (cut.rstrip() + " " + MARK_TRUNCATED).strip()
        truncated = True

    return Sanitisation(text=body, original_length=original_length,
                        truncated=truncated, removed=removed)


_URL_RE = re.compile(r"^https?://[^\s<>\"'\\{}|^`\x00-\x20]{1,1990}$")


def sanitise_url(url: Any) -> str:
    """Return a safe, storable URL or raise.

    A URL is text too: query strings carry injections, and `javascript:` /
    `data:` schemes carry payloads. Only http(s) survives, whitespace and
    control characters are rejected outright rather than stripped (a URL that
    needed stripping was not the URL the publisher meant), and the length cap
    keeps a data-URI-sized blob out of the store. Idempotent: a URL this
    function returns passes it unchanged.
    """
    if url is None or url == "":
        return ""
    if not isinstance(url, str):
        raise DataValidationError("url must be a string",
                                  got=type(url).__name__)
    # Rejected, not stripped. Everywhere else this module removes what it does
    # not like, because the surrounding prose is still worth reading. A URL is
    # atomic: silently deleting a bidi override or folding a homoglyph yields a
    # *different* URL from the one the publisher wrote, and storing that as
    # though it were the source is worse than storing no source at all.
    if _INVISIBLE_RE.search(url) or _CONTROL_RE.search(url):
        raise DataValidationError(
            "url contains invisible or control characters", field="url",
            got=url[:120])
    candidate = url.strip()
    if unicodedata.normalize("NFKC", candidate) != candidate:
        raise DataValidationError(
            "url contains characters that normalise to something else "
            "(homoglyph or compatibility form)", field="url", got=candidate[:120])
    if len(candidate) > MAX_URL_LENGTH:
        raise DataValidationError("url exceeds the maximum length",
                                  field="url", got=len(candidate))
    if not _URL_RE.match(candidate):
        raise DataValidationError(
            "url must be a plain http(s) URL with no whitespace or control "
            "characters", field="url", got=candidate[:120])
    if contains_instruction_text(candidate):
        raise DataValidationError("url carries instruction-shaped text",
                                  field="url", got=candidate[:120])
    return candidate


def raw_digest(*parts: Any) -> str:
    """SHA-256 over the *pre-sanitisation* text.

    The sanitised body is what Olympus reads; this is what proves which
    original document produced it. Without it an operator investigating a
    suspicious score cannot tell a benign article that tripped a pattern from
    an article that really was an attack.
    """
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part if part is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# taint
# ---------------------------------------------------------------------------

def taint_origin(kind: str, ident: str = "") -> str:
    """The provenance string for a value derived from external content.

    Half of the taint mechanism, and deliberately the half that lives down
    here: producing an origin needs to know *what the content was*, which only
    this layer knows. Applying the marker needs to know about `risk.Tainted`,
    which this layer must not import — `agents.taint_external` does that. Split
    that way, the untrusted layer can label its own output but has no code path
    to the limits API, which is the property the layer-boundary test pins.
    """
    return f"{TAINTED}:{kind}:{ident}" if ident else f"{TAINTED}:{kind}"


# ---------------------------------------------------------------------------
# news items
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsItem:
    """One external document, already neutralised.

    Construct with `NewsItem.create`. The direct constructor is not private —
    deserialisation needs it — but it *re-verifies* sanitisation rather than
    trusting the caller, so passing raw text to it raises rather than
    producing an item that carries live instructions. That is the invariant
    this class exists to hold: **there is no NewsItem, anywhere, whose text has
    not been through `sanitise_external_text`.**
    """
    item_id: str
    source: str
    url: str
    headline: str
    body: str
    published_at: datetime
    instruments: tuple[str, ...] = ()
    language: str = "en"
    #: Digest of the ORIGINAL text, before sanitisation (see `raw_digest`).
    raw_hash: str = ""
    #: Counts of what the sanitiser removed, by kind. Empty for clean text.
    redactions: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if not self.item_id or not str(self.item_id).strip():
            raise DataValidationError("news item_id must be non-empty")
        object.__setattr__(self, "published_at",
                           ensure_utc(self.published_at, field_name="published_at"))
        object.__setattr__(self, "instruments", tuple(self.instruments))
        object.__setattr__(self, "redactions", dict(self.redactions))
        object.__setattr__(self, "url", sanitise_url(self.url))
        # The invariant check. Re-running the sanitiser is cheap and is the
        # only way to make the guarantee hold for EVERY construction path,
        # including a future one nobody has written yet.
        for name, cap in (("source", MAX_HEADLINE_LENGTH),
                          ("headline", MAX_HEADLINE_LENGTH),
                          ("body", MAX_TEXT_LENGTH),
                          ("language", 16)):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise DataValidationError(f"{name} must be a string",
                                          field=name, got=type(value).__name__)
            if sanitise_external_text(value, max_length=cap) != value:
                raise DataValidationError(
                    f"{name} has not been sanitised; build a NewsItem with "
                    "NewsItem.create() so external text passes through "
                    "sanitise_external_text()", field=name)
        for symbol in self.instruments:
            if not isinstance(symbol, str) or not symbol.strip():
                raise DataValidationError(
                    "instrument keys must be non-empty strings",
                    field="instruments")
            if contains_instruction_text(symbol):
                # An instrument key is used as a dict key and echoed into audit
                # records; it is a channel like any other.
                raise DataValidationError(
                    "instrument key carries instruction-shaped text",
                    field="instruments", got=symbol[:80])

    @classmethod
    def create(cls, *, item_id: str, source: str, headline: str,
               body: str = "", published_at: datetime, url: str = "",
               instruments: Iterable[str] = (), language: str = "en",
               ) -> "NewsItem":
        """The sanitising constructor. The intended entry point for all callers."""
        digest = raw_digest(source, headline, body, url)
        head = sanitise_external_text_report(headline,
                                             max_length=MAX_HEADLINE_LENGTH)
        text = sanitise_external_text_report(body, max_length=MAX_TEXT_LENGTH)
        src = sanitise_external_text_report(source, max_length=MAX_HEADLINE_LENGTH)
        lang = sanitise_external_text(language or "en", max_length=16)
        removed: dict[str, int] = {}
        for report in (head, text, src):
            for key, count in report.removed.items():
                removed[key] = removed.get(key, 0) + count
            if report.truncated:
                removed["truncated"] = removed.get("truncated", 0) + 1
        return cls(item_id=str(item_id), source=src.text, url=url,
                   headline=head.text, body=text.text, published_at=published_at,
                   instruments=tuple(str(s) for s in instruments),
                   language=lang, raw_hash=digest, redactions=removed)

    @property
    def was_redacted(self) -> bool:
        return bool(self.redactions)

    @property
    def text(self) -> str:
        """Headline and body together — what an analyser reads."""
        return f"{self.headline}\n{self.body}".strip()

    def to_dict(self) -> dict:
        return {"item_id": self.item_id, "source": self.source, "url": self.url,
                "headline": self.headline, "body": self.body,
                "published_at": jsonable(self.published_at),
                "instruments": list(self.instruments), "language": self.language,
                "raw_hash": self.raw_hash, "redactions": dict(self.redactions)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NewsItem":
        """Rebuild from the store. Still goes through `__post_init__`, so a
        tampered store row is rejected rather than trusted."""
        return cls(
            item_id=data["item_id"], source=data.get("source", ""),
            url=data.get("url", ""), headline=data.get("headline", ""),
            body=data.get("body", ""),
            published_at=datetime.fromisoformat(data["published_at"]),
            instruments=tuple(data.get("instruments") or ()),
            language=data.get("language", "en"),
            raw_hash=data.get("raw_hash", ""),
            redactions=dict(data.get("redactions") or {}))

    @property
    def taint_origin(self) -> str:
        """Provenance for anything derived from this item. Hand it, with the
        derived value, to `agents.taint_external`."""
        return taint_origin("news", self.item_id)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

class SentimentProvider(ABC):
    """Source of `NewsItem`s. Implementations MUST sanitise.

    The contract is narrow on purpose: `fetch` returns already-neutralised
    items, so no caller has to remember to sanitise and no caller *can* forget
    — `NewsItem` refuses to exist otherwise.

    No networked implementation ships in this package. A real feed belongs
    behind an operator-configured connector with its own egress allowlist
    (`olympus.security.assert_egress_allowed`), and wiring one in here would
    make this module untestable offline, which is precisely where the
    injection tests need to run.
    """

    #: Stable identifier written into audit records.
    name: str = "provider"

    @abstractmethod
    def fetch(self, instruments: Sequence[str],
              since: datetime | None = None) -> list[NewsItem]:
        """Items mentioning any of `instruments`, published at/after `since`."""

    def health(self) -> dict:
        """Overridable liveness probe. The monitor calls this and never trusts
        it to be quick or correct — see `monitor._probe`."""
        return {"name": self.name, "ok": True}


class NullSentimentProvider(SentimentProvider):
    """Returns nothing, always.

    The default. A deployment with no configured news feed must behave like a
    deployment with an *empty* news feed — never like one whose sentiment
    layer silently fails open into "neutral, high confidence".
    """

    name = "null"

    def fetch(self, instruments: Sequence[str],
              since: datetime | None = None) -> list[NewsItem]:
        return []


class StaticSentimentProvider(SentimentProvider):
    """A fixed corpus. Test fixture and replay source.

    Accepts raw dicts and sanitises them on the way in, which makes it the
    natural place to point an injection test: a fixture built from hostile raw
    text must still yield harmless items.
    """

    name = "static"

    def __init__(self, items: Iterable[NewsItem] = ()):
        self._items: tuple[NewsItem, ...] = tuple(items)

    @classmethod
    def from_raw(cls, records: Iterable[Mapping[str, Any]]) -> "StaticSentimentProvider":
        """Build from untrusted raw records, sanitising each."""
        return cls(NewsItem.create(**dict(record)) for record in records)

    @property
    def items(self) -> tuple[NewsItem, ...]:
        return self._items

    def fetch(self, instruments: Sequence[str],
              since: datetime | None = None) -> list[NewsItem]:
        wanted = {str(i) for i in instruments} if instruments else None
        cutoff = ensure_utc(since, field_name="since") if since else None
        out = []
        for item in self._items:
            if wanted is not None and not (wanted & set(item.instruments)):
                continue
            if cutoff is not None and item.published_at < cutoff:
                continue
            out.append(item)
        # Deterministic ordering: the analyser sums floats in list order, so an
        # unstable sort would make the score irreproducible.
        return sorted(out, key=lambda i: (i.published_at, i.item_id))


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SentimentResult:
    """An aggregate opinion about one instrument. Never a trade instruction.

    `score` and `confidence` are ordinary floats rather than marked values
    because they are meant to be *used* — by signal generation, which is
    downstream of risk and cannot change a limit. Anything handing them toward
    something configuration-shaped must wrap them first, using `.taint_origin`
    and `agents.taint_external`; `risk.assert_untainted` then refuses them.
    """
    instrument_key: str
    score: float                      # [-1, 1]; negative is bearish
    confidence: float                 # [0, 1]
    volume: int                       # number of items behind the score
    ts: datetime
    items_considered: tuple[str, ...] = ()      # item_ids, for traceability
    warnings: tuple[str, ...] = ()
    model_version: str = ""

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        score = float(self.score)
        confidence = float(self.confidence)
        if not -1.0 <= score <= 1.0:
            raise DataValidationError("sentiment score must be within [-1, 1]",
                                      field="score", got=score)
        if not 0.0 <= confidence <= 1.0:
            raise DataValidationError("confidence must be within [0, 1]",
                                      field="confidence", got=confidence)
        if int(self.volume) < 0:
            raise DataValidationError("volume must be >= 0", field="volume",
                                      got=self.volume)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "volume", int(self.volume))
        object.__setattr__(self, "items_considered", tuple(self.items_considered))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def direction(self) -> SignalDirection:
        """Coarse direction. The dead band is deliberate — a score of 0.02 over
        two articles is noise, and turning noise into LONG is how a sentiment
        layer becomes a random-trade generator."""
        if self.score >= 0.15:
            return SignalDirection.LONG
        if self.score <= -0.15:
            return SignalDirection.SHORT
        return SignalDirection.FLAT

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key, "score": self.score,
                "confidence": self.confidence, "volume": self.volume,
                "ts": jsonable(self.ts),
                "items_considered": list(self.items_considered),
                "warnings": list(self.warnings),
                "model_version": self.model_version}

    @property
    def taint_origin(self) -> str:
        return taint_origin("sentiment", self.instrument_key)

    def to_signal(self, *, horizon: int = 0,
                  data_quality: DataQuality = DataQuality.OK) -> Signal:
        """Express this as a `Signal` — an opinion, in the one shape the
        strategy layer accepts. Note what this does NOT return: a TradeIntent.
        External content reaches the trading pipeline only as one voice among
        several, and never as an order."""
        direction = self.direction
        strength = self.score
        if direction is SignalDirection.FLAT:
            strength = 0.0
        return Signal(
            source="sentiment", instrument_key=self.instrument_key,
            direction=direction, strength=strength, confidence=self.confidence,
            ts=self.ts, horizon=horizon,
            rationale=f"lexicon sentiment over {self.volume} item(s)",
            features={"score": self.score, "volume": self.volume},
            model_version=self.model_version, data_quality=data_quality)


# ---------------------------------------------------------------------------
# analysers
# ---------------------------------------------------------------------------

class SentimentAnalyzer(ABC):
    """Turns items into a score. Deterministic implementations only, please.

    An LLM-backed analyser is permitted by this interface but must satisfy the
    same contract: same items in, same result out, or the backtest that
    justified trading on sentiment proved nothing about the live system.
    """

    model_version: str = "abstract"

    @abstractmethod
    def analyse(self, instrument_key: str, items: Sequence[NewsItem], *,
                ts: datetime | None = None) -> SentimentResult:
        ...


#: Weighted lexicon. Small, auditable, and biased toward market vocabulary
#: rather than general affect — "fine" is negative in a regulatory headline and
#: positive in prose, so the trading sense wins. Weights above 1.0 are reserved
#: for words that are near-decisive on their own.
POSITIVE_LEXICON: Mapping[str, float] = {
    "gain": 1.0, "gains": 1.0, "gained": 1.0, "surge": 1.5, "surges": 1.5,
    "surged": 1.5, "rally": 1.2, "rallies": 1.2, "rallied": 1.2,
    "profit": 1.0, "profits": 1.0, "profitable": 1.0,
    "beat": 1.2, "beats": 1.2, "upgrade": 1.3, "upgraded": 1.3,
    "outperform": 1.2, "outperformed": 1.2, "strong": 0.8, "stronger": 0.8,
    "growth": 0.8, "bullish": 1.3, "soar": 1.5, "soars": 1.5, "soared": 1.5,
    "jump": 1.0, "jumps": 1.0, "jumped": 1.0, "rise": 0.8, "rises": 0.8,
    "rose": 0.8, "expand": 0.6, "expansion": 0.6, "approval": 1.0,
    "approved": 1.0, "win": 0.8, "wins": 0.8, "won": 0.8, "boost": 1.0,
    "boosted": 1.0, "optimistic": 0.9, "exceeds": 1.1, "exceeded": 1.1,
    "tops": 1.0, "topped": 1.0, "raised": 0.7, "dividend": 0.5,
    "buyback": 0.9, "breakthrough": 1.2, "recovery": 0.9, "rebound": 1.0,
}

NEGATIVE_LEXICON: Mapping[str, float] = {
    "loss": 1.0, "losses": 1.0, "lost": 0.9, "fall": 0.9, "falls": 0.9,
    "fell": 0.9, "drop": 1.0, "drops": 1.0, "dropped": 1.0, "plunge": 1.5,
    "plunges": 1.5, "plunged": 1.5, "slump": 1.3, "slumps": 1.3,
    "slumped": 1.3, "miss": 1.1, "misses": 1.1, "missed": 1.1,
    "downgrade": 1.3, "downgraded": 1.3, "underperform": 1.2,
    "underperformed": 1.2, "weak": 0.9, "weakness": 0.9, "weaker": 0.9,
    "bearish": 1.3, "crash": 1.6, "crashes": 1.6, "crashed": 1.6,
    "decline": 1.0, "declines": 1.0, "declined": 1.0, "cut": 0.7,
    "cuts": 0.7, "layoff": 1.2, "layoffs": 1.2, "probe": 1.1,
    "lawsuit": 1.2, "fraud": 2.0, "bankruptcy": 2.0, "bankrupt": 2.0,
    "default": 1.5, "defaults": 1.5, "recall": 1.2, "warning": 1.0,
    "warns": 1.0, "warned": 1.0, "halt": 1.1, "halted": 1.1,
    "investigation": 1.2, "fined": 1.3, "sink": 1.0, "sinks": 1.0,
    "sank": 1.0, "tumble": 1.3, "tumbles": 1.3, "tumbled": 1.3,
    "delisting": 1.6, "delisted": 1.6, "resigns": 0.9, "resigned": 0.9,
    "shortfall": 1.2, "writedown": 1.3, "impairment": 1.1,
}

#: Words that invert the next scored token. "not profitable" scoring positive
#: is the single most embarrassing failure mode of a bag-of-words lexicon.
NEGATORS = frozenset({"not", "no", "never", "without", "hardly", "barely",
                      "fails", "failed", "unable", "denies", "denied"})

#: Words that amplify the next scored token.
INTENSIFIERS: Mapping[str, float] = {
    "very": 1.5, "sharply": 1.6, "significantly": 1.4, "massively": 1.8,
    "deeply": 1.4, "severely": 1.7, "record": 1.3, "unexpectedly": 1.3,
}

#: How far a negator or intensifier reaches. Three tokens covers "not
#: especially strong" without letting a negator at the start of a sentence
#: flip a word at the end of it.
_MODIFIER_WINDOW = 3

_TOKEN_RE = re.compile(r"[a-z][a-z'-]*")

#: Smoothing in the score denominator. Its job is to stop a single matched word
#: from producing a score of ±1.0: one article saying "shares fell" is weak
#: evidence, and a saturated score would let it dominate a blended signal.
_SCORE_SMOOTHING = 3.0
#: Same idea for confidence — evidence has to accumulate before the analyser
#: claims to be sure.
_CONFIDENCE_HALF_WEIGHT = 4.0


class LexiconSentimentAnalyzer(SentimentAnalyzer):
    """Deterministic bag-of-words scoring with negation and intensifiers.

    Chosen over a model for one reason: reproducibility. This function is a
    pure map from (items, lexicon) to a score, so a backtest that includes
    sentiment is replayable years later, and a surprising score is explainable
    by pointing at the words that produced it — neither of which is true of a
    model behind an API.

    Its accuracy ceiling is low, and that is fine. Sentiment enters the
    pipeline as one `Signal` among several and is gated by the risk engine like
    everything else; the failure mode it must not have is *irreproducibility*.
    """

    model_version = "lexicon-1"

    def __init__(self, *, clock: Clock | None = None,
                 positive: Mapping[str, float] | None = None,
                 negative: Mapping[str, float] | None = None):
        self.clock = clock or default_clock()
        self.positive = dict(POSITIVE_LEXICON if positive is None else positive)
        self.negative = dict(NEGATIVE_LEXICON if negative is None else negative)

    # -- scoring ----------------------------------------------------------

    def score_text(self, text: str) -> tuple[float, float]:
        """Return `(net, magnitude)` for one document.

        `net` is signed evidence, `magnitude` is total evidence. Keeping both
        is what lets the aggregate distinguish "nothing said" (magnitude 0)
        from "said both things" (magnitude high, net ~0) — two situations with
        very different meanings that a single number conflates.
        """
        tokens = _TOKEN_RE.findall(_normalise(text).lower())
        net = 0.0
        magnitude = 0.0
        pending_negation = 0
        pending_boost = 0.0
        boost_ttl = 0
        for token in tokens:
            if token in NEGATORS:
                pending_negation = _MODIFIER_WINDOW
                continue
            if token in INTENSIFIERS:
                pending_boost = INTENSIFIERS[token]
                boost_ttl = _MODIFIER_WINDOW
                continue
            weight = self.positive.get(token)
            sign = 1.0
            if weight is None:
                weight = self.negative.get(token)
                sign = -1.0
            if weight is None:
                if pending_negation:
                    pending_negation -= 1
                if boost_ttl:
                    boost_ttl -= 1
                    if not boost_ttl:
                        pending_boost = 0.0
                continue
            if boost_ttl and pending_boost:
                weight *= pending_boost
                pending_boost = 0.0
                boost_ttl = 0
            if pending_negation:
                sign = -sign
                pending_negation = 0
            net += sign * weight
            magnitude += weight
        return net, magnitude

    def analyse(self, instrument_key: str, items: Sequence[NewsItem], *,
                ts: datetime | None = None) -> SentimentResult:
        when = ensure_utc(ts, field_name="ts") if ts else self.clock.now()
        warnings: list[str] = []
        net = 0.0
        magnitude = 0.0
        considered: list[str] = []
        redacted_items = 0
        for item in items:
            if not isinstance(item, NewsItem):
                # The type check is the boundary. Accepting a bare string here
                # would let a caller route unsanitised text straight into the
                # analyser and out the other side as a "score" nobody suspects.
                raise DataValidationError(
                    "sentiment analysis accepts NewsItem only; build one with "
                    "NewsItem.create() so the text is sanitised",
                    got=type(item).__name__)
            item_net, item_magnitude = self.score_text(item.text)
            net += item_net
            magnitude += item_magnitude
            considered.append(item.item_id)
            if item.was_redacted:
                redacted_items += 1

        volume = len(considered)
        if volume == 0:
            warnings.append("no_items")
        if redacted_items:
            # Surfaced rather than swallowed: a burst of redactions across a
            # feed is what an attack on the sentiment layer looks like from the
            # inside, and it belongs in the monitor's alerts, not in a log line.
            warnings.append(f"instruction_text_removed_from_{redacted_items}_items")
        if magnitude == 0.0 and volume:
            warnings.append("no_lexicon_matches")

        score = 0.0 if magnitude == 0.0 else net / (magnitude + _SCORE_SMOOTHING)
        score = max(-1.0, min(1.0, score))
        coverage = magnitude / (magnitude + _CONFIDENCE_HALF_WEIGHT)
        breadth = volume / (volume + 2.0)
        confidence = coverage * (0.5 + 0.5 * breadth)
        return SentimentResult(
            instrument_key=instrument_key, score=round(score, 6),
            confidence=round(max(0.0, min(1.0, confidence)), 6),
            volume=volume, ts=when, items_considered=tuple(considered),
            warnings=tuple(warnings), model_version=self.model_version)


def analyse_all(analyzer: SentimentAnalyzer, provider: SentimentProvider,
                instruments: Sequence[str], *, since: datetime | None = None,
                ts: datetime | None = None) -> dict[str, SentimentResult]:
    """One result per instrument, from one provider fetch.

    A convenience, but a load-bearing one: it fetches once and partitions
    locally, so every instrument in a batch is scored against the same corpus
    snapshot. Fetching per instrument would let the feed move underneath the
    batch and produce a set of scores that never coexisted.
    """
    items = provider.fetch(instruments, since)
    out: dict[str, SentimentResult] = {}
    for key in instruments:
        relevant = [i for i in items if key in i.instruments]
        out[key] = analyzer.analyse(key, relevant, ts=ts)
    return out


__all__ = [
    "MAX_TEXT_LENGTH", "MAX_HEADLINE_LENGTH", "MAX_URL_LENGTH", "TAINTED",
    "MARK_INSTRUCTION", "MARK_ROLE_TAG", "MARK_FENCE", "MARK_COMMENT",
    "MARK_TRUNCATED",
    "Sanitisation", "sanitise_external_text", "sanitise_external_text_report",
    "sanitise_url", "contains_instruction_text", "raw_digest",
    "taint_origin",
    "NewsItem", "SentimentProvider", "NullSentimentProvider",
    "StaticSentimentProvider", "SentimentResult", "SentimentAnalyzer",
    "LexiconSentimentAnalyzer", "analyse_all",
    "POSITIVE_LEXICON", "NEGATIVE_LEXICON", "NEGATORS", "INTENSIFIERS",
]
