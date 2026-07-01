# Design — HERMES, the Browser Operator

This specs a dedicated operator that can perform **autonomous logins and
credentialed actions**, run **always-on** on the heartbeat, and feed
**METIS/Prometheus** — *without* dismantling the capability-separation moat the
browser harness was built on. **Status: implemented** (all four phases shipped —
see the phase list below), gated behind the master switch (`OLYMPUS_OPERATOR`,
default off). Where this design and the code diverge, the code (and
`docs/THREAT_MODEL.md`) is authoritative — e.g. `browser_login` ships as an
`operator._gate`-guarded tool under the coarse `browser.operate` scope, not as a
spine ActionType with a per-domain `operate:<domain>` scope.

See [BROWSER_HARNESS.md](BROWSER_HARNESS.md) for the read-only harness this
extends, and [THREAT_MODEL.md](THREAT_MODEL.md) for the tool-surface binding.

## The tension, stated honestly

The harness's safety rests on one rule: **the agent that ingests untrusted web
content never holds the actuator.** "Autonomous credentialed action" sounds like
a direct violation — the operator *must* act on a logged-in session. The
resolution is not to weaken the rule but to **split the work across a trust
boundary** so the rule still holds end to end.

```
  ingests untrusted web            TRUST BOUNDARY            holds the actuator
  ───────────────────────   ┌────────────────────────┐   ────────────────────
  ARGUS (planner)           │  typed Action Plan       │   HERMES (operator)
  web=True, NO actuator  ──▶│  → actions.prepare()     │──▶ web=False, actuator
  reads pages as DATA       │  → human scope / approval │   never reads page
  proposes, cannot execute  └────────────────────────┘   prose as instructions
```

- **ARGUS** keeps reading the untrusted web and keeps having its actuator
  stripped (unchanged). It can *propose* an operator action, but proposing routes
  through `actions.prepare()` — it cannot execute.
- **HERMES** is **non-ingesting** (`web=False`, no data MCP → `_ingests()` is
  False → `allow_action` is True, so it legitimately keeps the actuator). It is
  deliberately **not** given `browser_open`/`browser_read` (the tools that return
  page *prose*). It acts only through **declarative templates** from a vetted
  Site Profile, on an **allowlisted domain**, branching on **structured
  predicates** (does this selector exist?), never on free-form page text.

So the dangerous path — *untrusted page text interpreted as instructions by the
thing holding the actuator* — never exists in a single run.

## Fit #1 — Autonomous logins & credentialed actions = ActionTypes on the spine

The operator invents **no new permission system**. Each credentialed capability
is registered as an `actions.ActionType` (`olympus/actions.py`), inheriting the
whole governance model for free:

| Operator capability | risk_class | scope | reversible (undo) |
| --- | --- | --- | --- |
| `browser_login` | `NOTABLE` | `operate:<domain>` | logout |
| `browser_operate` (idempotent template, e.g. "set quantity") | `NOTABLE` | `operate:<domain>` | template-defined |
| `browser_operate` (irreversible template, e.g. "place order") | `IRREVERSIBLE` / `FINANCIAL_LEGAL` | `operate:<domain>` | none |

What this buys, with zero new machinery (proven patterns in
`tests/test_threat_model.py`):

- **Deny-first.** No `operate:<domain>` scope granted → `approve()` fails closed
  ("not granted"). The operator does nothing by default.
- **Autonomy is a one-time grant, not a standing yes-to-everything.**
  `actions.grant_scope(user, "operate:amazon.com")` lets *reversible / NOTABLE*
  templates on that domain auto-execute thereafter (deny-first, not
  deny-always). This **is** the "autonomous" in autonomous action.
- **Irreversible always stops.** `IRREVERSIBLE`/`FINANCIAL_LEGAL` templates never
  auto-execute regardless of autonomy level (`_min_level_to_auto` = 99); they
  wait for explicit human approval every time.
- **Runaway budgets.** The spine's per-class daily limits already cap how many
  actions can fire unattended.
- **Audit.** Every prepared/approved/executed action is recorded; every CDP call
  is on the session ledger.

**Credentials** come from the encrypted vault (`olympus/vault.py`:
`vault.get(user, "site:<domain>")` → `{username, password}`, Fernet-encrypted).
The `execute` callback fills the login form via selectors from the Site Profile;
**the password never enters the model context** — the model references a vault
*key name*, never the secret. 2FA / CAPTCHA → hard stop, escalate to the user.

## Fit #2 — Always-on, via the heartbeat that already exists

`heartbeat.tick()` already runs `scheduler.run_due()`. Operator autonomy is just
**Operator Playbooks** (`olympus/playbooks.py`): `(domain, template, params,
schedule, scope)`. On each tick:

