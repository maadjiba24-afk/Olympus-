# Security residuals — the accepted limits, in one place

This document is an **index of known, accepted limits** — the things Olympus
deliberately does *not* claim to solve. They are documented so they are never
mistaken for bugs, and so a reviewer can find them all without spelunking.

Nothing here is a defect or an open task. Each item is a limit **by design**,
with its mitigations and its authoritative source doc named. Where a residual is
gated behind an operator switch, the switch is **human-only** — the agent never
flips it. If you are looking for the exhaustive per-surface analysis, follow the
"Source" link; this file only consolidates and cross-references.

> Scope note: "residual" means a limit that remains *after* the shipped
> controls. It is not a promise to remove the limit — some are inherent to the
> problem (see §1), and "hardening" them further would be security theater.

---

## 1. AP2 payment-mandate residuals

**Source:** [`AP2_THREAT_MODEL.md`](AP2_THREAT_MODEL.md),
[ADR 0002](adr/0002-ap2-mandate-cosignature.md).

- Published red-team work (e.g. arXiv 2510.25819) shows **mandate-spoofing and
  injection-during-construction are not fully solved** in *any* AP2-style
  design. The co-signature, capability binding, trusted-construction refusal,
  and human-visible transaction summary **reduce but do not eliminate** them.
- A **compromised vault or compromised trusted channel defeats the model** —
  these are pre-existing trust anchors, not introduced by the mandate work.
- **LLM-mediated construction can still be socially engineered.** The
  human-visible summary (display/sign parity) is the backstop, and it depends
  on the human actually reading it.

**Why the blast radius is bounded:** by default there is **no live rail** — a
signed mandate authorizes nothing to move money, so a defeated control's blast
radius is an *internal record*, not a payment. The live path
([ADR 0006](adr/0006-live-payment-cutover.md)) ships **inert** and can only
move money after two deliberate human acts.

## 2. Signing-key custody

**Source:** [`SIGNING.md`](SIGNING.md), [ADR 0002](adr/0002-ap2-mandate-cosignature.md).

- **Vault-local key custody** means a *single vault compromise forges both* the
  system and user signatures. The on-device external co-signer
  (`mandate.command_signer`) + an out-of-band **pinned** user public key
  (`OLYMPUS_MANDATE_USER_PUBKEY`) remove this for the co-signature — but the
  *default* remains vault-local.
- The **public default signing seed is forgeable by anyone.** Instances on it
  are labeled `dev` (integrity, never authenticity). This is now **enforced at
  boot in production** (M2): with `OLYMPUS_ENV=production` set, the CLI **refuses
  to boot** (nonzero exit, actionable remediation) when the seed is unset or is
  the shipped dev seed — `witness.require_production_seed`, called in
  `cli.main()`. A non-production instance on the default seed still boots but
  logs a one-line dev-posture warning (dev is allowed, never silent). Sovereign
  mode likewise fails closed on the default seed; `SIGNING.md` carries the
  compromise-response runbook.

## 3. Append-only ledger truncation

**Source:** module docstrings in `olympus/deltas.py`, `olympus/ledger.py`;
[`anchor.py`](../olympus/anchor.py).

- The signed logs (checkpoint chains, speculation records, delta snapshots) are
  tamper-evident against **reorder / edit / forgery / cross-target transplant**,
  and (since the H3 hardening) fail closed on a **corrupt line**. But an
  append-only log with no out-of-band reference **cannot by itself detect
  wholesale truncation** of its newest records.
- **Mitigation:** the external **head anchor** (`OLYMPUS_ANCHOR`) publishes each
  signed head outside the host's write domain, so `olympus verify-anchor`
  detects truncation/rollback. It is **off by default** — an operator enables it.

## 4. Human-only enablement switches (inert by default, by design)

The agent never sets any of these; each is an operator's deliberate act. Their
"off" state is not a gap — it is the safe default.

| Switch | What it enables | Reference |
|---|---|---|
| `OLYMPUS_PAYMENT_LIVE` + a registered adapter | real-money charges | [ADR 0006](adr/0006-live-payment-cutover.md) |
| `OLYMPUS_COMPUTER_USE` + a registered actuator | OS-level computer use | `olympus/computeruse.py` |
| `OLYMPUS_RL_SCAFFOLD` | the offline reward-model fit/export | `olympus/rlscaffold.py` |
| `OLYMPUS_MCP_EXPOSED` (+ auth token) | network-exposing the MCP server | [`OPENAI_ENDPOINT.md`](OPENAI_ENDPOINT.md), `olympus/mcp_server.py` |
| `OLYMPUS_ANCHOR` | the external head anchor | §3 above |
| `OLYMPUS_SOVEREIGN` | zero-egress sovereignty | [`SOVEREIGNTY.md`](SOVEREIGNTY.md) |
| `OLYMPUS_SLEEPTIME` / `_AUTOAPPLY` | sleep-time self-improvement / auto-apply | `olympus/sleeptime.py` |
| `OLYMPUS_OTLP_ENDPOINT` | telemetry export (egress-gated under sovereign mode) | [`Parked-3` / `otel.py`](../olympus/otel.py) |

## 5. Features gated on real adoption (not code gaps)

These are inert until *real, human-labeled* usage data exists — synthetic/test
traffic is excluded by design, so they cannot be satisfied in a test harness:

- **Learned router (SPEC-04)** — needs a threshold of labeled real routing
  outcomes across task-types/sources. Source: [`LEARNED_ROUTING.md`](LEARNED_ROUTING.md).
- **Offline RL reward model** — needs enough real preference pairs across
  contexts; it is **advisory only**, with no write-back into any decision path.
  Source: `olympus/rlscaffold.py`.
- **10-clean-cycle sleep-time graduation** — must actually be *run* ten clean
  cycles (`olympus sleeptime-supervise`); it is an operational milestone.

## 6. What the test suite does — and does not — cover

The passing unit suite proves **architecture, gates, and security logic**. It
does **not** score **AI-output quality** — that is measured separately by
`olympus eval` / `olympus scores`. A green suite means the guardrails are
correct, not that a given answer is good.

**Now gated in CI (M5).** Answer quality is regression-gated by the
**answer-quality gate** (`.github/workflows/quality-gate.yml` →
`scripts/quality_gate.py`): it runs the benchmark and **fails the build** when
any specialist regresses more than a tolerance (default 1.0/10) below the
committed baseline (`olympus/quality_baseline.json`). The pass/fail comparison
is the pure, unit-tested `evals.regression_check`; the live benchmark run makes
real model calls, so — like the replay gate — the workflow **needs a model-key
repo secret and skips cleanly (exit 0) without one**. Any one of these works
(first present wins; `scripts/ci_provider_resolve.py`): `ANTHROPIC_API_KEY`
(native), or `OPENAI_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY` /
`GROQ_API_KEY` / `MISTRAL_API_KEY` / `XAI_API_KEY` / `OPENROUTER_API_KEY` /
`KIMI_API_KEY` via the OpenAI-compatible provider, with the eval model
discovered from that account's own `/models` inventory; spend capped by
`OLYMPUS_DAILY_BUDGET`. **The baseline is live** (first keyed run's real
scores, `moonshot-v1-32k`, provenance in the file) and the first gated run
passed against it. Because scores are model-dependent, the gate **enforces
only when the resolved model matches the baseline's recorded model** — on any
other model it reports without gating until a maintainer re-baselines
(`--update-baseline`, a human act, never the agent's). The residual therefore
narrows but does not vanish: quality is only *actually* scored where a key
remains configured.

---

*This index is a convenience. When it and a source doc disagree, the source doc
(and the code it describes) is authoritative.*
