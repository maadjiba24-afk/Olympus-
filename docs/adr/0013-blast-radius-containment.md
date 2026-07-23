# ADR 0013: Blast-radius containment — owning each of Strix's damage vectors

Status: accepted
Date: 2026-07-23

## Context

An autonomous security agent's "blast radius" is how much damage it can do if it
is wrong, misconfigured, hijacked, or misused. [Strix](https://github.com/usestrix/strix)'s
is large by construction (see `docs/STRIX_TRACKING.md`): prompt-only scope, an
open-egress sandbox (NET_ADMIN/NET_RAW + host-gateway) that can reach the
operator's own machine / LAN / cloud-metadata, a refusal-suppression prompt that
removes the model's own judgment, arbitrary/weaponized payloads that break or
exfiltrate, and a removed audit trail. Stacked, a single failure turns an
autonomous attacker loose with real destructive capability and no brakes.

ADR 0011 already inverted most of this. This ADR makes the containment **owned
and demonstrable**: each blast-radius vector maps to a *named* Olympus control,
one vector is upgraded from an implicit property to an *active* control, and a
self-check *proves* every vector is contained (the guardrails, made verifiable —
the same discipline the assessment self-benchmark applies to detection quality).

## Decision (a): active egress confinement

Per-tool `require_scope()` already gates *which* target each call may touch.
`assess.confined_egress()` goes further: for the whole duration of an assessment
it pins outbound network to ONLY the signed authorization's hosts, enforced at
the gated-fetch layer (`tools._http_get` / `_http_get_bytes` / `_http_probe` all
call `assess.egress_confined_reason` after their SSRF/egress preamble). A host
outside the signed scope is refused at the socket layer, fail-closed — so even a
*hijacked* assessment (e.g. one steered by an injected target page) physically
cannot reach an out-of-scope host, the operator's LAN, or a metadata endpoint.
This is the inversion of Strix's open-egress sandbox. It is a strict **no-op**
when no assessment is confining egress (the context var is unset by default), so
every ordinary Olympus fetch is byte-for-byte unchanged. `run_assessment` runs
its phases inside `confined_egress`.

## Decision (b): a containment self-check

`assess.containment()` maps each of Strix's five blast-radius vectors to its
owning Olympus control and proves it — running LIVE checks where it can:

| Strix vector | Owned control (proven) |
|---|---|
| Prompt-only scope → act on unintended hosts | `require_scope()` raises before any I/O (live) |
| Open egress → host / LAN / metadata | `confined_egress()` refuses out-of-scope, no-op when inactive (live) |
| Refusal-suppression → no backstop | Aegis prompt carries no suppression directives + signed authorization (asserted) |
| Arbitrary payloads / spraying | active validation is benign-marker, parameter-directed, capped ≤ 20 (asserted) |
| Removed audit trail | `authorize_assessment` is an IRREVERSIBLE signed action on the ledger (live) |

`olympus assess containment` prints the scorecard; `test_assess.py` asserts all
five stay contained, so a regression that widens the blast radius fails CI.

## Consequences

- The guardrails are now **owned, active, and demonstrable**, not implicit: an
  operator (or a reviewer) can *prove* the assessment can't leave its scope.
- No new tool, action, or command (containment is a library + an `assess`
  subcommand); the shared fetch path gains one cheap, no-op-by-default check.
- Tests: 7 new in `tests/test_assess.py`.

## NOT in scope

- Confinement applies to Olympus's assessment egress, not to arbitrary
  system processes — Olympus has no open sandbox to confine (the reason this is
  belt-and-suspenders rather than the sole defense).
- The declined offensive surfaces (arbitrary-target exploitation, spraying,
  open-egress Kali sandbox) remain declined — ADR 0011 Decision (f), DEFERRED
  #16/#18. Containment makes the *absence* of those provable.
