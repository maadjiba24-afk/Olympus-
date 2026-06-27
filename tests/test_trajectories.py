"""Training-trajectory export from conversations and traces."""

import json

from olympus import config, memory, trajectories


def test_conversation_trajectories_pairs_turns():
    memory.save_conversation("c1", [
        {"role": "user", "content": "How should I price my SaaS product?"},
        {"role": "assistant", "content": "Anchor on value: tier at $19/$49/$99."},
    ])
    traj = trajectories.conversation_trajectories()
    assert len(traj) == 1
    msgs = traj[0]["messages"]
    assert msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant"


def test_skips_short_and_state_turns():
    memory.save_conversation("c2", [
        {"role": "user", "content": "[Conversation state — durable context]"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "hi"},          # too short
        {"role": "assistant", "content": "x"},      # too short
    ])
    assert trajectories.conversation_trajectories() == []


def test_compress_dedupes():
    recs = [{"a": 1}, {"a": 1}, {"a": 2}]
    assert trajectories.compress(recs) == [{"a": 1}, {"a": 2}]


def test_trace_trajectories_extract_path():
    base = config.MEMORY_DIR / "traces"
    base.mkdir(parents=True, exist_ok=True)
    rec = {"id": "r1", "meta": {"input": "do a thing"},
           "decisions": [
               {"type": "route", "agent": {"name": "zeus", "role": "router"},
                "rationale": {"mode": "delegate"}},
               {"type": "plan", "agent": {"name": "athena", "role": "supervisor"},
                "rationale": [{"specialist": "plutus"}]},
           ]}
    (base / "20260101.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    traj = trajectories.trace_trajectories()
    assert len(traj) == 1
    assert traj[0]["input"] == "do a thing"
    assert [p["step"] for p in traj[0]["path"]] == ["route", "plan"]
    assert traj[0]["path"][0]["agent"] == "zeus"


def test_export_writes_jsonl(tmp_path):
    memory.save_conversation("c3", [
        {"role": "user", "content": "Tell me about compound interest please"},
        {"role": "assistant", "content": "Compound interest grows on principal+interest."},
    ])
    out = tmp_path / "data.jsonl"
    n = trajectories.export(str(out), kind="messages")
    assert n == 1
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["messages"][0]["role"] == "user"
