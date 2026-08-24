# The Governed Browser Harness

Olympus can drive a real browser over the Chrome DevTools Protocol (CDP). This
document explains *why it is built the way it is*: it is a direct response to
the popular open-web pattern (an LLM wired straight to Chrome, `exec`-ing
agent-written code into a credentialed session, auto-registering hundreds of
unreviewed "skills", phoning home by default). Each of that pattern's strengths
is kept and turned into something hard to copy; each of its weaknesses is named
out loud and answered with an enforced control rather than spun.

The implementation lives in [`olympus/browser.py`](../olympus/browser.py); the
guarantees below are pinned to code in [`tests/test_browser.py`](../tests/test_browser.py).

## Strengths kept — and turned into moats

A *moat* must compound or lock in, or be something a rival can't adopt without
contradicting their own pitch ("minimal, no intermediaries, the agent writes
its own code"). Every governance layer here is exactly such an intermediary.

| Open-web strength | How Olympus makes it a moat |
| --- | --- |
| Persistent CDP connection (daemon holds the socket) | The session keeps a **replayable, auditable ledger** of every CDP call. The socket is trivial to copy; an accumulated, diffable record of sessions is not. Plugs into Olympus's existing `replaystore`/`witness`/`trace`. |
| Self-improving skills accrete over time | Skills are **provenance-stamped and reliability-scored** (`successes/runs`, content hash, source/author/time) and ranked by *measured* success — a data network effect. The mechanism is copyable; the verified, ranked corpus is not. |
| Careful credential hygiene | **Capability separation** (already in `specialists.py`): the credentialed actuator is unreachable from any run that ingests untrusted content. A rival built on `exec`-into-a-live-session can't retrofit this without abandoning the live-session model — i.e. their core feature. |
| Local-threat awareness | A **published, CI-enforced threat model** (`docs/THREAT_MODEL.md`): every exposed tool has an entry, no entry is stale. "Remove all intermediaries" is precisely the intermediary that enforces this. |
| Tiny dependency tree | The real-browser transport is an **optional, lazily-imported** extra; the core dependency set is unchanged and the surface stays small and named — `threatmodel.py` calls this out vs. "a harness that auto-registers hundreds". |
| Direct CDP / no abstraction tax | The raw `cdp` power is kept **under** an egress-gated, replayable, capability-separated layer. A pure minimalist can offer one or the other, not both. |

## Weaknesses named — and turned into credibility assets

A credibility asset states the danger plainly and ships the enforced control
that defeats it. No cosmetic spin.

| Open-web weakness | The control Olympus enforces |
| --- | --- |
| `exec` into a browser holding live credentials → prompt-injection = account takeover | **Capability separation.** `browser_act` is a registered `ACTION_TOOL`; `security.filter_tools` strips it from any run that also ingests untrusted page content. An injected page cannot reach the actuator that operates your logged-in tabs. |
| "~1,000 lines" markets the core, hides a large unreviewed skill payload | **Provenance + a measured score on every skill, and a CI-verified capability count** that can't drift from code (`capabilities.py`). |
| Self-healing → non-deterministic, not reproducible | **A per-session ledger** of every CDP call: adaptation is admitted, and made auditable/diffable. |
| Pixel-coordinate clicking is fragile | **Selector-first** (`browser_read`/`browser_act` take CSS selectors), with x/y as an explicit fallback, and a **reliability score** per skill so flakiness is measured, not hidden. |
| OSS as a funnel to a hosted cloud (lock-in) | **BYOK, local-first.** The harness attaches to *your* Chrome; no mandatory cloud, and skills are a local, portable JSON file. |
| Telemetry opt-out, phones home by default | **Default-deny egress, at two layers.** Every *navigation* passes the SSRF + egress-allowlist gate (full DNS + resolved-IP validation). And every *sub-resource* request the loaded page issues on its own — `fetch`/XHR, an `<img>` beacon, a tracking pixel — is blocked at the network layer (`Network.setBlockedURLs`, from `security.subresource_block_patterns`) when it targets a metadata host or an IP-literal private/loopback/link-local address. So an injected page can't beacon to `169.254.169.254` or `http://10.x` even without a navigation. (Honest limit: the sub-resource layer matches URL strings, so it catches metadata hosts and IP-literal targets but not a public hostname that DNS-rebinds to a private IP for a sub-resource — the navigation gate's resolve-time IP check still guards the top-level document; this is defense-in-depth beneath it.) |
| Redirect / DNS-rebind slips past a resolve-time URL check | **The gate is re-run against the *landed* URL** after navigation, not just the requested one: a 3xx or JS hop onto an internal host is blocked and the tab is sent to `about:blank` rather than surfacing its content to the model. |

