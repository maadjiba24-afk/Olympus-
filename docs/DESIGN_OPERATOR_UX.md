# Design — Operator for non-engineers (plain-English setup)

The operator (HERMES) was first built with an engineer-shaped surface: env vars
(`OLYMPUS_OPERATOR`, `OLYMPUS_OPERATOR_DOMAINS`), a CLI scope grant, and a vault
key you set by hand. That's fine for developers and wrong for everyone else.

This layer makes the operator usable by a normal person **entirely through
conversation** — no env vars, no CLI, no "vault" — while keeping the engineer
surface as an additive override. Same governance underneath; friendlier door.

## Two surfaces, one safety core

| | Non-engineer (default) | Engineer / admin (override) |
| --- | --- | --- |
| Turn it on | "set up Amazon for me" → `operator_authorize_site` writes their prefs | `OLYMPUS_OPERATOR=1`, `OLYMPUS_OPERATOR_DOMAINS=…` |
| Sign in | manual: they log in themselves, we reuse the session | vault credentials + site profile |
| Approve an action | plain-English "yes" in chat (irreversible always asks) | `olympus approve <id>`, scopes, autonomy levels |
| See state | `operator_status` in plain language | `olympus pending`, prefs files |

`enabled(user)` is `env OR the user's own opt-in`; `authorized(user, domain)` is
`(env domains OR the user's authorized sites) AND the egress allowlist`. So a
normal person enables exactly the sites they ask for, and an engineer can still
pre-authorize via env. Nothing about the safety model changes — irreversible
actions still route through the approval spine.

## The login decision (the crux)

A normal person must never type a password into the chat (the model would see
it). Two safe paths, chosen per site:

- **Manual (default).** Olympus opens the site; the person signs in themselves,
  handling any 2FA; Olympus reuses that live session. **It never sees or stores
  a password.** Simplest and safest. The only cost: sessions expire, so an
  occasional re-login. This is the path the assistant leads with.
- **Remember (opt-in).** For sites they use often and trust, the person can ask
  Olympus to save the sign-in. Credentials are captured by a **secure local
  prompt — never the chat** — and stored in the encrypted vault
  (`operator.remember_credentials`, key `site:<domain>`). Auto-login each time.

`operator_authorize_site(domain, login)` records the choice; `operator_status`
shows it; `operator_forget_site` removes the site and deletes any saved sign-in.

## What a normal person experiences

> **You:** reorder my dog food on Amazon
> **Olympus:** I'm not set up for Amazon yet. Open it and sign in like normal
> (I never see your password), then tell me you're ready.
> *(they sign in, say "ready")*
> **Olympus:** Found your usual — Blue Buffalo, $42.99. Place it?
> **You:** yes
> **Olympus:** Ordered.

No commands. The irreversible "place order" still went through the approval
spine — it just surfaced as a plain-English "Place it?".

## Built in this pass (tested, behind the per-user opt-in)

- Per-user, persisted operator settings in `prefs` (`enabled`, `advanced`,
  per-site `login` mode) — `operator.authorize_site` / `forget_site` /
  `sites` / `login_mode` / `enabled` / `authorized`.
- Gating throughout (`browser_login`, `browser_operate`, `operator_schedule`,
  scheduled jobs, and the spine `execute`) is now **per-user**: env OR opt-in.
- Three conversational tools for HERMES: `operator_authorize_site`,
  `operator_forget_site`, `operator_status`. Prompt steers to manual sign-in.
- **Manual mode works end to end today** — no password handling at all.
- `remember_credentials` storage primitive + `advanced` flag are in place.

## Still to wire (interactive app layer — designed, not yet built)

These need the live REPL / a real browser and so aren't unit-testable here:

1. **Secure password capture in chat** for "remember" mode — a private local
   prompt (e.g. `getpass`/a one-time localhost form) the REPL runs when the user
   opts in; the model never receives the password. Today the primitive exists
   but the in-chat secure entry does not, so the assistant steers to manual.
2. **Auto-launching/attaching the browser** so the person doesn't run Chrome
   with debugging flags themselves.
3. **Inline plain-English approval** — surfacing a pending irreversible action
   as "Place it?" and treating "yes" as the approval, instead of `olympus
   approve <id>`.
4. **Advanced-mode toggle UX** — hide every engineer affordance unless the user
   turns on advanced; the `advanced` flag is stored, the UX gating is pending.
