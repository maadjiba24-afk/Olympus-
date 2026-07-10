# Design — Boundary-Enforcement Layer (Egress Gateway Spine)

**Status:** phase A implemented (`olympus/egress.py`, off by default, gated by
`OLYMPUS_EGRESS_GUARD`; `email_egress_held`/`webhook_egress_held` ActionTypes) ·
**Scope:** large, phased · **Depends on:** the existing
actions spine (`actions.py`), the signed decision log (`trace.py`),
`security.py`, and — conceptually — Document 1's `contracts.py`.
**Companion doc:** `DESIGN_OUTPUT_CONTRACTS.md` (Document 1 — build that first).

> This document is grounded in an audit of the real repository. Every line
> reference below was verified against source, not the README. Where the README
> and the code disagreed, the code wins and the disagreement is flagged.

---

## Part 0 — The thesis, stated once

Feature 2 is not, at its core, a classification feature. **It is a chokepoint
feature.** The audit established the decisive fact: *Olympus has no unified point
through which outbound data passes.* Egress is scattered across at least seven
modules — `gmail.py`, `tools.py` (SMTP + webhook), six chat platforms
(`discord/slack/telegram/whatsapp/signal`), `github.py`, and the eight executors
in `builtin_actions.py`. Redaction (`security.anonymize`) runs on exactly **one**
of those paths — the contribution pool (`contrib.py:61-62`). Everything else
leaves the box unclassified, unredacted, ungated.

Document 1's output-contract primitive works cleanly for one reason: `_run_one`
(`orchestrator.py:328`) is a genuine funnel — every in-pipeline specialist
output passes through it, so a single clamp covers everything. **Feature 2 has no
such funnel, so the first and hardest job is to build one.** Classification,
redaction, and route-to-approval are then *properties checked at that one
chokepoint* — the easy layer on top, exactly as the engineering instinct says
they should be.

So the spine of this document is: **introduce `olympus/egress.py`, a single
gateway every outbound site calls before anything leaves the process; refactor
the seven-plus egress sites to route through it; then layer three-class channel
classification and route-to-approval on top.** The refactor is the bulk of the
work. The classification is ~200 lines on top of it. This ordering is
deliberate and non-negotiable: a classification scheme with no chokepoint to
enforce at is a data model with nowhere to live.

---

## Part 1 — What already exists (so we build nothing twice)

The audit confirmed a substantial substrate. Reuse it; do not reinvent it.

**The actions spine is the model for "route to approval."** `actions.py` has a
full prepare → approve → execute lifecycle with a typed registry. The key
interface facts (verified):

- `ActionType` is a frozen dataclass: `name`, `risk_class`, `scope`, `preview`,
  `execute`, optional `undo`, `description` (`actions.py:79-89`).
- `register(action_type)` adds it; **`risk_class` must be one of the four
  constants** or it raises (`actions.py:98-101`).
- Risk classes: `TRIVIAL`, `NOTABLE`, `IRREVERSIBLE`, `FINANCIAL_LEGAL`
  (`actions.py:34-38`). `IRREVERSIBLE` and `FINANCIAL_LEGAL` map to auto-run
  level `99` — i.e. **can never auto-execute, always need explicit approval**
  (`actions.py:64-72`).
- `prepare(user, type_name, payload, title, why)` **rejects unregistered
  types** (`actions.py:266-268`) — risk class comes from the registered
  `ActionType`, not the caller. There is **no generic `ApprovalQueue` object**;
  `pending()` scans saved `Action` JSON for `status in (PREPARED, APPROVED)`.
- Every transition is appended to an immutable per-user `audit.jsonl`
  (`actions.py` `_audit`).

**Consequence for our design (this corrects an earlier assumption):** you cannot
drop an arbitrary "this egress was blocked, please approve" event into the queue.
**You register a new `ActionType` per egress channel** whose `execute` performs
the real send. That is more plumbing than "just enqueue," but it is *better*: the
blocked egress inherits the entire risk-class / autonomy / rate-limit / audit
spine for free, and its `undo` (where the channel supports it — Gmail draft
delete, calendar cancel, archive→unarchive already exist in `builtin_actions.py`)
comes along too.

**The classification raw material already exists, half-built:**

- Channel split: `ACTION_TOOLS` / `INGESTION_TOOLS` frozensets
  (`security.py:29-37`) and per-connector `type="data"|"action"`
  (`connectors.py:147`), `Plugin.action: bool` (`connectors.py:42`).
