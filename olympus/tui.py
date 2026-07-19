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

import os
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
    "/goal": "standing goals: /goal <text> [:: done-means] · /goal list|drop|wait <id> [pid]",
    "/learn": "distill a reusable skill from a url/path/workflow: /learn <source>",
    "/journey": "timeline of everything learned: /journey [show|rm <ref>]",
    "/moa": "one-shot mixture-of-agents (labeled drafts + aggregate): /moa <question>",
    "/prompt": "compose a multi-line question in $EDITOR",
    "/reasoning": "show how the last answer was produced (pipeline trace)",
    "/timestamps": "toggle timestamps on replies: /timestamps on|off",
    "/undo": "remove the last N exchanges from the conversation: /undo [N]",
    "/steer": "nudge the current/next task mid-run: /steer <note>",
    "/lang": "set reply language: /lang <language|auto>",
    "/contribute": "share anonymized insights: /contribute on|off",
    "/growth": "see how Olympus has adapted to you over time",
    "/reset": "start fresh — distill this chat into durable state, then clear it",
    "/new": "switch to a fresh session (old one stays resumable): /new [name]",
    "/bg": "run a task in the background while you keep chatting: /bg <task>",
    "/btw": "ephemeral side question — leaves no trace in this session: /btw <q>",
    "/model": "swap the primary model mid-session: /model <name>",
    "/fast": "toggle fast mode for this session: /fast on|off",
    "/sessions": "list past sessions (resume with `olympus sessions`)",
    "/progress": "set how much live progress to show: off|stages|all|verbose",
    "/doctor": "check readiness (provider, sandbox, security, capabilities)",
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
    if name == "/goal":
        from . import goals
        sub, _, rest = arg.strip().partition(" ")
        user = getattr(bot, "user", "cli")
        if not arg.strip() or sub == "list":
            return (True, goals.summary(user), False)
        if sub == "drop":
            return (True, goals.set_status(rest.strip(), "dropped"), False)
        if sub == "done":
            return (True, goals.set_status(rest.strip(), "done",
                                           evidence="closed manually"), False)
        if sub == "wait":
            parts = rest.split()
            if len(parts) != 2 or not parts[1].isdigit():
                return (True, "Usage: /goal wait <goal id> <pid>", False)
            return (True, goals.wait_on(parts[0], int(parts[1])), False)
        text, _, contract = arg.partition("::")
        return (True, goals.add(user, text.strip(), contract.strip()), False)
    if name == "/learn":
        from . import learn
        # The TUI runs on the operator's own machine — paths are fair game.
        return (True, learn.distill(arg, allow_paths=True), False)
    if name == "/journey":
        from . import journey
        sub, _, ref = arg.strip().partition(" ")
        user = getattr(bot, "user", "cli")
        if sub == "show":
            return (True, journey.show(ref.strip(), user), False)
        if sub == "rm":
            return (True, journey.remove(ref.strip(), user), False)
        return (True, journey.timeline(user), False)
    if name == "/moa":
        from . import moa
        if not arg.strip():
            return (True, "Usage: /moa <question> — every pool member "
                          "answers; the strongest aggregates.", False)
        try:
            return (True, moa.one_shot(arg.strip()), False)
        except Exception as err:
            return (True, f"moa failed: {err}", False)
    if name == "/reasoning":
        return (True, reasoning_view(bot), False)
    if name == "/timestamps":
        on = arg.strip().lower() not in ("off", "0", "false", "no")
        bot._show_timestamps = on
        return (True, f"Timestamps {'on' if on else 'off'}.", False)
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
    if name == "/reset":
        return (True, bot.reset(), False)
    if name == "/progress":
        import os
        from . import config
        choice = arg.strip().lower()
        if choice not in ("off", "stages", "all", "verbose"):
            return (True, f"Progress mode is '{config.progress_mode()}'. "
                    "Set with: /progress off|stages|all|verbose", False)
        os.environ["OLYMPUS_PROGRESS"] = choice
        return (True, f"Progress mode set to '{choice}'.", False)
    if name == "/doctor":
        from . import doctor
        return (True, doctor.render(), False)
    if name == "/learned":
        from . import digest
        return (True, digest.learned_recently(), False)
    if name == "/btw":
        if not arg:
            return (True, "Usage: /btw <question> — answered without touching "
                    "this session's history or memory", False)
        return (True, bot.ask_ephemeral(arg), False)
    if name == "/bg":
        if not arg:
            return (True, "Usage: /bg <task> — runs in the background; the "
                    "answer announces itself here when ready", False)
        return (True, start_background_task(bot, arg), False)
    if name == "/model":
        return (True, bot.set_model(arg), False)
    if name == "/fast":
        import os
        from . import config
        choice = arg.strip().lower()
        if choice not in ("on", "off"):
            state = "on" if config.fast_mode() else "off"
            return (True, f"Fast mode is {state}. Toggle with: /fast on|off",
                    False)
        os.environ["OLYMPUS_FAST"] = "1" if choice == "on" else "0"
        return (True, f"Fast mode {choice} — light stages "
                + ("run on the pool's quickest model; the optional review "
                   "stage is skipped." if choice == "on"
                   else "run on the strongest model again."), False)
    if name == "/sessions":
        from . import memory
        sessions = memory.list_conversations(prefix="cli")[:10]
        if not sessions:
            return (True, "No saved sessions yet.", False)
        lines = ["Recent sessions (resume: exit, then `olympus sessions`):"]
        for s in sessions:
            lines.append(f"  {s['id']}  ({s['turns']} turn(s)) — {s['preview']}")
        return (True, "\n".join(lines), False)
    return (False, None, False)


