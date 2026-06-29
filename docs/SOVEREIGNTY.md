# Sovereignty — provable zero-egress mode

Olympus can already call local / open-weight models (Ollama, vLLM, any
OpenAI-compatible endpoint). That makes it *capable* of running with no data
leaving the machine. **Sovereign mode** turns that latent capability into an
**enforced, fail-closed guarantee**: with it on, Olympus will not send data to
any host outside an explicit allowlist, excludes remote models from selection
entirely, routes by data-sensitivity class, and **refuses rather than leaking**.

The moat is not "can it call Ollama" — it already can. The moat is *"can it
prove nothing left the box"*, which a hosted black-box orchestrator can never
offer.

## Threat model

The adversary is **accidental or covert egress** of sensitive data while using a
capable multi-agent system:

- a specialist or tool silently calling a remote frontier API on data that must
  stay on-prem;
- a misconfiguration (a remote model left in the pool) causing a tie-break or
  fallback to a hosted model;
- a request that *should* be local being routed remotely because nothing
  enforced the constraint.

Sovereign mode addresses these at Olympus's **own application-layer egress
choke point**. See the honest boundary statement at the end for what this does
and does not guarantee.

## The egress invariant

When `OLYMPUS_SOVEREIGN` is on:

1. **Every outbound model call and tool fetch funnels through one check** —
   `security.assert_egress_allowed(host)` — which raises a typed
   `EgressBlocked` for any host not on the allowlist.
2. **The model pool excludes non-local members before selection runs.** A
   remote frontier model can never be chosen — not as the best model, not as a
   tie-break, not as a fallback. The existing capability-score selection is
   unchanged; it simply runs on the eligible (local) set.
3. **Fail closed.** If no eligible local model is configured, Olympus raises
   `NoLocalModelError` and stops. It never silently downgrades to a remote
   model or proceeds with a blocked call.

The allowlist is: **loopback (always implicitly allowed)** + **known local
providers** (the `providers.py` catalog entries with `auth="local"`, e.g.
Ollama) + **`OLYMPUS_EGRESS_ALLOWLIST`** (comma-separated hostnames, IPs, or
CIDRs).

With sovereign mode **off** (the default) the egress check is a pure no-op and
behavior is byte-for-byte unchanged.

## Settings

| Setting | Meaning |
| ------- | ------- |
| `OLYMPUS_SOVEREIGN` | `1`/`true`/`on` to enforce zero-egress mode. Off by default. |
| `OLYMPUS_EGRESS_ALLOWLIST` | Comma-separated hosts/IPs/CIDRs allowed to receive data (in addition to loopback + local providers). E.g. `vllm.internal,10.0.0.0/8`. |
| `X-Olympus-Data-Class` (header) | Per-request data class on `/v1/chat/completions`: `public` / `internal` / `restricted`. |
| `--data-class` (CLI flag on `olympus ask`) | Same taxonomy for the CLI. |

## Data-class policy table

A minimal, explicit taxonomy — a control plane, not a DLP product.

| Data class | Routing |
| ---------- | ------- |
| `restricted` | **Local-only, always** — regardless of the global sovereign flag. |
| `internal`   | Local-only **when sovereign mode is on**. |
| `public`     | May use remote **only when sovereign mode is off**. |

Default when a request specifies no class: **`public`** when sovereign mode is
off (most permissive); **at least `internal`** (i.e. local-only) when sovereign
mode is on.

So a `restricted` request stays on local models even on an instance that is
otherwise allowed to use remote APIs — useful for mixed workloads.

## Reproducible recipe: a fully local council

1. **Run a local model server.** With [Ollama](https://ollama.com):

   ```bash
   ollama serve                      # serves the OpenAI-compatible API on :11434
   ollama pull qwen2.5               # or deepseek-r1 / glm4 / llama3.1 — any open weights
   ```

   (vLLM works identically — point `base_url` at its `/v1` endpoint.)

2. **Configure Olympus to use it as the pool's local member.**

   ```bash
   export OLYMPUS_PROVIDER=openai
   export OLYMPUS_BASE_URL=http://localhost:11434/v1
   export OLYMPUS_MODEL=qwen2.5
   ```

3. **Turn on sovereign mode and start the server.**

   ```bash
   export OLYMPUS_SOVEREIGN=1
   export OLYMPUS_API_KEYS=devkey            # gate the OpenAI-compatible API
   olympus status                            # shows sovereign=ON, allowlist, eligible local members
   olympus serve --host 127.0.0.1 --port 8484
   ```

4. **Drive it with any OpenAI client** (see `docs/OPENAI_ENDPOINT.md`). Every
   request runs the full council on the local model; nothing leaves the box.

   ```bash
   curl -s http://127.0.0.1:8484/v1/chat/completions \
     -H "Authorization: Bearer devkey" -H "Content-Type: application/json" \
     -d '{"model":"olympus-council","messages":[{"role":"user","content":"hi"}]}'
   ```

If you also list a remote model (in `OLYMPUS_MODELS`), sovereign mode filters it
out of selection, and any attempt to reach its host raises `EgressBlocked` —
proven by `olympus status` (it won't appear under "eligible local") and by the
fail-closed tests.

### A model on another box on your LAN

If your local server isn't on loopback (e.g. a vLLM box at `10.0.5.20`), add it
to the allowlist so it counts as a permitted local destination:

```bash
export OLYMPUS_EGRESS_ALLOWLIST=10.0.5.20        # or a CIDR like 10.0.0.0/8
export OLYMPUS_BASE_URL=http://10.0.5.20:8000/v1
```

## Status / proof surface

- `olympus status` prints: sovereign mode on/off, the active allowlist, the
  default data class, every configured model, and which models are eligible
  (local).
- `GET /api/metrics` and `GET /api/status` include a `sovereignty` block with
  the same fields — so an operator can show the live configuration to an
  auditor.

## Honest boundary statement (what is and isn't guaranteed)

This is **application-layer enforcement at Olympus's own egress choke point**.
It guarantees that *Olympus's* model calls, tool fetches, and connector calls
will not send data to a non-allowlisted host while sovereign mode is on, and
that it fails closed instead of downgrading.

It is **not**:

- an **OS-level firewall** — it does not block egress by *other* processes, nor
  Olympus traffic that bypasses these code paths (none is intended to). For a
  hard guarantee, pair sovereign mode with an egress firewall / network policy.
- **DNS interception or pinning** — the allowlist matches on hostname and on
  resolved IP/CIDR, but a DNS-rebinding attacker who flips a record between the
  allowlist check and the connection is not fully defeated (the same caveat the
  SSRF guard documents). Prefer IP/CIDR allowlist entries for hosts you control.
- **cryptographic proof** of what was sent — signing the egress claim is
  deliberately out of scope here (a later spec). What you get today is an
  enforced, inspectable, fail-closed policy, surfaced for audit via
  `olympus status`.

In short: sovereign mode makes Olympus *itself* provably refuse to leak. Combine
it with OS/network controls when you need a guarantee that spans the whole host.
