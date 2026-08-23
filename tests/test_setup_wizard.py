"""Guided setup wizard + the TUI welcome screen / status line."""

from olympus import firstrun, providers, tui, config


def _script(inputs):
    """Feed a sequence of answers to input()/getpass via firstrun helpers."""
    it = iter(inputs)
    return lambda *a, **k: next(it)


def test_wizard_composes_a_two_provider_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(firstrun, "_env_detected", lambda: [])  # isolate env-scan
    # avoid network: model discovery returns fixed lists
    monkeypatch.setattr(providers, "fetch_models",
                        lambda prov, key="", base="": ["deepseek-chat"]
                        if prov.key == "deepseek" else ["glm-4.5-flash"])
    # Picker order (Anthropic's two rows merged into one): 1 Anthropic, 2 OpenAI,
    # 3 DeepSeek, 4 GLM, 5 Kimi, ...
    answers = iter([
        "2",        # setup style: Full
        "3",        # choose provider: DeepSeek
        "1",        # pick model #1 (deepseek-chat)
        "y",        # add another?
        "4",        # choose provider: GLM
        "1",        # pick model #1 (glm-4.5-flash)
        "n",        # add another? no
        "n",        # sandbox? no (fast auto-on for a pool)
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


def test_quick_setup_one_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(firstrun, "_env_detected", lambda: [])
    monkeypatch.setattr(providers, "fetch_models",
                        lambda prov, key="", base="": ["deepseek-chat"])
    answers = iter([
        "1",        # setup style: Quick
        "3",        # provider: DeepSeek
        "1",        # model #1
        "n",        # add another? no
    ])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr(firstrun, "_ask_secret", lambda prompt: "sk-test")
    assert firstrun.wizard() is True
    saved = firstrun._read_saved()
    assert saved["OLYMPUS_MODEL"] == "deepseek-chat"
    # Quick applies recommended defaults with no extra questions.
    assert saved["OLYMPUS_FAST"] == "1"
    assert saved["OLYMPUS_EXEC_SECURITY"] == "enforce"


def test_wizard_cancels_with_no_members(monkeypatch, tmp_path):
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(firstrun, "_env_detected", lambda: [])
    # pick a keyed provider but enter no key → skipped; then decline another
    answers = iter(["2", "2", "n"])  # Full, provider OpenAI, "add another? no"
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr(firstrun, "_ask_secret", lambda prompt: "")  # no key
    assert firstrun.wizard() is False


def test_env_detection_offers_found_key(monkeypatch, tmp_path):
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(providers, "fetch_models",
                        lambda prov, key="", base="": ["deepseek-chat"])
    # A DeepSeek key is already in the environment → offered without pasting.
    dsp = providers.get("deepseek")
    monkeypatch.setattr(firstrun, "_env_detected", lambda: [(dsp, "sk-env")])
    answers = iter([
        "2",        # Full
        "y",        # use the detected DeepSeek key?
        "1",        # pick model #1
        "n",        # add another? no
        "n",        # fast mode? (single member → asked)
        "n",        # sandbox? no
        "n",        # messaging? no
    ])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    assert firstrun.wizard() is True
    saved = firstrun._read_saved()
    assert saved["OLYMPUS_MODEL"] == "deepseek-chat"


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


def test_status_line_shows_context_fraction():
    s = tui.status_line("anthropic/claude", 1.0, ctx_frac=0.42)
    assert "ctx 42%" in s


def test_key_providers_have_key_url():
    # Every API-key provider should tell users where to get a key (#7).
    for p in providers.CATALOG:
        if p.auth == "api_key" and p.key != "custom":
            assert p.key_url, f"{p.key} missing key_url"


def test_picker_merges_anthropic_rows():
    # The two Anthropic rows fold into a single 'Anthropic (Claude)' entry (#10).
    entries = firstrun._picker_entries()
    labels = [e[0] for e in entries]
    assert labels[0] == "Anthropic (Claude)"
    assert sum("Claude" in lbl or "Anthropic" in lbl for lbl in labels) == 1
    # OpenAI is second now (indices shifted by the merge).
    assert entries[1][2]().key == "openai"


def test_env_detected_finds_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-detected")
    found = {p.key for p, _k in firstrun._env_detected()}
    assert "deepseek" in found


# --- round-2 micro-items: model filter (#34), gateway checklist (#35) --------

def test_filter_models_substring_case_insensitive():
    models = ["openai/gpt-4o", "anthropic/claude-sonnet-4", "meta/llama-3-70b"]
    assert firstrun._filter_models(models, "CLAUDE") == [
        "anthropic/claude-sonnet-4"]
    assert firstrun._filter_models(models, "") == models


def test_pick_model_filters_large_lists(monkeypatch):
    # 40 discovered models → the wizard asks for a filter before listing.
    many = [f"vendor/model-{i:02d}" for i in range(38)] + [
        "anthropic/claude-x", "openai/gpt-9"]
    monkeypatch.setattr(providers, "fetch_models", lambda *a, **k: many)
    answers = iter([
        "claude",   # filter query
        "1",        # pick the single hit
    ])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    prov = providers.get("openrouter")
    assert firstrun._pick_model(prov, "k", "") == "anthropic/claude-x"


def test_pick_model_blank_filter_lists_first_30(monkeypatch):
    many = sorted(f"m-{i:03d}" for i in range(50))
    monkeypatch.setattr(providers, "fetch_models", lambda *a, **k: many)
    answers = iter([
        "",         # blank filter → first 30
        "2",        # pick #2
    ])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    assert firstrun._pick_model(providers.get("openrouter"), "k", "") == "m-001"


def test_gateway_checklist_status_and_multiselect(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")   # telegram: configured
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    values: dict = {}
    names = list(firstrun._GATEWAYS)
    tg, wh = names.index("telegram") + 1, names.index("webhook") + 1
    answers = iter([f"{wh}", "lab-owner"])             # configure webhook only
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    monkeypatch.setattr(firstrun, "_ask_secret", lambda *_: "s3cret")
    firstrun._configure_gateway(values)
    out = capsys.readouterr().out
    assert "[x] Telegram" in out and "(configured)" in out
    assert "(not configured)" in out                   # the untouched ones
    assert values["OLYMPUS_WEBHOOK_SECRET"] == "s3cret"
    assert values["OLYMPUS_WEBHOOK_USER"] == "lab-owner"


def test_gateway_checklist_blank_configures_nothing(monkeypatch):
    values: dict = {}
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    firstrun._configure_gateway(values)
    assert values == {}
