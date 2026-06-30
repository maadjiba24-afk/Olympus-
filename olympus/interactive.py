"""What the interactive app does *around* a model turn — kept out of the model
loop and behind an injectable IO so it is fully testable without a terminal.

Two jobs, both consumer-facing:
  1. Secure credential capture: if the turn asked to remember a site sign-in, a
     private prompt (the model never sees it) collects username/password and
     stores them in the vault.
  2. Plain-English approval: any action the spine is holding for approval is
     shown and confirmed with a simple yes/no, instead of `olympus approve <id>`.

See docs/DESIGN_OPERATOR_UX.md.
"""

from __future__ import annotations

from . import approvals, securecapture

_YES = {"y", "yes", "yeah", "yep", "sure", "ok", "okay", "do it", "confirm",
        "go", "go ahead", "approve"}


def is_yes(answer: str) -> bool:
    return (answer or "").strip().lower() in _YES


class IO:
    """Default terminal IO. Tests pass a fake with the same three methods."""

    def line(self, prompt: str) -> str:
        return input(prompt)

    def secret(self, prompt: str) -> str:
        import getpass
        return getpass.getpass(prompt)

    def out(self, text: str) -> None:
        print(text)


def after_turn(user: str, io: IO | None = None) -> list[str]:
    """Run the post-turn interactions for `user`. Returns a list of short event
    tags (for logs/tests). Never raises on user EOF/interrupt."""
    io = io or IO()
    events: list[str] = []

    # 1) Secure credential capture (remember mode).
    domain = securecapture.pending(user)
    if domain:
        io.out(f"\n🔒 Saving your {domain} sign-in — this prompt is private and "
               "never shown to the assistant.")
        try:
            username = io.line("   username: ").strip()
            password = io.secret("   password: ")
        except (EOFError, KeyboardInterrupt):
            securecapture.clear(user)
            io.out("   (skipped — nothing saved)")
            password = ""
            username = ""
        if username and password:
            securecapture.fulfill(user, domain, username, password)
            io.out(f"   Saved securely. I'll sign in to {domain} for you.")
            events.append(f"saved-login:{domain}")
        elif securecapture.pending(user):      # still pending → user gave nothing
            securecapture.clear(user)
            io.out("   (skipped — nothing saved)")

    # 2) Plain-English approval of held actions.
    for action in approvals.pending(user):
        io.out("")
        io.out(action.preview or action.title)
        if is_yes(io.line("Approve this? [y/N] ")):
            done = approvals.approve(user, action.id)
            ok = getattr(done, "status", "") == "executed"
            io.out("   ✓ Done." if ok else f"   {done.status}: {done.error or ''}")
            events.append(f"approved:{action.id}")
        else:
            approvals.reject(user, action.id, "declined at prompt")
            io.out("   Skipped.")
            events.append(f"declined:{action.id}")

    return events
