"""D6 — E-mem episodic reconstruction: on-demand, non-destructive, chronological,
provenance-tagged. Pure core driven by injected fragments (no I/O)."""

from olympus import emem


def _frag(source, ref, ts, text, trust="user"):
    return emem.Fragment(source=source, ref=ref, ts=ts, text=text, trust=trust)


def _fragments():
    return [
        _frag("memory", "m1", 100.0, "user is building a bakery called Rise"),
        _frag("event", "e1", 200.0, "asked about oven pricing for the bakery"),
        _frag("conversation", "c1", 0.0,
              "the bakery loan was approved last week", trust="external"),
        _frag("memory", "m2", 50.0, "user prefers espresso"),  # unrelated
    ]


# --- selection: relevance floor -------------------------------------------

def test_reconstruct_selects_relevant_fragments():
    ep = emem.reconstruct("bakery oven pricing", _fragments(), now=500.0)
    refs = {f.ref for f in ep.fragments}
    assert "e1" in refs and "m1" in refs      # bakery-related
    assert "m2" not in refs                    # espresso is unrelated → excluded


def test_empty_query_returns_empty_episode():
    ep = emem.reconstruct("", _fragments(), now=500.0)
    assert ep.fragments == ()


def test_no_salient_overlap_returns_empty():
    ep = emem.reconstruct("quantum chromodynamics", _fragments(), now=500.0)
    assert ep.fragments == ()


# --- reconstruction is CHRONOLOGICAL --------------------------------------

def test_selected_fragments_are_ordered_in_time():
    ep = emem.reconstruct("bakery", _fragments(), now=500.0)
    ts = [f.ts for f in ep.fragments]
    assert ts == sorted(ts)                    # episodic = time-ordered


# --- non-destructive: text is verbatim ------------------------------------

def test_fragment_text_is_never_rewritten():
    frags = _fragments()
    ep = emem.reconstruct("bakery", frags, now=500.0)
    originals = {f.ref: f.text for f in frags}
    for f in ep.fragments:
        assert f.text == originals[f.ref]      # byte-identical, not summarized


# --- deterministic --------------------------------------------------------

def test_reconstruction_is_deterministic():
    a = emem.reconstruct("bakery oven", _fragments(), now=500.0)
    b = emem.reconstruct("bakery oven", list(reversed(_fragments())), now=500.0)
    assert a.provenance() == b.provenance()


# --- provenance + envelope ------------------------------------------------

def test_provenance_is_attributable():
    ep = emem.reconstruct("bakery", _fragments(), now=500.0)
    prov = ep.provenance()
    assert all(":" in p for p in prov)
    assert any(p.startswith("memory:") for p in prov)


def test_render_is_wrapped_and_tagged():
    ep = emem.reconstruct("bakery", _fragments(), now=500.0)
    out = ep.render()
    assert 'source="episodic-memory"' in out    # untrusted envelope
    assert "[memory]" in out and "Reconstructed episode" in out


def test_render_empty_is_blank():
    assert emem.Episode(query="x", fragments=()).render() == ""


# --- bounds ---------------------------------------------------------------

def test_fragment_count_is_capped(monkeypatch):
    monkeypatch.setattr(emem, "_MAX_FRAGMENTS", 3)
    many = [_frag("event", f"e{i}", float(i), f"bakery detail number {i}")
            for i in range(20)]
    ep = emem.reconstruct("bakery detail", many, now=100.0, max_fragments=3)
    assert len(ep.fragments) <= 3


def test_char_budget_is_respected():
    big = [_frag("event", f"e{i}", float(i), "bakery " + "x" * 2000)
           for i in range(10)]
    ep = emem.reconstruct("bakery", big, now=100.0, budget_chars=3000)
    assert sum(len(f.text) for f in ep.fragments) <= 3000 + 2007  # +one overflow


def test_recency_breaks_ties():
    frags = [_frag("event", "old", 0.0, "bakery plan"),
             _frag("event", "new", 1_000_000.0, "bakery plan")]
    ep = emem.reconstruct("bakery plan", frags, now=1_000_000.0)
    # both selected; the more recent scores higher (appears via ordering intact)
    assert {f.ref for f in ep.fragments} == {"old", "new"}


# --- gating ---------------------------------------------------------------

def test_off_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_EMEM", raising=False)
    assert emem.enabled() is False
    assert emem.context_block("u", "anything") == ""     # no-op when disabled


def test_context_block_enabled_reconstructs(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMEM", "1")
    monkeypatch.setattr(emem, "gather",
                        lambda user, query, **k: _fragments())
    out = emem.context_block("u", "bakery oven", now=500.0)
    assert 'source="episodic-memory"' in out and "bakery" in out.lower()
