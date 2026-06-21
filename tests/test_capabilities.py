"""The generated capability manifest + drift gate (Task 3 of the moat plan).

What these prove:
1. The manifest counts are derived from the live registries (not hand-typed).
2. The committed `capabilities.json` and the README numbers match the code —
   the repo is consistent right now.
3. The gate genuinely FAILS on a deliberate mismatch (stale manifest, wrong
   README number, missing/unknown claim) and PASSES when consistent — so a
   published number can never silently drift.
"""

import json

from olympus import actions, builtin_actions, capabilities, cli  # noqa: F401
from olympus import tools
from olympus.specialists import SPECIALISTS


# --- 1. counts come from the code ----------------------------------------

def test_manifest_counts_match_live_registries():
    m = capabilities.manifest()
    assert m["agents"]["count"] == len(SPECIALISTS)
    assert m["agents"]["names"] == sorted(SPECIALISTS)
    assert m["tools"]["count"] == len(tools.HANDLERS)
    assert m["actions"]["count"] == len(actions.registered())
    assert m["commands"]["count"] == len(cli.command_names())
    # the manifest is deterministic JSON (sorted keys, trailing newline)
    assert capabilities.to_json(m).endswith("}\n")
    assert json.loads(capabilities.to_json(m)) == m


# --- 2. the repo is consistent right now ---------------------------------

def test_committed_manifest_is_current():
    path = capabilities.manifest_path()
    assert path.exists(), "capabilities.json must be committed"
    assert path.read_text(encoding="utf-8") == capabilities.to_json(), (
        "capabilities.json is stale — run `olympus capabilities --write`")


def test_repo_passes_the_drift_gate():
    assert capabilities.check_repo() == []


# --- 3. the gate catches drift -------------------------------------------

def _good_readme(m):
    return (f"ships <!--cap:agents-->{m['agents']['count']}<!--/cap--> agents, "
            f"<!--cap:tools-->{m['tools']['count']}<!--/cap--> tools, "
            f"<!--cap:commands-->{m['commands']['count']}<!--/cap--> commands.")


def test_check_passes_when_consistent():
    m = capabilities.manifest()
    assert capabilities.check(capabilities.to_json(m), _good_readme(m), m) == []


def test_check_fails_on_wrong_readme_number():
    m = capabilities.manifest()
    bad = "we have <!--cap:agents-->999<!--/cap--> specialists."
    problems = capabilities.check(capabilities.to_json(m), bad, m)
    assert any("999" in p and "agents" in p for p in problems)


def test_check_fails_on_stale_manifest():
    m = capabilities.manifest()
    problems = capabilities.check('{"schema_version": 1}', _good_readme(m), m)
    assert any("stale" in p for p in problems)


def test_check_fails_on_missing_manifest():
    m = capabilities.manifest()
    problems = capabilities.check(None, _good_readme(m), m)
    assert any("missing" in p for p in problems)


def test_check_fails_when_readme_has_no_bound_claims():
    m = capabilities.manifest()
    problems = capabilities.check(capabilities.to_json(m),
                                  "no claims here at all", m)
    assert any("no bound capability claims" in p for p in problems)


def test_check_flags_unknown_claim_key():
    m = capabilities.manifest()
    problems = capabilities.check(capabilities.to_json(m),
                                  "<!--cap:wizards-->3<!--/cap-->", m)
    assert any("wizards" in p for p in problems)


def test_readme_claims_parser():
    claims = capabilities.readme_claims(
        "a <!--cap:agents-->12<!--/cap--> b <!--cap:tools-->26<!--/cap-->")
    assert ("agents", 12) in claims and ("tools", 26) in claims