# --- /bg: one-shot background tasks ----------------------------------------

_BG_THREADS: list = []      # kept for tests / clean shutdown introspection
_BG_COUNTER = {"n": 0}


def start_background_task(bot, task: str) -> str:
    """Run `task` through the full verified pipeline on a background thread.
    The chat stays live; when the answer is ready it announces itself and is
    saved to reports (so it survives even if the terminal scrolls past)."""
    import threading
    from . import memory, orchestrator
    _BG_COUNTER["n"] += 1
    n = _BG_COUNTER["n"]
    user = getattr(bot, "user", "cli")
    pool = getattr(bot, "pool", None)

    def work() -> None:
        try:
            # A separate Olympus so the background run never races the live
            # session's history; same user, so memory context still applies.
            worker = orchestrator.Olympus(user=user, pool=pool)
            answer = worker.ask(task)
        except Exception as err:
            answer = f"[background task failed: {str(err)[:200]}]"
        try:
            memory.set_user(user)
            memory.save("reports", f"Background task #{n}: {task[:60]}", answer)
        except Exception:
            pass
        print(f"\n  🔔 [bg #{n} done] {task[:60]}\n"
              f"olympus ▸ {answer}\n")

    t = threading.Thread(target=work, daemon=True, name=f"bg-{n}")
    # Keep only live threads (+ this one) so a long session can't leak finished
    # Thread objects into an ever-growing list.
    _BG_THREADS[:] = [x for x in _BG_THREADS if x.is_alive()]
    _BG_THREADS.append(t)
    t.start()
    return (f"Running in the background as #{n} — keep chatting; "
            "I'll announce the answer here (it also lands in reports).")


MAX_REF_CHARS = 20_000       # per injected reference, keeps prompts bounded
_REF_RE = None               # compiled lazily (import re only when needed)