- Redaction primitives: `security.anonymize` with regex categories `_EMAIL`,
  `_PHONE`, `_LONGNUM`, `_URL_CRED`, `_KEYISH` (`security.py:118-139`).
- A de facto sensitivity notion: the memory write-gate routes
  `sensitivity == "high"` items to a held-for-approval candidate queue
  (`recall.py:150-153`). **But the audit is emphatic about its limit:** the
  categories (health/finance/legal/identifiers) exist only as *English words in
  an LLM extraction prompt* (`recall.py:105-107`); the machine label is a binary
  `normal|high` enum (`recall.py:58-59`); the queue is memory-candidates only,
  not egress. So there is **no enforceable code-level data taxonomy today.** We
  are building the first one.

**The signed log is the audit substrate** (same as Document 1): `trace.py`
records signed decisions; we add an `egress` decision type.

---

## Part 2 — The classification scheme, designed from zero

The audit forced one principle: **classify channels, not data.** The reason is
structural, not a preference. Data-taint classification would require a
sensitivity label to *survive* through `memory.py`, `usermem.py`, every tool
result, and `trace.py`, then be re-checked at all seven-plus egress sites. That
is the two-month build, and on a pre-distribution framework it is the wrong
investment. Channel classification asks a smaller, enforceable question at one
chokepoint: *"this payload is leaving through this channel — is that allowed, and
what must be stripped first?"*

### 2.1 The three data classes

Derived from the boundaries that already exist in the code, so the scheme is
provably complete against the real surface rather than invented:

| Class | Name | Definition (anchored to existing code) |
|---|---|---|
| **C0** | `PUBLIC` | Safe to leave through any channel. Marketing copy, public research summaries, generic answers. The default *floor*. |
| **C1** | `OPERATIONAL` | User's own working content — emails they're sending, calendar invites, task payloads. May leave through *user-directed* channels (their Gmail, their webhooks) but not through *broadcast* or *pooled* channels. |
| **C2** | `SENSITIVE` | The categories the memory gate already names — health, finance, legal, personal identifiers, secrets/keys. Maps directly onto the existing `recall.py:105-107` list and the `security.py:118-122` secret regexes. May leave **only** through an explicitly user-approved action, never auto, never pooled, never broadcast. |

Three classes, not five. Each maps to a distinction the system *already makes
somewhere*, which is what makes "designed from zero" honest rather than
arbitrary: C2 is the memory gate's `high`; C0/C1 split the `normal` bucket along
the line the capability-separation rule already implies (user-directed vs.
external-facing).

### 2.2 The channel classes (egress side)

Every egress site is tagged with what it *is*, derived from the audit's D11 list:

| Channel kind | Members (verified egress sites) | Max class it may emit |
|---|---|---|
| `USER_DIRECTED` | the user's own Gmail send/draft (`builtin_actions._gmail_*`), their SMTP `_send_email` (`tools.py:561`), their calendar invite (`_cal_create_execute`), their configured webhooks (`_call_webhook`, `tools.py:605`) | **C1** without approval; **C2 only via the approval gate** |
| `BROADCAST` | chat-platform sends — `discord.notify`, `slack.send`, `telegram.notify`, `whatsapp._send`, `signal.send` | **C0 only** (these reach operator-defined channels / groups; never emit C1/C2 without approval) |
| `EXTERNAL_SINK` | `github.create_issue` (`github.py:22`), any future third-party post | **C0 only** |
| `POOLED` | the cross-model contribution pool (`contrib.offer`) | **C0 only, and only after `security.anonymize`** (this path already redacts — we formalize it as the rule, not the exception) |

The matrix *is* the policy. It is small enough to read in one screen and it is
derived from real sites, so it has no gaps the code doesn't have.

### 2.3 How a payload gets its class

Two mechanisms, cheapest first:

1. **Channel default.** If the caller doesn't classify, the payload inherits the
   *most restrictive plausible* class for safety: anything going to a
   `USER_DIRECTED` channel defaults to C1; anything to `BROADCAST`/`EXTERNAL_SINK`/
   `POOLED` is *asserted* C0 and **scanned to confirm** (step 2). Fail-closed: an
   unclassifiable payload heading for a C0-only channel is treated as C2.