1. load due operator playbooks;
2. for each, `actions.prepare()` the templated action;
3. `actions.can_auto_execute()` decides — within granted scope + budget it runs
   unattended; otherwise it queues for approval and notifies the user;
4. record the outcome and update the Site Profile's reliability score.

A playbook is itself proposed (`playbooks.propose`) and **human-approved** before
it can ever run unattended. Master kill switch: `OLYMPUS_OPERATOR=0` (or a
sentinel file) disables the whole operator path instantly; a dry-run mode
executes `preview()` only.

## Fit #3 — Woven into METIS (learn) and Prometheus (evolve)

- **METIS** (daily learning cycle) consumes HERMES session ledgers + action
  outcomes: recompute each Site Profile's reliability (`successes/runs`), flag
  drifted selectors (a login that suddenly fails its success-marker check),
  prune flaky profiles, and write durable lessons. The operator's lived
  experience becomes scored, ranked knowledge — the data-network-effect moat,
  now fed by real actions, not just reads.
- **PROMETHEUS** (evolution/self-upgrade) watches operator failure signatures and
  **proposes** fixes — a patched selector in a Site Profile, a new action
  template, a prompt tweak — through the existing `propose_upgrade` / playbook
  approval path. Self-healing operator, human-gated. Prometheus proposes; it
  never self-applies a credentialed capability.

## New named surface (each gets a THREAT_MODEL.md row)

| Tool | Kind | Governance |
| --- | --- | --- |
| `browser_login` | credentialed actuator | ActionType (NOTABLE, `operate:<domain>`); vault creds; success-marker verified; password never in model context |
| `browser_operate` | credentialed actuator | ActionType; risk_class per template; IRREVERSIBLE → approval each run |
| `browser_exists` | non-ingesting predicate | returns a bool, never page prose — safe for the actuator-holder |
| `site_profile_record` / `site_profiles` | first-party write / read | declarative per-domain spec; provenance + reliability score (extends the skill registry) |

HERMES's loadout is `BASE_TOOLS + {browser_exists, browser_login,
browser_operate, site_profiles, schedule_task}`. It does **not** include
`browser_open`/`browser_read`. Argus is unchanged (still no actuator).

## Deny-first defaults (the operator fails closed)

| Condition (default state) | Result |
| --- | --- |
| `OLYMPUS_OPERATOR` unset | entire operator path disabled |
| domain not in `OLYMPUS_OPERATOR_DOMAINS` (⊆ egress allowlist) | refused before any navigation |
| no vault entry `site:<domain>` | `browser_login` refuses (no secret to use) |
| no `operate:<domain>` scope | every operator ActionType fails closed |
| template is IRREVERSIBLE/FINANCIAL_LEGAL | never auto-executes — human approval each run |
| daily budget for the risk class exhausted | further auto-executes refused |
| 2FA / CAPTCHA / unknown post-login state | hard stop, escalate to user |

## Residual risk (named, not hidden)

Autonomous credentialed action is intrinsically higher-risk than reading. The
controls are **defense-in-depth, not one magic boundary**:

- A malicious/injected page on an *authenticated, allowlisted* domain could try
  to mislead the operator. Mitigation: the operator can only run **pre-approved
  declarative templates** against **explicitly allowlisted domains**, branching
  on structured predicates — there is no "do whatever the page says" path, and
  irreversible steps still need approval.
- Vault compromise blast radius is bounded to allowlisted domains; credentials
  never transit the model context.
- DNS-rebind/redirect onto an internal host is already blocked by the harness's
  landed-URL re-check.

If any control can't be satisfied, the operator does nothing and asks.

## Phased implementation — all four phases shipped

1. **✅ Profiles + predicates + login.** `SiteProfile` (provenance + score),
   `browser_exists`, vault-backed `browser_login` with success verification.
   Master switch off by default.
2. **✅ `browser_operate` on the spine.** Two operate `ActionType`s
   (`browser_operate` NOTABLE, `browser_operate_irreversible` IRREVERSIBLE)
   registered in `builtin_actions`; declarative templates via
   `site_template_record`; scopes (`browser.operate`), autonomy gating,
   IRREVERSIBLE→approval, daily caps, and audit all inherited from the spine.
3. **✅ Always-on.** Operator jobs (`operator_schedule`) run by
   `heartbeat.tick()` via `operator.run_due()` — each run re-gated by the spine,
   so nothing irreversible auto-fires. Off entirely unless `OLYMPUS_OPERATOR`.
4. **✅ METIS/Prometheus.** `operator_review` prunes drifted profiles (run by
   Metis and on the daily heartbeat); `propose_site_profile` lets Prometheus
   file human-reviewable profile patches (never self-applied).

Counts, THREAT_MODEL.md rows, README, and tests are updated per phase so CI's
named-surface and capability bindings stay green throughout.