def expand_references(text: str, *, read=None, fetch=None) -> tuple[str, list[str]]:
    """Expand `@file` and `@https://url` references into context blocks.

    `@path` (an existing local file) and `@http(s)://…` tokens are replaced
    inline by their bare name, and the referenced content is appended as a
    clearly delimited context block. Tokens that are neither an existing file
    nor a URL (e.g. an @handle) pass through untouched. Returns the expanded
    text and a list of notes describing what was injected (for the UI).

    `read`/`fetch` are injectable for tests; production uses the filesystem
    and the SSRF-guarded web fetcher. URL content is wrapped as untrusted —
    a page must not be able to steer the council."""
    global _REF_RE
    import re
    if _REF_RE is None:
        _REF_RE = re.compile(r"(?:(?<=\s)|^)@(\S+)")
    from pathlib import Path

    def _read(path: str) -> str:
        return Path(path).expanduser().read_text(encoding="utf-8",
                                                 errors="replace")

    def _fetch(url: str) -> str:
        from . import tools
        return tools._strip_html(tools._http_get(url))

    read = read or _read
    fetch = fetch or _fetch
    blocks: list[str] = []
    notes: list[str] = []

    def _sub(match) -> str:
        ref = match.group(1).rstrip(".,;:!?)")
        if ref.startswith(("http://", "https://")):
            from . import security
            try:
                body = fetch(ref)[:MAX_REF_CHARS]
            except Exception as err:
                notes.append(f"could not fetch {ref}: {err}")
                return match.group(0)
            blocks.append(f"[context from {ref}]\n"
                          + security.wrap_untrusted(body, source=ref)
                          + "\n[end context]")
            notes.append(f"injected {ref} ({len(body)} chars)")
            return ref
        p = Path(ref).expanduser()
        if p.is_file():
            try:
                body = read(ref)[:MAX_REF_CHARS]
            except Exception as err:
                notes.append(f"could not read {ref}: {err}")
                return match.group(0)
            blocks.append(f"[context from file {ref}]\n{body}\n[end context]")
            notes.append(f"injected {ref} ({len(body)} chars)")
            return ref
        return match.group(0)          # not a file, not a URL — leave it

    expanded = _REF_RE.sub(_sub, text)
    if blocks:
        expanded = expanded + "\n\n" + "\n\n".join(blocks)
    return expanded, notes


def reasoning_view(bot) -> str:
    """How the last answer was produced: the run's recorded pipeline trace —
    routing, specialist spans, verification decisions. Olympus's honest
    analog of Hermes's /reasoning: what's shown is the signed decision path
    that actually ran, not free-form model musings."""
    run_id = getattr(bot, "last_run_id", None)
    if not run_id:
        return "No run yet — ask something first, then /reasoning."
    from . import trace as trace_mod
    rec = trace_mod.load_run(run_id)
    if rec is None:
        return f"Run {run_id} isn't recorded (traces may have been pruned)."
    lines = [f"Run {rec.get('id')} — {rec.get('kind')} · "
             f"{rec.get('total_secs', '?')}s"]
    events = rec.get("events") or []
    if events:
        lines.append("pipeline:")
        for e in events[:40]:
            stage = e.get("stage", "?")
            extra = {k: v for k, v in e.items()
                     if k not in ("stage", "t") and isinstance(v, (str, int))}
            tail = ("  " + ", ".join(f"{k}={str(v)[:60]}"
                                     for k, v in list(extra.items())[:3])
                    if extra else "")
            lines.append(f"  · {stage}{tail}")
    decisions = rec.get("decisions") or []
    if decisions:
        lines.append("decisions:")
        for d in decisions[:20]:
            who = (d.get("agent") or {}).get("role") or \
                (d.get("agent") or {}).get("channel") or "?"
            lines.append(f"  ⚖ {d.get('type', d.get('decision_type', '?'))} "
                         f"[{who}] → {str(d.get('status', ''))[:40]}")
    lines.append(f"(full detail: olympus explain {run_id})"
                 if rec.get("decisions") else "")
    return "\n".join(l for l in lines if l)


