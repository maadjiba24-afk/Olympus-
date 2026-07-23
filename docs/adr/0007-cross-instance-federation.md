# ADR 0007: Cross-instance federation

Status: accepted
Date: 2026-07-22

## Context

Olympus can already answer another agent and (opt-in) call one via `a2a.py`,
but a peer there is just a URL: there is no identity, no trust model, and no way
to share what one instance has learned with another. Meanwhile every primitive a
safe federation needs already exists in the codebase and was, until now, unused
for this purpose:

- an ed25519 root of trust with domain-separated subkeys
  (`witness.sign_with(label, ...)` / `sub_public_key_hex(label)`),
- a governed, SSRF/DNS-rebinding-hardened outbound opener that re-guards every
  redirect hop (`a2a._default_fetch` → `tools._pinned_opener`),
- an untrusted-content envelope and memory scrubbers
  (`security.wrap_untrusted`, `sanitize_for_memory`, `anonymize`),
- a fail-closed bearer-auth pattern (`mcp_server`),
- the file/Postgres KV store for a peer registry.

This ADR locks the federation design before code so the trust boundary is
explicit and every new network surface is opt-in and fail-closed.

## Decision (a): identity is a witness subkey, not new crypto

A federation identity is `witness.sub_public_key_hex("federation/v1")` — derived
from the same custody seed as the release/decision-log key but distinct from it
(leaking the federation subkey never yields another). No new dependency, no new
key-management story. A peer is authenticated by a **challenge/response**: it
must sign a fresh nonce with the private key behind the public key you pinned.
Every federation message is an **envelope** signed over its `canonical_json`, so
tampering breaks verification and identity travels with the payload.

## Decision (b): trust is pinned and tiered

An unpinned peer gets nothing. A pinned peer carries one of three trust levels:
`blocked`, `task` (may ask the council), or `trusted` (may also contribute
lessons). The registry lives in `store` (`federation.peers`). Signature validity
alone is never sufficient — the signer's key must match a pinned peer at the
required tier.

## Decision (c): the network surface is opt-in and fail-closed

- Outbound (`call_peer`) and serving are gated by `OLYMPUS_FEDERATION` (default
  OFF), mirroring `OLYMPUS_A2A`.
- Outbound sends a signed envelope through the hardened opener; a name-level
  egress check rejects obvious localhost/metadata URLs up front, and the pinned
  opener is the authoritative SSRF/DNS-rebinding defense. The peer's reply
  envelope is verified against the pinned key, then returned **wrapped as
  untrusted** — a remote instance's output is never trusted as instructions.
- The inbound listener refuses to start unless a bearer token is configured, and
  the authenticated routes (`/federation/task`, `/federation/lessons`,
  `/federation/capabilities`) fail closed on a missing/wrong token. The public
  routes (`/federation/identity`, `/federation/handshake`) carry only public
  metadata / a proof of our own key.
- Inbound task text reaches the council only via `security.wrap_untrusted`, so
  capability separation strips action tools exactly as for web content.

## Decision (d): shared learning is scrubbed, trusted-only, and candidate-only

A `trusted` peer's lessons are scrubbed of secrets and PII
(`sanitize_for_memory` + `anonymize`) on the way out AND on the way in, then
**staged as candidates** for the operator's gate — never auto-committed. Cross-
instance learning therefore passes the same admission gate as any other memory:
federation can propose, it can never silently write.

## Decision (e): capability discovery + multi-peer aggregation reuse the same envelope

Deepening federation without widening its trust surface: a pinned peer can POST a
signed request to `/federation/capabilities` and receive a signed card of what an
instance offers — its specialist roster (key + scrubbed title) and skill *count*,
never skill contents (those cross only through the gated lesson sync). The route
carries the exact same gate as `/federation/task` (auth token + pinned peer with
≥ `task` trust + signed envelope), and the reply's text fields are scrubbed on
import because a file-defined agent's title is operator-supplied. `call_peers`
fans one task across several trusted peers, collecting each reply as
untrusted-wrapped data and isolating a failing peer (`{"peer", "error"}`) so one
dead instance never sinks the fan-out. Both are pure additions over the existing
signed-envelope + trust + scrub primitives — no new crypto, no new trust level,
no new network defense.

## Consequences

Federation is a thin, auditable composition of existing trust primitives rather
than a new subsystem. The default install is unchanged (every flag off). The
inbound `ask` and outbound `fetcher` are dependency-injected, so the whole layer
is unit-tested with no sockets and no live pipeline (`tests/test_federation.py`).
What federation deliberately does NOT add: a persistent listening port by
default, automatic peer discovery, or any path that applies a peer's data
without a signature, a trust tier, and the operator's gate.
