"""Olympus command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import heartbeat, memory, orchestrator


def _chat() -> None:
    print("OLYMPUS — Zeus speaking. Type 'exit' to leave.\n")
    bot = orchestrator.Olympus(report=lambda msg: print(f"  {msg}"))
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
    elif args.command == "heartbeat":
        try:
            heartbeat.run_forever()
        except KeyboardInterrupt:
            print("\nHeartbeat stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
