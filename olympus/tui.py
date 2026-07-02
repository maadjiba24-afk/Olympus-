"""A richer terminal UI for the interactive chat.

The old `olympus` REPL was a bare `input()` loop. This adds the things a real
terminal UI needs, all with the standard library:

  * **Slash-command autocomplete** — press Tab to complete `/sc` → `/scan`
    (via `readline` when available; the command set is shared with `/help`).
  * **Multiline input** — end a line with `\\` to continue, or open a fenced
    ```\n…\n``` block to paste code/prompts spanning many lines.
  * **Streaming output** — Zeus's final answer prints token-by-token.
  * **In-chat commands** — /help /scan /audit /watch /queue /good /bad /lang
    /contribute, plus /exit.

The parsing helpers are pure functions so they can be unit-tested without a
terminal.
"""

from __future__ import annotations

import sys

# name → one-line help. Drives /help, Tab-completion, and dispatch.
COMMANDS: dict[str, str] = {
    "/help": "show this help",
    "/scan": "scan the web for opportunities & world events now",
    "/audit": "Olympus audits and upgrades itself",
    "/watch": "watch a YouTube video and learn from it: /watch <url>",
    "/queue": "queue a YouTube video for the autonomous loop: /queue <url>",
    "/good": "rate the last answer good (optionally /good <comment>)",
    "/bad": "rate the last answer bad (optionally /bad <comment>)",
    "/undo": "remove the last N exchanges from the conversation: /undo [N]",
    "/steer": "nudge the current/next task mid-run: /steer <note>",
    "/lang": "set reply language: /lang <language|auto>",
    "/contribute": "share anonymized insights: /contribute on|off",
    "/growth": "see how Olympus has adapted to you over time",
    "/learned": "see what Olympus learned/did on its own while you were away",
    "/exit": "leave Olympus",
}


def complete(prefix: str) -> list[str]:
    """Tab-completion candidates for a slash-command prefix."""
    if not prefix.startswith("/"):
        return []
    return sorted(c for c in COMMANDS if c.startswith(prefix))


def help_text() -> str:
    width = max(len(c) for c in COMMANDS)
    lines = ["Commands:"]
    lines += [f"  {c.ljust(width)}  {desc}" for c, desc in COMMANDS.items()]
    lines.append("Multiline: end a line with \\ to continue, or wrap text in "
                 "``` fences. Anything else is a question to the council.")
    return "\n".join(lines)


def read_block(read_line, prompt: str = "you ▸ ",
               cont: str = "  ... ") -> str | None:
    """Read one logical input, supporting `\\` continuation and ``` fences.

    `read_line(prompt)` is the line source (input() in production, a fake in
    tests). Returns the assembled text, or None on EOF."""
    try:
        first = read_line(prompt)
    except EOFError:
        return None
    if first is None:
        return None
    stripped = first.strip()
    if stripped == "```":                         # fenced block until closing ```
        buf: list[str] = []
        while True:
            try:
                line = read_line(cont)
            except EOFError:
                break
            if line is None or line.strip() == "```":
                break
            buf.append(line)
        return "\n".join(buf)
    # backslash continuation
    if first.endswith("\\"):
        buf = [first[:-1]]
        while True:
            try:
                line = read_line(cont)
            except EOFError:
                break
            if line is None:
                break
            if line.endswith("\\"):
                buf.append(line[:-1])
            else:
                buf.append(line)
                break
        return "\n".join(buf)
    return first


