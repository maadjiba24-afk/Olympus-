"""SecretRef indirection: env:/file:/vault:/keychain: references resolve at
the credential choke points; plain values pass through untouched."""

import json

import pytest

from olympus import config, secretref


def test_plain_values_pass_through():
    assert secretref.resolve("sk-plain-key") == "sk-plain-key"
    assert secretref.resolve("") == ""
    assert secretref.resolve(None) == ""
    assert secretref.is_ref("sk-plain") is False


def test_env_ref(monkeypatch):
    monkeypatch.setenv("MY_REAL_KEY", "sk-from-env")
    assert secretref.resolve("env:MY_REAL_KEY") == "sk-from-env"
    assert secretref.resolve("env:MISSING_VAR") == ""


def test_file_ref(tmp_path):
    secret_file = tmp_path / "token"
    secret_file.write_text("sk-from-file\n", encoding="utf-8")
    assert secretref.resolve(f"file:{secret_file}") == "sk-from-file"
    assert secretref.resolve(f"file:{tmp_path}/missing") == ""


@pytest.mark.requires_crypto
def test_vault_ref_roundtrip(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "test-passphrase")
    ref = secretref.store("openai-main", "sk-from-vault")
    assert ref == "vault:openai-main"
    assert secretref.resolve(ref) == "sk-from-vault"


def test_vault_ref_without_key_fails_closed(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    assert secretref.resolve("vault:anything") == ""


def test_keychain_ref_invokes_platform_tool(monkeypatch):
    calls = {}

    class _Out:
        returncode = 0
        stdout = "sk-from-keychain\n"

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _Out()

    monkeypatch.setattr(secretref.subprocess, "run", fake_run)
    assert secretref.resolve("keychain:olympus/api") == "sk-from-keychain"
    assert "olympus" in " ".join(calls["cmd"])


def test_settings_from_env_resolves_refs(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PROVIDER", "anthropic")
    monkeypatch.setenv("REAL_ANTHROPIC", "sk-ant-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env:REAL_ANTHROPIC")
    s = config.Settings.from_env()
    assert s.api_key == "sk-ant-real"


def test_pool_member_refs_resolve(monkeypatch, tmp_path):
    key_file = tmp_path / "k2"
    key_file.write_text("sk-member-two\n", encoding="utf-8")
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "gpt-5")
    monkeypatch.setenv("OLYMPUS_API_KEY", "k1")
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps([
        {"provider": "openai", "model": "deepseek-v3",
         "api_key": f"file:{key_file}",
         "api_keys": ["env:ROTATE_A"]},
    ]))
    monkeypatch.setenv("OLYMPUS_ROTATE_PLACEHOLDER", "x")  # keep env tidy
    monkeypatch.setenv("ROTATE_A", "sk-rotate-a")
    pool = config.ModelPool.from_env()
    member = next(m for m in pool.members if m.model == "deepseek-v3")
    assert member.api_key == "sk-member-two"
    assert "sk-rotate-a" in member.api_keys


def test_unresolvable_member_ref_yields_unusable_not_crash(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PROVIDER", "openai")
    monkeypatch.setenv("OLYMPUS_MODEL", "gpt-5")
    monkeypatch.setenv("OLYMPUS_API_KEY", "k1")
    monkeypatch.setenv("OLYMPUS_MODELS", json.dumps([
        {"provider": "openai", "model": "deepseek-v3",
         "api_key": "vault:not-there"},
    ]))
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    pool = config.ModelPool.from_env()          # must not raise
    assert all(m.model != "deepseek-v3" or not m.api_key
               for m in pool.members)


def test_telegram_token_accepts_ref(monkeypatch, tmp_path):
    token_file = tmp_path / "tg"
    token_file.write_text("123:ABC\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"file:{token_file}")
    from olympus import telegram
    assert telegram._token() == "123:ABC"


@pytest.mark.requires_crypto
def test_secret_cli_ls_without_key_fails_gracefully(monkeypatch, capsys):
    # A vault that exists but can't be opened (no OLYMPUS_SECRET_KEY) must
    # explain itself, not traceback.
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    from olympus import cli, secretref
    secretref.store("cli-audit", "sk-x")
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    rc = cli.main(["secret", "ls"])
    out = capsys.readouterr().out
    assert rc == 1 and "OLYMPUS_SECRET_KEY" in out
    rc = cli.main(["secret", "rm", "cli-audit"])
    out = capsys.readouterr().out
    assert rc == 1 and "OLYMPUS_SECRET_KEY" in out


@pytest.mark.requires_crypto
def test_secret_cli_roundtrip_with_key(monkeypatch, capsys):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "k")
    from olympus import cli, secretref
    secretref.store("cli-audit2", "sk-y")
    rc = cli.main(["secret", "ls"])
    out = capsys.readouterr().out
    assert rc in (0, None) and "cli-audit2" in out
    rc = cli.main(["secret", "rm", "cli-audit2"])
    assert rc in (0, None)
