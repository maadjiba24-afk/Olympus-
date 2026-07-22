# ADR 0009: The self-evolving moat

Status: accepted
Date: 2026-07-22

## Context

ADR 0007 (federation) and ADR 0008 (the shared opt-in/replay-safe contract)
landed six capabilities absorbed from an external harness as native Olympus
modules: vector recall, swarm topologies, quorum consensus, bandit routing,
file-defined agents, and cross-instance federation. But *absorbed* is not a
*moat*. A static bolt-on does not compound — and two of the six were not even
reachable by an operator (federation had zero callers; the HNSW graph had no
live user).

To turn the capabilities into a durable, compounding advantage they must be
(a) owned and usable, not dormant; (b) wired into Olympus's OWN self-evolution
spine (`evolve.py`) so they measure themselves and tune within guardrails as
they run; and (c) observable, so the compounding is visible. This ADR locks how
that wiring is done, inheriting the doctrine of ADR 0003/0005: measure
everything, impose nothing risky, and never break byte-identical replay.

## Decision (a): capabilities feed the evolution spine

Each capability records OK / DEGRADED / FAIL telemetry to `evolve` through a
single `_evolve_record` chokepoint that is **skipped on the replay path** and can
never raise into the caller. The capabilities therefore accrue a track record
that surfaces in `evolve.health()` / `review()` / `summary()`.

**Consensus is the flagship closed loop.** A formed quorum records OK; a
floor-miss (too few verifiers produced a verdict) records DEGRADED. On sustained
degradation `evolve.review()` widens the verifier panel via the registered
`consensus.verifiers` tunable (bounded `[1, 7]`, `on_fail=increase`), so
transient verifier errors get outvoted — the capability gets stronger the more
it is used.

**Swarm consultation** feeds health telemetry only. It has no parameter whose
`evolve` auto-tune model (step toward `on_fail` on degradation) genuinely fits,
so — rather than invent a fake tuning signal — it stays a surfaced suggestion, in
keeping with "measure everything, impose nothing risky."

## Decision (b): self-tuning never escapes the guardrails, or replay

- Only parameters registered in `evolve` with `[lo, hi]` bounds and marked
  non-security (or `tighten_only`) may auto-move. No capability gains a
  self-modifying path outside `evolve`'s existing bounded doctrine.
- An explicit env override (`OLYMPUS_CONSENSUS_VERIFIERS`) always wins over the
  tuned value — operator control is never overridden by the tuner.
- **Replay-safety of a self-tuning decision-path knob:** the resolved panel size
  is frozen per run with `replaystore.frozen_context`, so a run recorded at one
  size replays at that size even after the tunable has since grown. Self-tuning
  can change future runs; it can never make a past run diverge.

## Decision (c): the moat is usable and observable

- **Usable.** Federation is driven from the CLI (`olympus federation identity |
  add-peer | peers | remove-peer | call | serve | lessons`); it is no longer a
  library with no callers.
- **At scale.** `OLYMPUS_ANN`'s payoff is a PERSISTENT HNSW index in `docrag`,
  built once and keyed to a corpus signature (per-doc mtime + chunk count) so it
  rebuilds only when documents change — real sublinear recall, not a per-call
  rebuild. A one-shot `nearest()` stays an exact scan, because building a graph
  to answer a single query never pays off.
- **Observable.** `olympus moat` shows every capability's enabled state, its
  self-evolution health, and its current self-tuned settings, so an operator can
  watch the moat compound.

## Consequences

The capabilities now compound: the more they run, the better their settings, and
an operator can see it. Self-evolution continues **autonomously** through the
heartbeat's periodic `evolve.review()` — no external driver is required once the
wiring is in place. What is deliberately NOT done: no auto-tune of a
security-relevant or decision-path knob without replay-freezing; no fabricated
telemetry signal to force a tuning loop that does not have a real one; and no
capability escaping `evolve`'s bounded, reversible doctrine (`evolve reset`
remains the human lever).
