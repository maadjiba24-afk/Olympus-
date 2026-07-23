"""Domain lore — the web-context system's compounding, self-evolving memory."""

import pytest

from olympus import config, domainlore


@pytest.fixture()
def lore(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    monkeypatch.delenv("OLYMPUS_WEB_LORE", raising=False)
    return tmp_path


# --- gating -----------------------------------------------------------------

def test_enabled_by_default_inert_under_replay(lore, monkeypatch):
    assert domainlore.enabled() is True
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert domainlore.enabled() is False
    domainlore.observe("https://x.com/", ok=True)      # no-op under replay
    assert domainlore.known("https://x.com/") is None


def test_opt_out(lore, monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEB_LORE", "0")
    assert domainlore.enabled() is False
    assert domainlore.hint("https://x.com/") == {}


# --- compounding knowledge --------------------------------------------------

def test_observe_accumulates_and_hints_compound(lore):
    domainlore.observe("https://s.co/a", ok=True, bytes_=1000,
                       sitemap="https://s.co/sitemap.xml", site_name="SiteCo")
    domainlore.observe("https://s.co/b", ok=True, bytes_=2000)
    r = domainlore.known("https://s.co/x")
    assert r.scrapes == 2 and r.successes == 2
    assert r.sitemap_url == "https://s.co/sitemap.xml"
    assert r.site_name == "SiteCo"
    # the learned sitemap is offered back as a hint for the next visit
    assert domainlore.hint("https://s.co/anything")["sitemap_url"] \
        == "https://s.co/sitemap.xml"


def test_interaction_bias_needs_repeated_signal(lore):
    domainlore.observe("https://js.io/", ok=True, actions_helped=True)
    assert "prefer_actions" not in domainlore.hint("https://js.io/")   # 1 win
    domainlore.observe("https://js.io/", ok=True, actions_helped=True)
    assert domainlore.hint("https://js.io/").get("prefer_actions") is True  # 2 wins


def test_failures_and_blocks_tracked(lore):
    domainlore.observe("https://bad.io/", ok=False)
    domainlore.observe("https://bad.io/", ok=False, blocked=True)
    r = domainlore.known("https://bad.io/")
    assert r.failures == 1 and r.blocks == 1


def test_running_mean_bytes(lore):
    domainlore.observe("https://s.co/", ok=True, bytes_=1000)
    domainlore.observe("https://s.co/", ok=True, bytes_=3000)
    assert domainlore.known("https://s.co/").avg_bytes == pytest.approx(2000)


# --- resilience -------------------------------------------------------------

def test_corrupt_store_quarantined_not_wiped(lore):
    domainlore.observe("https://s.co/", ok=True)
    p = domainlore._path()
    p.write_text("{ not json", encoding="utf-8")
    assert domainlore._load() == {}
    assert p.with_name(p.name + ".corrupt").exists()


def test_unknown_field_ignored(lore):
    import json
    domainlore._path().write_text(
        json.dumps({"s.co": {"domain": "s.co", "brand_new_field": 1}}),
        encoding="utf-8")
    assert domainlore.known("https://s.co/").domain == "s.co"   # forward-compatible


def test_corpus_is_bounded(lore, monkeypatch):
    monkeypatch.setattr(domainlore, "MAX_DOMAINS", 2)
    for i in range(5):
        domainlore.observe(f"https://d{i}.com/", ok=True)
    assert len(domainlore._load()) <= 2


# --- moat / self-tuner wiring ----------------------------------------------

def test_report_and_stats(lore):
    assert "nothing learned yet" in domainlore.report()
    domainlore.observe("https://s.co/", ok=True, sitemap="https://s.co/sm.xml")
    s = domainlore.stats()
    assert s["domains"] == 1 and s["sitemaps"] == 1
    assert "domain(s) learned" in domainlore.report()


def test_wired_into_moat_board(lore):
    from olympus import moat
    caps = moat.status()["capabilities"]
    assert "web_context" in caps
    assert caps["web_context"]["flag"] == "OLYMPUS_WEB_LORE"


def test_evolve_exposes_webctx_tunable():
    from olympus import evolve
    t = evolve.current("webctx", "fetch_timeout")
    assert 6 <= t <= 20                                 # self-tuned within bounds


# --- iter 3: closed learn -> apply loop -------------------------------------

def test_learns_mobile_win_from_byte_gain(lore):
    domainlore.observe("https://m.io/", ok=True, bytes_=100)          # baseline
    domainlore.observe("https://m.io/", ok=True, bytes_=1000, used_mobile=True)
    assert domainlore.known("https://m.io/").mobile_helped == 1        # 10x > 1.2x
    # a mobile fetch that does NOT beat the baseline is not counted as a win
    domainlore.observe("https://m.io/", ok=True, bytes_=110, used_mobile=True)
    assert domainlore.known("https://m.io/").mobile_helped == 1


def test_prefer_mobile_needs_two_wins_then_hints(lore):
    for _ in range(2):
        domainlore.observe("https://m.io/", ok=True, bytes_=100)
        domainlore.observe("https://m.io/", ok=True, bytes_=1000, used_mobile=True)
    assert domainlore.known("https://m.io/").mobile_helped >= 2
    assert domainlore.hint("https://m.io/").get("prefer_mobile") is True


def test_scrape_auto_applies_learned_mobile(lore, monkeypatch):
    from olympus import webctx, tools
    seen = []

    def fake(u, timeout=30, headers=None):
        seen.append("iPhone" in (headers or {}).get("User-Agent", ""))
        return "<h1>x</h1>"
    monkeypatch.setattr(tools, "_http_get", fake)
    # pre-seed the learned bias, then scrape with mobile unset (None = auto)
    for _ in range(2):
        domainlore.observe("https://m.io/", ok=True, bytes_=100)
        domainlore.observe("https://m.io/", ok=True, bytes_=1000, used_mobile=True)
    seen.clear()
    webctx.scrape("https://m.io/", formats=("markdown",))     # mobile None → auto
    assert seen[-1] is True                                   # applied learned mobile


def test_explicit_mobile_false_overrides_learned_bias(lore, monkeypatch):
    from olympus import webctx, tools
    seen = []
    monkeypatch.setattr(tools, "_http_get",
                        lambda u, timeout=30, headers=None:
                        seen.append("iPhone" in (headers or {}).get("User-Agent", ""))
                        or "<h1>x</h1>")
    for _ in range(2):
        domainlore.observe("https://m.io/", ok=True, bytes_=100)
        domainlore.observe("https://m.io/", ok=True, bytes_=1000, used_mobile=True)
    seen.clear()
    webctx.scrape("https://m.io/", formats=("markdown",), mobile=False)  # explicit
    assert seen[-1] is False                                  # caller's choice wins
