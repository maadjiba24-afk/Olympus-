"""OS-level computer use — GATED, SANDBOXED, default-off, human-in-the-loop.

"Computer use" is the agent driving the OS directly: screenshot the screen,
move/click the mouse, type keystrokes, launch a program. It is the most
powerful — and most dangerous — capability in the system, so it is built
strictly as a *framework of safety rails* here, with the actual actuation
pluggable and DISABLED by default. Nothing in this module can touch the OS
unless an operator both enables it and installs a real actuator.

Every rail the rest of Olympus already has, applied to computer use:

  * **On by an operator, never by the agent.** `enabled()` is False unless
    `OLYMPUS_COMPUTER_USE` is set. The default actuator REFUSES every action
    (fail closed) — a real actuator must be explicitly registered.
  * **Approval spine.** Each action is a registered `actions.ActionType` at
    `IRREVERSIBLE` risk (screenshots at `NOTABLE`), so it flows through the
    existing prepare → approve → execute gate, needs the `computer.use` scope,
    obeys daily limits, and can NEVER auto-execute (irreversible ⇒ level 99).
  * **ABC contract.** A `computer.use` behavioral contract (recovery `block`)
    re-checks, at the actuation chokepoint: the capability is enabled, an
    actuator is ready, a launched command is not cmdguard-DENY, and typed text
    carries no exfiltrated secret. Defense in depth over the imperative checks.
  * **Never bypasses cmdguard/security.** A `launch` command is scanned by
    `cmdguard`; typed text is scanned by `security.secret_exfil_reason`.
  * **Signed audit.** Every actuation is a witness-signed ledger record
    (`deltas`), externally anchored for free — with no secret in it (typed text
    is recorded only by length).
"""

from __future__ import annotations

import os

from . import actions, deltas

SCOPE = "computer.use"

# A legitimate `key` payload is a key NAME or a chord of them (Return, Tab,
# Escape, ctrl+c, cmd+shift+4, F5, ...). Only such values are kept verbatim in
# the signed, externally-anchored audit; anything else (a pasted blob, a
# secret) is recorded by length only. A permissive char-class regex is NOT
# enough — a short alphanumeric secret matches one — so we require every
# '+'-separated part to be a KNOWN key token (a modifier, a named key, or a
# single character).
_KEY_MODIFIERS = frozenset({
    "ctrl", "control", "alt", "option", "opt", "shift", "cmd", "command",
    "meta", "super", "win", "windows", "fn"})
_KEY_NAMES = frozenset({
    "return", "enter", "tab", "escape", "esc", "space", "spacebar",
    "backspace", "delete", "del", "home", "end", "pageup", "pagedown",
    "up", "down", "left", "right", "insert", "capslock", "numlock",
    "printscreen", "pause", "break", "menu", "plus", "minus",
}) | frozenset(f"f{i}" for i in range(1, 25))


def _is_key_token(tok: str) -> bool:
    t = tok.strip().lower()
    return len(tok) == 1 or t in _KEY_MODIFIERS or t in _KEY_NAMES


def _looks_like_keyspec(keys: str) -> bool:
    """True only for a genuine key name / chord — every '+'-joined part is a
    known key token. A pasted secret (even a short alphanumeric one) is not."""
    if not keys or len(keys) > 32:
        return False
    parts = keys.split("+")
    return all(_is_key_token(p) for p in parts if p != "") and any(parts)

# Action name → whether it is a world-changing actuation. ALL are IRREVERSIBLE
# risk — including `screenshot`, which can capture on-screen secrets — so NO
# computer-use action can ever auto-execute (irreversible ⇒ autonomy level 99);
# every one needs an explicit human approval.
_ACTIONS = {
    "computer_screenshot": False,
    "computer_move":       True,
    "computer_click":      True,
    "computer_type":       True,
    "computer_key":        True,
    "computer_launch":     True,
}


class ComputerUseError(RuntimeError):
    """A computer-use action was refused (disabled, no actuator, or unsafe)."""


def enabled() -> bool:
    """Operator-only master switch (default OFF). The agent never sets this."""
    return os.environ.get("OLYMPUS_COMPUTER_USE", "").strip().lower() in (
        "1", "on", "true", "yes")


# --- actuators --------------------------------------------------------------