def editor_compose(run_editor=None) -> str:
    """/prompt: open $EDITOR on a temp file and return what was written.
    `run_editor(path)` is injectable for tests; production shells out."""
    import subprocess
    import tempfile
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"

    def _default(path: str) -> None:
        subprocess.run(f"{editor} {path}", shell=True)

    run_editor = run_editor or _default
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False,
                                     prefix="olympus-prompt-") as f:
        path = f.name
        f.write("")
    try:
        run_editor(path)
        text = open(path, encoding="utf-8").read().strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return text


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
                spend: float | None = None,
                ctx_frac: float | None = None) -> str:
    """A compact per-turn status bar: model · context · time · spend · mode."""
    parts = [f"⚡ {model}"]
    if ctx_frac is not None:
        parts.append(f"ctx {min(ctx_frac, 9.99):.0%}")
    parts.append(f"{secs:.1f}s")
    if spend is not None:
        parts.append(f"${spend:.4f} today")
    if fast:
        parts.append("fast")
    return "  " + " · ".join(parts)


def run(pool=None, conversation_id: str = "cli-default") -> None:
    import time
    from . import config, interaction, orchestrator, usage
    pool = pool or config.ModelPool.from_env()
    _install_readline()
    print(welcome_banner(pool))
    if conversation_id != "cli-default":
        print(f"\n  [session: {conversation_id}]")
    print()

    def report(m: str) -> None:
        # Honor the progress-verbosity mode (off / stages / all / verbose).
        if config.progress_allows(m):
            print(f"  {m}")

    # Interactive session → a specialist's ask_user prompts the terminal.
    # Installed before the Olympus is built so it captures the provider.
    interaction.set_provider(interaction.cli_provider())
    bot = orchestrator.Olympus(report=report,
                               user="cli", conversation_id=conversation_id,
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
        if text == "/prompt":
            # Compose a long question in $EDITOR, then run it as a question.
            text = editor_compose()
            if not text:
                print("  (empty — nothing sent)")
                continue
            print(f"\nyou ▸ {text[:200]}{'…' if len(text) > 200 else ''}")
        if text.split()[0].lower() == "/new":
            # Switch to a fresh session (handled here, not in dispatch_command,
            # because it swaps the loop's bot). The old session stays on disk —
            # resume it any time with `olympus sessions`.
            arg = text.partition(" ")[2].strip()
            from . import memory as _memory
            name = _memory.safe_id(arg) if arg else time.strftime("%Y%m%d-%H%M%S")
            conversation_id = f"cli-{name}"
            bot = orchestrator.Olympus(report=report, user="cli",
                                       conversation_id=conversation_id,
                                       pool=pool)
            print(f"\nolympus ▸ Fresh session: {conversation_id} "
                  "(the old one is saved — `olympus sessions` to go back).\n")
            continue
        handled, output, should_exit = dispatch_command(bot, text)
        if should_exit:
            break
        if handled:
            if output:
                print(f"\nolympus ▸ {output}\n")
            continue
        # @file / @url references: inject their content as context blocks.
        if "@" in text:
            try:
                text, notes = expand_references(text)
                for note in notes:
                    print(f"  [{note}]")
            except Exception as err:
                print(f"  [reference expansion failed: {err}]")
        # A real question → stream the final answer token-by-token.
        try:
            if getattr(bot, "_show_timestamps", False):
                print("\n  [" + time.strftime("%H:%M:%S") + "]", end="")
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
            try:
                budget = config.history_token_budget(bot.pool.primary().model)
                used = orchestrator.Olympus._estimate_tokens(bot.history)
                ctx_frac = used / budget if budget else None
            except Exception:
                ctx_frac = None
            live = bot.pool.primary()   # bot.pool so /model swaps show up
            print("\n" + status_line(
                f"{live.provider}/{live.model or 'default'}",
                secs, fast=config.fast_mode(), spend=spend,
                ctx_frac=ctx_frac) + "\n")
        except Exception as err:
            print(f"\n  [error] {err}\n")
        # Post-turn interactions outside the model loop: secure credential
        # capture (remember mode) and plain-English approval of held actions.
        try:
            from . import interactive
            interactive.after_turn(getattr(bot, "user", "cli"))
        except Exception:
            pass
