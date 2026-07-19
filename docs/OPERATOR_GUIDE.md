# Letting Olympus operate your accounts (Hermes)

Olympus can act on websites **on your behalf** — log into sites you authorize
and run tasks there (reorder something, pay a bill, check an account). This is
the **Hermes** operator. It is **off by default** and every action passes
through Olympus's approval spine, so nothing runs on your accounts without your
say-so.

> **Trust model in one line:** Hermes only touches domains you explicitly
> authorize, it never treats a web page's text as instructions, it fails closed
> on anything unexpected (2FA, CAPTCHA, a changed page), and anything
> irreversible always waits for your approval.

## Quick start

```bash
# 1. Turn the operator on
olympus operator enable

# 2. Grant the one scope it needs, and pick how much autonomy you want
olympus grant browser.operate
olympus autonomy 2                 # L2 = act after one explicit approval (safe default)

# 3. Authorize a site (manual sign-in — you log in yourself, we reuse the session)
olympus operator authorize amazon.com

# 4. Just ask, in the interactive app:
olympus
> reorder the dog food I bought last month on Amazon
```

Check what's set up and what has run at any time:

```bash
olympus operator status      # on/off, authorized sites, pending approvals, recent actions
olympus operator list        # site profiles (built-in + ones Hermes has learned)
olympus operator history     # what Olympus did on your accounts, newest first
olympus actions              # anything waiting for your approval
```

Remove a site (and delete any stored credentials for it):

```bash
olympus operator forget amazon.com
```

## Two ways to log in

- **Manual (default, recommended).** You sign in yourself in a visible browser
  window — including 2FA — and Olympus reuses that session. Your password never
  goes near Olympus.
- **Remember.** Only if you ask: `olympus operator authorize <domain> --remember`.
  You'll then be prompted **privately** (in the interactive app) to type the
  username and password; they go straight into the encrypted vault and the
  model never sees them. Requires `OLYMPUS_SECRET_KEY` to be set (the vault key).

## How a task actually runs (the safety gates)

Every operator action passes, in order:

1. **Operator enabled** for you, **and** the domain **authorized**.
2. **Egress allowlist** (only enforced in sovereign mode) — a second network fence.
3. **Capability separation** — Hermes never reads the open web, so a malicious
   page can't reach its credentialed actuator.
4. **Permission scope** `browser.operate` must be granted.
5. **Autonomy level** — irreversible/financial actions *never* auto-run; they
   wait for your approval regardless of level.
6. **Daily rate caps** — a runaway loop is capped.
7. **Approval spine** — held actions surface via `olympus actions` (or a yes/no
   prompt in the interactive app).
8. **Success check** — the action must reach a known success marker or it's
   recorded as FAILED. No blind "assume it worked."
9. **Immutable audit** — every step is logged; `olympus operator history` reads it back.

## Site profiles

A **site profile** is a declarative recipe (login selectors + named action
templates) for a domain. Olympus ships a small built-in catalog
(`olympus/profiles/`, see its README for the format) and Hermes records new
profiles as it learns a site. Your own recorded profile for a domain always
overrides a built-in one. Templates are fixed step lists (`click` / `fill` /
`wait` / `assert`) — never "do what the page says."

## Attaching a browser

Manual login needs a visible browser you can drive. Point Olympus at one with:

```bash
export OLYMPUS_BROWSER_AUTOLAUNCH=1     # let Olympus open a headed Chrome, or
export OLYMPUS_BROWSER_CDP_URL=...      # attach to a Chrome you already run
```

(Chromium is bundled via Playwright if you don't set `OLYMPUS_BROWSER_BIN`.)

## Turning it all the way off

`olympus operator disable` stops the operator (authorizations are kept for next
time). To also drop the capability entirely: `olympus revoke browser.operate`.