2. **Content scan (the existing regexes, promoted).** `security.py`'s secret/PII
   regexes (`_EMAIL`, `_PHONE`, `_LONGNUM`, `_URL_CRED`, `_KEYISH`) become a
   *classifier*: if a payload bound for a C0-only channel contains a secret-shaped
   or identifier-shaped match, it is reclassified C2 and **blocked/routed**, not
   silently redacted (redaction is acceptable only on the `POOLED` path, which is
   distilled-methods-only by design). This reuses code that already exists and is
   already tested; we are giving it a second job.

No LLM call on the hot path. Classification is regex + channel default —
deterministic, replay-safe, free. (An optional LLM second-opinion for C1-vs-C2 on
`USER_DIRECTED` content can be added later behind a flag, but the v1 is
regex-only so it never adds latency or nondeterminism to the egress path.)

---

## Part 3 — The spine: `olympus/egress.py`

The single gateway. Every outbound site calls exactly one function before data
leaves the process.

```python
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
signed decision log (trace.py) as an `egress` decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import config, security


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
    assertion (or PUBLIC) stands.
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
    cls = classify(text, asserted=asserted)
    ceiling = _MAX_AUTO[channel]

    if _ORDER[cls] <= _ORDER[ceiling]:
        return Decision(Verdict.ALLOW, cls, channel, "within channel policy")

    # Over the ceiling. POOLED gets redaction (distilled-methods-only path);
    # everything else is held for explicit approval.
    if channel is ChannelKind.POOLED:
        red = security.anonymize(text)
        return Decision(Verdict.REDACT, DataClass.PUBLIC, channel,
                        "redacted for the contribution pool", redacted_text=red)

    # Route to the approval spine. Requires a registered ActionType for this
    # channel (Part 4). If one isn't registered, fail closed with HOLD and a
    # reason — the caller must not send.
    reason = (f"{cls.value} content may not auto-egress via "
              f"{channel.value}; held for approval")
    if action_type is not None and payload is not None:
        from . import actions
        try:
            actions.prepare(user, action_type, payload,
                            why=reason)
        except Exception as err:
            reason = f"{reason} (approval routing failed: {err})"
    return Decision(Verdict.HOLD, cls, channel, reason)
```

Design notes that carry weight:

- **`guard()` is the only thing callers learn.** The refactor (Part 5) is
  mechanical precisely because every site does the same three lines: call
  `guard`, branch on `verdict`, send-or-don't. Complexity lives in one module.
- **It reuses `security`'s regexes directly** rather than copying them — one
  source of truth for "what looks like a secret," already tested.
- **No LLM, no I/O in `classify`** — replay-safe and free, the same discipline
  Document 1's `contracts.check` follows.
- **HOLD routes to the *existing* spine.** We do not build a new approval queue;
  we call `actions.prepare` with a per-channel registered type (Part 4).

---

## Part 4 — Registering egress channels as action types

Because `actions.prepare` rejects unregistered types (`actions.py:266-268`),
each `USER_DIRECTED` channel that can carry C2 needs a registered `ActionType`
whose `execute` performs the real send. The good news: **these executors already
exist** in `builtin_actions.py` — `_email_execute`, `_gmail_send_execute`,
`_cal_create_execute`, `_webhook_execute`. Several are *already registered* as
action types today. So "route a held egress to approval" largely means *reusing
the action types that already wrap these sends*.

The pattern, for any egress not already an action type:

```python
# in builtin_actions.register_builtins()
actions.register(actions.ActionType(
    name="email_egress_held",
    risk_class=actions.IRREVERSIBLE,        # sending is irreversible (never auto)
    scope="email",
    preview=_email_preview,                 # already exists
    execute=_email_execute,                 # already exists
    description="An email whose content was classified SENSITIVE and held "
                "for explicit approval before sending.",
))
```

The audit flagged that **no `FINANCIAL_LEGAL` action type is registered among
builtins.** If any egress channel can carry payments/contracts, register it with
`risk_class=FINANCIAL_LEGAL` so it inherits the never-auto + financial-rate-limit
treatment. For v1, the realistic egress channels are all `IRREVERSIBLE`.

---

## Part 5 — The refactor (the bulk of the work), phased

The seven-plus sites must route through `guard()`. Do it in priority order so
value lands before the long tail, and so each phase is independently shippable.

### Phase A — the two raw actuators (highest risk, smallest surface)

`tools._send_email` (`tools.py:561`) and `tools._call_webhook` (`tools.py:605`).
These are the canonical egress and are referenced by the action executors. Wrap
the *send* with a `guard()` call:

