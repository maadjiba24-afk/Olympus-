# ADR 0008: Native adaptive-coordination extensions

Status: accepted
Date: 2026-07-22

## Context

A survey of a large external agent harness (ruflo/claude-flow) surfaced five
capabilities Olympus either lacked or had only a weaker adjacent form of:
approximate-nearest-neighbour vector recall, explicit swarm topologies, quorum
consensus, exploratory (bandit) model routing, and a runtime file-defined agent
registry. The goal was to absorb the genuinely useful ideas **natively** — in
Olympus's own idioms and safety model — rather than bolt on a foreign subsystem
or copy the external project's anti-patterns (an ungated shell-exec tool, a
telemetry/monetisation funnel, `@latest` self-invocation, permission-widening
defaults, distributed-consensus theatre over an in-process transport).

This ADR locks the shared design contract every one of these extensions obeys,
so they read as one coherent layer rather than five ad-hoc features.

## Decision (a): opt-in and strictly non-regressing

Every extension is gated by its own `OLYMPUS_*` flag, default OFF, and when off
the code path is a literal no-op — the default install is byte-identical to the
prior behaviour. Where an extension accelerates an existing path (the ANN index
behind the cosine seams), it stays EXACT below a size threshold, so it can only
ever add candidates, never change a small-corpus result. The capability manifest
(`capabilities.py`) is unchanged: these are internal capabilities, not new
tools/actions/commands, so Olympus's truthful-accounting gate keeps holding — no
inflated counts.

## Decision (b): replay-safe by construction

None of these may introduce non-determinism onto the council's replay hot path:

- `annindex` derives each HNSW node's level from a seeded hash of its own id (no
  global rng), so the same vectors always build the same graph.
- `dytopo` swarm topologies stay pure, deterministic, and hard-capped.
- `consensus` folds verdicts with deterministic, tie-broken tallies.
- `bandit_routing` uses UCB1 (deterministic argmax), NOT Thompson sampling, and
  is forced OFF during replay like the learned selector.
- `agentreg` loads files in sorted order with no clock/rng.

Consultation and multi-verifier fan-out run under copied contexts and propagate
`ReplayDivergence` unmasked, exactly like the existing verify stage.

## Decision (c): the security spine is reused, never bypassed

- File-defined agents (`agentreg`) are safety-bounded by construction: always
  `system=False`/`code_exec=False`, tools filtered against
  `security.ACTION_TOOLS`, and unable to shadow a built-in — mirroring
  `subagents._is_privileged`.
- The swarm consultation pass routes every refinement through the existing
  `_run_one` funnel (output contract, per-worker sandbox root, replay freezing).
- Consensus only ever ADDS verifier scrutiny and falls back to the single
  verifier on failure; it can never weaken the existing guarantee.

(Federation, the sixth capability, carries the heaviest new trust surface and is
specified separately in ADR 0007.)

## Consequences

Five capabilities land as small, independently-testable, dependency-free modules
(`annindex`, `consensus`, `bandit_routing`, `agentreg`, plus `dytopo`
extensions) that compose with the existing pipeline through named seams rather
than replacing it. Each ships with its own test module and stays off until an
operator opts in. What was deliberately NOT absorbed: any ungated execution
surface, any pre-consent network beacon, any self-updating install behaviour, or
any distributed-consensus claim not backed by a real transport.
