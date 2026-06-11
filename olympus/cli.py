"""Olympus command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import heartbeat, memory, orchestrator


def _chat() -> None:
    print("OLYMPUS — Zeus speaking. Type 'exit' to leave, "
          "'/good' or '/bad' to rate the last answer.\n")
    bot = orchestrator.Olympus(report=lambda msg: print(f"  {msg}"),
                               user="cli", conversation_id="cli-default")
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
    sub.add_parser("skills", help="list the self-built skill library")
    sub.add_parser("gate", help="benchmark-gate provisional skills now")
    p_usage = sub.add_parser("usage", help="show estimated token/cost spend")
    p_usage.add_argument("--days", type=int, default=7)

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

    if args.command in (None, "chat"):
        _chat()
    elif args.command == "ask":
        bot = orchestrator.Olympus(report=lambda msg: print(f"  {msg}", file=sys.stderr))
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
    elif args.command == "skills":
        from . import skills
        print(skills.index())
    elif args.command == "gate":
        print(orchestrator.gate_skills())
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
