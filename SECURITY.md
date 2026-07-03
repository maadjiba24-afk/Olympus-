# Security Policy

## Supported versions

Security and critical-correctness fixes follow the policy in
[docs/SUPPORT.md](docs/SUPPORT.md):

| Line | Status |
| --- | --- |
| Current MINOR | Active — features, fixes, and security patches. |
| Previous MINOR | Maintained for **6 months** after the next MINOR ships — security and critical-correctness fixes. |
| Older | End-of-life — please upgrade to a maintained line. |

The single source of truth for the current version is `pyproject.toml`. Security
fixes ship as a PATCH on every maintained line.

## Reporting a vulnerability

**Please report suspected vulnerabilities privately — do not open a public
issue**, so a fix can ship before the problem is widely known.

- Preferred: open a [GitHub private security advisory](https://github.com/maadjiba24-afk/Olympus-/security/advisories/new)
  ("Report a vulnerability"). This keeps the report private to the maintainers.
- Alternatively, if you run an instance, the in-app **📣 report** button and the
  operator contact in the Privacy/Terms pages (`OLYMPUS_OPERATOR_CONTACT`) reach
  the operator directly.

Please include enough to reproduce: affected version/commit, configuration, and
a minimal proof-of-concept. We aim to acknowledge a report within a few days and
to agree on a coordinated disclosure timeline before any public detail is shared.

Out of scope: findings that require a malicious operator (the operator already
controls the deployment, the signing seed, and the OAuth encryption key), or
that depend on an upstream model provider's behaviour rather than Olympus.

## What Olympus already does to limit blast radius

These are design properties you can rely on, not promises of perfection:

- **Human-approved actions.** Olympus *prepares* sensitive or irreversible
  actions (sending email, calendar writes, webhooks) but never performs them
  without explicit user approval. Data connectors and action connectors are
  separated: the fact-checking stage can read the web but cannot act.
- **Tamper-evident audit trail.** Every run's decision path is signed with an
  Ed25519 root of trust (`olympus verify --log <run_id>`), and releases ship a
  signed manifest verified against a pinned key (`olympus verify`). See
  [docs/SIGNING.md](docs/SIGNING.md) and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).
- **Untrusted content is treated as data.** Specialist outputs and fetched web
  content are explicitly flagged as untrusted in the verifier's prompt — never
  obeyed as instructions (prompt-injection defence).
- **Memory hygiene.** The memory-extraction model is instructed not to store
  secrets, passwords, or key-shaped strings, and injection-marker lines are
  stripped before a write (`sanitize_for_memory`). This is best-effort
  extraction guidance, not a guaranteed secret filter on the write path — treat
  it as hygiene, not a hard boundary. OAuth tokens are encrypted at rest with a
  key the operator controls.
- **Cost & abuse limits.** Per-minute and per-day rate limits, a daily budget,
  and bring-your-own-key (as a wall or a free allowance) bound spend and abuse on
  a public instance.

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the full threat model and
the assumptions behind these properties.