### Robustness limits (hardening)

The session bounds its own resource use so a hostile or slow page can't degrade
the agent: a single CDP frame is size-capped and a stuck reply times out rather
than wedging the loop; reads wait (bounded) for `readyState=complete`; the CDP
ledger is a bounded circular buffer; and the skill store caps field/step
lengths, bounds the library (dropping the lowest-reliability tail), and skips
malformed entries on load.

## The tool surface

| Tool | Kind | Governance |
| --- | --- | --- |
| `browser_open` | ingests untrusted | SSRF + egress gate on the URL; output wrapped as untrusted |
| `browser_read` | ingests untrusted | output wrapped as untrusted |
| `browser_act` | credentialed actuator | `ACTION_TOOL` — stripped from any ingesting run |
| `browser_skill_record` | first-party write | steps sanitized; provenance + content hash recorded |
| `browser_skills` | first-party read | ranked by measured reliability |

## Attaching a real browser

By default no browser is attached and the tools say so honestly. Install the
optional browser dependencies first:

```bash
pip install "olympus-council[browser]"
```

### Chromium or Chrome

Chromium remains the default engine and uses the Chrome DevTools Protocol
transport.

Let Olympus launch a local headed Chromium browser:

```bash
export OLYMPUS_BROWSER_AUTOLAUNCH=1
```

Or attach to a Chrome/Chromium instance that you started yourself:

```bash
google-chrome --remote-debugging-port=9222
export OLYMPUS_BROWSER_CDP_URL=http://127.0.0.1:9222
```

`OLYMPUS_BROWSER_CDP_URL` accepts either a DevTools HTTP base or a ready
`ws://` page-target URL. It is supported only by the Chromium engine.

### Firefox

Install Playwright's Firefox browser, then select the engine:

```bash
python -m playwright install firefox
export OLYMPUS_BROWSER_ENGINE=firefox
export OLYMPUS_BROWSER_AUTOLAUNCH=1
```

### WebKit / Safari-compatible testing

Install Playwright WebKit, then select `webkit` or its operator-friendly
`safari` alias:

```bash
python -m playwright install webkit
export OLYMPUS_BROWSER_ENGINE=webkit
export OLYMPUS_BROWSER_AUTOLAUNCH=1
```

`OLYMPUS_BROWSER_ENGINE=safari` selects the same Playwright WebKit engine.
It does not launch Apple's Safari application directly.

Set `OLYMPUS_BROWSER_HEADLESS=1` for any auto-launched engine when no visible
browser window is required. Olympus's SSRF, subresource, capability-separation,
and audit-ledger protections continue to apply across all transports.

The existing real-browser smoke suites currently exercise Chromium/CDP:

```bash
OLYMPUS_BROWSER_SMOKE=1 pytest tests/test_browser_smoke.py -q
OLYMPUS_BROWSER_REAL=1 pytest tests/test_browser_real.py -q
```

## Browser ownership (who the browser belongs to)

Olympus drives **one** browser per installation. Whoever is signed in through it
is signed in for everything that touches it — there is no such thing as an
"unprivileged" read against a credentialed browser. On a multi-user instance
that makes the browser a shared credential, so it is bound to a single
authenticated owner by a durable lease.

### Who may own it