def dispatch_command(bot, raw: str):
    """Handle an in-chat slash command. Returns (handled, output_or_None,
    should_exit)."""
    name, _, arg = raw.strip().partition(" ")
    name = name.lower()
    if name not in COMMANDS:
        return (False, None, False)
    if name == "/exit":
        return (True, None, True)
    if name == "/help":
        return (True, help_text(), False)
    if name == "/scan":
        from . import orchestrator
        return (True, orchestrator.opportunity_scan(), False)
    if name == "/audit":
        from . import orchestrator
        return (True, orchestrator.evolution_audit(), False)
    if name == "/watch":
        from . import orchestrator
        if not arg:
            return (True, "Usage: /watch <youtube-url>", False)
        return (True, orchestrator.watch_and_learn(arg), False)
    if name == "/queue":
        from . import memory
        if not arg:
            return (True, "Usage: /queue <youtube-url>", False)
        memory.watchlist_add(arg)
        return (True, "Queued for the heartbeat.", False)
    if name == "/undo":
        try:
            n = int(arg) if arg.strip() else 1
        except ValueError:
            return (True, "Usage: /undo [N]", False)
        return (True, bot.undo(n), False)
    if name == "/steer":
        from . import steering
        if not arg.strip():
            return (True, "Usage: /steer <note> — the running (or next) task "
                          "sees it after its next tool call", False)
        key = getattr(bot, "conversation_id", None) or f"user-{bot.user}"
        ok = steering.put(key, arg)
        return (True, "Noted — the task will see this after its next tool call."
                if ok else "Steering queue is full; note dropped.", False)
    if name in ("/good", "/bad"):
        return (True, bot.feedback("up" if name == "/good" else "down", arg), False)
    if name == "/lang":
        return (True, bot.set_language(arg or "auto"), False)
    if name == "/contribute":
        on = arg.strip().lower() in ("on", "yes", "true", "1", "enable")
        return (True, bot.set_contribute(on), False)
    if name == "/growth":
        from . import companion
        return (True, companion.summary("cli"), False)
    if name == "/learned":
        from . import digest
        return (True, digest.learned_recently(), False)
    return (False, None, False)


def _install_readline() -> None:
    try:
        import readline
    except ImportError:
        return

    def completer(text, state):
        matches = complete(text)
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")
    # Don't treat '/' as a word break, so '/sc' completes as one token.
    readline.set_completer_delims(" \t\n")


def welcome_banner(pool) -> str:
    """A branded launch screen: the model pool + a capability overview drawn
    from the live manifest, so a new user immediately sees what they have."""
    from . import capabilities
    from .specialists import SPECIALISTS
    lines = [
        "        ⚡  O L Y M P U S",
        "   a self-improving council of AI specialists",
        "",
    ]
    lines.append(pool.assignment())
    try:
        m = capabilities.manifest()
        roster = ", ".join(SPECIALISTS[k].name for k in sorted(SPECIALISTS))
        lines += [
            "",
            f"  {m['agents']['count']} specialists · {m['tools']['count']} tools"
            f" · {m['commands']['count']} commands",
            f"  council: {roster}",
        ]
    except Exception:
        pass
    lines += [
        "",
        "  Type a question in plain English. /help for commands, /growth to see",
        "  how Olympus is adapting to you, /exit to leave.",
    ]
    return "\n".join(lines)


def status_line(model: str, secs: float, *, fast: bool = False,
                spend: float | None = None) -> str:
    """A compact per-turn status bar: model · time · spend · mode."""
    parts = [f"⚡ {model}", f"{secs:.1f}s"]
    if spend is not None:
        parts.append(f"${spend:.4f} today")
    if fast:
        parts.append("fast")
    return "  " + " · ".join(parts)


def run(pool=None) -> None:
    import time
    from . import config, orchestrator, usage
    pool = pool or config.ModelPool.from_env()
    _install_readline()
    print(welcome_banner(pool))
    print()
    bot = orchestrator.Olympus(report=lambda m: print(f"  {m}"),
                               user="cli", conversation_id="cli-default",
                               pool=pool)

    def read_line(prompt):
        try:
            return input(prompt)
        except KeyboardInterrupt:
            print()
            return ""

    while True:
        text = read_block(read_line)
        if text is None:
            print()
            break
        text = text.strip()
        if not text:
            continue
        if text in ("exit", "quit"):
            break
        handled, output, should_exit = dispatch_command(bot, text)
        if should_exit:
            break
        if handled:
            if output:
                print(f"\nolympus ▸ {output}\n")
            continue
        # A real question → stream the final answer token-by-token.
        try:
            sys.stdout.write("\nolympus ▸ ")
            sys.stdout.flush()
            t0 = time.time()
            for piece in bot.ask_stream(text):
                sys.stdout.write(piece)
                sys.stdout.flush()
            secs = time.time() - t0
            try:
                spend = usage.today_spend()
            except Exception:
                spend = None
            print("\n" + status_line(
                f"{pool.primary().provider}/{pool.primary().model or 'default'}",
                secs, fast=config.fast_mode(), spend=spend) + "\n")
        except Exception as err:
            print(f"\n  [error] {err}\n")
        # Post-turn interactions outside the model loop: secure credential
        # capture (remember mode) and plain-English approval of held actions.
        try:
            from . import interactive
            interactive.after_turn(getattr(bot, "user", "cli"))
        except Exception:
            pass
