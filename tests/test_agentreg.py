"""Dynamic agent registry (file-defined agents) + Claude-Code hook parity."""

import pytest

from olympus import agentreg, connectors, security, specialists


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setenv("OLYMPUS_AGENTS_DIR", str(d))
    return d


def _write(d, name, text):
    (d / name).write_text(text, encoding="utf-8")


# --- gating --------------------------------------------------------------

def test_disabled_by_default():
    assert agentreg.enabled() is False


def test_enabled_via_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_AGENTS", "1")
    assert agentreg.enabled() is True


# --- parsing + build -----------------------------------------------------

def test_loads_file_agent(agents_dir):
    _write(agents_dir, "translator.md",
           "---\nname: Babel\ntitle: Translation Specialist\n"
           "description: Translates text between languages.\n"
           "role: reasoning\neffort: medium\nweb: true\n---\n"
           "You are a careful translator. Preserve meaning and tone.")
    loaded = agentreg.load(static_keys=set())
    assert "translator" in loaded
    spec = loaded["translator"]
    assert spec.name == "Babel" and spec.title == "Translation Specialist"
    assert spec.web is True and spec.role == "reasoning"
    assert "careful translator" in spec.prompt_text


def test_prompt_text_feeds_system_prompt(agents_dir, monkeypatch):
    _write(agents_dir, "poet.md", "---\nname: Poet\n---\nWrite only in haiku.")
    spec = agentreg.load(static_keys=set())["poet"]
    # system_prompt uses the inline prompt (no prompts/poet.md file exists)
    assert "Write only in haiku." in spec.system_prompt()


# --- safety enforcement --------------------------------------------------

def test_file_agent_is_never_privileged(agents_dir):
    _write(agents_dir, "sneaky.md",
           "---\nname: Sneaky\ntools: search_documents, send_email, "
           "update_prompt\n---\nBody.")
    spec = agentreg.load(static_keys=set())["sneaky"]
    assert spec.system is False and spec.code_exec is False
    # action tools stripped; only the data tool survives
    assert "search_documents" in spec.extra_tools
    assert "send_email" not in spec.extra_tools
    assert "update_prompt" not in spec.extra_tools
    assert not (set(spec.extra_tools) & set(security.ACTION_TOOLS))


def test_cannot_shadow_builtin(agents_dir):
    _write(agents_dir, "plutus.md", "---\nname: FakePlutus\n---\nImpostor.")
    loaded = agentreg.load(static_keys={"plutus"})
    assert "plutus" not in loaded          # built-in key is protected


def test_empty_or_bodyless_skipped(agents_dir):
    _write(agents_dir, "empty.md", "---\nname: X\n---\n")
    _write(agents_dir, "blank.md", "   ")
    assert agentreg.load(static_keys=set()) == {}


def test_deterministic_load(agents_dir):
    _write(agents_dir, "a.md", "---\nname: A\n---\nAlpha.")
    _write(agents_dir, "b.md", "---\nname: B\n---\nBeta.")
    assert list(agentreg.load(set())) == list(agentreg.load(set()))


# --- install / uninstall into the live registry --------------------------

def test_install_merges_and_uninstall_reverses(agents_dir, monkeypatch):
    monkeypatch.setenv("OLYMPUS_AGENTS", "1")
    _write(agents_dir, "scribe.md",
           "---\nname: Scribe\ntitle: Note Taker\ndescription: Takes notes.\n"
           "---\nYou take excellent notes.")
    registry = dict(specialists.SPECIALISTS)   # a copy to mutate safely
    before = set(registry)
    added = agentreg.install(registry)
    assert added == ["scribe"]
    assert "scribe" in registry
    assert agentreg.installed_keys() == ["scribe"]
    agentreg.uninstall(registry)
    assert set(registry) == before


def test_install_noop_when_disabled(agents_dir, monkeypatch):
    monkeypatch.delenv("OLYMPUS_AGENTS", raising=False)
    _write(agents_dir, "x.md", "---\nname: X\n---\nBody.")
    registry = dict(specialists.SPECIALISTS)
    assert agentreg.install(registry) == []


def test_installed_agent_appears_in_roster(agents_dir, monkeypatch):
    monkeypatch.setenv("OLYMPUS_AGENTS", "1")
    _write(agents_dir, "guide.md",
           "---\nname: Guide\ntitle: Tour Guide\ndescription: Gives tours.\n"
           "---\nBe a helpful guide.")
    try:
        agentreg.install(specialists.SPECIALISTS)
        assert "guide: Guide" in specialists.roster()
    finally:
        agentreg.uninstall(specialists.SPECIALISTS)


# --- hook parity (session_end + pre_compact added natively) --------------

def test_new_hook_events_registered():
    for ev in ("session_end", "pre_compact"):
        assert ev in connectors.HOOK_EVENTS


def test_pre_compact_hook_fires():
    connectors.clear_hooks()
    seen = []

    @connectors.hook("pre_compact")
    def _snap(user, text):
        seen.append((user, text))

    connectors.emit("pre_compact", "alice", "some history")
    assert seen == [("alice", "some history")]
    connectors.clear_hooks()


def test_unknown_hook_still_rejected():
    with pytest.raises(ValueError):
        connectors.hook("not_a_real_event")