class Actuator:
    """The thing that actually drives the OS. NONE ships enabled — a real one
    (X11/pyautogui, a VM channel, an OS API) must be registered by the operator,
    so no GUI-automation dependency is pulled into the core and nothing can move
    the mouse until a human wires it up."""
    name = "base"

    def screenshot(self) -> dict: raise NotImplementedError
    def move(self, x: int, y: int) -> dict: raise NotImplementedError
    def click(self, x: int, y: int, button: str = "left") -> dict: raise NotImplementedError
    def type(self, text: str) -> dict: raise NotImplementedError
    def key(self, keys: str) -> dict: raise NotImplementedError
    def launch(self, command: str) -> dict: raise NotImplementedError


class DisabledActuator(Actuator):
    """The default: refuses everything, fail closed."""
    name = "disabled"

    def _refuse(self, *a, **k):
        raise ComputerUseError(
            "no computer-use actuator is installed — OS control is disabled by "
            "default. An operator must register a real actuator; nothing drives "
            "the OS until they do.")

    screenshot = move = click = type = key = launch = _refuse


_actuator: Actuator = DisabledActuator()


def register_actuator(actuator: Actuator) -> None:
    """Install the OS actuator — the operator's deliberate act."""
    global _actuator
    _actuator = actuator


def reset_actuator() -> None:
    global _actuator
    _actuator = DisabledActuator()


# --- the guarded actuation chokepoint --------------------------------------

def _ctx(name: str, payload: dict, *, actuator: Actuator, approved: bool) -> dict:
    return {
        "computer_use_enabled": enabled(),
        "computer_actuator_ready": not isinstance(actuator, DisabledActuator),
        # Human-in-the-loop is enforced AT the chokepoint, not just by callers:
        # the actuation is refused unless it carries approval evidence (only the
        # approval-spine executor, which runs post-approval, sets this True).
        "computer_action_approved": bool(approved),
        # A launched command is a shell command — it must clear cmdguard.
        "command": payload.get("command") if name == "computer_launch" else None,
        # Text entered at the OS level must carry no exfiltrated secret — this
        # covers BOTH `type` (bulk text) and `key` (keystrokes can be abused to
        # enter a secret one field at a time).
        "typed_text": (payload.get("text") if name == "computer_type"
                       else payload.get("keys") if name == "computer_key"
                       else None),
    }


def _summary(name: str, payload: dict) -> dict:
    """A SECRET-FREE summary for the audit record — typed text by length only,
    and a launch command by its EXECUTABLE + a hash (never the verbatim args,
    which can carry an inline credential like `mysql -psecret`)."""
    if name == "computer_type":
        return {"action": name, "text_len": len(str(payload.get("text", "")))}
    if name == "computer_launch":
        import hashlib
        cmd = str(payload.get("command", ""))
        return {"action": name, "exec": cmd.split()[0] if cmd.split() else "",
                "argc": max(0, len(cmd.split()) - 1),
                "command_hash": hashlib.sha256(cmd.encode()).hexdigest()[:16]}
    if name in ("computer_click", "computer_move"):
        return {"action": name, "x": payload.get("x"), "y": payload.get("y"),
                "button": payload.get("button", "left")}
    if name == "computer_key":
        keys = str(payload.get("keys", ""))
        # A real key-name/chord is kept for forensics; anything else (a pasted
        # blob) is recorded by length only, never verbatim.
        out = {"action": name, "keys_len": len(keys)}
        if _looks_like_keyspec(keys):
            out["keys"] = keys
        return out
    return {"action": name}


def _actuate(name: str, payload: dict, actuator: Actuator) -> dict:
    if name == "computer_screenshot":
        return actuator.screenshot()
    if name == "computer_move":
        return actuator.move(int(payload.get("x", 0)), int(payload.get("y", 0)))
    if name == "computer_click":
        return actuator.click(int(payload.get("x", 0)), int(payload.get("y", 0)),
                              str(payload.get("button", "left")))
    if name == "computer_type":
        return actuator.type(str(payload.get("text", "")))
    if name == "computer_key":
        return actuator.key(str(payload.get("keys", "")))
    if name == "computer_launch":
        return actuator.launch(str(payload.get("command", "")))
    raise ComputerUseError(f"unknown computer-use action {name!r}")


