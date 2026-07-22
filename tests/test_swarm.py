"""Native swarm topologies + the consultation driver (dytopo extensions)."""

from olympus import dytopo


def _edge_set(topo):
    return {(e.src, e.dst) for e in topo.edges}


# --- explicit shapes -----------------------------------------------------

def test_mesh_is_all_to_all():
    topo = dytopo.mesh(["a", "b", "c"])
    assert _edge_set(topo) == {
        ("a", "b"), ("a", "c"), ("b", "a"), ("b", "c"), ("c", "a"), ("c", "b")}
    assert set(topo.nodes) == {"a", "b", "c"}


def test_star_in_workers_report_to_hub():
    topo = dytopo.star("hub", ["w1", "w2"], mode="in")
    assert _edge_set(topo) == {("w1", "hub"), ("w2", "hub")}
    assert "hub" in topo.nodes


def test_star_out_and_both():
    assert _edge_set(dytopo.star("h", ["w"], mode="out")) == {("h", "w")}
    assert _edge_set(dytopo.star("h", ["w"], mode="both")) == {
        ("h", "w"), ("w", "h")}


def test_hierarchical_parent_to_child():
    topo = dytopo.hierarchical([["root"], ["a", "b"], ["c"]])
    assert _edge_set(topo) == {
        ("root", "a"), ("root", "b"), ("a", "c"), ("b", "c")}


def test_ring_closes_the_loop():
    topo = dytopo.ring(["a", "b", "c"])   # nodes sorted → a,b,c
    assert _edge_set(topo) == {("a", "b"), ("b", "c"), ("c", "a")}
    assert dytopo.ring(["solo"]).edges == ()   # <2 nodes → no ring


def test_named_topology_dispatch():
    assert dytopo.named_topology("mesh", ["a", "b"]).edges
    assert _edge_set(dytopo.named_topology("star", ["a", "b"], hub="a")) == {
        ("b", "a")}
    assert dytopo.named_topology(
        "hierarchical", ["a", "b"], layers=[["a"], ["b"]]).edges
    # unknown kind falls back to mesh
    assert _edge_set(dytopo.named_topology("bogus", ["a", "b"])) == {
        ("a", "b"), ("b", "a")}


# --- determinism + bounds ------------------------------------------------

def test_shapes_are_deterministic():
    a = dytopo.mesh(["c", "a", "b"]).as_dict()
    b = dytopo.mesh(["a", "b", "c"]).as_dict()
    assert a == b                     # order-independent, sorted nodes


def test_mesh_respects_edge_cap():
    topo = dytopo.mesh([f"n{i}" for i in range(40)])   # 40*39 would be 1560
    assert len(topo.nodes) <= dytopo._MAX_NODES
    assert len(topo.edges) <= dytopo._MAX_EDGES


def test_dedup_and_falsy_nodes():
    topo = dytopo.mesh(["a", "a", "", "b"])
    assert set(topo.nodes) == {"a", "b"}


# --- gating --------------------------------------------------------------

def test_swarm_gating(monkeypatch):
    assert dytopo.swarm_enabled() is False
    monkeypatch.setenv("OLYMPUS_SWARM", "true")
    assert dytopo.swarm_enabled() is True
    assert dytopo.swarm_topology_kind() == "star"        # default
    monkeypatch.setenv("OLYMPUS_SWARM_TOPOLOGY", "mesh")
    assert dytopo.swarm_topology_kind() == "mesh"


# --- consultation driver (injected runner → no model needed) -------------

def test_consultation_refines_via_runner():
    topo = dytopo.star("lead", ["w1"], mode="in")   # w1 -> lead
    outputs = [("lead", "lead answer"), ("w1", "w1 answer")]

    def runner(consulter, peer_ctx, own):
        return f"{own} + consulted[{consulter}->{peer_ctx}]"

    refined = dict(dytopo.run_consultation(topo, outputs, runner))
    # w1 consults lead, so w1's output is refined with lead's context.
    assert refined["w1"] == "w1 answer + consulted[w1->lead answer]"
    assert refined["lead"] == "lead answer"          # lead consults no one


def test_consultation_preserves_order_and_shape():
    topo = dytopo.mesh(["a", "b"])
    outputs = [("a", "A"), ("b", "B")]
    out = dytopo.run_consultation(topo, outputs, lambda c, p, o: o + "!")
    assert [k for k, _ in out] == ["a", "b"]          # input order kept
    assert dict(out) == {"a": "A!", "b": "B!"}


def test_consultation_runner_failure_leaves_output_untouched():
    topo = dytopo.mesh(["a", "b"])
    outputs = [("a", "A"), ("b", "B")]

    def boom(consulter, peer_ctx, own):
        raise RuntimeError("model down")

    assert dict(dytopo.run_consultation(topo, outputs, boom)) == {
        "a": "A", "b": "B"}


def test_consultation_falsy_refine_is_ignored():
    topo = dytopo.mesh(["a", "b"])
    outputs = [("a", "A"), ("b", "B")]
    assert dict(dytopo.run_consultation(topo, outputs, lambda c, p, o: "")) == {
        "a": "A", "b": "B"}


def test_consultation_skips_missing_peer_context():
    topo = dytopo.star("lead", ["w1"], mode="in")
    outputs = [("lead", ""), ("w1", "w1 answer")]     # lead has no context
    called = []

    def runner(consulter, peer_ctx, own):
        called.append(consulter)
        return own + " refined"

    out = dict(dytopo.run_consultation(topo, outputs, runner))
    assert called == []                # no peer_ctx for w1's consulted → skip
    assert out["w1"] == "w1 answer"
