"""The unified egress gateway.

Every piece of data that leaves the Olympus process — email, webhook, chat
send, GitHub issue, contribution pool — passes through `guard()` first. This is
the chokepoint that makes class-aware egress control possible: without it,
egress is scattered across ~7 modules and cannot be governed in one place.

guard() classifies the payload, checks it against the channel's policy, and
returns a Decision: ALLOW (caller proceeds), REDACT (caller proceeds with the
returned redacted payload — POOLED channel only), or HOLD (caller must NOT send;
the egress has been routed to the approval spine as a prepared action).

Enforcement is gated by config.egress_guard_enabled(); OFF BY DEFAULT, so this
is inert until an operator opts in. Every decision is recorded into the existing
signed decision log (trace.py) as an `egress` decision — never a separate log.

PHASE A only: the gateway plus the two raw actuators (tools._send_email,
tools._call_webhook). Other egress sites (contrib/chat/github/sandbox) are
NOT wired yet (Phases B–D) — see docs/DESIGN_BOUNDARY_LAYER.md Part 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import config, security  # noqa: F401  (config kept for symmetry/future use)


class DataClass(Enum):
    PUBLIC = "C0"
    OPERATIONAL = "C1"
    SENSITIVE = "C2"


class ChannelKind(Enum):
    USER_DIRECTED = "user_directed"
    BROADCAST = "broadcast"
    EXTERNAL_SINK = "external_sink"
    POOLED = "pooled"


class Verdict(Enum):
    ALLOW = "allow"
    REDACT = "redact"     # proceed with redacted payload (POOLED only)
    HOLD = "hold"         # do NOT send; routed to approval


# The policy matrix from Part 2.2: max class each channel may emit WITHOUT
# approval. C2 to a USER_DIRECTED channel is allowed only via the approval gate
# (handled as HOLD → prepared action), never inline.
_MAX_AUTO = {
    ChannelKind.USER_DIRECTED: DataClass.OPERATIONAL,   # C1
    ChannelKind.BROADCAST:     DataClass.PUBLIC,         # C0
    ChannelKind.EXTERNAL_SINK: DataClass.PUBLIC,         # C0
    ChannelKind.POOLED:        DataClass.PUBLIC,         # C0 (after redact)
}
_ORDER = {DataClass.PUBLIC: 0, DataClass.OPERATIONAL: 1, DataClass.SENSITIVE: 2}


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    data_class: DataClass
    channel: ChannelKind
    reason: str
    redacted_text: str | None = None   # set when verdict is REDACT


def classify(text: str, *, asserted: DataClass | None = None) -> DataClass:
    """Determine a payload's class. Regex-based, deterministic, no LLM, no I/O.

    If the content carries secret- or identifier-shaped matches, it is SENSITIVE
    regardless of what the caller asserted (fail-closed). Otherwise the caller's
    assertion (or PUBLIC) stands. Reuses security.py's regexes directly — one
    source of truth for "what looks like a secret".
    """
    if security._KEYISH.search(text) or security._URL_CRED.search(text):
        return DataClass.SENSITIVE
    # identifier-shaped: emails/phones/long numbers present in bulk → sensitive.
    hits = (len(security._EMAIL.findall(text))
            + len(security._PHONE.findall(text))
            + len(security._LONGNUM.findall(text)))
    if hits >= 1 and (asserted is None or asserted == DataClass.PUBLIC):
        # A payload asserted PUBLIC that contains PII is not public.
        return DataClass.SENSITIVE
    return asserted or DataClass.PUBLIC


def guard(text: str, channel: ChannelKind, *, user: str,
          asserted: DataClass | None = None,
          action_type: str | None = None,
          payload: dict | None = None) -> Decision:
    """The chokepoint. Classify `text`, check against `channel` policy, decide.

    On HOLD, if `action_type` is registered, route the egress to the approval
    spine as a prepared action (the caller must then NOT send directly).
    """
    # A stored secret (raw or encoded) in the payload is an exfiltration, not
    # a classification question — held for approval on EVERY channel,
    # including POOLED (redaction can't be trusted to strip an exact value).
    leak = security.secret_exfil_reason(text, user)
    if leak:
        return _record(Decision(Verdict.HOLD, DataClass.SENSITIVE, channel,
                                leak))

    cls = classify(text, asserted=asserted)
    ceiling = _MAX_AUTO[channel]

    if _ORDER[cls] <= _ORDER[ceiling]:
        return _record(Decision(Verdict.ALLOW, cls, channel,
                                "within channel policy"))

    # Over the ceiling. POOLED gets redaction (distilled-methods-only path);
    # everything else is held for explicit approval.
    if channel is ChannelKind.POOLED:
        red = security.anonymize(text)
        return _record(Decision(Verdict.REDACT, DataClass.PUBLIC, channel,
                                "redacted for the contribution pool",
                                redacted_text=red))

    # Route to the approval spine. Requires a registered ActionType for this
    # channel (Part 4). If one isn't registered, fail closed with HOLD and a
    # reason — the caller must not send.
    reason = (f"{cls.value} content may not auto-egress via "
              f"{channel.value}; held for approval")
    if action_type is not None and payload is not None:
        from . import actions
        try:
            actions.prepare(user, action_type, payload, why=reason)
        except Exception as err:
            reason = f"{reason} (approval routing failed: {err})"
    return _record(Decision(Verdict.HOLD, cls, channel, reason))


_STATUS = {Verdict.ALLOW: "ok", Verdict.HOLD: "hold", Verdict.REDACT: "redact"}


def _record(decision: Decision) -> Decision:
    """Record the egress decision into the current run's signed Trace, if a run
    is in scope. Best-effort and never raises — recording must never block an
    egress verdict. The rationale carries only verdict/class/channel/reason
    (deterministic; never the raw payload), so the record's core is replay-
    stable. No run in scope (e.g. a one-shot routine or a direct call) → no-op.
    """
    from . import trace as trace_mod
    tr = trace_mod.current()
    if tr is not None:
        try:
            tr.decision(
                "egress",
                {"channel": decision.channel.value, "role": "egress"},
                {"verdict": decision.verdict.value,
                 "data_class": decision.data_class.value,
                 "channel": decision.channel.value,
                 "reason": decision.reason},
                status=_STATUS[decision.verdict])
        except Exception:
            pass
    return decision
