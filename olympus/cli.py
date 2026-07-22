"""Olympus command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import heartbeat, memory, orchestrator

# The label every surface MUST attach to a pass produced under the public
# default seed — a default-seed pass proves integrity, never authenticity.
_DEV_LABEL = ("DEV / UNVERIFIED — signed by the public default key; proves the "
              "log is internally consistent, not that it came from a trusted "
              "signer.")


def _chat(conversation_id: str = "cli-default") -> None:
    from . import tui
    tui.run(conversation_id=conversation_id)


def _last_session_id() -> str:
    """The most recently used terminal session (for `olympus -c`)."""
    from . import memory
    sessions = memory.list_conversations(prefix="cli")
    return sessions[0]["id"] if sessions else "cli-default"


def _verify_run_combined(run_id: str, require_production: bool) -> int:
    """Auditor-facing single PASS/FAIL for a recorded run: BOTH halves of the
    guarantee — replay (byte-identical decision path) AND decision-log signature
    against the trusted key. Reuses orchestrator.replay_run and
    witness.verify_run; adds the pinned-key authenticity and dev-seed posture
    gates. Returns a process exit code (0 = PASS)."""
    from . import witness, trace, replaystore, orchestrator as orch
    run = trace.load_run(run_id)
    if not run:
        print(f"[verify] FAIL: no recorded run '{run_id}' in the traces.")
        return 1

    posture = "dev" if witness.log_signed_by_default(run) else "production"

    # Half 1 — replay the reasoning against the frozen responses.
    replay_ok, replay_msg = True, ""
    try:
        original, _fresh, diffs = orch.replay_run(run_id)
        if diffs:
            replay_ok = False
            d0 = diffs[0]
            o = (d0.get("original") or {}).get("decision_type", "∅")
            r = (d0.get("replayed") or {}).get("decision_type", "∅")
            replay_msg = (f"decision #{d0['index']} DIVERGED ({o} → {r}); "
                          f"{len(diffs)} decision(s) differ from the recording")
        else:
            n = len(original.get("decisions", []))
            replay_msg = f"{n} decision(s) replayed byte-identically"
    except replaystore.ReplayDivergence as err:
        replay_ok, replay_msg = False, str(err)
    except ValueError as err:
        replay_ok, replay_msg = False, str(err)

    # Half 2 — decision-log signature. A dev-posture run is signed by the public
    # default seed, so verify its signature against that default key (integrity
    # only) rather than the local key — otherwise a run recorded in dev fails
    # here the moment the instance sets OLYMPUS_SIGNING_SEED, and the dev-PASS
    # branch below becomes unreachable. Authenticity for production runs is still
    # enforced by the pinned-key check further down.
    sig = witness.verify_run(
        run_id,
        pin=witness.default_public_key_hex() if posture == "dev" else None)
    sig_ok = bool(sig.get("ok"))
    sig_msg = ("decision-log signature valid" if sig_ok
               else "; ".join(sig.get("problems", []) or ["signature invalid"]))
    # Pinned-key authenticity applies to PRODUCTION runs only: a dev-posture run
    # (signed by the public default key) is integrity-only by definition and is
    # handled by the dev label / --require-production gate below, not by the pin
    # (which is the production trust anchor).
    pins = witness.pinned_pubkeys()
    signer = ((run.get("log_signature") or {}).get("publicKey") or "").lower()
    if sig_ok and posture == "production" and pins and signer not in pins:
        sig_ok = False
        sig_msg = (f"signed by an UNTRUSTED key {signer[:16]}… that matches "
                   f"none of the {len(pins)} pinned public key(s)")

    print(f"Run {run_id} — verification ({posture} signing posture)")
    print(f"  replay    : {'PASS' if replay_ok else 'FAIL'} — {replay_msg}")
    print(f"  signature : {'PASS' if sig_ok else 'FAIL'} — {sig_msg}")

    if require_production and posture == "dev":
        print(f"  RESULT    : FAIL — {_DEV_LABEL}\n"
              "              --require-production forbids a default-seed run.")
        return 1
    if not (replay_ok and sig_ok):
        print("  RESULT    : FAIL — this run is NOT verifiable as recorded.")
        return 1
    if posture == "dev":
        print(f"  RESULT    : PASS (integrity only) — {_DEV_LABEL}")
    else:
        print("  RESULT    : PASS — replay-identical and signed by the trusted "
              "production key.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the full CLI parser. Exposed (not inlined in main) so the
    capability manifest can introspect the real command surface from code."""
    parser = argparse.ArgumentParser(
        prog="olympus",
        description="OLYMPUS — self-improving multi-agent AI system",
    )
    parser.add_argument("-c", "--continue", dest="continue_last",
                        action="store_true",
                        help="continue the most recent chat session")
    sub = parser.add_subparsers(dest="command")

    p_chat = sub.add_parser("chat", help="interactive conversation (default)")
    p_chat.add_argument("--session", help="named session id to open/resume")
    p_sess = sub.add_parser(
        "sessions", help="list past chat sessions and resume one")
    p_sess.add_argument("id", nargs="?",
                        help="session id to resume (no arg = pick from a list)")
    p_setup = sub.add_parser(
        "setup", help="choose your AI provider & save your API key; or edit one "
                      "section: setup <model|terminal|gateway|tools>")
    p_setup.add_argument("section", nargs="?",
                         help="edit just one part (model/terminal/gateway/tools)")
    p_cfg = sub.add_parser(
        "config", help="view or change saved settings (secrets are masked)")
    p_cfg.add_argument("action", nargs="?", default="show",
                       choices=["show", "set", "edit"])
    p_cfg.add_argument("kv", nargs="*",
                       help="for set: <KEY> <VALUE>")
    sub.add_parser("doctor", help="check readiness: provider, sandbox, security, "
                                  "memory, optional capabilities")
    p_soul = sub.add_parser(
        "soul", help="view or edit Olympus's personality (~/.olympus/soul.md), "
                     "injected into every reply")
    p_soul.add_argument("action", nargs="?", default="show",
                        choices=["show", "edit"])
    sub.add_parser("version", help="show the installed Olympus version")
    sub.add_parser("growth", help="show how Olympus has adapted to you over time")
    p_up = sub.add_parser("upgrade", help="update Olympus to the latest release")
    p_up.add_argument("--git", action="store_true",
                      help="upgrade from the GitHub repo (latest main) instead "
                           "of PyPI")
    p_prof = sub.add_parser(
        "profile", help="show or set what Olympus remembers about you")
    p_prof.add_argument("about", nargs="*",
                        help="a note about you (no args = show the card)")
    p_prof.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"),
                        help="set a fact, e.g. --set company Acme")
    p_prof.add_argument("--clear", action="store_true", help="forget it all")
    p_mem = sub.add_parser(
        "memory", help="inspect/approve/forget durable memories; or carry your "
                       "file memory out (migrate/export/import/delete)")
    p_mem.add_argument(
        "action", nargs="?", default="list",
        choices=["card", "list", "candidates", "approve", "reject", "forget", "search",
                 "migrate", "export", "import", "delete"])
    p_mem.add_argument("arg", nargs="*",
                       help="id (approve/reject/forget), query (search), or "
                            "archive path (export/import)")
    p_mem.add_argument("--user", help="memory namespace for export/delete "
                                      "(default: shared)")
    p_mem.add_argument("--all", action="store_true",
                       help="export every user's memory, not just one")
    p_mem.add_argument("--out", help="archive path for export")
    p_mem.add_argument("--category", help="limit delete to one category "
                                          "(e.g. lessons)")
    p_mem.add_argument("--id", dest="note_id",
                       help="limit delete to one note (filename or stem)")
    p_mem.add_argument("--encrypt", action="store_true",
                       help="encrypt the export with OLYMPUS_SECRET_KEY")
    p_mem.add_argument("--yes", action="store_true",
                       help="skip the confirmation prompt on delete")
    p_pb = sub.add_parser(
        "playbook", help="save/run/manage repeatable workflows")
    p_pb.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "proposed", "show", "save", "run", "forget",
                 "approve", "reject"])
    p_pb.add_argument("name", nargs="?", help="playbook name or id")
    p_pb.add_argument("steps", nargs="*",
                      help="for save: steps separated by ';'")
    p_gr = sub.add_parser(
        "graph", help="inspect the people/companies relationship graph")
    p_gr.add_argument("entity", nargs="*",
                      help="an entity to describe (no args = list everything)")
    p_gr.add_argument("--forget", metavar="ENTITY", help="remove an entity")
    sub.add_parser("outcomes", help="Olympus's track record: what you approved, "
                                    "edited, or declined")
    sub.add_parser("status", help="instance health: provider, spend, usage")
    sub.add_parser("routing-stats",
                   help="routing-outcome telemetry (SPEC-04 Phase A) + the "
                        "Phase B data-gate readiness check")
    sub.add_parser("learned", help="what Olympus learned/did on its own "
                                   "(the autonomous loop)")
    sub.add_parser("reports", help="problem reports users submitted from the web UI")
    sub.add_parser("errors", help="recent captured runtime errors (operator view)")
    sub.add_parser("dashboard", help="one consolidated operator health view "
                                     "(uptime, spend, reports, errors, policy)")
    p_backup = sub.add_parser(
        "backup", help="archive, encrypt, sign, and deliver a data backup of "
                       "your memory/accounts/tokens off-droplet")
    p_backup.add_argument("--list", action="store_true",
                          help="list local backup archives instead of making one")
    p_backup.add_argument("--full", action="store_true",
                          help="include the (large, reproducible) replay caches")
    p_backup.add_argument("--no-deliver", action="store_true",
                          help="make the archive but don't run OLYMPUS_BACKUP_CMD")
    p_restore = sub.add_parser(
        "restore", help="restore MEMORY_DIR from a backup archive "
                        "(verifies signature + every file hash)")
    p_restore.add_argument("archive", help="path to a backup archive")
    p_restore.add_argument("--into", help="restore target dir (default: MEMORY_DIR)")
    p_restore.add_argument("--force", action="store_true",
                           help="overwrite a non-empty target")
    p_restore.add_argument("--insecure", action="store_true",
                           help="proceed despite a signature/hash failure "
                                "(not recommended)")

    p_replay = sub.add_parser(
        "replay", help="re-execute a recorded run against its frozen LLM "
                       "responses and prove the decision path is unchanged")
    p_replay.add_argument("run_id", help="the run id from a trace")
    p_explain = sub.add_parser(
        "explain", help="show the decision path of a recorded run, or one "
                        "decision record by id")
    p_explain.add_argument("id", help="a run id, or a decision record id")

    p_cap = sub.add_parser(
        "capabilities", help="print the code-generated capability manifest "
                             "(agents, tools, commands)")
    p_cap.add_argument("--write", action="store_true",
                       help="(re)write capabilities.json from the live code")
    p_cap.add_argument("--check", action="store_true",
                       help="exit non-zero if the committed manifest or README "
                            "numbers drift from the code")

    p_sign = sub.add_parser(
        "sign", help="generate a signed release manifest (verification.json) "
                     "over the package files")
    p_sign.add_argument("--out", help="manifest output path "
                                      "(default: in-package verification.json)")
    p_sign.add_argument("--dev", action="store_true",
                        help="allow signing under the public default seed "
                             "(marks the manifest as dev — local use only)")
    p_verify = sub.add_parser(
        "verify", help="verify the signed release manifest, or a recorded run's "
                       "signed decision log")
    p_verify.add_argument("--manifest", help="manifest path "
                                             "(default: in-package verification.json)")
    p_verify.add_argument("--log", metavar="RUN_ID",
                          help="instead, verify a recorded run's "
                               "decision-log signature")
    p_verify.add_argument("--run", metavar="RUN_ID",
                          help="verify a recorded run END TO END: replay "
                               "(byte-identical decision path) AND decision-log "
                               "signature, reported as one PASS/FAIL")
    p_verify.add_argument("--allow-dev", action="store_true",
                          help="accept a dev manifest (signed by the public "
                               "default seed) for local use")
    p_verify.add_argument("--require-production", action="store_true",
                          help="FAIL (exit non-zero) if the artifact was signed "
                               "under the public default seed, regardless of "
                               "internal validity (the inverse of --allow-dev)")
    p_otel = sub.add_parser(
        "otel-export",
        help="export a recorded run's decision-log STRUCTURE (no content) to the "
             "configured OTLP endpoint (OLYMPUS_OTLP_ENDPOINT) — one trace per "
             "run, one span per decision")
    p_otel.add_argument("run", metavar="RUN_ID",
                        help="the recorded run id to export")
    sub.add_parser(
        "verify-anchor",
        help="compare every locally signed log head (checkpoints, speculation, "
             "delta snapshots) against the external anchor sink and report "
             "divergence — detects truncation an append-only log can't self-detect")
    sub.add_parser("witness-pubkey", aliases=["pubkey"],
                   help="print the Ed25519 public key for the "
                                          "current signing seed (to pin it)")
    p_keygen = sub.add_parser(
        "keygen", help="generate a 256-bit signing seed file (mode 0600) and "
                       "print the public key to pin — see docs/SIGNING.md")
    p_keygen.add_argument("--out", metavar="PATH", default=None,
                          help="where to write the seed file "
                               "(default: <memory dir>/signing_seed)")
    p_keygen.add_argument("--force", action="store_true",
                          help="overwrite an existing seed file (the old key "
                               "becomes unrecoverable)")

    p_ask = sub.add_parser("ask", help="one-shot question through the full pipeline")
    p_ask.add_argument("question", nargs="+")
    p_ask.add_argument("--data-class", choices=("public", "internal", "restricted"),
                       default=None,
                       help="data-sensitivity class for routing; 'restricted' "
                            "stays local even with sovereign mode off")

    p_res = sub.add_parser(
        "research",
        help="deep research: plan → iterative web search/read → cited report")
    p_res.add_argument("question", nargs="+")
    p_res.add_argument("--rounds", type=int, default=None,
                       help="search/read rounds (default "
                            "OLYMPUS_RESEARCH_ROUNDS or 4)")
    p_res.add_argument("--out", default=None,
                       help="also write the report to this markdown file")

    sub.add_parser("scan", help="Argus: scan the web for opportunities now")
    sub.add_parser("audit", help="Prometheus: self-audit and self-upgrade now")

    p_watch = sub.add_parser("watch", help="Mnemosyne: watch a YouTube video now")
    p_watch.add_argument("url")

    p_queue = sub.add_parser("queue", help="queue a YouTube video for the heartbeat")
    p_queue.add_argument("url")

    p_search = sub.add_parser("search", help="full-text search across all past "
                                             "conversations")
    p_search.add_argument("query", nargs="+", help="words to search for")
    p_search.add_argument("--reindex", action="store_true",
                          help="rebuild the index from conversation files first")
    p_traj = sub.add_parser("export-trajectories",
                            help="export past runs as training-data JSONL "
                                 "(SFT pairs or decision trajectories)")
    p_traj.add_argument("--out", required=True, help="output .jsonl path")
    p_traj.add_argument("--kind", default="messages",
                        choices=["messages", "trace"])
    sub.add_parser("heartbeat", help="run the self-recurring autonomous loop")
    sub.add_parser("tick", help="run ONE heartbeat tick and exit "
                                "(for cron/serverless/hibernating deploys)")
    sub.add_parser("next-wake", help="seconds until the next scheduled work is "
                                     "due (for external schedulers)")
    p_sched = sub.add_parser("schedule", help="manage natural-language "
                                              "scheduled tasks (run by the heartbeat)")
    p_sched.add_argument("action", nargs="?", default="list",
                         choices=["list", "add", "remove", "enable", "disable", "run"])
    p_sched.add_argument("name", nargs="?", default="")
    p_sched.add_argument("interval", nargs="?", default="daily",
                         help="e.g. daily, hourly, 'every 6h'")
    p_sched.add_argument("prompt", nargs="*", help="the task to run each time")
    p_sched.add_argument("--to", default="", dest="deliver_to",
                         help="deliver result to: telegram|discord|slack|signal|ntfy")
    p_sched.add_argument("--on-exit", type=int, default=0, dest="on_exit_pid",
                         metavar="PID",
                         help="event-driven: run once when this process exits "
                              "(the interval argument is ignored)")
    p_sched.add_argument("--on-change", default="", dest="on_change_cmd",
                         metavar="CMD",
                         help="change-driven: run whenever this command's "
                              "output changes (the interval argument is ignored)")
    p_goal = sub.add_parser("goal", help="standing goals with completion "
                                         "contracts (worked by the heartbeat)")
    p_goal.add_argument("action", nargs="?", default="list",
                        choices=["list", "add", "check", "work", "drop",
                                 "done", "wait"])
    p_goal.add_argument("detail", nargs="*",
                        help='add: <goal text> [:: <what done means>] · '
                             'check/work/drop/done: <goal id> · '
                             'wait: <goal id> <pid>')
    p_dist = sub.add_parser("distill", help="learn a reusable skill from a "
                                            "URL, path, or described workflow")
    p_dist.add_argument("source", nargs="+", help="url | file/dir | description")
    p_jour = sub.add_parser("journey", help="the timeline of what Olympus "
                                            "has learned (show/rm entries)")
    p_jour.add_argument("action", nargs="?", default="list",
                        choices=["list", "show", "rm"])
    p_jour.add_argument("ref", nargs="?", default="",
                        help="entry ref from the timeline")
    p_moa = sub.add_parser("moa", help="one-shot mixture-of-agents: every "
                                       "pool member answers, strongest "
                                       "aggregates")
    p_moa.add_argument("prompt", nargs="+", help="the question")
    sub.add_parser("learn", help="Metis: run the daily learning cycle now")
    sub.add_parser("eval", help="run the quality benchmark and save the score")
    sub.add_parser("code-eval", help="run execution-scored coding benchmarks "
                                     "(runs Hephaestus's code against tests)")
    sub.add_parser("skills", help="list the self-built skill library")
    sub.add_parser("skills-starter",
                   help="install a small curated starter-skill pack "
                        "(provisional — benchmark-gated like any other skill)")
    sub.add_parser("gate", help="benchmark-gate provisional skills now")
    sub.add_parser("curate", help="grade the skill library; prune what a "
                                  "benchmark proves safe to remove")
    p_evolve = sub.add_parser("evolve", help="feature self-evolution: health "
                                             "board, run the review, or reset "
                                             "auto-tuned params to defaults")
    p_evolve.add_argument("action", nargs="?", default="status",
                          choices=["status", "review", "reset", "log"])
    p_evolve.add_argument("feature", nargs="?", default=None,
                          help="feature to reset/filter (default: all)")
    p_sleep = sub.add_parser("sleeptime",
                             help="idle-time memory refinement: status, run a "
                                  "cycle, list/apply/revert reversible rewrites")
    p_sleep.add_argument("action", nargs="?", default="status",
                         choices=["status", "run", "proposals", "apply",
                                  "revert"])
    p_sleep.add_argument("ident", nargs="?", default=None,
                         help="user (proposals) or snapshot id (revert)")
    sub.add_parser(
        "sleeptime-supervise",
        help="run ONE supervised reflection cycle (apply hard-off), print the "
             "evidence report, grade it CLEAN/DIRTY on the signed scoreboard — "
             "the executable form of the 10-clean-cycle graduation rule")
    p_paylive = sub.add_parser(
        "pay-live",
        help="LIVE payment cutover status: which human acts are still missing "
             "(operator flag, registered adapter, pinned user key) and the "
             "live caps — the go-live checklist derived from the code gates. "
             "Read-only; --reconcile audits signed live charges against the "
             "adapter. This build ships INERT (no adapter, switch off).")
    p_paylive.add_argument("--reconcile", metavar="USER", default=None,
                           help="reconcile a user's signed live charges "
                                "against the adapter's records")
    p_rl = sub.add_parser(
        "rl-scaffold",
        help="OFFLINE preference-data + reward-model scaffold from the logged "
             "human-labeled outcomes — read-only dataset report; --fit fits the "
             "transparent linear reward model (gated on real data), --export "
             "signs it. NOT a live training loop; changes no behavior.")
    p_rl.add_argument("--fit", action="store_true",
                      help="fit the offline reward model (needs OLYMPUS_RL_SCAFFOLD)")
    p_rl.add_argument("--export", action="store_true",
                      help="record a witness-signed export (needs OLYMPUS_RL_SCAFFOLD)")
    sub.add_parser("liveeval", help="sampled online quality-eval of recent runs "
                                    "(rule-based scorers; opt-in, read-only)")
    p_a2a = sub.add_parser("a2a", help="agent-to-agent interop: print this "
                                       "instance's A2A agent card, or call "
                                       "another agent (egress-gated, opt-in)")
    p_a2a.add_argument("action", nargs="?", default="card",
                       choices=["card", "call"])
    p_a2a.add_argument("url", nargs="?", default=None, help="peer task URL (call)")
    p_a2a.add_argument("message", nargs="?", default=None, help="message (call)")
    p_scaf = sub.add_parser("scaffold-evolve",
                            help="propose-only scaffold evolution: status, or "
                                 "list archived proposals as diffs (never "
                                 "auto-applies)")
    p_scaf.add_argument("action", nargs="?", default="status",
                        choices=["status", "proposals"])
    p_scaf.add_argument("module", nargs="?", default=None,
                        help="filter proposals by module")
    sub.add_parser("mcp-serve", help="expose Olympus as an MCP server on "
                                     "stdio (for Claude Desktop, IDEs, ...)")
    p_skim = sub.add_parser("skill-import", help="import agentskills.io SKILL.md "
                                                 "file(s) into the skill library")
    p_skim.add_argument("path", help="a SKILL.md, its directory, a tree to "
                                     "scan, or a public GitHub URL (repo, "
                                     "tree, blob, or raw SKILL.md — remote "
                                     "imports are always provisional)")
    p_skim.add_argument("--provisional", action="store_true",
                        help="route imports through the benchmark gate")
    p_skex = sub.add_parser("skill-export", help="export a skill as an "
                                                 "agentskills.io SKILL.md")
    p_skex.add_argument("name", help="skill name to export")
    p_skex.add_argument("--out", default=".", help="destination directory")
    p_mig = sub.add_parser("import-agent", help="migrate memories/skills/profile "
                                                "from another agent (OpenClaw/Hermes-style)")
    p_mig.add_argument("path", help="the other agent's data directory")
    p_mig.add_argument("--keys", action="store_true",
                       help="also import recognised provider API keys")
    p_train = sub.add_parser("train", help="score all specialists and have "
                                           "Prometheus strengthen the weakest")
    p_train.add_argument("--focus", type=int, default=2,
                         help="how many of the weakest specialists to improve")
    sub.add_parser("scores", help="show per-specialist benchmark scores")
    p_models = sub.add_parser("models",
                              help="show the model pool and role assignments")
    p_models.add_argument("action", nargs="?", default="show",
                          choices=["show", "discover"],
                          help="'discover' lists the models your configured "
                               "key can reach (live)")
    sub.add_parser("contrib", help="show the cross-model contribution queue size")
    p_usage = sub.add_parser("usage", help="show estimated token/cost spend")
    p_usage.add_argument("--days", type=int, default=7)
    p_budget = sub.add_parser(
        "budget", help="show or set the daily spend cap on your API key (USD)")
    p_budget.add_argument("amount", nargs="?", type=float,
                          help="e.g. 5  (0 removes the cap)")

    # --- the Action spine (controlled-autonomy execution) ---
    sub.add_parser("actions", help="list pending actions awaiting approval")
    p_ap = sub.add_parser("approve", help="approve and execute a prepared action")
    p_ap.add_argument("action_id")
    p_rj = sub.add_parser("reject", help="reject a prepared action")
    p_rj.add_argument("action_id")
    p_rj.add_argument("reason", nargs="*")
    p_ed = sub.add_parser("edit", help="edit a prepared action before approving")
    p_ed.add_argument("action_id")
    p_ed.add_argument("changes", nargs="+", metavar="field=value",
                      help="e.g. subject='New subject' body='...'")
    p_un = sub.add_parser("undo", help="undo a reversible, executed action")
    p_un.add_argument("action_id")
    p_au = sub.add_parser("autonomy", help="show or set the autonomy level (0-4)")
    p_au.add_argument("level", nargs="?", type=int)
    p_ea = sub.add_parser("earned-autonomy",
                          help="show or toggle earned per-domain autonomy (on/off)")
    p_ea.add_argument("state", nargs="?", choices=["on", "off"])
    p_gr = sub.add_parser("grant", help="grant a permission scope (e.g. email)")
    p_gr.add_argument("scope")
    p_rv = sub.add_parser("revoke", help="revoke a scope ('all' = kill switch)")
    p_rv.add_argument("scope")
    p_lim = sub.add_parser(
        "limit", help="show or set daily execution caps per action type")
    p_lim.add_argument("type", nargs="?", help="action type, e.g. gmail_send")
    p_lim.add_argument("n", nargs="?", type=int, help="max per day (0 = off)")

    sub.add_parser("connectors", help="list configured MCP servers and plugins")
    p_mcp = sub.add_parser("add-mcp", help="add an MCP server connector")
    p_mcp.add_argument("name")
    p_mcp.add_argument("url")
    p_mcp.add_argument("--type", choices=["data", "action"], default="data")
    p_mcp.add_argument("--auth-env", help="env var holding the bearer token")
    p_mcp.add_argument("--specialists", help="comma-separated specialist keys")

    p_web = sub.add_parser("web", help="serve the browser chat UI")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8484)

    p_serve = sub.add_parser(
        "serve", help="serve the HTTP API incl. the OpenAI-compatible "
                      "/v1/* endpoints (set OLYMPUS_API_KEYS to expose it)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8484)

    sub.add_parser("matrix", help="run the Matrix channel "
                                  "(needs MATRIX_HOMESERVER + MATRIX_ACCESS_TOKEN)")
    sub.add_parser("telegram", help="run the Telegram gateway "
                                    "(needs TELEGRAM_BOT_TOKEN)")
    p_gw = sub.add_parser(
        "gateway", help="run ALL configured chat channels together in one "
                        "always-on daemon (Telegram/Discord/Slack/Signal/"
                        "WhatsApp/email/webhook)")
    p_gw.add_argument("--only", default="",
                      help="comma-separated subset of channels to run "
                           "(default: every configured channel)")
    p_gw.add_argument("--list", action="store_true", dest="list_channels",
                      help="list which channels are configured, then exit")
    p_gw.add_argument("--status", action="store_true", dest="gw_status",
                      help="report a running daemon's per-channel health "
                           "(reads its status file), then exit")
    p_secret = sub.add_parser(
        "secret", help="named secrets for SecretRef config indirection — "
                       "reference them as vault:NAME instead of pasting "
                       "credentials into config/env")
    p_secret.add_argument("action", nargs="?", default="ls",
                          choices=["set", "ls", "rm"])
    p_secret.add_argument("name", nargs="?", default="")
    p_wiki = sub.add_parser(
        "wiki", help="the memory wiki: concept pages maintained by nightly "
                     "dreaming (list | show <page> | lint | dream | rm <page>)")
    p_wiki.add_argument("action", nargs="?", default="list",
                        choices=["list", "show", "lint", "dream", "rm"])
    p_wiki.add_argument("page", nargs="?", default="")
    p_wiki.add_argument("--user", default="shared",
                        help="memory namespace (default: shared)")
    p_restrict = sub.add_parser(
        "restrict", help="scope a conversation/user to a capability profile "
                         "(full | reader | guest | custom)")
    p_restrict.add_argument("user", nargs="?", default="",
                            help="conversation/user id (e.g. tg-12345)")
    p_restrict.add_argument("profile", nargs="?", default="",
                            help="profile name; omit to show current")
    p_restrict.add_argument("--clear", action="store_true",
                            help="remove the explicit restriction")
    p_restrict.add_argument("--profiles", action="store_true",
                            dest="list_profiles", help="list known profiles")
    p_pair = sub.add_parser(
        "pair", help="mint a one-time pairing code so a chat can talk to "
                     "Olympus (channels are untrusted by default)")
    p_pair.add_argument("channel", nargs="?", default="telegram",
                        help="channel to pair (default: telegram)")
    p_pair.add_argument("--revoke", metavar="SENDER_ID",
                        help="unpair a previously paired chat/sender id")
    p_pair.add_argument("--list", action="store_true", dest="list_paired",
                        help="list paired sender ids for the channel")
    p_doc = sub.add_parser(
        "documents", aliases=["docs"],
        help="your document workspace: list | read <name> | delete <name>")
    p_doc.add_argument("action", nargs="?", default="list",
                       choices=["list", "read", "delete"])
    p_doc.add_argument("name", nargs="?", help="document name (read/delete)")

    p_gal = sub.add_parser(
        "gallery", aliases=["images"],
        help="images generated into the workspace: list | delete <name>")
    p_gal.add_argument("action", nargs="?", default="list",
                       choices=["list", "delete", "edit"])
    p_gal.add_argument("name", nargs="?", help="image file name (delete/edit)")
    p_gal.add_argument("prompt", nargs="*", help="edit prompt (edit)")

    sub.add_parser(
        "agenda",
        help="your scheduled tasks and upcoming calendar events in one view")

    p_todo = sub.add_parser(
        "todo", aliases=["todos", "notes"],
        help="your notes, todos, and reminders: list | add <text> | done <id> "
             "| rm <id> | clear")
    p_todo.add_argument("action", nargs="?", default="list",
                        choices=["list", "add", "done", "rm", "clear"])
    p_todo.add_argument("text", nargs="*", help="item text (add) or id (done/rm)")
    p_todo.add_argument("--due", default="",
                        help="due time for a reminder, e.g. '2026-07-10 09:00'")
    p_todo.add_argument("--note", action="store_true",
                        help="add as a kept note instead of a tickable todo")

    p_health = sub.add_parser(
        "health",
        help="runtime health of the moving parts (models, memory, gateway, "
             "search, push, connections); exits non-zero if anything is down")
    p_health.add_argument("--json", action="store_true", dest="as_json",
                          help="emit the structured report as JSON")

    p_triage = sub.add_parser(
        "triage",
        help="triage the inbox into important/promotions/spam/other "
             "(read-only; needs a connected Google account)")
    p_triage.add_argument("--query", default="in:inbox",
                          help="Gmail search (default 'in:inbox')")
    p_triage.add_argument("--max", type=int, default=20, dest="max_results",
                          help="how many messages to triage (default 20)")

    p_cmp = sub.add_parser(
        "compare",
        help="blind multi-model compare: same prompt, every configured model, "
             "revealed only after you pick")
    p_cmp.add_argument("prompt", nargs="?", help="the prompt to compare")
    p_cmp.add_argument("--pick", metavar="LABEL",
                       help="record a pick (A/B/…) for --reveal")
    p_cmp.add_argument("--reveal", metavar="ID",
                       help="reveal a prior comparison by id")
    p_cmp.add_argument("--tally", action="store_true",
                       help="show your running blind-pick tally")

    p_op = sub.add_parser(
        "operator",
        help="manage the Hermes operator (acts on YOUR authorized accounts)")
    p_op.add_argument(
        "action", nargs="?", default="status",
        choices=["status", "enable", "disable", "authorize", "forget",
                 "list", "history"],
        help="status (default) | enable | disable | authorize <domain> | "
             "forget <domain> | list (site profiles) | history")
    p_op.add_argument("target", nargs="?",
                      help="domain, for authorize/forget")
    p_op.add_argument("--remember", action="store_true",
                      help="authorize with saved-password (remember) login "
                           "instead of manual sign-in")
    p_op.add_argument("--limit", type=int, default=20,
                      help="rows to show for history (default 20)")

    p_wa = sub.add_parser("whatsapp", help="run the WhatsApp Cloud API gateway "
                                           "(needs WHATSAPP_* env vars)")
    p_wa.add_argument("--host", default="0.0.0.0")
    p_wa.add_argument("--port", type=int, default=8485)

    p_dc = sub.add_parser("discord", help="serve the Discord interactions "
                                          "endpoint (needs DISCORD_* env vars)")
    p_dc.add_argument("--host", default="0.0.0.0")
    p_dc.add_argument("--port", type=int, default=8486)

    p_sl = sub.add_parser("slack", help="serve the Slack Events endpoint "
                                        "(needs SLACK_* env vars)")
    p_sl.add_argument("--host", default="0.0.0.0")
    p_sl.add_argument("--port", type=int, default=8487)

    p_mm = sub.add_parser("mattermost", help="serve the Mattermost outgoing-"
                          "webhook endpoint (needs MATTERMOST_OUTGOING_TOKEN)")
    p_mm.add_argument("--host", default="0.0.0.0")
    p_mm.add_argument("--port", type=int, default=8489)
    p_gc = sub.add_parser("googlechat", help="serve the Google Chat app "
                          "endpoint (needs GOOGLECHAT_VERIFY_TOKEN)")
    p_gc.add_argument("--host", default="0.0.0.0")
    p_gc.add_argument("--port", type=int, default=8490)

    sub.add_parser("signal", help="run the Signal gateway over signal-cli REST "
                                  "(needs SIGNAL_* env vars)")
    p_email = sub.add_parser("email", help="run the email gateway — answer "
                                           "unread mail via the Gmail adapter")
    p_email.add_argument("--interval", type=int, default=60,
                         help="seconds between inbox polls (default 60)")
    p_hook = sub.add_parser("webhook", help="serve the inbound webhook gateway "
                                            "(POST {user,text} → {reply})")
    p_hook.add_argument("--host", default="0.0.0.0")
    p_hook.add_argument("--port", type=int, default=8487)

    from . import codegraph_cli as _cg_cli
    p_cg = sub.add_parser(
        "codegraph", help="the code knowledge graph: build/update/watch it, "
                          "query/path/impact/verify against it, analyze "
                          "communities, export (json/graphml/mermaid/html/"
                          "cypher/obsidian/wiki), PR dashboard, global graph")
    p_cg.add_argument("action", choices=_cg_cli.ACTIONS)
    p_cg.add_argument("arg", nargs="*", help="action arguments (symbol, "
                                             "question, tag, dsn, …)")
    p_cg.add_argument("--project", default="self",
                      help="graph namespace (default: self)")
    p_cg.add_argument("--root", default=".",
                      help="tree to build/update/watch from (default: .)")
    p_cg.add_argument("--code-only", action="store_true",
                      help="skip documents (md/rst/txt/yaml)")
    p_cg.add_argument("--budget", type=int, default=2000,
                      help="token budget for query/benchmark output")
    p_cg.add_argument("--dfs", action="store_true",
                      help="depth-first instead of breadth-first query")
    p_cg.add_argument("--depth", type=int, default=2,
                      help="impact traversal depth")
    p_cg.add_argument("--force", action="store_true",
                      help="recompute communities instead of using the cache")
    p_cg.add_argument("--merge", action="store_true",
                      help="dedup: actually merge the duplicate groups")
    p_cg.add_argument("--formats", default=None,
                      help="export formats, comma-separated "
                           "(json,graphml,mermaid,html,cypher,report,"
                           "obsidian,wiki)")
    p_cg.add_argument("--out", default=None,
                      help="export output directory (default codegraph-out)")
    p_cg.add_argument("--neo4j", default=None, metavar="BOLT_URI",
                      help="also push to Neo4j (needs the neo4j driver)")
    p_cg.add_argument("--falkordb", default=None, metavar="HOST[:PORT]",
                      help="also push to FalkorDB (needs the falkordb driver)")
    p_cg.add_argument("--db-user", default="neo4j")
    p_cg.add_argument("--db-password", default="")
    p_cg.add_argument("--base", default=None,
                      help="prs: only PRs targeting this base branch")
    p_cg.add_argument("--repo", default=None,
                      help="prs: owner/repo override (default GITHUB_REPO)")

    return parser


def command_names() -> list[str]:
    """Every registered subcommand name, from the live parser."""
    names: list[str] = []
    for action in build_parser()._subparsers._group_actions:  # type: ignore[union-attr]
        if isinstance(action, argparse._SubParsersAction):
            names.extend(action.choices.keys())
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    from . import firstrun
    firstrun.load_env_file()        # saved keys apply to every command
    try:                            # vault-stored settings too (env wins)
        from . import opconfig
        opconfig.apply_secrets()
    except Exception:
        pass                        # a broken vault must never block the CLI

    # Sovereign boot invariant: a sovereign instance must not run on the PUBLIC
    # default signing seed (its decision logs and backups would be forgeable).
    # Checked here — after the saved env is loaded, before any command runs —
    # so the failure is one actionable line at boot, not a surprise later.
    # `keygen` is exempt: it is the fix the error prescribes. Non-sovereign
    # instances are untouched. witness.sign() re-checks (defense in depth).
    if args.command != "keygen":
        from . import config as _config, witness as _witness
        # Production boot invariant (M2): OLYMPUS_ENV=production must run on a
        # configured, non-default signing seed. On the public default seed
        # (unset or the shipped dev seed) every decision log/backup/attestation
        # would be forgeable — refuse to boot with one actionable line. `keygen`
        # is exempt (it is the fix). Non-production instances just get a warning.
        try:
            _witness.require_production_seed()
        except _witness.WitnessError as err:
            print(f"[production] {err}", file=sys.stderr)
            return 1
        if _config.sovereign_mode():
            try:
                _witness.check_sovereign_seed()
            except _witness.WitnessError as err:
                print(f"[sovereign] {err}", file=sys.stderr)
                return 1

    if args.command == "setup":
        if getattr(args, "section", None):
            firstrun.setup_section(args.section)
        else:
            firstrun.wizard()
    elif args.command == "config":
        if args.action == "show":
            print(firstrun.show_config())
        elif args.action == "edit":
            print(firstrun.open_in_editor())
        elif args.action == "set":
            if len(args.kv) < 2:
                print("Usage: olympus config set <KEY> <VALUE>")
                return 1
            print(firstrun.config_set(args.kv[0], " ".join(args.kv[1:])))
    elif args.command == "doctor":
        from . import doctor
        print(doctor.render())
        return 0 if doctor.is_ready() else 1
    elif args.command == "soul":
        from . import soul
        print(soul.edit() if args.action == "edit" else soul.show())
    elif args.command == "version":
        from . import __version__
        print(f"olympus-council {__version__}")
    elif args.command == "codegraph":
        from . import codegraph_cli
        return codegraph_cli.run(args)
    elif args.command == "growth":
        from . import companion
        print(companion.summary("cli"))
    elif args.command == "upgrade":
        from . import selfupdate
        return selfupdate.run(force_git=args.git)
    elif args.command == "profile":
        from . import profile
        user = "cli"
        if args.clear:
            print(profile.clear(user))
        elif args.set:
            print(profile.set_fact(user, args.set[0], args.set[1]))
        elif args.about:
            print(profile.set_about(user, " ".join(args.about)))
        else:
            card = profile.card(user)
            print(card.strip() if card else
                  "Nothing saved yet. Try: olympus profile \"I'm the founder "
                  "of Acme; keep replies concise.\"")
    elif args.command == "memory":
        from . import usermem, recall
        user = "cli"
        arg = " ".join(args.arg).strip()
        if args.action == "migrate":
            r = memory.migrate_notes()
            print(f"Migrated {r['migrated']} note(s) to schema v"
                  f"{memory.NOTE_SCHEMA_VERSION} (scanned {r['scanned']}).")
        elif args.action == "export":
            out = args.out or (arg or None)
            if not out:
                print("Where to? Use: memory export --out <archive> "
                      "[--user U | --all] [--encrypt]")
                return 1
            m = memory.export_memory(out, user=args.user,
                                     all_users=args.all, encrypt=args.encrypt)
            scope = "all users" if args.all else f"user '{m['scope']['user']}'"
            print(f"Exported {len(m['files'])} file(s) for {scope} to {out} "
                  f"(schema v{m['schema_version']}"
                  + (", encrypted)." if args.encrypt else ")."))
        elif args.action == "import":
            src = args.out or (arg or None)
            if not src:
                print("Which archive? Use: memory import <archive>")
                return 1
            try:
                r = memory.import_memory(src)
            except ValueError as err:
                print(f"[refused] {err}")
                return 1
            print(f"Imported {r['count']} file(s) (schema v"
                  f"{r['schema_version']}, {r['verified']} checksum-verified).")
        elif args.action == "delete":
            target = args.user or "shared"
            # Show what will go before doing it, then require confirmation.
            from . import config as _cfg
            roots = (memory._memory_roots(target) if not args.category
                     else [memory._dir(args.category, memory.safe_id(target))])
            doomed = [p.relative_to(_cfg.MEMORY_DIR).as_posix()
                      for r in roots if r.exists()
                      for p in sorted(r.rglob("*")) if p.is_file()
                      and (args.note_id is None
                           or args.note_id in (p.name, p.stem))]
            if not doomed:
                print("Nothing matches that scope — nothing deleted.")
                return 0
            print(f"This will permanently delete {len(doomed)} file(s):")
            for rel in doomed:
                print(f"  {rel}")
            if not args.yes:
                ok = input("Type 'delete' to confirm: ").strip().lower()
                if ok != "delete":
                    print("Aborted — nothing deleted.")
                    return 0
            removed = memory.delete_memory(target, category=args.category,
                                           note_id=args.note_id)
            print(f"Deleted {len(removed)} file(s).")
        elif args.action == "card":
            from . import usermem as _um
            print(_um.render_card(args.user or user))
        elif args.action == "list":
            mems = usermem.active_memories(user)
            if not mems:
                print("No durable memories yet — they form as you chat.")
            for m in sorted(mems, key=lambda x: x["type"]):
                eff = usermem.effective_confidence(m)
                print(f"  [{m['id']}] ({m['type']}, conf {eff:.2f}) {m['content']}")
        elif args.action == "candidates":
            cands = usermem.candidates(user)
            if not cands:
                print("No memories awaiting your approval.")
            for c in cands:
                print(f"  [{c['id']}] ({c['type']}, {c.get('reason','')}) "
                      f"{c['content']}\n     → memory approve {c['id']} | reject {c['id']}")
        elif args.action == "approve":
            c = usermem.pop_candidate(user, arg)
            if not c:
                print("No such candidate.")
            else:
                usermem.add_memory(user, type=c["type"], content=c["content"],
                                   confidence=c.get("confidence", 0.7),
                                   key=c.get("key"),
                                   importance=c.get("importance", 0.5),
                                   sensitivity=c.get("sensitivity", "normal"),
                                   provenance=c.get("provenance", []))
                print("Saved.")
        elif args.action == "reject":
            print("Dismissed." if usermem.pop_candidate(user, arg) else "No such candidate.")
        elif args.action == "forget":
            print("Forgotten." if usermem.tombstone(user, arg) else "No such memory.")
        elif args.action == "search":
            hits = recall.retrieve(user, arg)
            if not hits:
                print("Nothing relevant.")
            for m in hits:
                print(f"  [{m['id']}] ({m['type']}) {m['content']}")
    elif args.command == "playbook":
        from . import playbooks
        user = "cli"
        name = args.name or ""
        if args.action in ("list", "proposed"):
            status = playbooks.PROPOSED if args.action == "proposed" else playbooks.ACTIVE
            items = playbooks.list_all(user, status=status)
            if not items:
                print("No playbooks yet." if args.action == "list"
                      else "No proposed playbooks.")
            for p in items:
                print(f"  [{p['id']}] {p['name']}  (v{p['version']}, "
                      f"used {p['use_count']}x, {len(p['steps'])} steps)")
        elif args.action == "show":
            p = playbooks.get(user, name)
            if not p:
                print("No such playbook.")
            else:
                print(f"{p['name']}  (v{p['version']}, {p['status']})")
                for i, s in enumerate(p["steps"], 1):
                    print(f"  {i}. {s}")
        elif args.action == "save":
            steps = " ".join(args.steps).split(";")
            try:
                p = playbooks.save(user, name, steps)
                print(f"Saved '{p['name']}' (v{p['version']}, {len(p['steps'])} steps). "
                      f"Run it anytime with: olympus playbook run \"{p['name']}\"")
            except ValueError as err:
                print(f"Error: {err}")
        elif args.action == "run":
            block = playbooks.run_block(user, name)
            print(block.strip() if block else "No such active playbook.")
        elif args.action == "approve":
            print("Approved." if playbooks.approve(user, name) else "No such playbook.")
        elif args.action == "reject":
            print("Removed." if playbooks.delete(user, name) else "No such playbook.")
        elif args.action == "forget":
            print("Forgotten." if playbooks.delete(user, name) else "No such playbook.")
    elif args.command == "graph":
        from . import relgraph
        user = "cli"
        if args.forget:
            print("Removed." if relgraph.forget(user, args.forget)
                  else "No such entity.")
        elif args.entity:
            desc = relgraph.describe(user, " ".join(args.entity))
            print(desc or "No such entity in the graph.")
        else:
            ns = relgraph.nodes(user)
            if not ns:
                print("The relationship graph is empty — it fills in as you "
                      "mention people and companies.")
            for n in ns:
                conns = relgraph.neighbors(user, n["id"])
                print(f"  {n['label']} ({n['kind']}) — {len(conns)} connection(s)")
    elif args.command == "outcomes":
        from . import outcomes
        user = "cli"
        s = outcomes.stats(user)["overall"]
        if not s["total"]:
            print("No outcomes yet — they accrue as you approve, edit, or "
                  "reject prepared actions.")
        else:
            print(f"Track record ({s['total']} actions): "
                  f"{s['approved']} approved as-is, "
                  f"{s['approved_after_edit']} approved after edit, "
                  f"{s['rejected']} rejected, {s['undone']} undone.")
            for ins in outcomes.insights(user):
                print(f"\n  💡 {ins['message']}")
    elif args.command == "learned":
        from . import digest
        firstrun.load_env_file()
        print(digest.learned_recently())
    elif args.command == "status":
        from . import config, usage, accounts
        firstrun.load_env_file()
        s = config.Settings.from_env()
        print("OLYMPUS status")
        print(f"  provider/model : {s.provider} / {s.model or '(default)'}")
        print(f"  key configured : {'yes' if firstrun.configured() else 'NO'}")
        b = usage.budget_status()
        print(f"  daily budget   : "
              + (f"${b['spent']:.4f} / ${b['limit']:.2f}"
                 + ("  ⚠ reached" if b["exceeded"] else "")
                 if b["enabled"] else "off"))
        print(f"  accounts       : "
              + ("required (login)" if accounts.require_login() else "open"))
        sov = config.sovereign_status()
        print(f"  sovereign mode : "
              + ("ON — zero-egress, local-only" if sov["sovereign"] else "off"))
        if sov["sovereign"] or sov["allowlist"]:
            print(f"  egress allowlist: "
                  + (", ".join(sov["allowlist"]) or "(loopback + local only)"))
        print(f"  default class  : {sov['default_data_class']}")
        print(f"  models         : " + (", ".join(sov["members"]) or "(none)"))
        print(f"  eligible local : "
              + (", ".join(sov["eligible_local"]) or "(none — sovereign would "
                 "fail closed)"))
        print()
        print(usage.report(7))
    elif args.command == "replay":
        try:
            original, fresh, diffs = orchestrator.replay_run(args.run_id)
        except ValueError as err:
            print(f"[error] {err}")
            return 1
        n = len(original.get("decisions", []))
        if not diffs:
            print(f"✓ Re-executable replay of run {args.run_id}: "
                  f"{n} decision(s) replayed byte-identically against the "
                  "frozen LLM responses. The reasoning path is reproducible.")
        else:
            print(f"✗ Replay of run {args.run_id} diverged in "
                  f"{len(diffs)} decision(s):")
            for d in diffs:
                orig = d["original"] or {}
                rep = d["replayed"] or {}
                print(f"  - decision #{d['index']}: "
                      f"{orig.get('decision_type', '∅')} → "
                      f"{rep.get('decision_type', '∅')}")
            return 1
    elif args.command == "explain":
        import json
        from . import trace
        run = trace.load_run(args.id)
        if run:
            decisions = run.get("decisions", [])
            inp = (run.get("meta") or {}).get("input", "")
            print(f"Run {args.id} ({run.get('kind', '?')}) — "
                  f"{len(decisions)} decision(s)")
            if inp:
                print(f"  input: {inp[:200]}")
            for d in decisions:
                # The model lives in the top-level decision field
                # (d['model'] = {provider, model, version}, from _model_meta) —
                # the agent dict is {name, role} with no model key, so reading it
                # there printed 'model=?' on every line.
                model = (d.get("model") or {}).get("model") or "?"
                ref = d.get("model_response_ref") or "—"
                out = (d.get("outcome") or {}).get("status", "?")
                print(f"\n  [{d.get('record_id')}] {d.get('decision_type')} "
                      f"({out}, model={model}, response={ref[:12]})")
                rat = d.get("rationale")
                if rat is not None:
                    text = rat if isinstance(rat, str) else \
                        json.dumps(rat, ensure_ascii=False)
                    print(f"    rationale: {text[:300]}")
        else:
            found = trace.find_record(args.id)
            if not found:
                print(f"No run or decision record '{args.id}' found.")
                return 1
            print(f"Decision {args.id} (run {found['run_id']}):")
            print(json.dumps(found["decision"], indent=2, ensure_ascii=False))
    elif args.command == "capabilities":
        from . import capabilities
        if args.write:
            path = capabilities.write()
            print(f"Wrote {path}")
        elif args.check:
            problems = capabilities.check_repo()
            if problems:
                for p in problems:
                    print(f"[drift] {p}")
                return 1
            print("Capabilities consistent: the manifest and README match the "
                  "code.")
        else:
            print(capabilities.to_json(), end="")
    elif args.command == "sign":
        from pathlib import Path
        from . import witness
        if not witness.available():
            print("Cannot sign: the cryptography backend is unavailable.")
            return 1
        try:
            path = witness.write_manifest(Path(args.out) if args.out else None,
                                          dev=args.dev)
        except witness.WitnessError as err:
            print(f"[sign] {err}")
            return 1
        kind = "DEV (local use only)" if witness.is_default_seed() else "release"
        print(f"Wrote signed {kind} manifest: {path}")
        print(f"  signing key (public): {witness.public_key_hex()}")
    elif args.command in ("witness-pubkey", "pubkey"):
        from . import witness
        if not witness.available():
            print("Cannot derive key: the cryptography backend is unavailable.")
            return 1
        pub = witness.public_key_hex()
        print(pub)
        # Key-custody guidance on stderr so `olympus pubkey` still pipes the bare
        # key on stdout (e.g. into witness_pubkey.txt) while telling an operator
        # how to make it a real root of trust.
        if witness.is_default_seed():
            print("\nposture: DEV — this is the PUBLIC default key; anyone can "
                  "forge signatures with it.\n"
                  "To make signatures authentic:\n"
                  "  export OLYMPUS_SIGNING_SEED=\"$(python -c 'import secrets;"
                  "print(secrets.token_hex(32))')\"\n"
                  "  olympus pubkey > olympus/witness_pubkey.txt   # pin the "
                  "derived key\n"
                  "  # or set OLYMPUS_PINNED_PUBKEY to this value\n"
                  "See docs/SIGNING.md (HSM/KMS recommended for the seed).",
                  file=sys.stderr)
        else:
            pins = witness.pinned_pubkeys()
            state = ("matches a pinned key" if pub.lower() in pins
                     else "NOT yet pinned — add it to witness_pubkey.txt / "
                          "OLYMPUS_PINNED_PUBKEY" if not pins
                     else "matches NONE of the pinned keys (rotation in "
                          "progress? append it to witness_pubkey.txt)")
            print(f"\nposture: PRODUCTION — derived from OLYMPUS_SIGNING_SEED; "
                  f"{state}.", file=sys.stderr)
    elif args.command == "keygen":
        from pathlib import Path
        from . import config, witness
        out = Path(args.out) if args.out else config.MEMORY_DIR / "signing_seed"
        try:
            seed = witness.write_seed_file(out, force=args.force)
        except witness.WitnessError as err:
            print(f"[keygen] {err}")
            return 1
        # The seed itself is NEVER printed — only where it lives and the
        # derived PUBLIC key an operator pins for verifiers.
        print(f"Wrote a new 256-bit signing seed: {out}  (mode 0600)")
        if witness.available():
            print(f"  public key: {witness.pubkey_for_seed(seed)}")
        else:
            print("  public key: (cannot derive here — the cryptography "
                  "backend is unavailable; run `olympus pubkey` with the seed "
                  "configured on a host where it is)")
        print("\nNext steps:")
        print(f"  export OLYMPUS_SIGNING_SEED_FILE={out}")
        print("  # pin the public key for verifiers: set OLYMPUS_PINNED_PUBKEY "
              "to it,\n"
              "  # or add it to olympus/witness_pubkey.txt (one key per line).\n"
              "  # systemd LoadCredential / Docker-secrets recipes: "
              "docs/SIGNING.md")
    elif args.command == "otel-export":
        from . import otel, trace
        if not otel.enabled():
            print("[otel-export] OTLP is off — set OLYMPUS_OTLP_ENDPOINT "
                  "(e.g. http://localhost:4318) to a collector's OTLP/HTTP "
                  "receiver first.")
            return 1
        run = trace.load_run(args.run)
        if not run:
            print(f"[otel-export] no recorded run {args.run!r}.")
            return 1
        ok = otel.export_run(run)
        print(f"[otel-export] {'exported' if ok else 'FAILED to export'} run "
              f"{args.run} ({len(run.get('decisions') or [])} decision span(s)) "
              f"to {otel.endpoint()}.")
        return 0 if ok else 1
    elif args.command == "verify-anchor":
        from . import anchor
        rep = anchor.verify_anchor()
        if not rep["enabled"]:
            print("[verify-anchor] anchoring is OFF — set OLYMPUS_ANCHOR "
                  "(git|dir) plus its sink path to enable external head "
                  "anchoring. Without it, truncation of an append-only log "
                  "is not externally detectable.")
            return 1
        for p in rep["problems"]:
            print(f"[verify-anchor] problem: {p}")
        for d in rep["divergences"]:
            print(f"[verify-anchor] DIVERGENCE {d['kind']} at {d['target']}: "
                  f"{d['detail']}")
        if rep["ok"]:
            print(f"[verify-anchor] PASS — {rep['checked']} anchored head(s) "
                  "all match their local logs.")
            return 0
        print(f"[verify-anchor] FAIL — {len(rep['divergences'])} divergence(s) "
              f"across {rep['checked']} anchored head(s).")
        return 1
    elif args.command == "verify":
        from pathlib import Path
        from . import witness
        if not witness.available():
            print("Cannot verify: the cryptography backend is unavailable.")
            return 1
        require_prod = getattr(args, "require_production", False)
        if args.run:
            return _verify_run_combined(args.run, require_prod)
        if args.log:
            from . import trace
            run = trace.load_run(args.log)
            dev = (witness.log_signed_by_default(run) if run
                   else witness.is_default_seed())
            # Verify a dev-posture log against the default seed's key (integrity
            # only), so it doesn't fail once the instance hardens its own seed.
            r = witness.verify_run(
                args.log,
                pin=witness.default_public_key_hex() if dev else None)
            if not r["ok"]:
                for p in r["problems"]:
                    print(f"[verify] {p}")
                return 1
            if dev and require_prod:
                print(f"[verify] FAIL: run {args.log} is {_DEV_LABEL} "
                      "--require-production forbids it.")
                return 1
            if dev:
                print(f"✓ run {args.log}: decision log is internally consistent.")
                print(f"  {_DEV_LABEL}")
            else:
                print(f"✓ run {args.log}: decision log is intact and signed by "
                      "the trusted (production) key.")
        else:
            r = witness.verify_release(
                Path(args.manifest) if args.manifest else None,
                allow_dev=args.allow_dev)
            if not r["ok"]:
                for p in r["problems"]:
                    print(f"[verify] {p}")
                return 1
            if r.get("is_dev") and require_prod:
                print(f"[verify] FAIL: manifest is {_DEV_LABEL} "
                      "--require-production forbids it.")
                return 1
            if r.get("is_dev"):
                print("✓ verified: every tracked file matches the signed "
                      "manifest and its signature is internally consistent.")
                print(f"  {_DEV_LABEL}")
            else:
                print("✓ verified: every tracked file matches the signed "
                      "manifest and the signature is from the trusted key.")
    elif args.command == "reports":
        from . import support
        items = support.recent(100)
        if not items:
            print("No problem reports yet.")
        for r in items:
            who = r.get("user", "?") + (f" ({r['contact']})" if r.get("contact")
                                        else "")
            print(f"\n[{r.get('ts', '?')}] {who}")
            print("  " + r.get("message", "").replace("\n", "\n  "))
    elif args.command == "errors":
        from . import errors
        items = errors.recent(50)
        if not items:
            print("No errors captured. 🎉")
        for e in items:
            print(f"\n[{e.get('ts', '?')}] {e.get('where', '?')}")
            print(f"  {e.get('error', '')}")
            if e.get("context"):
                print(f"  context: {e['context']}")
    elif args.command == "dashboard":
        from . import dashboard
        print(dashboard.render())
    elif args.command == "backup":
        from . import backup, config
        if args.list:
            items = backup.list_backups()
            if not items:
                print("No local backups yet. Run `olympus backup` to make one.")
            for b in items:
                enc = "encrypted" if b["encrypted"] else "PLAINTEXT"
                print(f"  {b['name']}  ({b['bytes'] // 1024} KB, {enc})")
            return 0
        res = backup.run(full=args.full, deliver_off=not args.no_deliver)
        if not res.get("ok"):
            print(f"✗ backup failed at {res.get('stage')}: {res.get('error')}")
            return 1
        print(f"✓ wrote {res['name']} — {res['files']} files, "
              f"{res['bytes'] // 1024} KB, "
              f"{'encrypted' if res.get('encrypted') else 'UNENCRYPTED'}, "
              f"{'signed' if res.get('signed') else 'unsigned'}.")
        if res.get("delivered"):
            print(f"  delivered off-droplet via {res.get('via')}.")
        elif res.get("reason"):
            print(f"  not delivered: {res['reason']}")
        if not config.backup_command():
            print("  tip: set OLYMPUS_BACKUP_CMD (e.g. 'rclone copy {path} "
                  "spaces:bucket/') to get the copy OFF this machine.")
    elif args.command == "restore":
        from . import backup
        try:
            res = backup.restore(args.archive, into=args.into,
                                 force=args.force, insecure=args.insecure)
        except backup.BackupError as err:
            print(f"✗ restore refused: {err}")
            return 1
        except Exception as err:
            # A wrong vault key (Fernet InvalidToken), a corrupt gzip/tar, or bad
            # manifest JSON otherwise escapes as a raw traceback — surface it as
            # the documented refusal instead.
            print(f"✗ restore failed: {type(err).__name__}: {err}")
            return 1
        # Distinguish "no signature present" from "signature present but FAILED"
        # (an --insecure restore of a tampered signed archive) — collapsing both
        # to 'unsigned' hides a security-relevant fact.
        if res.get("signature_ok"):
            sig_state = "signature verified"
        elif res.get("signed"):
            sig_state = "signature INVALID — restored via --insecure"
        else:
            sig_state = "unsigned"
        print(f"✓ restored {res['restored']} files into {res['into']} "
              f"({sig_state}).")
        if res["mismatched"]:
            print(f"  ⚠ {len(res['mismatched'])} file(s) failed integrity: "
                  f"{res['mismatched'][:5]}")
    elif args.command in (None, "chat"):
        if not firstrun.ensure_ready():
            return 1
        cid = getattr(args, "session", None) or (
            _last_session_id() if getattr(args, "continue_last", False)
            else "cli-default")
        _chat(cid)
    elif args.command == "sessions":
        from . import memory
        if args.id:
            if not firstrun.ensure_ready():
                return 1
            _chat(args.id)
            return 0
        sessions = memory.list_conversations(prefix="cli")[:15]
        if not sessions:
            print("No saved sessions yet — `olympus` starts one.")
            return 0
        import datetime as _dt
        print("Recent sessions:")
        for i, s in enumerate(sessions, 1):
            when = _dt.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
            print(f"  {i:2}) {s['id']}  ({when}, {s['turns']} turn(s))")
            print(f"      {s['preview']}")
        try:
            raw = input("Resume which? (number, blank to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if raw.isdigit() and 1 <= int(raw) <= len(sessions):
            if not firstrun.ensure_ready():
                return 1
            _chat(sessions[int(raw) - 1]["id"])
    elif args.command == "ask":
        if not firstrun.ensure_ready():
            return 1
        from . import config, security
        try:
            pool = config.ModelPool.from_env()
            if config.data_class_local_only(args.data_class):
                pool = pool.local_only()      # fail-closed if no local member
        except security.SovereigntyError as err:
            print(f"[sovereign] {err}", file=sys.stderr)
            return 1
        bot = orchestrator.Olympus(
            report=lambda msg: print(f"  {msg}", file=sys.stderr),
            pool=pool)
        print(bot.ask(" ".join(args.question)))
    elif args.command == "research":
        if not firstrun.ensure_ready():
            return 1
        from . import research
        report = research.run(" ".join(args.question),
                              report=lambda msg: print(f"  {msg}",
                                                       file=sys.stderr),
                              rounds=args.rounds)
        print(report)
        if args.out:
            from pathlib import Path
            Path(args.out).write_text(report, encoding="utf-8")
            print(f"\n[written to {args.out}]", file=sys.stderr)
    elif args.command == "scan":
        print(orchestrator.opportunity_scan())
    elif args.command == "audit":
        print(orchestrator.evolution_audit())
    elif args.command == "watch":
        print(orchestrator.watch_and_learn(args.url))
    elif args.command == "queue":
        memory.watchlist_add(args.url)
        print(f"Queued for the next heartbeat pass: {args.url}")
    elif args.command == "learn":
        print(orchestrator.daily_learning())
    elif args.command == "eval":
        from . import evals
        print(evals.run_and_save())
    elif args.command == "code-eval":
        from . import code_eval
        print(code_eval.run_and_save())
    elif args.command == "skills":
        from . import skills
        print(skills.index())
    elif args.command == "skills-starter":
        from . import skillpack
        msgs = skillpack.install_starter_pack()
        print("Installed the starter pack (provisional — run `olympus gate` to "
              "benchmark them):")
        for m in msgs:
            print(f"  · {m}")
    elif args.command == "gate":
        print(orchestrator.gate_skills())
    elif args.command == "curate":
        from . import curator
        print(curator.curate())
    elif args.command == "evolve":
        from . import evolve
        if args.action == "review":
            print(evolve.review())
        elif args.action == "reset":
            print(evolve.reset(args.feature))
        elif args.action == "log":
            import json as _json
            for e in evolve.events(args.feature):
                print(_json.dumps(e, sort_keys=True))
        else:
            import json as _json
            print(_json.dumps(evolve.summary(), indent=2))
    elif args.command == "sleeptime-supervise":
        from . import memory as _mem, supervise
        report = supervise.run_supervised_cycle()
        rendered = supervise.render_report(report)
        print(rendered)
        _mem.save("reports", "sleeptime supervision cycle", rendered)
        return 0 if report["grade"] == "CLEAN" else 1
    elif args.command == "pay-live":
        from . import paylive
        import json as _json
        if args.reconcile:
            print(_json.dumps(paylive.reconcile(args.reconcile), indent=2))
            return 0
        st = paylive.cutover_status()
        print(_json.dumps(st, indent=2))
        return 0 if st["ready"] else 1
    elif args.command == "rl-scaffold":
        from . import rlscaffold
        try:
            if args.export:
                report = rlscaffold.export(fit=True)
            else:
                report = rlscaffold.collect_report(fit=args.fit)
        except rlscaffold.RLScaffoldError as err:
            print(err)
            return 1
        print(rlscaffold.render_report(report))
        if args.export:
            print(f"\nsigned export: {report.get('snapshot_hash', '')[:16]}")
        return 0
    elif args.command == "sleeptime":
        from . import sleeptime, config as _cfg
        import json as _json
        if args.action == "run":
            lines = sleeptime.run()
            print("\n".join(lines) if lines
                  else ("Sleep-time is disabled (set OLYMPUS_SLEEPTIME=1)."
                        if not _cfg.sleeptime_enabled()
                        else "Nothing to refine."))
        elif args.action == "proposals":
            user = args.ident or "cli"
            props = sleeptime.proposals(user)
            if not props:
                print("No proposals.")
            for p in props:
                print(sleeptime.render_diff(p))
                print()
        elif args.action == "revert":
            if not args.ident:
                print("Usage: olympus sleeptime revert <snapshot-id> "
                      "(see `sleeptime status`).")
            else:
                user = "cli"
                ok = sleeptime.revert(user, args.ident)
                print("Reverted." if ok else "No such snapshot.")
        elif args.action == "apply":
            print("Auto-apply is governed: enable a graduated loop with "
                  "OLYMPUS_SLEEPTIME_AUTOAPPLY=1; manual apply of a single "
                  "proposal is intentionally not exposed.")
        else:
            st = sleeptime.state()
            st["graduated"] = sleeptime.graduated()
            st["enabled"] = _cfg.sleeptime_enabled()
            st["autoapply"] = _cfg.sleeptime_autoapply()
            print(_json.dumps(st, indent=2))
    elif args.command == "liveeval":
        from . import liveeval
        import json as _json
        print(_json.dumps(liveeval.report(), indent=2))
    elif args.command == "a2a":
        from . import a2a
        import json as _json
        if args.action == "call":
            if not args.url or not args.message:
                print("Usage: olympus a2a call <peer-task-url> <message>")
            else:
                try:
                    print(a2a.call_agent(args.url, args.message))
                except a2a.A2AError as err:
                    print(f"Refused: {err}")
        else:
            print(_json.dumps(a2a.card(), indent=2))
    elif args.command == "scaffold-evolve":
        from . import scaffold_evolve as _se
        import json as _json
        if args.action == "proposals":
            props = _se.proposals(args.module, valid_only=True)
            if not props:
                print("No surfaceable proposals.")
            for p in props:
                print(_se.render_diff(p))
                print()
        else:
            print(_json.dumps({
                "enabled": _se.enabled(),
                "archived": len(_se.archive()),
                "surfaceable": len(_se.proposals(valid_only=True)),
                "note": "propose-only; nothing auto-applies (ADR 0003)"},
                indent=2))
    elif args.command == "mcp-serve":
        from . import mcp_server
        try:
            mcp_server.serve_stdio()
        except KeyboardInterrupt:
            pass
    elif args.command == "train":
        print(orchestrator.train_specialists(focus=args.focus))
    elif args.command == "models":
        from . import config
        if getattr(args, "action", "show") == "discover":
            from . import providers
            settings = config.Settings.from_env()
            if not settings.usable():
                print("No usable model credentials configured. "
                      "Run `olympus setup`.")
                return 1
            found = providers.discover_for_settings(settings)
            if not found:
                print(f"No models discovered for {settings.provider}"
                      f"{'/' + settings.base_url if settings.base_url else ''} "
                      "(the provider may not expose a model list, or the key "
                      "lacks access).")
            else:
                print(f"{len(found)} model(s) reachable with the configured "
                      f"{settings.provider} credentials:")
                for mid in found:
                    print(f"  {mid}")
                print("\nAdd any of these to your pool via OLYMPUS_MODELS "
                      "or `olympus setup model`.")
        else:
            print(config.ModelPool.from_env().assignment())
    elif args.command in ("actions", "approve", "reject", "edit", "undo",
                          "autonomy", "earned-autonomy", "grant", "revoke",
                          "limit"):
        from . import actions, builtin_actions  # noqa: F401 (registers built-ins)
        user = "cli"
        if args.command == "actions":
            items = actions.pending(user)
            if not items:
                print("No actions awaiting approval.")
            for a in items:
                print(f"\n[{a.id}] {a.title}  ({a.risk_class})")
                if a.why:
                    print(f"  why: {a.why}")
                print("  " + a.preview.replace("\n", "\n  "))
                print(f"  → approve {a.id}  |  edit {a.id} field=value  |  "
                      f"reject {a.id} <reason>")
        elif args.command == "approve":
            a = actions.approve(user, args.action_id)
            print(f"{a.status}: {a.error or a.result}")
        elif args.command == "reject":
            a = actions.reject(user, args.action_id, " ".join(args.reason))
            print(f"Rejected {a.id}.")
        elif args.command == "edit":
            changes = {}
            for pair in args.changes:
                if "=" not in pair:
                    print(f"Skipping '{pair}' — expected field=value.")
                    continue
                key, _, value = pair.partition("=")
                changes[key.strip()] = value
            a = actions.edit(user, args.action_id, changes)
            print(f"Edited {a.id} — still awaiting approval. New preview:\n")
            print("  " + a.preview.replace("\n", "\n  "))
        elif args.command == "undo":
            a = actions.undo(user, args.action_id)
            print(f"{a.status}: {a.error or 'reversed'}")
        elif args.command == "autonomy":
            if args.level is None:
                print(f"Autonomy level: L{actions.autonomy_level(user)}")
            else:
                print(actions.set_autonomy(user, args.level))
        elif args.command == "earned-autonomy":
            from . import trust
            if args.state is None:
                print(trust.report(user))
            else:
                print(trust.set_enabled(user, args.state == "on"))
        elif args.command == "grant":
            print(actions.grant_scope(user, args.scope))
        elif args.command == "revoke":
            print(actions.revoke_all(user) if args.scope == "all"
                  else actions.revoke_scope(user, args.scope))
        elif args.command == "limit":
            if args.type is None:
                eff = actions.limits(user)
                if not eff:
                    print("No action types registered.")
                print("Daily execution limits (0 = unlimited):")
                for name, n in sorted(eff.items()):
                    print(f"  {name}: {n if n else 'unlimited'}")
            elif args.n is None:
                print(f"{args.type}: {actions.daily_limit(user, args.type) or 'unlimited'} per day")
            else:
                print(actions.set_limit(user, args.type, args.n))
    elif args.command in ("documents", "docs"):
        from . import documents
        user = "cli"
        if args.action == "list":
            print(documents.render_list(user))
        elif args.action == "read":
            if not args.name:
                print("Usage: olympus documents read <name>")
                return 1
            body = documents.read(user, args.name)
            print(body if body is not None else f"No document named '{args.name}'.")
        elif args.action == "delete":
            if not args.name:
                print("Usage: olympus documents delete <name>")
                return 1
            print(f"Deleted '{args.name}'." if documents.delete(user, args.name)
                  else f"No document named '{args.name}'.")
    elif args.command in ("gallery", "images"):
        from . import gallery
        if args.action == "list":
            print(gallery.render_list())
        elif args.action == "delete":
            if not args.name:
                print("Usage: olympus gallery delete <name>")
                return 1
            print(f"Deleted '{args.name}'." if gallery.delete_image(args.name)
                  else f"No image named '{args.name}'.")
        elif args.action == "edit":
            prompt = " ".join(args.prompt).strip()
            if not args.name or not prompt:
                print('Usage: olympus gallery edit <name> "<edit prompt>"')
                return 1
            from . import media
            print(media.edit_image(prompt, args.name))
    elif args.command == "agenda":
        from . import scheduler
        print(scheduler.summary())
        from . import calendar as gcal
        if gcal.configured():
            try:
                events = gcal.upcoming()
            except Exception as err:
                events = None
                print(f"\nCalendar unavailable: {str(err)[:120]}")
            if events:
                print("\nUpcoming calendar:")
                for e in events:
                    print(f"  {e['start']}  {e['summary']}")
            elif events is not None:
                print("\nNo calendar events in the next two weeks.")
        else:
            print("\n(Connect a Google account to see upcoming calendar events.)")
    elif args.command in ("todo", "todos", "notes"):
        from . import todos
        user = "cli"
        if args.action == "list":
            print(todos.render_list(user))
        elif args.action == "add":
            text = " ".join(args.text).strip()
            if not text:
                print('Usage: olympus todo add "buy milk" [--due "2026-07-10 09:00"] [--note]')
                return 1
            it = todos.add(user, text, kind="note" if args.note else "todo",
                           due=args.due or None)
            print(f"Added [{it['id']}]: {it['text']}")
        elif args.action == "done":
            tid = (args.text[0] if args.text else "")
            print("Marked done." if todos.complete(user, tid)
                  else f"No item with id '{tid}'.")
        elif args.action == "rm":
            tid = (args.text[0] if args.text else "")
            print("Removed." if todos.delete(user, tid)
                  else f"No item with id '{tid}'.")
        elif args.action == "clear":
            n = todos.clear_done(user)
            print(f"Cleared {n} done item(s).")
    elif args.command == "health":
        from . import health
        rep = health.report()
        if args.as_json:
            import json as _json
            print(_json.dumps(rep, indent=2))
        else:
            print(health.render(rep))
        return 0 if rep["ok"] else 1
    elif args.command == "triage":
        from . import spamtriage, gmail
        if not gmail.configured():
            print("No Google account connected. Connect one (GMAIL_* env or the "
                  "web OAuth flow) to triage your inbox.")
            return 1
        print(spamtriage.render(args.query, max(1, min(args.max_results, 50))))
    elif args.command == "compare":
        from . import compare
        user = "cli"
        if args.tally:
            print(compare.render_tally(user))
            return 0
        if args.reveal:
            out = compare.reveal(user, args.reveal, args.pick or "")
            if out is None:
                print(f"No comparison with id '{args.reveal}'.")
                return 1
            print("Which model wrote each answer:")
            for label, model in sorted(out["mapping"].items()):
                mark = "  ← your pick" if out.get("choice") == label else ""
                print(f"  {label}: {model}{mark}")
            return 0
        if not args.prompt:
            print('Usage: olympus compare "<prompt>"   '
                  '(or --reveal <id> [--pick A] | --tally)')
            return 1
        result = compare.run(user, args.prompt)
        if "error" in result:
            print(result.get("hint") or result["error"])
            if result.get("models"):
                print("Configured: " + ", ".join(result["models"]))
            return 1
        print(f"Blind comparison {result['id']} — pick the best answer:\n")
        for a in result["answers"]:
            print(f"── Answer {a['label']} " + "─" * 40)
            print(a["text"].strip() + "\n")
        # Interactive reveal when attached to a terminal; otherwise print how.
        import sys as _sys
        if _sys.stdin.isatty():
            choice = input("Which is best? [label / Enter to skip] ").strip()
            out = compare.reveal(user, result["id"], choice)
            print("\nWhich model wrote each answer:")
            for label, model in sorted(out["mapping"].items()):
                mark = "  ← your pick" if out.get("choice") == label else ""
                print(f"  {label}: {model}{mark}")
        else:
            print(f"Reveal with: olympus compare --reveal {result['id']} "
                  f"[--pick <label>]")
    elif args.command == "operator":
        from . import browser, operator
        user = "cli"
        act = args.action
        if act == "status":
            print(operator.status_summary(user))
        elif act == "enable":
            s = operator._settings(user)
            s["enabled"] = True
            operator._save_settings(user, s)
            print("Operator enabled. Next: authorize a site with "
                  "`olympus operator authorize <domain>`, and grant the scope "
                  "with `olympus grant browser.operate`.")
        elif act == "disable":
            s = operator._settings(user)
            s["enabled"] = False
            operator._save_settings(user, s)
            print("Operator disabled. Authorized sites are kept; re-enable "
                  "anytime with `olympus operator enable`.")
        elif act == "authorize":
            if not args.target:
                print("Usage: olympus operator authorize <domain> [--remember]")
                return 1
            mode = "remember" if args.remember else "manual"
            d = operator.authorize_site(user, args.target, mode)
            print(f"Authorized {d} (login: {mode}).")
            if mode == "remember":
                print("Run the interactive app (`olympus`) and you'll be "
                      "prompted to enter the password privately — it never "
                      "passes through the model.")
            else:
                print("Manual login: you sign in yourself in the browser "
                      "(handles 2FA); Olympus reuses that session.")
        elif act == "forget":
            if not args.target:
                print("Usage: olympus operator forget <domain>")
                return 1
            ok = operator.forget_site(user, args.target)
            print(f"Forgot {args.target.strip().lower()} (authorization + any "
                  "saved credentials removed)." if ok
                  else f"{args.target} was not authorized.")
        elif act == "list":
            profs = browser.list_profiles()
            if not profs:
                print("No site profiles yet. Built-in profiles ship in "
                      "olympus/profiles/; Hermes records new ones as it learns "
                      "a site.")
            for p in profs:
                tmpls = ", ".join(p.templates) or "—"
                print(f"  {p.domain}  [{p.source}]  reliability "
                      f"{p.reliability}  templates: {tmpls}")
        elif act == "history":
            print(operator.render_history(user, args.limit))
    elif args.command == "budget":
        from . import usage
        if args.amount is None:
            b = usage.budget_status()
            if not b["enabled"]:
                print("No daily budget set. Olympus will not cap spend on your "
                      "API key.\nSet one with `olympus budget 5` (USD/day).")
            else:
                print(f"Daily budget: ${b['spent']:.4f} / ${b['limit']:.2f} "
                      f"spent today" + ("  ⚠ reached" if b["exceeded"] else ""))
        else:
            print(usage.set_budget(args.amount))
    elif args.command == "routing-stats":
        from . import routing_outcomes as ro
        g = ro.gate_status()
        s = g["stats"]
        print("Routing-outcome telemetry (SPEC-04 Phase A — passive; changes no "
              "routing)")
        print(f"  rows total       : {s['total_rows']} "
              f"(synthetic {s['synthetic_rows']}, pending {s['pending_rows']})")
        print(f"  labeled (real)   : {s['labeled']}")
        print(f"  distinct sources : {s['distinct_users']}")
        if s["task_types"]:
            print("  by task-type     :")
            for t, n in sorted(s["task_types"].items(), key=lambda kv: -kv[1]):
                print(f"      {t:12s} {n}")
        if s["by_specialist_model"]:
            print("  by specialist/model :")
            for mk, n in sorted(s["by_specialist_model"].items(),
                                key=lambda kv: -kv[1])[:20]:
                print(f"      {mk:28s} {n}")
        if any(s["signals"].values()):
            print("  signals          : "
                  + ", ".join(f"{k}={v}" for k, v in s["signals"].items() if v))
        th = g["thresholds"]
        print()
        if g["met"]:
            print("  GATE READINESS   : ✅ MET — Phase B data threshold reached "
                  f"(≥{th['labeled']} labeled, ≥{th['task_types']} task-types, "
                  f"≥{th['distinct_users']} sources).")
        else:
            print("  GATE READINESS   : ⛔ NOT MET — "
                  + "; ".join(g["reasons"]) + ".")
        from . import learned_routing
        ls = learned_routing.status()
        print()
        print("Learned routing (SPEC-04 Phase B — evidence-gated selector)")
        print(f"  opt-in flag      : "
              + ("ON (OLYMPUS_LEARNED_ROUTING)" if ls["flag_enabled"]
                 else "off — set OLYMPUS_LEARNED_ROUTING=1 to enable"))
        print(f"  status           : "
              + ("🟢 ACTIVE — may override the keyword heuristic where a "
                 "(specialist, model) cell has evidence" if ls["active"]
                 else "⚪ dormant — routing is the keyword heuristic, unchanged"
                 + ("" if ls["flag_enabled"] else " (flag off)")
                 + ("" if ls["gate_met"] else " (data gate not met)")))
        known = [c for c in ls["cells"] if c["known"]]
        if ls["cells"]:
            print(f"  evidence cells   : {len(ls['cells'])} "
                  f"({len(known)} with ≥{ls['min_cell_samples']} samples — "
                  "only these can influence a choice):")
            for c in ls["cells"][:15]:
                mark = "●" if c["known"] else "○"
                print(f"      {mark} {c['specialist']}/{c['model']:24s} "
                      f"n={c['n']:<5d} rate={c['rate']:.2f} "
                      f"wilsonLB={c['wilson_lb']:.2f}")
        else:
            print("  evidence cells   : none yet")
        print("  (The selector overrides the heuristic only when BOTH the "
              "challenger and the incumbent cells are known and the challenger's "
              "Wilson lower bound is strictly higher; otherwise the heuristic "
              "stands. Replay always uses the recorded decisions.)")
    elif args.command == "scores":
        from . import evals
        scores = evals.per_specialist_scores()
        if not scores:
            print("No scores (benchmark unavailable — set ANTHROPIC_API_KEY).")
        else:
            for s, sc in sorted(scores.items(), key=lambda kv: kv[1]):
                print(f"  {s}: {sc}/10")
    elif args.command == "contrib":
        from . import contrib
        print(f"Cross-model contribution queue: {contrib.count()} snapshots.")
    elif args.command == "usage":
        from . import usage
        print(usage.report(args.days))
    elif args.command == "connectors":
        from . import connectors
        print(connectors.summary())
    elif args.command == "add-mcp":
        from . import connectors
        specialists = ([s.strip() for s in args.specialists.split(",")]
                       if args.specialists else None)
        print(connectors.add_mcp_server(args.name, args.url, args.type,
                                        args.auth_env, specialists))
    elif args.command == "heartbeat":
        try:
            heartbeat.run_forever()
        except KeyboardInterrupt:
            print("\nHeartbeat stopped.")
    elif args.command == "import-agent":
        from . import migrate
        print(migrate.render(migrate.import_dir(args.path, user="cli",
                                                with_keys=args.keys)))
    elif args.command == "skill-import":
        from . import skillpack
        import os.path
        if "://" in args.path:
            print("\n".join(skillpack.import_url(args.path)))
        elif os.path.isdir(args.path) and not os.path.isfile(
                os.path.join(args.path, "SKILL.md")):
            msgs = skillpack.import_dir(args.path, provisional=args.provisional)
            print("\n".join(msgs) if msgs else "No SKILL.md files found.")
        else:
            print(skillpack.import_file(args.path, provisional=args.provisional))
    elif args.command == "skill-export":
        from . import skillpack
        try:
            print(f"Wrote {skillpack.export(args.name, args.out)}")
        except ValueError as err:
            print(str(err))
            return 1
    elif args.command == "export-trajectories":
        from . import trajectories
        n = trajectories.export(args.out, kind=args.kind)
        print(f"Exported {n} {args.kind} trajectories to {args.out}.")
    elif args.command == "search":
        from . import search
        if args.reindex:
            n = search.reindex()
            print(f"Reindexed {n} turns.")
        hits = search.search(" ".join(args.query))
        if not hits:
            print("No matches.")
        for h in hits:
            print(h.render())
    elif args.command == "tick":
        from . import hibernate
        result = hibernate.run_once()
        for line in result["log"]:
            print(f"[tick] {line}")
        secs = result["next_wake_secs"]
        print(f"Next work due in {secs:.0f}s "
              f"(~{secs / 3600:.1f}h). Safe to hibernate until then.")
    elif args.command == "next-wake":
        from . import hibernate
        print(int(hibernate.next_due_in()))
    elif args.command == "schedule":
        from . import scheduler
        if args.action == "list":
            print(scheduler.summary())
        elif args.action == "add":
            prompt = " ".join(args.prompt).strip()
            if not args.name or not prompt:
                print('Usage: olympus schedule add <name> <interval> "<prompt>" '
                      '[--to telegram]')
                return 1
            if args.on_exit_pid:
                try:
                    job = scheduler.add_on_exit(
                        args.name, args.on_exit_pid, prompt,
                        deliver_to=args.deliver_to, user="cli")
                except ValueError as err:
                    print(err)
                    return 1
                print(f"Scheduled '{job.name}' to run once when pid "
                      f"{job.watch_pid} exits.")
            elif args.on_change_cmd:
                try:
                    job = scheduler.add_on_change(
                        args.name, args.on_change_cmd, prompt,
                        deliver_to=args.deliver_to, user="cli")
                except ValueError as err:
                    print(err)
                    return 1
                print(f"Scheduled '{job.name}' to run when the output of "
                      f"`{job.watch_cmd}` changes.")
            else:
                job = scheduler.add(args.name, args.interval, prompt,
                                    deliver_to=args.deliver_to, user="cli")
                print(f"Scheduled '{job.name}' every "
                      f"{scheduler._human_interval(job.interval)}.")
        elif args.action == "remove":
            print("Removed." if scheduler.remove(args.name) else "No such job.")
        elif args.action in ("enable", "disable"):
            ok = scheduler.set_enabled(args.name, args.action == "enable")
            print("Updated." if ok else "No such job.")
        elif args.action == "run":
            ran = scheduler.run_due()
            print("\n".join(ran) if ran else "Nothing due right now.")
    elif args.command == "goal":
        from . import goals
        detail = " ".join(args.detail).strip()
        if args.action == "list":
            print(goals.summary())
        elif args.action == "add":
            text, _, contract = detail.partition("::")
            print(goals.add("cli", text.strip(), contract.strip()))
        elif args.action == "check":
            g = goals.get(detail)
            if g is None:
                print(f"No goal with id '{detail}'.")
                return 1
            v = goals.judge(g)
            state = "DONE" if v["done"] else "not done"
            print(f"[{g.id}] {state} (confidence {v['confidence']:.2f})\n"
                  f"evidence: {v['evidence'] or '(none)'}\n"
                  f"missing:  {v['missing'] or '(nothing)'}")
        elif args.action == "work":
            g = goals.get(detail)
            if g is None:
                print(f"No goal with id '{detail}'.")
                return 1
            print(goals.work_one(g))
        elif args.action == "drop":
            print(goals.set_status(detail, "dropped"))
        elif args.action == "done":
            print(goals.set_status(detail, "done",
                                   evidence="closed manually by the operator"))
        elif args.action == "wait":
            parts = detail.split()
            if len(parts) != 2 or not parts[1].isdigit():
                print("Usage: olympus goal wait <goal id> <pid>")
                return 1
            print(goals.wait_on(parts[0], int(parts[1])))
    elif args.command == "distill":
        from . import learn
        print(learn.distill(" ".join(args.source), allow_paths=True))
    elif args.command == "journey":
        from . import journey
        if args.action == "list":
            print(journey.timeline())
        elif args.action == "show":
            print(journey.show(args.ref))
        elif args.action == "rm":
            print(journey.remove(args.ref))
    elif args.command == "moa":
        from . import moa
        print(moa.one_shot(" ".join(args.prompt)))
    elif args.command in ("web", "serve"):
        from . import web
        try:
            web.serve(args.host, args.port)
        except KeyboardInterrupt:
            print("\nWeb UI stopped.")
    elif args.command == "telegram":
        from . import telegram
        try:
            telegram.run_bot()
        except KeyboardInterrupt:
            print("\nTelegram gateway stopped.")
    elif args.command == "matrix":
        from . import matrix
        try:
            matrix.run_bot()
        except KeyboardInterrupt:
            print("\nMatrix gateway stopped.")
    elif args.command == "mattermost":
        from . import mattermost
        try:
            mattermost.run_server(args.host, args.port)
        except KeyboardInterrupt:
            print("\nMattermost channel stopped.")
    elif args.command == "googlechat":
        from . import googlechat
        try:
            googlechat.run_server(args.host, args.port)
        except KeyboardInterrupt:
            print("\nGoogle Chat channel stopped.")
    elif args.command == "gateway":
        from . import gateway
        if args.gw_status:
            st = gateway.read_status()
            if not st.get("running"):
                print(f"Gateway daemon: not running ({st.get('reason', 'unknown')}).")
                return 1
            print(f"Gateway daemon: RUNNING (pid {st['pid']}, "
                  f"heartbeat {st['age_secs']}s ago)")
            for name, s in sorted((st.get("channels") or {}).items()):
                mark = {"running": "✓", "restarting": "↻", "failed": "✗",
                        "stopped": "•"}.get(s.get("status"), "•")
                extra = (f" — {s['last_error']}" if s.get("last_error") else "")
                rc = s.get("restarts", 0)
                print(f"  {mark} {name}: {s.get('status')}"
                      f"{f' ({rc} restart(s))' if rc else ''}{extra}")
        elif args.list_channels:
            live = gateway.configured_channels()
            print("Configured channels: " + (", ".join(live) if live
                  else "none (set a channel's env vars — see docs/GATEWAY.md)"))
        else:
            only = [c for c in args.only.split(",") if c.strip()] or None
            gateway.run_all(only=only)
    elif args.command == "secret":
        from . import secretref, vault
        if args.action == "set":
            if not args.name:
                print("Usage: olympus secret set <name>")
                return 1
            import getpass
            value = getpass.getpass(f"value for '{args.name}' (hidden): ")
            if not value:
                print("Nothing entered — nothing stored.")
                return 1
            try:
                ref = secretref.store(args.name, value)
            except Exception as err:
                print(f"Could not store secret: {err}")
                return 1
            print(f"Stored. Reference it in config as: {ref}")
        elif args.action == "ls":
            try:
                names = [n.removeprefix("secretref:")
                         for n in vault.names("operator")
                         if n.startswith("secretref:")]
            except vault.VaultError as err:
                print(f"Cannot open the vault: {err}")
                return 1
            print("\n".join(names) if names
                  else "No named secrets. Add one: olympus secret set <name>")
        elif args.action == "rm":
            if not args.name:
                print("Usage: olympus secret rm <name>")
                return 1
            try:
                vault.delete("operator", f"secretref:{args.name}")
            except vault.VaultError as err:
                print(f"Cannot open the vault: {err}")
                return 1
            print("Removed (if it existed).")
    elif args.command == "wiki":
        from . import wiki
        if args.action == "list":
            print(wiki.summary(args.user))
        elif args.action == "show":
            if not args.page:
                print("Usage: olympus wiki show <page>")
                return 1
            print(wiki.read(args.user, args.page))
        elif args.action == "lint":
            issues = wiki.lint(args.user)
            print("\n".join(issues) if issues
                  else "Wiki is fresh — no issues.")
        elif args.action == "dream":
            print(wiki.dream(args.user))
        elif args.action == "rm":
            if not args.page:
                print("Usage: olympus wiki rm <page>")
                return 1
            print("Removed." if wiki.remove(args.user, args.page)
                  else "No such page.")
    elif args.command == "restrict":
        from . import capprofile
        if args.list_profiles:
            for name, spec in sorted(capprofile.profiles().items()):
                denied = (f"{len(spec['deny'])} tools denied" if spec["deny"]
                          else "no restriction")
                print(f"{name:8s} {denied}, autonomy cap "
                      f"L{spec.get('max_autonomy', 4)}")
        elif not args.user:
            print("Usage: olympus restrict <user> <profile> "
                  "| --clear | --profiles")
            return 1
        elif args.clear:
            print(capprofile.clear(args.user))
        elif args.profile:
            print(capprofile.assign(args.user, args.profile))
        else:
            print(capprofile.summary(args.user))
    elif args.command == "pair":
        from . import pairing
        if args.list_paired:
            ids = pairing.paired(args.channel)
            print("\n".join(ids) if ids else
                  f"No {args.channel} chats are paired.")
        elif args.revoke:
            gone = pairing.unpair(args.channel, args.revoke)
            print("Unpaired." if gone else "That id wasn't paired.")
        else:
            code = pairing.issue_code(args.channel)
            print(f"Pairing code for {args.channel}: {code}\n"
                  f"Send `/pair {code}` to the bot within "
                  f"{pairing.PAIR_TTL // 60} minutes. Single use.")
    elif args.command == "whatsapp":
        from . import whatsapp
        try:
            whatsapp.run_server(args.host, args.port)
        except KeyboardInterrupt:
            print("\nWhatsApp gateway stopped.")
    elif args.command == "discord":
        from . import discord
        try:
            discord.run_server(args.host, args.port)
        except KeyboardInterrupt:
            print("\nDiscord gateway stopped.")
    elif args.command == "slack":
        from . import slack
        try:
            slack.run_server(args.host, args.port)
        except KeyboardInterrupt:
            print("\nSlack gateway stopped.")
    elif args.command == "signal":
        from . import signal as signal_gw
        try:
            signal_gw.run_bot()
        except KeyboardInterrupt:
            print("\nSignal gateway stopped.")
    elif args.command == "email":
        from . import email_gateway
        try:
            email_gateway.run(poll_seconds=args.interval)
        except KeyboardInterrupt:
            print("\nEmail gateway stopped.")
    elif args.command == "webhook":
        from . import webhook_gateway
        try:
            webhook_gateway.run_server(host=args.host, port=args.port)
        except KeyboardInterrupt:
            print("\nWebhook gateway stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
