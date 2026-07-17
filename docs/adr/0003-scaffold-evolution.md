# ADR 0003 — Governed scaffold evolution (propose-only)

- **Status:** Accepted — implemented (propose-only; nothing auto-applies)
- **Date:** 2026-07-17
- **Deciders:** Olympus maintainers
- **Related:** the deep-research scan (Darwin Gödel Machine, arXiv 2505.22954),
  `docs/THREAT_MODEL.md`, Agent Behavioral Contracts, the benchmark gate
  (`benchmarks.json`, Prometheus/Metis), `evolve.py`.

## Context

The Darwin Gödel Machine (DGM) is the deep-research scan's **highest-moat,
longest-horizon** capability: an agent that rewrites its own *scaffold code* and
keeps an open-ended archive of variants, gated by a benchmark. The scan flagged
it as directly in tension with Olympus's security spine — full autonomous
self-modification would let generated code edit the very guards that keep the
system safe. So this ADR adopts the *idea* (measured, archived, benchmark-gated
self-improvement of code) while removing the dangerous part (autonomy).

## Decision

Build scaffold evolution as a **strictly propose-only** capability. The engine
generates candidate patches, benchmarks them in isolation, archives the variants,
and surfaces them **as diffs through the approval gate** — and it has **no code
path that writes to Olympus's own source tree.** Applying a proposal is a human
developer action (review the diff, apply it with the normal git workflow); the
running agent never modifies itself.

Hard constraints, each enforced structurally:

1. **Non-security modules only.** A fail-closed allowlist (`_EVOLVABLE`) names the
   modules that may be proposed against; a denylist (`_SECURITY_MODULES`) names
   the guards that may never be touched (`security`, `cmdguard`, `actions`,
   `behavioral_contracts`, `mandate*`, `witness`, `vault`, `egress`, `capprofile`,
   `secretref`, `sandbox`, `approvals`, `replaygate`, …). A module not on the
   allowlist — or on the denylist — is refused. Unknown ⇒ not evolvable.
2. **Benchmarked in isolation.** A candidate must at minimum **compile**; a
   pluggable benchmark may run a targeted test subset. The candidate source is
   written only to a throwaway temp path — **never** the real module.
3. **Archived.** Every proposal (pass or fail) is recorded to an append-only,
   bounded archive with its benchmark result — the DGM open-ended archive, made
   inspectable.
4. **Governed by ABC.** A `scaffold.propose` behavioral contract (preconditions:
   target is evolvable + candidate compiles + benchmark passed; recovery
   `block`) gates whether a candidate may even be archived as a *proposal*.
5. **Nothing auto-applies; off by default.** `OLYMPUS_SCAFFOLD_EVOLVE` gates
   whether the engine runs at all. There is deliberately **no `apply()`
   function** in the module. Proposals are surfaced via `olympus scaffold-evolve
   proposals` as diffs for a human to apply out-of-band.

## Options considered

1. **Full DGM (autonomous self-rewrite + auto-apply).** Rejected — irreconcilable
   with the security spine; the scan itself ranked it a long-horizon R&D bet
   behind heavy sandboxing.
2. **No scaffold evolution.** Rejected — forgoes a real, high-moat capability
   that can be captured *safely* in propose-only form.
3. **Propose-only, allowlisted, benchmarked, archived, human-applied (chosen).**
   Captures the measured-self-improvement idea with zero autonomous
   self-modification and zero ability to touch a security module.

## Consequences

**Positive**
- A real self-improvement-of-code loop with no autonomy: the human is always the
  one who applies a change, after reading its diff and benchmark result.
- The security guards are structurally out of reach (allowlist + denylist,
  fail-closed).
- No new dependency; benchmarking uses in-process `compile()` plus a pluggable
  test hook.

**Negative / residual risk**
- A generated patch is only as good as its benchmark; a weak benchmark could
  green-light a subtly worse change. Mitigation: the human reviews the diff and
  result before applying; nothing lands automatically.
- The allowlist must be curated conservatively; a mis-classified security module
  would be a serious error, so the denylist is a second, independent guard and
  the default posture is "not evolvable."

## Non-goals

Autonomous self-modification, any auto-apply path, proposing against security
modules, and running generated code as anything other than an isolated
compile/benchmark (no generated code is imported into the live process).
