"""Skill-library curation (Hermes v0.12 curator): grade, prune behind the
same regression gate that admits skills, recommend consolidations to Metis."""

from olympus import curator, memory, skills


def _seed(n=5, specialist=None):
    for i in range(n):
        skills.create(f"Skill {i}", f"how-to number {i}",
                      "1. Do the thing.", specialist=specialist)


def _judge(verdicts):
    return lambda system, messages, schema: {"verdicts": verdicts}


def test_small_library_is_left_alone():
    _seed(2)
    out = curator.curate(judge_fn=_judge([]))
    assert "nothing to curate" in out
    assert skills.count() == 2


def test_prune_archives_when_benchmark_unaffected():
    _seed(5)
    out = curator.curate(
        judge_fn=_judge([{"name": "Skill 3", "verdict": "prune",
                          "reason": "duplicative"}]),
        eval_fn=lambda ids: 7.0)          # hiding it changes nothing
    assert "pruned 1" in out
    assert skills.count() == 4
    # Recoverable: archived, not deleted.
    from olympus import config
    archived = list((config.MEMORY_DIR / "skill_backups").glob("pruned-*.md"))
    assert len(archived) == 1 and "duplicative" in archived[0].read_text()


def test_prune_is_kept_when_benchmark_would_regress():
    _seed(5)
    scores = iter([7.0, 6.0])             # visible 7.0 → hidden 6.0: regression

    out = curator.curate(
        judge_fn=_judge([{"name": "Skill 1", "verdict": "prune",
                          "reason": "looks generic"}]),
        eval_fn=lambda ids: next(scores))
    assert "kept" in out and "regress" in out
    assert skills.count() == 5            # untouched, and not left hidden
    assert "Skill 1" in skills.index()


def test_prune_cap_limits_blast_radius(monkeypatch):
    monkeypatch.setattr(curator, "MAX_PRUNE_PER_RUN", 1)
    _seed(6)
    out = curator.curate(
        judge_fn=_judge([{"name": f"Skill {i}", "verdict": "prune",
                          "reason": "r"} for i in range(4)]),
        eval_fn=lambda ids: 5.0)
    assert "pruned 1" in out
    assert skills.count() == 5


def test_consolidations_are_queued_for_metis():
    _seed(5)
    out = curator.curate(
        judge_fn=_judge([{"name": "Skill 2", "verdict": "consolidate",
                          "target": "Skill 1", "reason": "same topic"}]),
        eval_fn=lambda ids: 5.0)
    assert "consolidation" in out
    assert skills.count() == 5            # curator never merges by itself
    assert "merge 'Skill 2' into 'Skill 1'" in memory.recent("lessons", 5)


def test_grading_failure_leaves_library_untouched():
    _seed(5)

    def broken(system, messages, schema):
        raise RuntimeError("model down")

    out = curator.curate(judge_fn=broken)
    assert "grading failed" in out and skills.count() == 5


def test_unknown_names_from_judge_are_ignored():
    _seed(5)
    out = curator.curate(
        judge_fn=_judge([{"name": "Not A Real Skill", "verdict": "prune",
                          "reason": "hallucinated"}]),
        eval_fn=lambda ids: 5.0)
    assert "pruned" not in out
    assert skills.count() == 5
