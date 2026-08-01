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

Install the optional browser support:

```bash
pip install "olympus-council[browser]"
```

Chromium is the default engine:

```bash
export OLYMPUS_BROWSER_AUTOLAUNCH=1
```

To attach to an already-running Chrome/Chromium instance instead:

```bash
export OLYMPUS_BROWSER_CDP_URL=http://127.0.0.1:9222
```

To use Firefox:

```bash
python -m playwright install firefox
export OLYMPUS_BROWSER_ENGINE=firefox
export OLYMPUS_BROWSER_AUTOLAUNCH=1
```

To use Safari-compatible Playwright WebKit:

```bash
python -m playwright install webkit
export OLYMPUS_BROWSER_ENGINE=safari
export OLYMPUS_BROWSER_AUTOLAUNCH=1
```

The `safari` value is an alias for Playwright WebKit; Olympus does not
directly launch Apple's Safari application.

## Turning it all the way off

`olympus operator disable` stops the operator (authorizations are kept for next
time). To also drop the capability entirely: `olympus revoke browser.operate`.

## OS-level computer use (screen + mouse + keyboard)

Hermes operates *web* sites through the browser. If you want Olympus to drive the
**whole desktop** — screenshot the screen, move/click the mouse, type, launch a
program — that is a separate, more powerful, and more dangerous capability, so it
is **off by default and takes two deliberate switches**:

```bash
export OLYMPUS_COMPUTER_USE=1                 # 1) allow the capability
export OLYMPUS_COMPUTER_USE_ACTUATOR=native   # 2) install the native actuator
```

The **native actuator** drives the OS through native command-line tools — no new
Python dependency — so install the ones for your platform:

- **Linux (X11):** `xdotool` plus a screenshot tool (`scrot`, `maim`,
  `gnome-screenshot`, or ImageMagick's `import`).
- **macOS:** `cliclick` (`brew install cliclick`); `screencapture` is built in.
- **Windows:** PowerShell (built in).

Check it's live with `olympus doctor` — the **computer use** line reports *off*,
*enabled but no actuator*, or *active (native actuator)*, and names any missing
tool. Every computer-use action still flows through the **same safety gates** as
everything else: it is IRREVERSIBLE-risk so it **never auto-executes** (each one
needs your explicit approval), a launched command is `cmdguard`-checked, typed
text is scanned for secrets, and every actuation is a signed audit record. Unset
either switch to turn it back off.
