"""Olympus command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import heartbeat, memory, orchestrator


def _chat() -> None:
    from . import config
    pool = config.ModelPool.from_env()
    print("OLYMPUS — Zeus speaking. Type 'exit' to leave, "
          "'/good' or '/bad' to rate the last answer.")
    if pool.is_multi():
        print(pool.assignment())
    print()
    bot = orchestrator.Olympus(report=lambda msg: print(f"  {msg}"),
                               user="cli", conversation_id="cli-default",
                               pool=pool)
    while True:
        try:
            user = input("you ▸ ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in {"exit", "quit"}:
            break
        if user.lower().startswith(("/good", "/bad")):
            verdict = "up" if user.lower().startswith("/good") else "down"
            print(f"  {bot.feedback(verdict, user.partition(' ')[2])}")
            continue
        if user.lower().startswith("/lang"):
            print(f"  {bot.set_language(user.partition(' ')[2] or 'auto')}")
            continue
        if user.lower().startswith("/contribute"):
            arg = user.partition(' ')[2].strip().lower()
            print(f"  {bot.set_contribute(arg in ('on','yes','true','1','enable'))}")
            continue
        try:
            reply = bot.ask(user)
        except Exception as err:
            print(f"  [error] {err}")
            continue
        print(f"\nolympus ▸ {reply}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="olympus",
        description="OLYMPUS — self-improving multi-agent AI system",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("chat", help="interactive conversation (default)")
    sub.add_parser("setup", help="choose your AI provider & save your API key")

    p_ask = sub.add_parser("ask", help="one-shot question through the full pipeline")
    p_ask.add_argument("question", nargs="+")

    sub.add_parser("scan", help="Argus: scan the web for opportunities now")
    sub.add_parser("audit", help="Prometheus: self-audit and self-upgrade now")

    p_watch = sub.add_parser("watch", help="Mnemosyne: watch a YouTube video now")
    p_watch.add_argument("url")

    p_queue = sub.add_parser("queue", help="queue a YouTube video for the heartbeat")
    p_queue.add_argument("url")

    sub.add_parser("heartbeat", help="run the self-recurring autonomous loop")
    sub.add_parser("learn", help="Metis: run the daily learning cycle now")
    sub.add_parser("eval", help="run the quality benchmark and save the score")
    sub.add_parser("code-eval", help="run execution-scored coding benchmarks "
                                     "(runs Hephaestus's code against tests)")
    sub.add_parser("skills", help="list the self-built skill library")
    sub.add_parser("gate", help="benchmark-gate provisional skills now")
    p_train = sub.add_parser("train", help="score all specialists and have "
                                           "Prometheus strengthen the weakest")
    p_train.add_argument("--focus", type=int, default=2,
                         help="how many of the weakest specialists to improve")
    sub.add_parser("scores", help="show per-specialist benchmark scores")
    sub.add_parser("models", help="show the model pool and role assignments")
    sub.add_parser("contrib", help="show the cross-model contribution queue size")
    p_usage = sub.add_parser("usage", help="show estimated token/cost spend")
    p_usage.add_argument("--days", type=int, default=7)

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
    p_gr = sub.add_parser("grant", help="grant a permission scope (e.g. email)")
    p_gr.add_argument("scope")
    p_rv = sub.add_parser("revoke", help="revoke a scope ('all' = kill switch)")
    p_rv.add_argument("scope")

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

    sub.add_parser("telegram", help="run the Telegram gateway "
                                    "(needs TELEGRAM_BOT_TOKEN)")

    args = parser.parse_args(argv)

    from . import firstrun
    firstrun.load_env_file()        # saved keys apply to every command

    if args.command == "setup":
        firstrun.wizard()
    elif args.command in (None, "chat"):
        if not firstrun.ensure_ready():
            return 1
        _chat()
    elif args.command == "ask":
        if not firstrun.ensure_ready():
            return 1
        from . import config
        bot = orchestrator.Olympus(
            report=lambda msg: print(f"  {msg}", file=sys.stderr),
            pool=config.ModelPool.from_env())
        print(bot.ask(" ".join(args.question)))
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
    elif args.command == "gate":
        print(orchestrator.gate_skills())
    elif args.command == "train":
        print(orchestrator.train_specialists(focus=args.focus))
    elif args.command == "models":
        from . import config
        print(config.ModelPool.from_env().assignment())
    elif args.command in ("actions", "approve", "reject", "edit", "undo",
                          "autonomy", "grant", "revoke"):
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
        elif args.command == "grant":
            print(actions.grant_scope(user, args.scope))
        elif args.command == "revoke":
            print(actions.revoke_all(user) if args.scope == "all"
                  else actions.revoke_scope(user, args.scope))
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
    elif args.command == "web":
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
