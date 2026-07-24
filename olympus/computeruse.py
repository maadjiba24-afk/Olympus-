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
import platform as _platform
import shutil
import subprocess

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


# --- the native (subprocess-backed) actuator -------------------------------
# A REAL actuator that drives the desktop via native command-line tools, so
# enabling computer use pulls in NO Python GUI-automation dependency (it shells
# out, exactly like sandbox.run) and the three-required-dependency footprint is
# unchanged. Still default-off and still behind every rail in `perform()`.

class _RunResult:
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _default_run(argv: list[str], *, stdin: str | None = None,
                 timeout: float = 20.0) -> _RunResult:
    """Run a short actuation command, capturing output. Text (e.g. for
    `xdotool type --file -`) is fed over STDIN, never as an argv element, so it
    never appears in the process table."""
    try:
        p = subprocess.run(
            argv, input=(stdin.encode("utf-8") if isinstance(stdin, str) else stdin),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return _RunResult(p.returncode,
                          p.stdout.decode("utf-8", "replace"),
                          p.stderr.decode("utf-8", "replace"))
    except FileNotFoundError as err:
        raise ComputerUseError(f"{argv[0]}: not found ({err})") from err
    except subprocess.TimeoutExpired:
        return _RunResult(124, "", f"{argv[0]} timed out")


def _default_spawn(command: str) -> dict:
    """Launch a program detached and return immediately. The command has already
    cleared cmdguard (in the ABC contract) and a human approval before this runs;
    it is started in its own session so it outlives the tick."""
    subprocess.Popen(command, shell=True, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "launched": True}


def _norm_system(name: str | None) -> str:
    n = (name or "").lower()
    if n.startswith("darwin") or n in ("mac", "macos"):
        return "darwin"
    if n.startswith("win"):
        return "windows"
    return "linux"


# Linux screenshot: first available tool wins (none is guaranteed present).
_LINUX_SHOT_TOOLS: list[tuple[str, list[str]]] = [
    ("scrot", ["scrot", "-o", "{path}"]),
    ("maim", ["maim", "{path}"]),
    ("gnome-screenshot", ["gnome-screenshot", "-f", "{path}"]),
    ("import", ["import", "-window", "root", "{path}"]),   # ImageMagick
]
_LINUX_BTN = {"left": "1", "middle": "2", "right": "3"}

# PowerShell one-liners for Windows (System.Windows.Forms / System.Drawing).
_WIN_SHOT = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
             "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
             "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
             "$g=[System.Drawing.Graphics]::FromImage($bmp); "
             "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
             "$bmp.Save('{path}')")
_WIN_MOVE = ("Add-Type -AssemblyName System.Windows.Forms; "
             "[System.Windows.Forms.Cursor]::Position="
             "New-Object System.Drawing.Point({x},{y})")
_WIN_CLICK = ("Add-Type -AssemblyName System.Windows.Forms; "
              "[System.Windows.Forms.Cursor]::Position="
              "New-Object System.Drawing.Point({x},{y}); "
              "Add-Type -MemberDefinition '[DllImport(\"user32.dll\")]public static "
              "extern void mouse_event(uint f,uint x,uint y,uint d,int e);' "
              "-Name U -Namespace W; "
              "[W.U]::mouse_event({down},0,0,0,0);[W.U]::mouse_event({up},0,0,0,0)")
_WIN_MOUSE = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}


def _tool_hint(tool: str, system: str) -> str:
    tips = {
        "xdotool": "install xdotool (needs an X11 session)",
        "cliclick": "install cliclick (`brew install cliclick`)",
        "osascript": "osascript ships with macOS",
        "screencapture": "screencapture ships with macOS",
        "powershell": "run on Windows PowerShell",
    }
    return (f"'{tool}' not found — {tips.get(tool, 'install it')} "
            f"to enable computer use on {system}")


