# ADR 0012: Self-discovery — acquiring knowledge and proposing features over time

Status: accepted
Date: 2026-07-23

## Context

Olympus already improves what it HAS, in several narrow, benchmark-gated places:
Prometheus upgrades prompts (rolled back on regression), Metis distills recent
experience into skills, `evolve.py` auto-tunes registered feature parameters
within hard guardrails, and the wiki refreshes concept pages by nightly
dreaming. What it lacked was a loop pointed at what it does **not yet know** or
**cannot yet do** — a way to notice gaps and close them over time, rather than
only polishing the existing surface.

This ADR records a native self-discovery loop that fills that gap, reusing the
existing spine (research → wiki for knowledge; the upgrade-proposal store for
features; the heartbeat for cadence) rather than adding a foreign subsystem. It
follows the doctrine of `evolve.py`/`outcomes.py`: notice, don't impose.

## Decision

`olympus/discovery.py` maintains a bounded, per-user **gap ledger** and runs a
bounded cycle on the heartbeat (opt-in, `OLYMPUS_DISCOVERY`, replay-inert):

**(a) Knowledge gaps → durable knowledge.** A gap ("I don't understand X") is
recorded — by an agent via the `note_knowledge_gap` tool, by friction
derivation, or by the operator (`olympus discover note`). The cycle researches
the top open gaps (`research.run`, whose fetches are SSRF-gated and whose output
is wrapped-untrusted upstream) and writes the cited result to a durable wiki
page. A **degraded** research result (no provider / "no usable evidence") is
never written as knowledge — the gap stays open and retries next cycle. So
Olympus acquires new knowledge over time, and only *real* knowledge is retained.

**(a′) Hands-free signal from verification.** Knowledge gaps no longer depend on
an agent remembering to call `note_knowledge_gap`. When Aletheia ships an answer
behind an UNVERIFIED banner because it could not support the factual claims (the
council `reject_after_rework` path or the quick-reply `direct_reject` path), the
orchestrator records the question as a `knowledge` gap automatically
(`_signal_knowledge_gap` → `discovery.note_gap`). It is best-effort (all failures
swallowed — never load-bearing on the answer path), opt-in and replay-inert
(gated on `discovery.enabled()`), and adds no extra tool or model call. So the
verifier's "I couldn't back this up" becomes a research target the next cycle
closes — the gap ledger fills itself from real friction.

**(b) Capability gaps → feature proposals.** Recurring action friction
(`outcomes.insights`) is derived into capability-gap candidates deterministically
(no model call); each is filed as a structured proposal on the existing upgrade
store (`memory.save("upgrades", …)`, surfaced in the digest and `olympus
discover`). Proposals are for the operator to review — **nothing is auto-built
or auto-changed**. This is the native, recurring form of the manual
"analyze the landscape → propose what to absorb" pattern that produced the
Firecrawl (ADR 0010) and Strix (ADR 0011) absorptions.

## Safety invariants

- **Notice, don't impose.** Knowledge is stored (sanitized at the memory sink,
  wrapped-untrusted upstream); features are proposed, never applied. No
  security-relevant behaviour changes here.
- **The topic is a seed, not knowledge.** `note_knowledge_gap` stores only a
  search phrase (own-state write, like `add_todo`); the *researched result* is
  the thing that must be trustworthy, and it goes through the same
  wrap-untrusted + sanitize path as any ingested content — so a poisoned page
  can't turn a gap into poisoned memory.
- **Bounded + replay-inert + opt-in.** The ledger is capped; each cycle does at
  most a few research passes and proposals; the heartbeat loop is off by default
  and never runs during replay; every step is isolated so a broken signal
  source can't crash the cycle.

## Capabilities delta

- New module `olympus/discovery.py` (gap ledger, capability-gap derivation,
  knowledge acquisition, feature proposal, the cycle, a report).
- 1 new tool (125 total): `note_knowledge_gap` (TRUSTED / own-state), on Metis,
  Argus, and Prometheus.
- 1 new command (127 total): `olympus discover` (run / note / gaps / report).
- Heartbeat cadence `DISCOVERY_EVERY` (opt-in via `OLYMPUS_DISCOVERY`).
- Tests: `tests/test_discovery.py` (17, incl. the hands-free UNVERIFIED→gap
  auto-signal: opt-in, replay-inert, never-raises).

## NOT in scope (deliberately)

- **Autonomous feature building.** Discovery proposes; a human (or the normal
  Prometheus benchmark-gated path) decides and builds. No self-modification is
  triggered here.
- **Autonomous outbound filing.** Proposals are stored locally by default; the
  existing `propose_upgrade` path (egress-guarded) is how a proposal becomes an
  upstream issue, on explicit action — not on a heartbeat.
