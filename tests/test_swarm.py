"""Native swarm topologies + the consultation driver (dytopo extensions)."""

import types

import anthropic

from olympus import dytopo, llm


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


# --- hardening: duplicate specialist keys are preserved (M1) -------------

def test_consultation_preserves_duplicate_keys():
    # Athena can assign one specialist to two DAG steps → duplicate keys.
    topo = dytopo.star("apollo", ["hermes"], mode="in")   # hermes -> apollo
    outputs = [("apollo", "A1"), ("apollo", "A2"), ("hermes", "H")]
    out = dytopo.run_consultation(
        topo, outputs, lambda c, p, o: o + "+")
    # NOTHING is collapsed: both apollo slots survive with their own text.
    assert out == [("apollo", "A1"), ("apollo", "A2"), ("hermes", "H+")]
    assert len(out) == 3


def test_consultation_refines_all_slots_of_a_consulter():
    topo = dytopo.star("lead", ["worker"], mode="in")     # worker -> lead
    outputs = [("worker", "W1"), ("worker", "W2"), ("lead", "L")]
    out = dict((i, v) for i, (k, v) in enumerate(
        dytopo.run_consultation(topo, outputs, lambda c, p, o: o + "!")))
    assert out[0] == "W1!" and out[1] == "W2!"     # both worker slots refined


# --- hardening: mesh cap is FAIR — no node is starved (L3) ---------------

def test_mesh_fair_cap_keeps_every_node_connected():
    topo = dytopo.mesh([f"n{i}" for i in range(30)])   # full mesh = 870 edges
    assert len(topo.edges) <= dytopo._MAX_EDGES
    srcs = {e.src for e in topo.edges}
    assert len(srcs) == len(topo.nodes)            # every node has an out-edge
    # each node's out-degree is bounded but non-zero
    from collections import Counter
    outdeg = Counter(e.src for e in topo.edges)
    assert all(v >= 1 for v in outdeg.values())


# --- hardening: swarm state is recorded in meta + restored on replay (H1) -

def _msg(text):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _swarm_client():
    import json as _json
    counter = {"n": 0}

    def stream(**params):
        schema = ((params.get("output_config") or {}).get("format") or {}).get(
            "schema") or {}
        props = schema.get("properties", {})
        blob = _json.dumps(params.get("messages") or "") + str(
            params.get("system") or "")

        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                if "mode" in props:                     # Zeus route
                    return _msg(_json.dumps(
                        {"mode": "delegate", "direct_reply": None,
                         "specialists": ["argus", "plutus"], "brief": "b",
                         "needs_verification": True}))
                if "steps" in props:                    # Athena plan: 2 steps
                    return _msg(_json.dumps({"steps": [
                        {"id": "s1", "specialist": "argus", "task": "t1",
                         "depends_on": []},
                        {"id": "s2", "specialist": "plutus", "task": "t2",
                         "depends_on": []}]}))
                if "verdict" in props:                  # Athena review
                    return _msg(_json.dumps(
                        {"verdict": "approve", "feedback": "",
                         "retry_specialists": []}))
                if "Specialist outputs to verify" in blob:   # Aletheia verify
                    return _msg('checked\nVERDICT: {"status": "pass", '
                                '"unsupported_claims": [], "confidence": 0.9}')
                counter["n"] += 1                       # specialist / consult run
                return _msg(f"finding-{counter['n']}")
        return _S()

    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def test_swarm_recorded_run_replays_clean(monkeypatch):
    """A run recorded with OLYMPUS_SWARM on must replay byte-identically even
    when the replay process has swarm unset — replay_run restores it from the
    recorded meta. Without the fix the verify stage would hash the UNrefined
    outputs and diverge."""
    from olympus import config, orchestrator, recall
    monkeypatch.setattr(recall, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OLYMPUS_SWARM", "1")
    monkeypatch.setenv("OLYMPUS_SWARM_TOPOLOGY", "star")
    monkeypatch.setattr(llm, "client", lambda settings=None: _swarm_client())

    bot = orchestrator.Olympus(pool=config.ModelPool.from_env())
    bot.ask("do the thing")
    rid = bot.last_run_id

    # Replay with swarm UNSET in the ambient env and the API hard-forbidden.
    monkeypatch.delenv("OLYMPUS_SWARM", raising=False)
    monkeypatch.delenv("OLYMPUS_SWARM_TOPOLOGY", raising=False)
    monkeypatch.setattr(llm, "client", lambda settings=None: (
        _ for _ in ()).throw(AssertionError("replay must not call the API")))
    original, _fresh, diffs = orchestrator.replay_run(rid)

    assert original["meta"]["swarm_enabled"] is True     # recorded in meta
    assert original["meta"]["swarm_topology"] == "star"
    assert diffs == []                                   # byte-identical replay