class NativeActuator(Actuator):
    """Drive the OS via native CLI tools — Linux (X11): `xdotool` + a screenshot
    tool (`scrot`/`maim`/`gnome-screenshot`/ImageMagick `import`); macOS:
    `screencapture` + `cliclick`/`osascript`; Windows: PowerShell. No Python GUI
    dependency. Each action fails with a clear `ComputerUseError` naming the tool
    to install when the toolchain is absent, so a half-provisioned box degrades
    honestly instead of moving the mouse unpredictably. Typed text goes over
    STDIN (Linux/macOS) so it never lands in the process table — defense in depth
    over the secret-exfil scan the chokepoint already runs. `run`/`spawn`/`which`
    are injectable, so the whole actuator is unit-tested offline with no real OS
    control."""
    name = "native"

    def __init__(self, *, system: str | None = None, run=None, spawn=None,
                 which=None, shot_dir=None):
        self.system = _norm_system(system or _platform.system())
        self._run = run or _default_run
        self._spawn = spawn or _default_spawn
        self._which = which or shutil.which
        self._shot_dir = shot_dir

    def missing_tools(self) -> list[str]:
        """Required binaries absent for this platform (empty list = ready to
        drive the OS). Lets `doctor` report 'active but xdotool missing' instead
        of failing only at the first click."""
        miss: list[str] = []
        if self.system == "linux":
            if self._which("xdotool") is None:
                miss.append("xdotool")
            if not any(self._which(t) is not None for t, _ in _LINUX_SHOT_TOOLS):
                miss.append("scrot|maim|gnome-screenshot|import")
        elif self.system == "darwin":
            miss += [t for t in ("screencapture", "cliclick")
                     if self._which(t) is None]
        else:
            if self._which("powershell") is None:
                miss.append("powershell")
        return miss

    def _do(self, argv: list[str], *, stdin: str | None = None) -> _RunResult:
        tool = argv[0]
        if self._which(tool) is None:
            raise ComputerUseError(_tool_hint(tool, self.system))
        res = self._run(argv, stdin=stdin)
        if getattr(res, "returncode", 0) != 0:
            err = (getattr(res, "stderr", "") or "").strip()[:200]
            raise ComputerUseError(
                f"{tool} failed (exit {res.returncode})" + (f": {err}" if err else ""))
        return res

    def _shotdir(self):
        from pathlib import Path
        if self._shot_dir is not None:
            d = Path(self._shot_dir)
        else:
            from . import sandbox
            d = sandbox.workdir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ps(self, script: str) -> list[str]:
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]

    def screenshot(self) -> dict:
        import base64
        import time as _t
        from pathlib import Path
        path = self._shotdir() / f"screen-{int(_t.time() * 1000)}.png"
        p = str(path)
        if self.system == "linux":
            argv = None
            for tool, tmpl in _LINUX_SHOT_TOOLS:
                if self._which(tool) is not None:
                    argv = [a.format(path=p) for a in tmpl]
                    break
            if argv is None:
                raise ComputerUseError(
                    "no screenshot tool found on Linux — install one of scrot, "
                    "maim, gnome-screenshot, or ImageMagick (import)")
        elif self.system == "darwin":
            argv = ["screencapture", "-x", p]
        else:
            argv = self._ps(_WIN_SHOT.format(path=p))
        self._do(argv)
        data = Path(path).read_bytes()
        return {"ok": True, "format": "png", "path": p, "bytes": len(data),
                "image": base64.b64encode(data).decode("ascii")}

    def move(self, x: int, y: int) -> dict:
        x, y = int(x), int(y)
        if self.system == "linux":
            argv = ["xdotool", "mousemove", "--sync", str(x), str(y)]
        elif self.system == "darwin":
            argv = ["cliclick", f"m:{x},{y}"]
        else:
            argv = self._ps(_WIN_MOVE.format(x=x, y=y))
        self._do(argv)
        return {"ok": True, "x": x, "y": y}

    def click(self, x: int, y: int, button: str = "left") -> dict:
        x, y = int(x), int(y)
        b = (button or "left").lower()
        if self.system == "linux":
            argv = ["xdotool", "mousemove", "--sync", str(x), str(y),
                    "click", _LINUX_BTN.get(b, "1")]
        elif self.system == "darwin":
            verb = {"right": "rc"}.get(b, "c")     # cliclick: c=left, rc=right
            argv = ["cliclick", f"{verb}:{x},{y}"]
        else:
            down, up = _WIN_MOUSE.get(b, _WIN_MOUSE["left"])
            argv = self._ps(_WIN_CLICK.format(x=x, y=y, down=down, up=up))
        self._do(argv)
        return {"ok": True, "x": x, "y": y, "button": b}

    def type(self, text: str) -> dict:
        text = str(text)
        if self.system == "linux":
            # text over STDIN — never in argv / the process table.
            self._do(["xdotool", "type", "--clearmodifiers", "--file", "-"],
                     stdin=text)
        elif self.system == "darwin":
            # osascript reads the whole script (which contains the text) from
            # STDIN, so the text is not a process argument either.
            esc = text.replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "System Events" to keystroke "{esc}"'
            self._do(["osascript", "-"], stdin=script)
        else:
            esc = text.replace("{", "{{").replace("}", "}}")
            for ch in "+^%~()[]":
                esc = esc.replace(ch, "{" + ch + "}")
            self._do(self._ps(
                "Add-Type -AssemblyName System.Windows.Forms; "
                f"[System.Windows.Forms.SendKeys]::SendWait('{esc}')"))
        return {"ok": True, "chars": len(text)}

    def key(self, keys: str) -> dict:
        keys = str(keys)
        if self.system == "linux":
            # xdotool keyspec: modifiers joined by '+', e.g. ctrl+c, Return.
            argv = ["xdotool", "key", "--clearmodifiers", keys.replace(" ", "")]
        elif self.system == "darwin":
            argv = ["cliclick", f"kp:{keys.split('+')[-1].lower()}"]
        else:
            argv = self._ps(
                "Add-Type -AssemblyName System.Windows.Forms; "
                f"[System.Windows.Forms.SendKeys]::SendWait('{keys}')")
        self._do(argv)
        return {"ok": True, "keys": keys}

    def launch(self, command: str) -> dict:
        # cmdguard + human approval already cleared this upstream in perform().
        out = self._spawn(str(command))
        return out if isinstance(out, dict) else {"ok": True, "launched": True}


