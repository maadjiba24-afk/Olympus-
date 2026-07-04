"""SecretRef — credentials referenced by name, resolved at runtime.

A config value that would normally BE a secret can instead SAY WHERE the
secret lives. Anywhere Olympus reads a credential (provider keys, pool
members, channel tokens) it accepts:

    env:NAME              another environment variable
    file:/path            first line of a file (docker/systemd secrets)
    vault:NAME            Olympus's encrypted vault (operator namespace)
    keychain:SVC[/ACCT]   the OS keychain (macOS `security`, Linux
                          `secret-tool`), best-effort

so `OLYMPUS_MODELS='[{"model": "gpt-5", "api_key": "vault:openai-main"}]'`
never carries the key itself, and a config file/env dump can be shared
without leaking anything. Plain values keep working unchanged.

Resolution fails CLOSED: a reference that can't be resolved yields an empty
string (an unusable credential), never a partial secret or a crash on the
request path.
"""

from __future__ import annotations

import os
import subprocess

PREFIXES = ("env:", "file:", "vault:", "keychain:")

_VAULT_USER = "operator"          # matches opconfig's vault namespace owner


def is_ref(value) -> bool:
    return isinstance(value, str) and value.strip().startswith(PREFIXES)


def resolve(value, default: str = "") -> str:
    """Resolve `value` if it is a reference; return it unchanged otherwise."""
    if value is None:
        return default
    if not is_ref(value):
        return value
    ref = value.strip()
    kind, _, rest = ref.partition(":")
    rest = rest.strip()
    try:
        if kind == "env":
            return (os.environ.get(rest) or default).strip() or default
        if kind == "file":
            with open(os.path.expanduser(rest), encoding="utf-8") as fh:
                return fh.readline().strip() or default
        if kind == "vault":
            from . import vault
            return (vault.get(_VAULT_USER, f"secretref:{rest}")
                    or default)
        if kind == "keychain":
            return _keychain(rest) or default
    except Exception:
        return default
    return default


def getenv(name: str, default: str = "") -> str:
    """os.environ.get with SecretRef resolution — the drop-in for reading a
    credential-bearing environment variable."""
    return resolve(os.environ.get(name, default), default)


def store(name: str, secret: str) -> str:
    """Store `secret` in the vault under a SecretRef name; returns the
    reference string to put in config. Requires OLYMPUS_SECRET_KEY."""
    from . import vault
    vault.put(_VAULT_USER, f"secretref:{name}", secret)
    return f"vault:{name}"


def _keychain(spec: str) -> str:
    """Read from the OS keychain. `spec` is 'service' or 'service/account'."""
    service, _, account = spec.partition("/")
    service, account = service.strip(), account.strip()
    if not service:
        return ""
    import platform
    if platform.system() == "Darwin":
        cmd = ["security", "find-generic-password", "-s", service, "-w"]
        if account:
            cmd[3:3] = ["-a", account]
    else:
        cmd = ["secret-tool", "lookup", "service", service]
        if account:
            cmd += ["account", account]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return out.stdout.strip() if out.returncode == 0 else ""