```python
def _send_email(to: str, subject: str, body: str, *, user: str = "shared") -> str:
    # ... existing config/allowlist checks unchanged ...
    if config.egress_guard_enabled():
        d = egress.guard(f"{subject}\n\n{body}", egress.ChannelKind.USER_DIRECTED,
                         user=user, asserted=egress.DataClass.OPERATIONAL,
                         action_type="email_egress_held",
                         payload={"to": to, "subject": subject, "body": body})
        if d.verdict is egress.Verdict.HOLD:
            return (f"[Held for approval: {d.reason}. Approve it with "
                    f"`olympus actions`.]")
        # ALLOW falls through to the existing send.
    # ... existing smtp send unchanged ...
```

Two real frictions the audit surfaced, name them in the PR:

1. **`_send_email` has no `user` parameter today.** Threading `user` to the
   egress sites is part of the refactor cost. Default it to `"shared"` so
   existing callers don't break, but the *value* of per-user policy and the
   per-user approval queue depends on the real user reaching here. Audit the
   call chain (`tools.py` handler → `agent.py` → orchestrator) for where `user`
   is available.
2. **The action executors call `_send_email` themselves** (`_email_execute`,
   `builtin_actions.py:57`). When `guard` HOLDs and routes to
   `email_egress_held`, whose `execute` calls `_email_execute` → `_send_email`
   again, you must **not** re-guard on the approved path (infinite hold loop).
   Solve it with an explicit bypass: the action executor calls a
   `_send_email(..., _approved=True)` variant that skips the guard, because
   approval *is* the gate clearing. This is the single subtlest bug in the whole
   design — write the test for it first (Part 7, test 8).

### Phase B — the contribution pool (formalize the existing redaction)

`contrib.offer` (`contrib.py:55`) already calls `security.anonymize`
(`contrib.py:61-62`). Replace that direct call with `guard(..., POOLED)` so the
redaction becomes a *policy decision recorded in the log* rather than an inline
side effect. Behavior is nearly identical; the difference is it's now auditable
and consistent with every other channel. Low risk, high consistency payoff.

### Phase C — broadcast + external sinks (C0-only enforcement)

The six chat sends and `github.create_issue`. These should only ever emit C0.
Wrap each with `guard(..., BROADCAST)` / `guard(..., EXTERNAL_SINK)`; a C1/C2
payload here is a bug (e.g. a synthesized answer containing the user's PII being
pushed to a Telegram group), and `guard` will HOLD it. Expect to find real
latent leaks here — that's the feature working.

### Phase D — the remaining `builtin_actions` executors

`_run_command_execute`, `_write_file_execute` write to the workspace/host. These
are arguably *not* network egress, but they are side-effecting sinks and the
audit lists them. Decide explicitly whether they're in scope for the egress
gateway or remain governed solely by the actions spine. **Recommendation: out of
scope for egress** — they don't leave the box over the network — but document the
decision so it isn't an accidental gap.

**Stop-anywhere property:** each phase is shippable alone. Phase A alone already
closes the highest-risk holes (email, webhook). If distribution pressure
intensifies, ship A, pause, resume at B later. The gateway exists after Phase A;
the rest is widening coverage.

---

## Part 6 — Config, defaults, replay (same discipline as Document 1)

```python
def egress_guard_enabled() -> bool:
    """Route outbound data through the egress gateway (OLYMPUS_EGRESS_GUARD=1).
    OFF BY DEFAULT — inert until an operator opts in, so it can't surprise a
    fresh install or a public BYOK instance."""
    return os.environ.get("OLYMPUS_EGRESS_GUARD", "").strip().lower() in (
        "1", "true", "yes", "on")
```

- **Default off**, same three-layer safety as the contract primitive: global flag
  off; even on, an `ALLOW` verdict is the common path and changes nothing; a
  `HOLD` degrades to "held for approval," which reuses an interface users already
  understand.
- **Replay safety:** `guard`/`classify` are pure and deterministic (regex +
  channel), so an `egress` decision record's *core* is replay-stable, like the
  `contract` record in Document 1 Part 8. Record whether the guard was enabled in
  `tr.meta` so a replay reconstructs the same mode. **Same caveat as Document 1:**
  the replay gate is weekly cron (`replay-gate.yml:11-14`), **not** per-PR — run
  `python scripts/tier1_exit_check.py` by hand before merging; nothing blocks the
  merge automatically.
- **The `egress` decision type** joins `route`/`plan`/`review`/`contract` in the
  signed log. Channel, class, and verdict are deterministic content; no volatile
  fields beyond those `_VOLATILE` already excludes.