_actuator: Actuator = DisabledActuator()


def register_actuator(actuator: Actuator) -> None:
    """Install the OS actuator — the operator's deliberate act."""
    global _actuator
    _actuator = actuator


def reset_actuator() -> None:
    global _actuator
    _actuator = DisabledActuator()


def actuator_name() -> str:
    """Which actuator is currently installed ('disabled', 'native', ...)."""
    return getattr(_actuator, "name", "unknown")


def actuator_ready() -> tuple[bool, list[str]]:
    """(toolchain present?, missing tools) for the installed actuator. The
    disabled actuator is never ready; an actuator without a probe is assumed
    ready (a custom operator actuator vouches for itself)."""
    probe = getattr(_actuator, "missing_tools", None)
    if isinstance(_actuator, DisabledActuator):
        return (False, [])
    if probe is None:
        return (True, [])
    missing = probe()
    return (not missing, missing)


_ACTUATOR_ON = ("native", "auto", "1", "on", "true", "yes")


def activate(actuator: Actuator | None = None) -> bool:
    """Install a real actuator when — and only when — the operator has opted in.

    Two deliberate switches are required, so OS control never turns on by
    accident: `OLYMPUS_COMPUTER_USE` gates the capability, and
    `OLYMPUS_COMPUTER_USE_ACTUATOR=native` selects the native actuator. With both
    set this registers a `NativeActuator`; otherwise it leaves the fail-closed
    `DisabledActuator` in place. Returns True iff a real actuator is now live.
    Never raises — a half-provisioned box simply stays disabled — and the
    approval spine still gates every action even once an actuator is live."""
    if not enabled():
        return False
    if actuator is None:
        want = os.environ.get("OLYMPUS_COMPUTER_USE_ACTUATOR", "").strip().lower()
        if want not in _ACTUATOR_ON:
            return False
        actuator = NativeActuator()
    register_actuator(actuator)
    return True


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

# Auto-install the native actuator when the operator has set BOTH switches
# (capability + actuator). Wrapped so a construction hiccup can never break
# import; the actuator probes for tools per-action, not here, so this is cheap
# and safe. When the switches are unset, the fail-closed DisabledActuator stands.
try:
    activate()
except Exception:                                    # pragma: no cover - defensive
    pass
