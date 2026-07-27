# Programme — Plugin Ecosystem

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Ordering rule, stated first because it is the whole point

> **A marketplace must not launch before permission enforcement, signing,
> revocation and review are operational.**

A marketplace is a distribution channel for code that runs inside Olympus with
Olympus's credentials. Shipping distribution before enforcement is how supply-
chain incidents happen. The marketplace is the **last** milestone here.

## 2. What exists, honestly

Plugin handlers and MCP clients resolve through the same tool chokepoint, so the
side-effect boundary already covers them — an unclassified plugin tool is
**denied by default** in shadow mode, and plugin lifecycle hooks were proven
unable to rewrite past the boundary.

What does **not** exist: a manifest, a declared permission set, a sandbox,
signing, revocation, or review. A plugin today runs with the host process's
privileges.

## 3. Default deny

The plugin system is default-deny at every level:

- a capability not declared in the manifest is unavailable;
- a permission not granted by the user is denied;
- network, filesystem and secret access are denied unless declared **and**
  granted;
- an unclassified tool is denied by the existing side-effect boundary.

## 4. The permission model

| Permission | Granularity | Default |
|---|---|---|
| Tool registration | per tool name, with a side-effect band | deny |
| Network | per host allowlist | deny |
| Filesystem | per path, read or write | deny |
| Secrets | per named secret, never bulk | deny |
| Council hooks | per hook, observe-only unless declared mutating | observe-only |
| User data | per store, per principal | deny |

A permission escalation in an update requires **re-consent** — silently gaining
a permission on upgrade is the classic mobile-app failure.

## 5. Sandboxing

Process isolation with a restricted filesystem view and a network proxy
enforcing the host allowlist. In-process plugins remain supported for
first-party and self-hosted use, clearly labelled as trusted, and are ineligible
for the marketplace.

## 6. Signing, revocation, incident response

Publisher-signed manifests with an attested build; signature verified at
install *and* at load. **Revocation must be effective without an Olympus
release** — a revoked plugin fails to load on next start and is reported. An
incident runbook covers: revoke, notify affected installs, provide an
uninstall path, and disclose.

## 7. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Manifest + declared capabilities + plugin SDK | a plugin without a manifest cannot load |
| M2 | Permission enforcement + user consent | an undeclared capability is unreachable, proven adversarially like the shadow boundary |
| M3 | Sandboxing | a sandboxed plugin cannot reach the filesystem or network outside its grant |
| M4 | Signing + revocation | an unsigned plugin fails; a revoked one fails on next load, no release needed |
| M5 | Review + private plugins | an enterprise can publish privately to its own org |
| M6 | **Public marketplace** | M1–M5 complete, plus a rehearsed incident drill |

## 8. Billing and compatibility

Paid plugins require the usage ledger to attribute plugin cost — so M6 also
depends on Billing. Compatibility: a plugin declares the API version it targets,
and the host refuses an incompatible one rather than failing at runtime.
