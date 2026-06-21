# Capabilities — generated from code, bound to the prose

Olympus's published capability counts (agents, tools, actions, CLI commands) are
**generated from the code**, committed as a manifest, and the README prose is
**bound** to that manifest so a number can never silently drift. CI fails if it
does.

This is the lesson from Ruflo: it *generates* capability baselines, yet its
README still contradicts itself (100 vs 98 vs 60 agents). Generating a manifest
isn't enough — the prose has to be pinned to it. Olympus does both.

## The manifest

`olympus/capabilities.py` introspects the live registries:

| Section | Source of truth |
| --- | --- |
| `agents` | `specialists.SPECIALISTS` |
| `tools` | `tools.HANDLERS` (the client-side tool surface) |
| `actions` | `actions.registered()` (after built-ins register) |
| `commands` | `cli.command_names()` (the argparse subcommands) |

`manifest()` returns `{schema_version, agents, tools, actions, commands}`, each
section being `{count, names}`. It is deterministic — sorted keys, sorted names,
no timestamps or version — so the committed file changes **only** when
capabilities actually change.

```bash
olympus capabilities            # print the manifest as JSON
olympus capabilities --write    # (re)generate olympus/capabilities.json
olympus capabilities --check    # exit non-zero on any drift (used by CI)
```

The generated file is committed at `olympus/capabilities.json` and ships as
package data (`pyproject.toml`'s `package-data` already includes `*.json`).

## Binding the README to it

Numbers in the README are wrapped in markers:

```
ships <!--cap:agents-->12<!--/cap--> specialist agents,
<!--cap:tools-->26<!--/cap--> tools, and <!--cap:commands-->42<!--/cap--> commands.
```

`olympus capabilities --check` parses every `<!--cap:KEY-->N<!--/cap-->` marker
and asserts `N == manifest[KEY].count`. Numbers that aren't wrapped (e.g. an
example of command output) are ignored, so only *claims you choose to bind* are
enforced — no false positives on incidental digits.

## The CI gate

`.github/workflows/ci.yml` runs `python -m olympus capabilities --check`. The
build fails if:

- `capabilities.json` is missing or stale (differs from freshly generated), or
- any bound README number disagrees with the code, or
- a marker references a manifest section that doesn't exist.

So adding a specialist, tool, or command means regenerating the manifest
(`olympus capabilities --write`) and updating any bound README number — and CI
won't go green until the published numbers match what's actually built.