---

## Part 7 — Tests (definition of done)

New `tests/test_egress.py`. Pure-unit first:

1. `classify`: clean text asserted C0 → C0; same text with an API-key-shaped
   string → C2 (overrides assertion); text with an email asserted C0 → C2;
   text asserted C1 with no PII → C1.
2. `guard` ALLOW: C0 to BROADCAST; C1 to USER_DIRECTED.
3. `guard` HOLD: C2 to USER_DIRECTED with a registered `action_type` → HOLD and
   an action is prepared (assert via `actions.pending`).
4. `guard` HOLD with **no** registered action type → HOLD, fail-closed, reason
   names the routing failure, nothing sent.
5. `guard` REDACT: C2 to POOLED → REDACT, `redacted_text` is the anonymized form,
   verdict class downgraded to C0.
6. C1/C2 to BROADCAST/EXTERNAL_SINK → HOLD (the latent-leak case).

Integration:

7. With `OLYMPUS_EGRESS_GUARD` unset, every egress site behaves exactly as today
   (the distribution-safety guarantee — test it explicitly, like Document 1's
   test 7).
8. **The re-guard loop:** with the guard on, a HELD email approved via the
   actions spine *sends exactly once* and is **not** re-held by `guard` on the
   approved path. This is the Phase-A subtle bug; its test is mandatory.
9. An `egress` decision record is written with the right verdict/class/channel
   on ALLOW, HOLD, and REDACT.

Replay:

10. Record a run that triggers an egress decision with the guard on; replay;
    assert `diff_decisions == []`. Must be green before merge (run it manually).

---

## Part 8 — Honest accounting of cost, risk, and what this is not

**This is the big build, and the refactor is most of it.** `egress.py` itself is
~150 lines; the classification layer ~100. The *refactor* — threading `user`
through seven-plus sites, wrapping each send, handling the re-guard bypass, and
chasing the latent C1/C2-to-broadcast leaks Phase C will surface — is where the
weeks go. That matches what you signed up for when you chose the chokepoint
spine over the minimal-footprint version.

**Known gaps, stated so none are discovered later:**

- The **out-of-band specialist callers** (`subagents.py`, `opportunity_scan`)
  that bypass `_run_one` also bypass nothing here *yet* — but if they ever emit
  egress directly, they must call `guard` too. Same root cause as Document 1's
  gap: enforcement at a funnel only covers what flows through the funnel.
- **`classify` is regex + channel default, not semantic.** It will miss
  sensitive content that contains no secret/PII regex match (e.g. a confidential
  business strategy in prose). That is an accepted v1 limitation — the C2 net
  catches identifiers and secrets, which is the high-value, low-false-positive
  core. A flagged LLM classifier is the documented upgrade path, deliberately
  not built now.
- **DNS-rebinding / SSRF** on webhook egress is out of scope here — that's
  `security.url_block_reason`'s job (`security.py:162`), already present, with its
  own documented rebinding caveat. The egress gateway governs *what class of
  data* leaves, not *whether the destination is internal*; the two guards
  compose.

**The moat, restated honestly.** The egress gateway is copyable as a pattern.
What is not copyable is the same pair Document 1 rides: self-hosted (you control
every egress site, so a single chokepoint is even *possible* — a hosted
orchestrator cannot interpose on the customer's own Gmail send), plus the signed,
re-executable log (so "this payload was classified C1 and left only through a
user-directed channel, here is the signed replayable proof" is a verifiable
claim). The gateway's deeper purpose is to make every outbound action a
*recorded, classified, signed decision* — turning "auditable orchestration" from
slogan into the literal content of the log. Document 1 started populating that
log with admissibility decisions at the input/processing boundary; this populates
it with egress decisions at the output boundary. Together they are the spine of
the only defensible story Olympus has: **provable, re-executable governance of
what the system reads, does, and emits — which a hosted competitor structurally
cannot offer because the boundaries never leave their box in a form the customer
controls.**

**Build order, final:** Document 1's `contracts.py` first (small, off by
default, starts the signed-decision habit). Then this, phased A→D, off by
default, the gateway before the classification. Do not build the data-taint
version. Do not skip the re-guard test. Do not let the refactor's size tempt you
into instrumenting only two sites and calling it a layer — that's the
minimal-footprint design you explicitly rejected, and it leaves an
architecture with no chokepoint, which is the one thing this whole document
exists to prevent.
