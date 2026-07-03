"""Tier E: soul.md personality injection, cron-attached skills, starter pack."""

from olympus import scheduler, skillpack, skills, soul


# --- soul.md --------------------------------------------------------------

def test_soul_block_empty_without_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path))
    assert soul.read() == ""
    assert soul.block() == ""


def test_soul_block_injects_when_present(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path))
    (tmp_path / "soul.md").write_text("# Soul\nBe terse. Never pad answers.")
    blk = soul.block()
    assert "Be terse" in blk and "personal directive" in blk


def test_soul_is_size_capped(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path))
    (tmp_path / "soul.md").write_text("x" * 10_000)
    assert len(soul.read()) <= 4000


def test_soul_scaffold_creates_template(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path))
    p = soul.ensure_scaffold()
    assert p.exists() and "Soul" in p.read_text()


def test_orchestrator_injects_soul_into_route(monkeypatch, tmp_path):
    from olympus import orchestrator
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path))
    (tmp_path / "soul.md").write_text("SIGNATURE_SOUL_MARKER")
    seen = {}

    def fake_complete_json(settings, system, messages, schema, **k):
        seen["system"] = system
        return {"mode": "direct", "direct_reply": "hi", "specialists": [],
                "brief": None, "clarifying_questions": [],
                "needs_verification": False}

    monkeypatch.setattr(orchestrator.backend, "complete_json", fake_complete_json)
    bot = orchestrator.Olympus(user="soul-u")
    bot.ask("hello")
    assert "SIGNATURE_SOUL_MARKER" in seen["system"]


# --- cron-attached skills -------------------------------------------------

def test_job_carries_skill(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "_path", lambda: tmp_path / "jobs.json")
    job = scheduler.add("wk", "weekly", "make the report", skill="weekly-report")
    assert job.skill == "weekly-report"
    assert scheduler.jobs()[0].skill == "weekly-report"


def test_effective_prompt_prepends_skill(monkeypatch, tmp_path):
    monkeypatch.setattr(skills, "read",
                        lambda name: "STEP 1: gather. STEP 2: summarize.")
    job = scheduler.Job(name="j", interval=3600, prompt="do the weekly report",
                        skill="weekly-report")
    eff = scheduler._effective_prompt(job)
    assert "weekly-report" in eff and "STEP 1: gather" in eff
    assert "do the weekly report" in eff


def test_effective_prompt_plain_without_skill():
    job = scheduler.Job(name="j", interval=3600, prompt="just do it")
    assert scheduler._effective_prompt(job) == "just do it"


def test_run_due_uses_attached_skill(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(skills, "read", lambda name: "SKILL_BODY_XYZ")
    scheduler.add("j", 1, "the task", skill="my-skill", now=0.0)
    captured = {}

    def runner(prompt, user):
        captured["prompt"] = prompt
        return "done"

    scheduler.run_due(now=10_000, runner=runner)
    assert "SKILL_BODY_XYZ" in captured["prompt"]
    assert "the task" in captured["prompt"]


# --- starter pack ---------------------------------------------------------

def test_starter_pack_installs_provisional(monkeypatch, tmp_path):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    msgs = skillpack.install_starter_pack()
    assert len(msgs) == len(skillpack.STARTER_PACK)
    assert all("provisional" in m for m in msgs)
    # the skills are readable afterwards
    body = skills.read("weekly-report")
    assert "Shipped" in body or "report" in body.lower()
