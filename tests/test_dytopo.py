"""D5 — DyTopo dynamic topology routing: deterministic, sparse/bounded, opt-in,
replay-safe. Pure core, no model/clock/rng."""

from olympus import dytopo


def _descs():
    return [
        dytopo.Descriptor("plutus", query="need market pricing and financials",
                          offer="financial analysis, pricing, revenue models"),
        dytopo.Descriptor("peitho", query="need pricing to write persuasive copy",
                          offer="persuasive copywriting and messaging"),
        dytopo.Descriptor("argus", query="scan the web for competitor data",
                          offer="web research, competitor scanning, sources"),
        dytopo.Descriptor("hephaestus", query="build code from a spec",
                          offer="software engineering, code, implementation"),
    ]


# --- induction: edges reflect query→offer matches --------------------------

def test_edges_connect_query_to_offer():
    topo = dytopo.induce(_descs(), max_out_degree=2, threshold=0.05)
    # peitho needs pricing → plutus offers pricing
    assert "plutus" in topo.neighbors("peitho")
    # no self-edges ever
    assert all(e.src != e.dst for e in topo.edges)


def test_is_deterministic():
    a = dytopo.induce(_descs())
    b = dytopo.induce(list(reversed(_descs())))     # input order must not matter
    assert a.as_dict() == b.as_dict()


def test_unrelated_specialist_has_no_forced_edges():
    topo = dytopo.induce(_descs(), threshold=0.2)
    # hephaestus (code) shares little with the others' offers at a high threshold
    assert topo.neighbors("hephaestus") == [] or \
        all(e.score >= 0.2 for e in topo.edges if e.src == "hephaestus")


# --- bounds are hard --------------------------------------------------------

def test_out_degree_is_capped():
    # everyone offers + queries the same words → dense candidate set
    ds = [dytopo.Descriptor(f"s{i}", query="data analysis pricing research",
                            offer="data analysis pricing research")
          for i in range(10)]
    topo = dytopo.induce(ds, max_out_degree=99)     # request more than the cap
    for n in topo.nodes:
        assert len(topo.neighbors(n)) <= dytopo._MAX_OUT_DEGREE
    assert topo.is_sparse()


def test_node_count_is_capped():
    ds = [dytopo.Descriptor(f"s{i}", query="x data", offer="x data")
          for i in range(dytopo._MAX_NODES + 20)]
    topo = dytopo.induce(ds)
    assert len(topo.nodes) <= dytopo._MAX_NODES


def test_total_edges_capped():
    ds = [dytopo.Descriptor(f"s{i}", query="a b c d", offer="a b c d")
          for i in range(dytopo._MAX_NODES)]
    topo = dytopo.induce(ds, max_out_degree=dytopo._MAX_OUT_DEGREE)
    assert len(topo.edges) <= dytopo._MAX_EDGES


def test_dedupes_specialists():
    ds = [dytopo.Descriptor("plutus", query="q", offer="o"),
          dytopo.Descriptor("plutus", query="q2", offer="o2")]
    topo = dytopo.induce(ds)
    assert topo.nodes == ("plutus",)


# --- rounds -----------------------------------------------------------------

def test_rounds_are_bounded_and_ordered():
    res = dytopo.route(_descs(), max_out_degree=2, max_rounds=3)
    assert len(res.rounds) <= 3
    # round 0 pairs each node with its single best neighbor
    for consulter, consulted in (res.rounds[0] if res.rounds else []):
        assert consulted == res.topology.neighbors(consulter)[0]
    assert res.summary()["sparse"] is True


def test_rounds_hard_cap():
    res = dytopo.route(_descs(), max_rounds=999)
    assert len(res.rounds) <= dytopo._MAX_ROUNDS


# --- edge cases + gating ----------------------------------------------------

def test_empty_input():
    topo = dytopo.induce([])
    assert topo.nodes == () and topo.edges == ()
    assert dytopo.rounds(topo) == []


def test_descriptor_without_name_is_skipped():
    ds = [dytopo.Descriptor("", query="q", offer="o"),
          dytopo.Descriptor("argus", query="q", offer="o")]
    topo = dytopo.induce(ds)
    assert topo.nodes == ("argus",)


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_DYTOPO", raising=False)
    assert dytopo.enabled() is False
    monkeypatch.setenv("OLYMPUS_DYTOPO", "1")
    assert dytopo.enabled() is True


def test_threshold_filters_weak_edges():
    topo_low = dytopo.induce(_descs(), threshold=0.0, max_out_degree=3)
    topo_high = dytopo.induce(_descs(), threshold=0.5, max_out_degree=3)
    assert len(topo_high.edges) <= len(topo_low.edges)
