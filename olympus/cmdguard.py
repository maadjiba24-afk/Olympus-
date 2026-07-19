"""Command security gate — a pre-execution classifier for shell commands.

This is the shell analogue of Aletheia (the hallucination controller): before a
command runs in the sandbox, it is *classified*, and catastrophic commands are
refused outright — even if a human (or an auto-approve policy) already said yes.
Trust is Olympus's whole pitch, and "a human clicked approve" is not the same as
"this command is safe to run on the host".

Why this exists on top of the Action spine:
  * `run_command` is already approval-gated, but some deployments auto-approve
    the `exec` scope, and the heartbeat/scheduler can run commands unattended.
    The gate protects *every* path into `sandbox.run()`, not just the
    interactive approval prompt.
  * A `rm -rf /` typed into an approval prompt should never run. The gate makes
    "never" mean never, regardless of who approved it.

Design rules:
  * **Deterministic and dependency-free.** Pure pattern matching over a tokenized
    command — always available, no model call, no network. (Hermes's scanner
    falls back to "pattern matching only" when its engine is down; ours *is* the
    always-on baseline, so it can never silently fail open.)
  * **Fail-closed.** The default mode is `enforce`: a DENY verdict blocks
    execution. An unrecognized value for OLYMPUS_EXEC_SECURITY falls back to
    `enforce`, never to `off` — you cannot accidentally disable the gate with a
    typo.

Modes (OLYMPUS_EXEC_SECURITY):
    enforce   (default) DENY-level commands are blocked; CONFIRM/SAFE run.
    paranoid  DENY *and* CONFIRM commands are blocked.
    audit     nothing is blocked, but the verdict is recorded in the result.
    off       gate disabled (not recommended; kept for trusted CI shells).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

# Verdict levels, ordered by severity.
SAFE = "safe"
CONFIRM = "confirm"
DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    level: str          # SAFE | CONFIRM | DENY
    reason: str         # human-readable explanation
    rule: str           # short rule id, for logging/telemetry

    @property
    def blocked_by(self) -> bool:
        return self.level == DENY

    def render(self) -> str:
        icon = {SAFE: "🟢", CONFIRM: "🟡", DENY: "🔴"}.get(self.level, "•")
        return f"{icon} {self.level.upper()}: {self.reason}"


def mode() -> str:
    m = os.environ.get("OLYMPUS_EXEC_SECURITY", "enforce").strip().lower()
    # Fail-closed on a typo: anything we don't recognize means "enforce", the
    # safe default — never "off".
    return m if m in ("enforce", "paranoid", "audit", "off") else "enforce"


# System paths whose recursive deletion / overwrite is catastrophic on the host.
_SYSTEM_TARGETS = {
    "/", "/*", "~", "~/", "$HOME", "${HOME}", ".", "..", "*",
    "/etc", "/usr", "/var", "/bin", "/sbin", "/boot", "/lib", "/lib64",
    "/sys", "/proc", "/dev", "/root", "/home", "/opt", "/srv",
}

# Regex rules that don't need tokenization — matched against the raw command.
# Each: (compiled_pattern, level, reason, rule_id).
_RAW_RULES: list[tuple[re.Pattern[str], str, str, str]] = [
    # Fork bombs — a shell function that recursively backgrounds itself.
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;?\s*:"),
     DENY, "fork bomb — will exhaust process table and hang the host",
     "fork-bomb"),
    (re.compile(r"\(\s*\)\s*\{[^}]*\|[^}]*&[^}]*\}\s*;"),
     DENY, "looks like a fork bomb (self-piping backgrounded function)",
     "fork-bomb-generic"),
    # Disk / filesystem destruction.
    (re.compile(r"\bmkfs\b|\bwipefs\b|\bfdisk\b|\bparted\b|\bblkdiscard\b"),
     DENY, "formats or repartitions a disk — destroys data irrecoverably",
     "mkfs"),
    (re.compile(r"\bdd\b[^\n]*\bof=\s*/dev/(sd|nvme|vd|hd|mapper|disk)"),
     DENY, "dd writing directly to a block device — wipes the disk",
     "dd-device"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd)[a-z0-9]*\b"),
     DENY, "redirecting output onto a raw disk device", "redirect-device"),
    # Refusing --no-preserve-root is the whole point of preserve-root.
    (re.compile(r"--no-preserve-root"),
     DENY, "--no-preserve-root defeats the guard against deleting /",
     "no-preserve-root"),
    # Host power state — the sandbox must not take the machine down.
    (re.compile(r"\b(shutdown|reboot|poweroff|halt|init)\b(\s+-?\w+)*"),
     DENY, "powers off or reboots the host", "power"),
    # Overwriting critical system files.
    (re.compile(r">\s*/etc/(passwd|shadow|sudoers|fstab|hosts)\b"),
     DENY, "overwrites a critical system file", "overwrite-etc"),
    (re.compile(r"\bchmod\b\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)?0*00?0?\s+/(\s|$)"),
     DENY, "chmod 000 on / — locks everyone out of the whole filesystem",
     "chmod-root"),
    # Piping a remote script straight into a shell — the classic supply-chain
    # foothold. Legitimate sometimes, so CONFIRM (a human must eyeball it).
    (re.compile(r"\b(curl|wget|fetch)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b"),
     CONFIRM, "pipes a downloaded script straight into a shell — review the URL",
     "curl-pipe-sh"),
    (re.compile(r"\bcurl\b[^\n|]*\|\s*(sudo\s+)?python[0-9.]*\b"),
     CONFIRM, "pipes a downloaded script into an interpreter — review the URL",
     "curl-pipe-python"),
    # Privilege escalation — allowed, but a human should see it.
    (re.compile(r"\bsudo\b|\bdoas\b|\bsu\s+-"),
     CONFIRM, "runs with elevated privileges", "sudo"),
    # History / credential exfiltration hints.
    (re.compile(r"\b(nc|ncat|netcat)\b[^\n]*(?:^|\s)-e(?:\s|$)"),
     CONFIRM, "netcat with -e — can open a reverse shell", "nc-shell"),
    # Wrapper-proof rm-rf-on-root: `bash -c "rm -rf /"`, `sh -c 'rm -fr /*'`…
    # hide the rm from tokenized analysis (the executable is bash), so also
    # catch the pattern in the raw string: rm + a recursive flag + a bare
    # system-root target (/ or /* followed by space, quote, or end).
    # DELIBERATE fail-closed trade-off: these raw rules see inside quotes (they
    # must — that's what makes them wrapper-proof), so a command that merely
    # *mentions* the pattern in a string (`grep 'rm -rf /' docs`) is also
    # denied. Printing a catastrophic command is rare; running one is fatal.
    (re.compile(r"\brm\b[^\n]*\s-[a-zA-Z]*[rR][a-zA-Z]*\s+/(?:\*|\s|['\"]|$)"),
     DENY, "recursive delete of the filesystem root", "rm-rf-root-raw"),
    # `find / -delete` (or -exec rm) walks the whole filesystem destructively.
    (re.compile(r"\bfind\s+/\s[^\n]*(-delete\b|-exec\s+rm\b)"),
     DENY, "find over the filesystem root with -delete/-exec rm", "find-root-delete"),
    # shred directly against a block device — same class as dd of=/dev/….
    (re.compile(r"\bshred\b[^\n]*\s/dev/(sd|nvme|vd|hd|mapper|disk)"),
     DENY, "shred against a raw block device — wipes the disk", "shred-device"),
]

# Command wrappers that precede the real command (`sudo rm …`, `env X=1 rm …`).
# Stripped before we identify the executable, so a wrapper can't hide an `rm`.
_WRAPPERS = frozenset({
    "sudo", "doas", "nice", "ionice", "time", "nohup", "env", "stdbuf",
    "setsid", "eatmydata", "command", "builtin", "exec",
})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop leading wrapper commands, their options, and VAR=val assignments so
    the first remaining token is the real executable."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if os.path.basename(tok) in _WRAPPERS or _ASSIGN_RE.match(tok):
            i += 1
            # `sudo -u user` etc. — skip an option and its argument heuristically.
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            continue
        break
    return tokens[i:]


def _iter_rm_targets(tokens: list[str]) -> tuple[bool, bool, list[str]]:
    """Parse an `rm` invocation: (is_rm, has_recursive_force, targets)."""
    tokens = _strip_wrappers(tokens)
    if not tokens or os.path.basename(tokens[0]) != "rm":
        return False, False, []
    recursive = force = False
    targets: list[str] = []
    for tok in tokens[1:]:
        if tok.startswith("-") and not tok.startswith("--"):
            recursive = recursive or ("r" in tok or "R" in tok)
            force = force or ("f" in tok)
        elif tok in ("--recursive",):
            recursive = True
        elif tok in ("--force",):
            force = True
        elif tok.startswith("--"):
            continue
        else:
            targets.append(tok)
    return True, (recursive and force) or recursive, targets


def _scan_one(command: str) -> Verdict:
    """Classify a single (already split) command segment."""
    cmd = command.strip()
    if not cmd:
        return Verdict(SAFE, "empty command", "empty")

    # Raw-pattern rules first — they catch shell-syntax attacks (fork bombs,
    # redirects) that tokenization would obscure. Most severe wins.
    worst: Verdict | None = None
    for pat, level, reason, rule in _RAW_RULES:
        if pat.search(cmd):
            v = Verdict(level, reason, rule)
            if worst is None or _severity(level) > _severity(worst.level):
                worst = v

    # Tokenized `rm` analysis — deny recursive removal of a system path/home.
    try:
        tokens = shlex.split(cmd, comments=True)
    except ValueError:
        tokens = cmd.split()
    is_rm, recursive, targets = _iter_rm_targets(tokens)
    if is_rm and recursive:
        for t in targets:
            norm = t.rstrip("/") or "/"
            if t in _SYSTEM_TARGETS or norm in _SYSTEM_TARGETS or t.startswith("/"):
                v = Verdict(DENY,
                            f"recursive delete of a system path ({t})",
                            "rm-rf-system")
                if worst is None or _severity(DENY) > _severity(worst.level):
                    worst = v
                break

    return worst or Verdict(SAFE, "no dangerous pattern matched", "ok")


def _severity(level: str) -> int:
    return {SAFE: 0, CONFIRM: 1, DENY: 2}.get(level, 0)


# Split on shell separators so `safe && rm -rf /` is judged on its worst part.
_SEPARATORS = re.compile(r"&&|\|\||;|\n|\|")


def scan(command: str) -> Verdict:
    """Classify a shell command, returning the most severe verdict across all
    of its `&&` / `;` / `|`-separated segments."""
    command = command or ""
    worst = Verdict(SAFE, "no dangerous pattern matched", "ok")
    for seg in _SEPARATORS.split(command):
        v = _scan_one(seg)
        if _severity(v.level) > _severity(worst.level):
            worst = v
    # Whole-command raw rules (a pipe like `curl … | sh` spans two segments).
    whole = _scan_one(command.replace("\n", " "))
    if _severity(whole.level) > _severity(worst.level):
        worst = whole
    return worst


def check(command: str) -> tuple[bool, Verdict]:
    """Decide whether a command may run under the current mode.

    Returns (allowed, verdict). `allowed` is False only when the mode enforces a
    block for this verdict's severity. In `audit` and `off` modes everything is
    allowed but the verdict is still returned so callers can surface it.
    """
    verdict = scan(command)
    m = mode()
    if m == "off":
        return True, verdict
    if m == "audit":
        return True, verdict
    if verdict.level == DENY:
        return False, verdict
    if m == "paranoid" and verdict.level == CONFIRM:
        return False, verdict
    return True, verdict
