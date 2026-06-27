"""Guided setup wizard + the TUI welcome screen / status line."""

from olympus import firstrun, providers, tui, config


def _script(inputs):
    """Feed a sequence of answers to input()/getpass via firstrun helpers."""
    it = iter(inputs)
    return lambda *a, **k: next(it)


def test_wizard_composes_a_two_provider_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    # avoid network: model discovery returns fixed lists
    monkeypatch.setattr(providers, "fetch_models",
                        lambda prov, key="", base="": ["deepseek-chat"]
                        if prov.key == "deepseek" else ["glm-4.5-flash"])
    # answers in order:
    answers = iter([
        "4",        # choose provider: deepseek (catalog index 4)
        "1",        # pick model #1 (deepseek-chat)
        "y",        # add another?
        "5",        # choose provider: glm
        "1",        # pick model #1 (glm-4.5-flash)
        "n",        # add another? no
        "n",        # fast mode? (pool>1 auto-enables anyway)
        "n",        # sandbox? no
        "n",        # messaging? no
    ])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr(firstrun, "_ask_secret", lambda prompt: "sk-test")

    assert firstrun.wizard() is True
    saved = firstrun._read_saved()
    assert saved["OLYMPUS_PROVIDER"] == "openai"
    assert saved["OLYMPUS_MODEL"] == "deepseek-chat"
    assert "glm-4.5-flash" in saved["OLYMPUS_MODELS"]
    assert saved.get("OLYMPUS_FAST") == "1"        # multi-member → fast on


def test_wizard_cancels_with_no_members(monkeypatch, tmp_path):
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    # pick a keyed provider but enter no key → skipped; then decline another
    answers = iter(["3", "n"])    # provider openai, then "add another? no"
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr(firstrun, "_ask_secret", lambda prompt: "")  # no key
    assert firstrun.wizard() is False


def test_welcome_banner_shows_pool_and_capabilities():
    pool = config.ModelPool((config.Settings(provider="openai", model="glm-4.5-flash",
                                              api_key="k", base_url="http://x"),))
    banner = tui.welcome_banner(pool)
    assert "OLYMPUS" in banner.replace(" ", "")
    assert "specialists" in banner and "tools" in banner and "commands" in banner
    assert "glm-4.5-flash" in banner


def test_status_line_formats():
    s = tui.status_line("openai/glm-4.5-flash", 2.3, fast=True, spend=0.0123)
    assert "glm-4.5-flash" in s and "2.3s" in s and "fast" in s and "$0.0123" in s
