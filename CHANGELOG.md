# Changelog

All notable changes to Olympus are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Olympus adheres to [Semantic Versioning](https://semver.org/). The single
source of truth for the current version is `pyproject.toml`; see
[docs/SUPPORT.md](docs/SUPPORT.md) for the versioning, LTS, and release-integrity
policy, and [RELEASING.md](RELEASING.md) for how a release is cut.

Categories: **Added** (new capabilities), **Changed** (behaviour changes),
**Fixed** (bug fixes), **Security** (hardening). A `MAJOR` bump that changes the
CLI surface, the public Python API, or the on-disk memory `schema_version`
carries a migration note here.

## [Unreleased]

### Added — operator capabilities (Hermes gap-closure)

A batch of capabilities closing the operator-axis gaps identified against
NousResearch/hermes-agent. Each ships with tests and is bound to the
CI-verified capability manifest.

- **Real execution environment** (`olympus/sandbox.py`). A workspace-confined
  shell + file surface with `local` and `docker` backends (timeout- and
  output-capped, path-escape-refused). Exposed as approval-gated actions
  `run_command` (irreversible) and `write_file` (reversible/undoable), plus
  read-only `read_file` / `list_dir` tools. Hephaestus gains the loadout.
- **Scriptable subagents** (`olympus/subagents.py` + `spawn_subagent` tool):
  ad-hoc, isolated, parallel specialist fan-out with per-branch failure
  containment.
- **Natural-language cron** (`olympus/scheduler.py`, `olympus schedule`,
  `schedule_task` tool): user-defined recurring tasks in plain English, run
  unattended by the heartbeat, results delivered to any platform.
- **Discord / Slack / Signal gateways** (`olympus/{discord,slack,signal}.py`)
  over a shared `gateway.py` router; Slack HMAC + Discord Ed25519 request
  verification.
- **Rich TUI** (`olympus/tui.py`): multiline input, `readline` slash-command
  autocomplete, streamed answers.
- **Cross-session search** (`olympus/search.py`, `olympus search`,
  `search_sessions` tool): SQLite FTS5 over all persisted conversations, with a
  LIKE fallback; indexed live on save.
- **Training-trajectory export** (`olympus/trajectories.py`,
  `olympus export-trajectories`): conversations → SFT pairs and traces →
  decision trajectories as JSONL.
- **Serverless / hibernation mode** (`olympus/hibernate.py`, `olympus tick`,
  `olympus next-wake`): run one tick and report the next-due time so an external
  scheduler can wake Olympus on demand.
- **agentskills.io interop** (`olympus/skillpack.py`, `olympus skill-import` /
  `skill-export`): import/export skills in the open SKILL.md standard.
- **Migration importer** (`olympus/migrate.py`, `olympus import-agent`): fold an
  OpenClaw/Hermes-style agent's memories, profile, and skills into Olympus;
  API keys are detected and reported, never silently stored (opt-in `--keys`).
- **Media tools** (`olympus/media.py`): `generate_image`, `text_to_speech`, and
  a link-extracting `browse_page`; generative tools degrade gracefully without a
  key and write only into the confined workspace.
- **Windows installer** (`install.ps1`): PowerShell one-liner mirroring the
  POSIX installer.

### Added — disaster recovery

- **Off-droplet data backups** (`olympus backup` / `olympus restore`,
  `olympus/backup.py`). Archives `MEMORY_DIR` (per-user memory, accounts, the
  encrypted OAuth tokens, the signed decision log), **encrypts** it at rest with
  the vault key, **signs** it with the witness Ed25519 root of trust, and hands
  it to `OLYMPUS_BACKUP_CMD` for off-machine delivery. Restore verifies the
  signature and every file's SHA-256, rejects path-traversal entries, and won't
  clobber a non-empty target. Runs on a cadence via the heartbeat (or the
  dedicated, token-free `backup` compose service). See
  [docs/BACKUPS.md](docs/BACKUPS.md).

## [0.16.0]

First formally catalogued release. Olympus is a provider-agnostic, multi-agent
assistant (Zeus routes, Athena supervises a council of specialists, Aletheia
fact-checks) with durable per-user memory, human-approved actions, and a web,
Telegram, and WhatsApp surface. This entry records the verifiability,
reliability, and public-launch work that defines the line; earlier history lives
in the git log and pull requests #1–#49.

### Added — verifiable reasoning & supply chain ("the moat")

- **Re-executable decision log.** Every run freezes its LLM requests/responses
  and pairs them with structured decision records, so a recorded run can be
  re-executed against the frozen responses and proven byte-identical, or the
  exact diverging request is pinpointed (`olympus replay`, `olympus explain`).
  See [docs/DECISION_LOG.md](docs/DECISION_LOG.md).
- **Signed releases & signed decision log.** One Ed25519 root of trust signs
  both a release manifest (`verification.json`, every tracked file's SHA-256)
  and each run's decision path. `olympus sign` / `olympus verify` /
  `olympus verify --log <run_id>`. See [docs/SIGNING.md](docs/SIGNING.md).
- **Capability manifest** generated from code and bound to the README numbers,
  enforced in CI (`olympus capabilities --check`). See
  [docs/CAPABILITIES.md](docs/CAPABILITIES.md).
- **Memory format contract** with a versioned on-disk `schema_version` and
  forward migration (`olympus memory migrate`). See
  [docs/MEMORY_FORMAT.md](docs/MEMORY_FORMAT.md).
- **Supply-chain integrity:** hash-pinned `requirements.lock`, a no-prerelease
  check, and a CycloneDX SBOM in CI. See [docs/SUPPLY_CHAIN.md](docs/SUPPLY_CHAIN.md).
- **Threat model** bound to the live tool handlers and enforced by
  `scripts/check_threat_model.py`. See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

### Added — reliability gate

- **Replay self-check tripwire:** the heartbeat re-runs real prompts on a
  cadence and a CI replay-gate workflow does the same on a schedule; a run that
  stops replaying byte-identically escalates (memory note, Telegram alert, and
  an auto-filed GitHub issue) instead of silently rotting the audit trail.
- **Operator reliability gate** (`scripts/reliability_gate.py`): runs three
  distinct prompts unattended end-to-end on a real key, proves each replays with
  zero new API calls, enforces a spend cap, and reports total spend.

### Added — public-launch safety kit

- **Problem-report channel:** a `📣 report` button and `/api/report` (works
  before login), captured durably and pushed to the operator (`olympus reports`).
- **Operator error capture:** unexpected failures are recorded to a durable
  log and rate-limited Telegram alert (`olympus errors`).
- **Cost protection:** bring-your-own-key as a *free allowance* — keyless users
  get `OLYMPUS_FREE_CHATS` operator-funded chats per day, then continue on their
  own key — alongside the all-or-nothing `OLYMPUS_REQUIRE_BYOK`, per-day and
  per-minute caps, and a daily budget.
- **Privacy & Terms pages** (`/privacy`, `/terms`) written to match real
  behaviour, with operator identity from `OLYMPUS_OPERATOR_NAME` /
  `OLYMPUS_OPERATOR_CONTACT`.

### Security — pre-release hardening

- Signing refuses to produce a release manifest under the public default seed
  (forgeable) unless explicitly marked `--dev`; verification trusts a manifest
  only against a pinned public key, and rejects dev manifests for release.
- The replay gate fails *loudly* (logged, never swallowed) on an unexpected
  internal error, distinguishing a genuine divergence from an infrastructure or
  account skip — so an empty wallet can't masquerade as a green gate.
- Load-bearing best-effort paths (memory extraction, connector token lookups)
  now log unexpected failures instead of failing silently; telemetry swallows
  are intentionally left untouched.
- `Trace.decision(status=...)` is mandatory, so a failure path can no longer
  silently record success and poison per-agent trust scoring.

[Unreleased]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/maadjiba24-afk/Olympus-/releases/tag/v0.16.0