| Principal | Owns? |
|---|---|
| Chat identity (`tg-`, `dc-`, `sl-`, `sg-`, `wa-`, `email-`, `hook-`) | yes — the platform authenticated it |
| Logged-in web account (`u:…`), with `OLYMPUS_REQUIRE_LOGIN=1` | yes |
| CLI (`cli`) | yes — shell access is box ownership |
| `action.user` / a background job's durable owner | yes |
| `OLYMPUS_BROWSER_OWNER` | yes — operator-configured |
| **`web-…` (no accounts configured)** | **no** |
| **`shared` (the heartbeat's namespace), empty, unrecognised** | **no** |

**Web browser tools require authenticated accounts.** With
`OLYMPUS_REQUIRE_LOGIN` unset, `web.py` derives the principal from a
caller-supplied `session=` value — that is a conversation selector, not an
authentication, and the id leaks through access logs and `Referer` headers. Such
a caller is refused from every credentialed browser operation. Set
`OLYMPUS_REQUIRE_LOGIN=1` to use browser tools over the web transport.

Background work is refused for the same reason: the heartbeat runs under
`shared`, so it can neither claim nor drive a user's browser. An operator job
executing through the approval spine runs as its own durable `action.user` and
is unaffected.

### How ownership is recorded

The lease lives in `MEMORY_DIR/browser_lease.json` (mode 0600) and holds a
schema version, the exact owner identity, the transport kind, a claim timestamp,
an immutable random `lease_id`, and a diagnostic browser fingerprint.

Every mutation — claim, fingerprint backfill, release, corruption handling —
runs inside a real cross-process lock (`fcntl.flock` on POSIX,
`msvcrt.locking` on Windows) held across the whole read/validate/write-or-delete
transaction. The lock file under `MEMORY_DIR/locks/` is never deleted; only the
lease record is.

The `lease_id` is defence in depth on top of that lock: releasing requires
naming the exact generation that was validated, and the delete re-checks it, so
a release that was interrupted cannot come back and remove a *different* lease
that has since been claimed by someone else.

The **fingerprint is diagnostic only** — it tells an operator whether the
browser behind a lease is still the same instance. It is never consulted to
decide ownership. A mismatch neither releases the lease nor admits a different
owner.

Two states are deliberately unrecoverable without an operator: a lease record
that will not parse, and one whose schema this build does not understand. Both
fail closed — an unreadable lease means "owned", never "free", because "free" is
the one reading that hands the browser away.

### Reconnect is not release

`browser.reset()` disconnects and reconnects. It **does not** release ownership,
because on every transport except Playwright `close()` only drops the
connection: an auto-launched Chromium keeps running with its profile intact
(nothing terminates it, and `launch_local` reuses a live one), and a remote
browser is entirely external. Cookies, tabs, and the profile survive a reset, so
releasing there would hand a fully credentialed browser to the next caller.

There is also **no TTL and no idle timeout**. A lease that lapsed on a timer
would transfer the browser exactly when the owner stopped watching.

### Releasing and reassigning

`browser.relinquish(owner)` is the only way a lease ends, and it destroys the
credentialed state first:

* **Playwright** — closes the owned context and browser and verifies both are
  closed. The context is ephemeral, so nothing persists on disk.
* **Auto-launched Chromium** — terminates the exact child process Olympus
  started, waits for confirmed exit, deletes the exact `mkdtemp` profile it
  created, and verifies the directory is gone.

**Any failure leaves the lease held.** A half-wiped browser handed to the next
owner is the breach this exists to prevent.

### Remote CDP: ownership does not transfer automatically

A browser attached over `OLYMPUS_BROWSER_CDP_URL` is *your* browser — Olympus
did not provision its profile and will not wipe your cookies, cache, tabs, or
storage. `relinquish()` therefore refuses on that transport, and a fingerprint
change (a browser restart, a different instance on the same port) does **not**
release the lease either.

Attaching to a remote browser that has no lease requires an explicitly
configured owner, checked **before** Olympus inspects the browser's targets:

```bash
export OLYMPUS_BROWSER_OWNER='tg-12345'      # exact principal, as Olympus sees it
```

Without it, a fresh process would attach to whichever page target the browser
lists first — potentially someone's authenticated tab — and the first caller
would inherit it.

`OLYMPUS_BROWSER_OWNER` **selects** an owner; it never **promotes** one. The
value must already be a trusted principal from the table above. Setting it to
`shared`, to a `web-…` session id, or to any unrecognised string grants nothing
at all — so a typo cannot hand the browser to the heartbeat's namespace, and
pasting a caller-controlled no-login web identity does not turn it into an
authentication.

**To provision a new dedicated browser** (the safe way to reassign):

```bash
# 1. Stop pointing Olympus at the old browser.
unset OLYMPUS_BROWSER_CDP_URL

# 2. Start a browser on a profile directory created for this purpose only.
mkdir -p ~/olympus-browser-profile
chrome --remote-debugging-port=9222 --remote-allow-origins='*' \
       --user-data-dir="$HOME/olympus-browser-profile" \
       --no-first-run --no-default-browser-check

# 3. Point Olympus at it and name its owner.
export OLYMPUS_BROWSER_CDP_URL='http://127.0.0.1:9222'
export OLYMPUS_BROWSER_OWNER='tg-12345'
```

Use a *fresh* profile directory. Copying an existing one copies the sessions in
it, which is the situation the lease exists to prevent.

### What this is not

This makes the browser **exclusive**, not **partitioned**. There are no
per-user browser contexts yet — one owner at a time holds the whole browser.
True per-user profiles, tab sets, and cookie jars are separate follow-up
hardening.
