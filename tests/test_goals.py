"""Standing goals with completion contracts (Hermes v0.13 /goal +
v0.18 evidence-judged completion): the work/judge loop closes a goal only
on evidence, never on assertion."""

from olympus import gateway, goals, tui


def _judge(done=False, confidence=0.0, evidence="", missing="more work"):
    return lambda system, messages, schema: {
        "done": done, "confidence": confidence,
        "evidence": evidence, "missing": missing}


def test_add_and_list():
    out = goals.add("alice", "get the newsletter signup flow live",
                    "the signup form accepts a real address end to end")
    assert "Goal" in out and "Done means" in out
    assert len(goals.active("alice")) == 1
    assert "newsletter" in goals.summary("alice")


def test_add_requires_text_and_caps_active():
    assert "Usage" in goals.add("alice", "  ")
    for i in range(goals.MAX_ACTIVE):
        goals.add("alice", f"goal {i}")
    assert "already" in goals.add("alice", "one too many")


def test_default_contract_applied():
    goals.add("alice", "learn Rust basics")
    g = goals.active("alice")[0]
    assert g.contract == goals.DEFAULT_CONTRACT


def test_work_cycle_logs_progress_and_stays_open():
    goals.add("alice", "ship the report")
    g = goals.active("alice")[0]
    line = goals.work_one(g, runner=lambda u, p: "drafted section 1",
                          judge_fn=_judge(missing="sections 2-3"))
    assert "not done yet" in line
    fresh = goals.get(g.id, user="alice")
    assert fresh.status == "active"
    assert fresh.progress and "drafted section 1" in fresh.progress[-1]["note"]


def test_goal_closes_only_on_confident_evidence():
    goals.add("alice", "ship the report")
    g = goals.active("alice")[0]
    # The model CLAIMS done but the judge finds no evidence — stays open.
    line = goals.work_one(g, runner=lambda u, p: "I did it, all done!",
                          judge_fn=_judge(done=True, confidence=0.4))
    assert "not done yet" in line
    assert goals.get(g.id, user="alice").status == "active"
    # Concrete evidence over the floor — closes.
    line = goals.work_one(
        g, runner=lambda u, p: "report.pdf sent to bob@x.com at 09:12",
        judge_fn=_judge(done=True, confidence=0.95,
                        evidence="report.pdf delivered, receipt logged"))
    assert "COMPLETE" in line
    assert goals.get(g.id, user="alice").status == "done"
    assert "report.pdf" in goals.get(g.id, user="alice").evidence


def test_goal_stalls_after_max_checks(monkeypatch):
    monkeypatch.setattr(goals, "MAX_CHECKS", 2)
    goals.add("alice", "unreachable goal")
    g = goals.active("alice")[0]
    goals.work_one(g, runner=lambda u, p: "tried",
                   judge_fn=_judge(missing="the impossible"))
    line = goals.work_one(g, runner=lambda u, p: "tried again",
                          judge_fn=_judge(missing="the impossible"))
    assert "STALLED" in line
    assert goals.get(g.id, user="alice").status == "stalled"


def test_run_due_works_new_goals_immediately_then_respects_cadence(monkeypatch):
    monkeypatch.setenv("OLYMPUS_GOALS_EVERY", "3600")
    goals.add("alice", "goal A")
    ran = goals.run_due(runner=lambda u, p: "worked",
                        judge_fn=_judge())
    assert len(ran) == 1                       # fresh goal → immediate cycle
    ran = goals.run_due(runner=lambda u, p: "worked",
                        judge_fn=_judge())
    assert ran == []                            # within cadence → nothing due


def test_run_due_disabled():
    goals.add("alice", "goal A")
    import os
    os.environ["OLYMPUS_GOALS_EVERY"] = "0"
    try:
        assert goals.run_due() == []
        assert goals.next_due_in() is None
    finally:
        del os.environ["OLYMPUS_GOALS_EVERY"]


def test_judge_failure_fails_closed():
    goals.add("alice", "goal A")
    g = goals.active("alice")[0]

    def broken(system, messages, schema):
        raise RuntimeError("provider down")

    v = goals.judge(g, judge_fn=broken)
    assert v["done"] is False and "judgment failed" in v["missing"]


def test_chat_surfaces():
    class FakeBot:
        user = "cli"
        conversation_id = "cli-default"
    handled, out, _ = tui.dispatch_command(
        FakeBot(), "/goal ship the deck :: deck emailed to the board")
    assert handled and "Goal" in out
    handled, out, _ = tui.dispatch_command(FakeBot(), "/goal list")
    assert handled and "ship the deck" in out
    gid = goals.active("cli")[0].id
    handled, out, _ = tui.dispatch_command(FakeBot(), f"/goal drop {gid}")
    assert handled and "dropped" in out

    out = gateway.reply_for({}, "U9", "/goal watch the euro :: alert sent",
                            prefix="sl")
    assert "Goal" in out[0]
    assert goals.active("sl-U9")
