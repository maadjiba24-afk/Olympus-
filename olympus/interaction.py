"""Mid-run user interaction — the seam behind the `ask_user` tool.

Adopted from Odysseus's `ask_user` (a specialist pauses mid-task to ask the
user a multiple-choice question). Olympus is headless-first and often runs
unattended, so the question is answered by a **thread-local provider** the
running surface installs — never by blocking on stdin from deep inside the
agent loop:

* An interactive surface (CLI chat, TUI) installs a provider that actually
  prompts and returns the choice.
* A headless surface (heartbeat, gateway, `ask`, scheduled task) installs
  nothing, so `ask_user` returns a clear "no interactive user — proceed with
  your best assumption and state it" instruction. The specialist keeps going
  instead of hanging, and the assumption is visible in its output.

Thread-local (not global) because specialists run in a ThreadPoolExecutor —
each worker inherits the provider the dispatcher set for the run, and one
conversation's prompt never leaks into another's.

Security: `ask_user` is not an ingestion tool and not an action — it neither
reads external content nor affects the world, so it stays available under
capability separation. Its options come from the model; the returned choice
is the user's, wrapped like any user turn.
"""

from __future__ import annotations

import threading
from typing import Callable

# provider(question, options) -> chosen string (an option, or free text).
Provider = Callable[[str, "list[str]"], str]

_local = threading.local()


def set_provider(provider: Provider | None) -> object:
    """Install the interaction provider for THIS thread. Returns the previous
    provider so a caller can restore it (use in try/finally)."""
    prev = getattr(_local, "provider", None)
    _local.provider = provider
    return prev


def reset_provider(previous: object) -> None:
    _local.provider = previous


def current() -> Provider | None:
    return getattr(_local, "provider", None)


def ask(question: str, options: list[str] | None = None) -> str:
    """Ask the user a question, offering `options` when interactive. In a
    headless run (no provider) returns guidance to proceed with an assumption
    rather than blocking."""
    question = (question or "").strip()
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    provider = current()
    if provider is None:
        return _proceed_instruction(opts)
    try:
        answer = provider(question, opts)
    except NoAnswer:
        # A surface-and-proceed provider (web/gateway) reported the question
        # but can't block for a live answer — fall through to the same
        # proceed-with-assumption guidance as headless.
        return _proceed_instruction(opts)
    except (EOFError, KeyboardInterrupt):
        return ("[The user dismissed the question. Proceed with your best "
                "assumption and state it.]")
    from . import security
    return security.wrap_untrusted(str(answer), source="user-answer")


class NoAnswer(Exception):
    """Raised by a provider that surfaced the question but cannot supply a
    live answer (an async surface), so ask() returns proceed guidance."""


def _proceed_instruction(opts: list[str]) -> str:
    listed = ("; ".join(opts)) if opts else "your best judgment"
    return ("[No interactive user is available to answer right now. "
            "Do not wait — proceed with the most reasonable assumption "
            f"(options were: {listed}) and state the assumption you made "
            "in your answer.]")


def cli_provider(io=None) -> Provider:
    """A provider that prompts on the terminal — installed by interactive
    front-ends. Numbered options; a bare number or free text both work."""
    from . import interactive
    io = io or interactive.IO()

    def provider(question: str, options: list[str]) -> str:
        io.out(f"\n❓ {question}")
        for i, opt in enumerate(options, 1):
            io.out(f"   {i}. {opt}")
        raw = io.line("   your answer: ").strip()
        if options and raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        return raw

    return provider


def reporting_provider(report) -> Provider:
    """A provider for async surfaces (web UI, chat gateways) that cannot block
    for a live answer within one run: it SURFACES the question through `report`
    so the user sees exactly what the agent asked, then raises NoAnswer so the
    run proceeds with a stated assumption. The user can course-correct in their
    next message — strictly better than the silent headless fallback.

    (A truly blocking prompt on these surfaces would require suspending and
    resuming the agent run across separate inbound messages/requests, which the
    request/response architecture doesn't support — so surface-and-proceed is
    the correct behavior here, not a blocking prompt.)"""
    def provider(question: str, options: list[str]) -> str:
        listed = f" (options: {'; '.join(options)})" if options else ""
        try:
            report(f"❓ {question}{listed} — no live answer channel here, so "
                   "I'll proceed with my best assumption; tell me if you'd "
                   "prefer otherwise.")
        except Exception:
            pass
        raise NoAnswer
    return provider
