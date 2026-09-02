"""Round 2 (Hermes screens 35-39): sessions, /bg, /btw, /model, /fast,
delta-setup, and the doctor paths block."""

import time

import pytest

from olympus import backend, config, doctor, firstrun, memory, orchestrator, tui


# --- named sessions & browse (#28) ------------------------------------------

def test_list_conversations_newest_first_with_preview():
    memory.save_conversation("cli-alpha", [
        {"role": "user", "content": "plan my product launch"},
        {"role": "assistant", "content": "sure"}])
    time.sleep(0.02)
    memory.save_conversation("cli-beta", [
        {"role": "user", "content": "review this contract"},
        {"role": "assistant", "content": "ok"}])
    sessions = memory.list_conversations(prefix="cli")
    ids = [s["id"] for s in sessions]
    assert ids.index("cli-beta") < ids.index("cli-alpha")   # newest first
    beta = next(s for s in sessions if s["id"] == "cli-beta")
    assert beta["turns"] == 1 and "contract" in beta["preview"]


def test_preview_prefers_distilled_state():
    memory.save_conversation("cli-distilled", [
        {"role": "user",
         "content": "[Conversation state — durable context]\nBuilding an AI "
                    "agent called Olympus; budget $50."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "ok next"},
    ])
    s = next(x for x in memory.list_conversations("cli")
             if x["id"] == "cli-distilled")
    assert "Olympus" in s["preview"]          # the state line, not "ok next"


def test_list_conversations_prefix_filter():
    memory.save_conversation("cli-x", [{"role": "user", "content": "a"}])
    memory.save_conversation("tg-user1", [{"role": "user", "content": "b"}])
    ids = {s["id"] for s in memory.list_conversations(prefix="cli")}
    assert "cli-x" in ids and "tg-user1" not in ids


def test_sessions_slash_command_lists():
    memory.save_conversation("cli-one", [{"role": "user", "content": "hello"}])
    bot = orchestrator.Olympus(user="cli")
    handled, out, _ = tui.dispatch_command(bot, "/sessions")
    assert handled and "cli-one" in out


# --- /btw ephemeral (#30) ----------------------------------------------------

def test_btw_leaves_no_trace(monkeypatch):
    bot = orchestrator.Olympus(user="btw-u", conversation_id="btw-conv")

    def fake_complete_json(_settings, _system, _messages, schema, **_kwargs):
        if schema is orchestrator.ROUTE_SCHEMA:
            return {"mode": "direct", "direct_reply": "42",
                    "specialists": [], "brief": None,
                    "clarifying_questions": [],
                    "needs_verification": False}
        assert schema is orchestrator._DIRECT_VERIFY_SCHEMA
        return {"checkable": True, "supported": True,
                "unsupported_claims": []}

    monkeypatch.setattr(orchestrator.backend, "complete_json",
                        fake_complete_json)
    before = list(bot.history)
    answer = bot.ask_ephemeral("what is 6x7?")
    assert answer == "42"
    assert bot.history == before                       # no history append
    assert memory.load_conversation("btw-conv") == []  # nothing persisted


def test_btw_via_dispatch(monkeypatch):
    bot = orchestrator.Olympus(user="btw-u2")
    monkeypatch.setattr(bot, "ask_ephemeral", lambda q: f"eph:{q}")
    handled, out, _ = tui.dispatch_command(bot, "/btw quick one")
    assert handled and out == "eph:quick one"
    handled, out, _ = tui.dispatch_command(bot, "/btw")
    assert "Usage" in out


# --- /bg background task (#29) ----------------------------------------------

def test_bg_runs_and_reports(monkeypatch, capsys):
    monkeypatch.setattr(orchestrator.Olympus, "ask",
                        lambda self, q: f"bg-answer:{q}")
    bot = orchestrator.Olympus(user="bg-u")
    handled, out, _ = tui.dispatch_command(bot, "/bg summarize the market")
    assert handled and "background" in out.lower()
    for t in tui._BG_THREADS:
        t.join(timeout=5)
    printed = capsys.readouterr().out
    assert "bg-answer:summarize the market" in printed
    memory.set_user("bg-u")
    assert "bg-answer" in memory.recent("reports")


def test_bg_usage_line():
    bot = orchestrator.Olympus(user="bg-u2")
    _, out, _ = tui.dispatch_command(bot, "/bg")
    assert "Usage" in out


# --- /model + /fast (#31) -----------------------------------------------------

def test_set_model_repoints_primary_only():
    s1 = config.Settings(provider="openai", model="deepseek-chat",
                         api_key="k1", base_url="http://a")
    s2 = config.Settings(provider="openai", model="glm-4.6",
                         api_key="k2", base_url="http://b")
    bot = orchestrator.Olympus(pool=config.ModelPool.of(s1, s2))
    msg = bot.set_model("deepseek-reasoner")
    assert "deepseek-reasoner" in msg
    assert bot.pool.primary().model == "deepseek-reasoner"
    assert bot.pool.primary().api_key == "k1"          # credential unchanged
    assert any(m.model == "glm-4.6" for m in bot.pool.members)  # member kept


def test_set_model_no_arg_shows_pool():
    bot = orchestrator.Olympus(
        settings=config.Settings(provider="openai", model="m", api_key="k",
                                 base_url="http://x"))
    out = bot.set_model("")
    assert "Usage" in out and "pool" in out.lower()


def test_fast_toggle_dispatch(monkeypatch):
    monkeypatch.delenv("OLYMPUS_FAST", raising=False)
    bot = orchestrator.Olympus(user="cli")
    _, out, _ = tui.dispatch_command(bot, "/fast on")
    assert config.fast_mode() is True and "on" in out
    _, out, _ = tui.dispatch_command(bot, "/fast off")
    assert config.fast_mode() is False
    _, out, _ = tui.dispatch_command(bot, "/fast")
    assert "Toggle" in out


# --- delta-setup (#32) --------------------------------------------------------

def test_doctor_gaps_map_to_sections(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OLYMPUS_API_KEY", "OPENAI_API_KEY",
              "OLYMPUS_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    gaps = firstrun._doctor_gaps()
    sections = {section for _c, section in gaps}
    assert "model" in sections           # missing provider → model section


def test_setup_missing_nothing_to_fix(monkeypatch, capsys):
    monkeypatch.setattr(firstrun, "_doctor_gaps", lambda: [])
    assert firstrun.setup_missing() is False
    assert "Nothing missing" in capsys.readouterr().out


def test_setup_missing_fixes_flagged_section(monkeypatch):
    gap = doctor.Check("provider", doctor.FAIL, "no key")
    monkeypatch.setattr(firstrun, "_doctor_gaps", lambda: [(gap, "model")])
    monkeypatch.setattr(firstrun, "_yes", lambda *a, **k: True)
    called = {}
    monkeypatch.setattr(firstrun, "setup_section",
                        lambda s: called.setdefault("section", s) or True)
    monkeypatch.setattr(doctor, "render", lambda **k: "(doctor)")
    assert firstrun.setup_missing() is True
    assert called["section"] == "model"


# --- paths block (#33) ---------------------------------------------------------

def test_paths_block_labels_editable_vs_managed():
    block = doctor.paths_block()
    assert "config.env" in block and "editable" in block
    assert "soul" in block and "managed" in block


def test_doctor_render_includes_paths(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert "Where stuff lives" in doctor.render()