def perform(name: str, payload: dict, *, user: str = "shared",
            actuator: Actuator | None = None, approved: bool = False) -> dict:
    """Actuate one computer-use action through every rail: enablement, the ABC
    `computer.use` contract (approval evidence + cmdguard + secret scan), then
    the actuator, then a signed audit record. Raises `ComputerUseError` /
    `behavioral_contracts.ContractViolation` on any refusal (fail closed).

    This is the ACTUATION chokepoint, and it SELF-ENFORCES human approval:
    `approved` defaults False, and the contract refuses an unapproved actuation.
    Only the approval-spine executor (which runs post-approval) passes
    `approved=True` — a direct call cannot skip the human. Every attempt,
    including a contract-blocked one, is a signed audit record."""
    if name not in _ACTIONS:
        raise ComputerUseError(f"unknown computer-use action {name!r}")
    actuator = actuator if actuator is not None else _actuator

    # Record the INTENT first, and FAIL CLOSED if it can't be signed: for OS
    # control the audit must never fail open, so an unrecordable actuation is
    # refused before it can touch the OS (the payrail 'charged' pattern).
    try:
        _record(user, name, payload, event="attempt", critical=True)
    except Exception as err:
        raise ComputerUseError(
            f"refusing to actuate {name!r}: the attempt could not be recorded "
            f"to the signed audit ({err})") from err

    if not enabled():
        _record(user, name, payload, event="blocked", reason="disabled")
        raise ComputerUseError(
            "computer use is disabled (OLYMPUS_COMPUTER_USE unset).")

    from . import behavioral_contracts as abc
    try:
        abc.enforce("computer.use", _ctx(name, payload, actuator=actuator,
                                         approved=approved))
    except abc.ContractViolation as v:
        _record(user, name, payload, event="blocked", reason=str(v)[:200])
        raise

    try:
        result = _actuate(name, payload, actuator)
    except Exception as err:                 # actuator refusal / any actuator bug
        _record(user, name, payload, event="refused", reason=str(err)[:200])
        raise ComputerUseError(str(err)) if not isinstance(
            err, ComputerUseError) else err
    _record(user, name, payload, event="performed")
    return result if isinstance(result, dict) else {"ok": True}


def _record(user: str, name: str, payload: dict, *, event: str,
            reason: str = "", critical: bool = False) -> str:
    """Append a witness-signed record of one computer-use event. `critical=True`
    (the pre-actuation 'attempt') PROPAGATES a write failure so an actuation
    that cannot be audited is refused; the post-hoc events (blocked / refused /
    performed) are best-effort but still loud (never silent)."""
    try:
        snap = deltas.record_snapshot(
            f"computeruse:{user}", kind="computer-use",
            state={**_summary(name, payload), "event": event, "reason": reason},
            provenance=deltas.Provenance(source="computeruse", trust="operator",
                                         detail=f"{name}:{event}"))
        return snap["snapshot_hash"]
    except Exception as err:
        if critical:
            raise
        # Audit-write failure on OS control is loud, never silent.
        from . import errors
        errors.capture("computeruse.record",
                       RuntimeError(f"unrecorded {name}:{event}: {err}"))
        return ""


def audit(user: str) -> list[dict]:
    """Every signed computer-use actuation record for a user."""
    return deltas.snapshots(f"computeruse:{user}")


# --- registration on the approval spine ------------------------------------

def _preview(name: str):
    def preview(payload: dict) -> str:
        return "COMPUTER USE — " + repr(_summary(name, payload))
    return preview


def _executor(name: str):
    # Runs at the approval-spine's execution chokepoint — reached ONLY after a
    # human approved the prepared action, so it asserts approval to the
    # chokepoint. This is the only sanctioned setter of approved=True.
    def execute(payload: dict) -> dict:
        return perform(name, payload, user=str(payload.get("_user", "shared")),
                       approved=True)
    return execute


_registered = False


def register_actions() -> None:
    """Register the computer-use actions on the approval spine (idempotent)."""
    global _registered
    if _registered:
        return
    for name in _ACTIONS:
        actions.register(actions.ActionType(
            name=name, risk_class=actions.IRREVERSIBLE, scope=SCOPE,
            preview=_preview(name), execute=_executor(name),
            description=f"OS computer use: {name.replace('computer_', '')}"))
    _registered = True


register_actions()
