"""Domain-lore fleet sharing + learned action-profile — the new-territory moat.

Covers:
  * a learned action profile (the safe steps that beat a domain's baseline) is
    recorded and offered back as a purely-additive hint;
  * raw per-domain lore can be shared, staged, and merged *additively* behind the
    operator's gate — never overwriting local truth, never relaxing a gate.
"""

import json

import pytest

from olympus import config, domainlore


@pytest.fixture()
def lore(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    monkeypatch.delenv("OLYMPUS_WEB_LORE", raising=False)
    return tmp_path


# --- learned action profile -------------------------------------------------

def test_action_profile_learned_and_hinted(lore):
    prof = json.dumps([{"type": "scroll"},
                       {"type": "click", "selector": ".load-more"}])
    domainlore.observe("https://shop.co/a", ok=True, bytes_=1000)      # baseline
    for _ in range(2):                                # two clear interaction wins
        domainlore.observe("https://shop.co/a", ok=True, bytes_=5000,
                           used_actions=True, action_profile=prof)
    r = domainlore.known("https://shop.co/x")
    assert r.actions_helped >= 2
    assert r.action_profile == prof
    h = domainlore.hint("https://shop.co/other")
    assert h.get("prefer_actions") is True
    assert h.get("action_profile") == [{"type": "scroll"},
                                       {"type": "click", "selector": ".load-more"}]


def test_action_profile_not_learned_without_bytegain(lore):
    prof = json.dumps([{"type": "scroll"}])
    domainlore.observe("https://nogain.co/a", ok=True, bytes_=4000)
    # actioned fetch that does NOT beat the baseline is not a win
    domainlore.observe("https://nogain.co/a", ok=True, bytes_=4000,
                       used_actions=True, action_profile=prof)
    assert "action_profile" not in domainlore.hint("https://nogain.co/x")


def test_bias_counter_is_capped(lore):
    for _ in range(300):
        domainlore.observe("https://m.co/a", ok=True, mobile_helped=True)
    assert domainlore.known("https://m.co/a").mobile_helped <= domainlore._BIAS_CAP


# --- fleet sharing: shareable / stage / merge -------------------------------

def test_shareable_only_durable_facts_with_track_record(lore):
    # a domain with a real track record + a useful fact is shareable
    domainlore.observe("https://good.co/a", ok=True, bytes_=1000,
                       sitemap="https://good.co/s.xml")
    domainlore.observe("https://good.co/b", ok=True, bytes_=1000)
    # a one-visit, factless domain is not
    domainlore.observe("https://thin.co/a", ok=True, bytes_=10)
    rows = domainlore.shareable(min_visits=2)
    doms = {r["domain"] for r in rows}
    assert "good.co" in doms and "thin.co" not in doms
    row = next(r for r in rows if r["domain"] == "good.co")
    # local-truth counters never leave the box
    assert "scrapes" not in row and "successes" not in row


def test_stage_and_merge_is_additive_and_gated(lore):
    # local knowledge already present
    domainlore.observe("https://site.co/a", ok=True, bytes_=1000,
                       sitemap="https://site.co/local.xml")
    staged = domainlore.stage_shared([
        {"domain": "site.co", "sitemap_url": "https://evil/other.xml",
         "has_jsonld": True, "feed_url": "https://site.co/feed"},
        {"domain": "new.co", "sitemap_url": "https://new.co/s.xml"},
    ], source="peerA")
    assert staged == 2
    assert len(domainlore.staged_shared()) == 2
    # nothing folded into the live corpus until the operator merges
    assert domainlore.known("https://new.co/x") is None
    assert domainlore.known("https://site.co/x").has_jsonld is False

    touched = domainlore.merge_staged()
    assert touched == 2
    assert domainlore.staged_shared() == []                # staging cleared

    site = domainlore.known("https://site.co/x")
    # additive: the peer FILLS gaps (jsonld, feed) but never OVERWRITES a
    # locally-learned fact (the local sitemap wins)
    assert site.sitemap_url == "https://site.co/local.xml"
    assert site.has_jsonld is True
    assert site.feed_url == "https://site.co/feed"
    # local truth (visit counts) is untouched by a merge
    assert site.scrapes == 1

    new = domainlore.known("https://new.co/x")
    assert new is not None and new.sitemap_url == "https://new.co/s.xml"
    assert new.scrapes == 0                                # a pure gift, no visits


def test_merge_dedups_and_rejects_bad_rows(lore):
    domainlore.stage_shared([
        {"domain": "ok.co", "sitemap_url": "https://ok.co/s.xml"},
        {"domain": "ok.co", "sitemap_url": "https://ok.co/s.xml"},   # dup (same peer)
        {"domain": "bad/domain", "sitemap_url": "x"},                # invalid
        {"nope": 1},                                                 # no domain
    ], source="peerA")
    staged = domainlore.staged_shared()
    assert len(staged) == 1 and staged[0]["domain"] == "ok.co"


def test_sharing_inert_under_replay(lore, monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert domainlore.stage_shared([{"domain": "x.co"}], source="p") == 0
    assert domainlore.merge_staged() == 0
