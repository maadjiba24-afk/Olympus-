"""Memory Wiki: concept pages, freshness linting, dreaming consolidation,
and retrieval into the pipeline context."""

import time

from olympus import gateway, usermem, wiki

DAY = 86400


def test_upsert_read_roundtrip():
    slug = wiki.upsert("u1", "Project Atlas", "A rewrite of the billing "
                       "system, due Q3. Stack: FastAPI + Postgres.")
    assert slug == "project-atlas"
    page = wiki.read("u1", slug)
    assert "Project Atlas" in page and "billing" in page


def test_upsert_rewrites_in_place_and_keeps_created():
    now = time.time()
    wiki.upsert("u1", "Deploys", "We deploy on Fridays.", now=now - 5 * DAY)
    wiki.upsert("u1", "Deploys", "We deploy on Tuesdays now.", now=now)
    assert len(wiki.pages("u1")) == 1
    page = wiki.pages("u1")[0]
    assert abs(page["updated"] - now) < 2
    assert "Tuesdays" in wiki.read("u1", "deploys")


def test_lint_flags_stale_but_not_durable():
    now = time.time()
    wiki.upsert("u1", "Old Project", "ancient content here",
                review_after_days=30, now=now - 60 * DAY)
    wiki.upsert("u1", "My Name", "The user is called Ada.",
                durable=True, review_after_days=30, now=now - 60 * DAY)
    issues = wiki.lint("u1", now=now)
    assert any("stale" in i and "old-project" in i for i in issues)
    assert not any("my-name" in i for i in issues)


def test_lint_flags_near_duplicates():
    wiki.upsert("u1", "Atlas Billing System", "a")
    wiki.upsert("u1", "Billing System Atlas v2", "b")
    issues = wiki.lint("u1")
    assert any("near-duplicate" in i for i in issues)


def test_context_block_finds_relevant_page():
    wiki.upsert("u1", "Project Atlas",
                "A rewrite of the billing system in FastAPI.")
    wiki.upsert("u1", "Coffee preferences", "Flat white, no sugar.")
    block = wiki.context_block("u1", "how is the atlas billing rewrite going?")
    assert "Project Atlas" in block
    assert "Coffee" not in block
    assert wiki.context_block("u1", "unrelated quantum topic") == ""


def test_dream_writes_and_retires_pages():
    wiki.upsert("u1", "Doomed Page", "obsolete stuff")
    usermem.add_memory("u1", type="project",
                       content="Atlas ships next month.", confidence=0.9)

    def runner(system, prompt, schema):
        assert "Doomed Page" in prompt          # existing index is shown
        assert "Atlas ships" in prompt          # new material is shown
        return {"pages": [
            {"title": "Project Atlas", "content": "Ships next month.",
             "review_after_days": 30},
            {"title": "Doomed Page", "content": "OBSOLETE"},
        ]}

    out = wiki.dream("u1", runner=runner)
    assert "1 page(s) written" in out and "1 retired" in out
    slugs = {p["slug"] for p in wiki.pages("u1")}
    assert slugs == {"project-atlas"}


def test_dream_skips_when_nothing_new():
    out = wiki.dream("u-empty", runner=lambda *a: {"pages": []})
    assert out == "nothing new to consolidate"
    # marked as dreamed: material older than the mark is not re-read
    assert wiki.last_dream("u-empty") > 0


def test_dream_only_reads_material_since_last_dream():
    usermem.add_memory("u2", type="project", content="Old fact.",
                       confidence=0.9)
    wiki.dream("u2", runner=lambda *a: {"pages": []})
    seen = {}

    def runner(system, prompt, schema):
        seen["prompt"] = prompt
        return {"pages": []}

    usermem.add_memory("u2", type="project", content="Brand new fact.",
                       confidence=0.9)
    wiki.dream("u2", runner=runner)
    assert "Brand new fact" in seen["prompt"]
    assert "Old fact" not in seen["prompt"]


def test_dream_failure_is_reported_not_raised():
    usermem.add_memory("u3", type="project", content="x", confidence=0.9)

    def boom(*a):
        raise RuntimeError("provider down")

    assert "dream failed" in wiki.dream("u3", runner=boom)


def test_dream_all_covers_users_with_material(monkeypatch):
    usermem.add_memory("u4", type="project", content="y", confidence=0.9)
    log = wiki.dream_all(runner=lambda *a: {"pages": [
        {"title": "T", "content": "c"}]})
    assert any("u4" in line for line in log)


def test_chat_wiki_commands():
    wiki.upsert(gateway.principal_id("u5"), "Project Atlas", "Billing rewrite.")
    out = gateway.reply_for({}, "u5", "/wiki")
    assert any("project-atlas" in c for c in out)
    out = gateway.reply_for({}, "u5", "/wiki show project-atlas")
    assert any("Billing rewrite" in c for c in out)
