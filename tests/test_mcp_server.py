"""MCP server mode (Hermes v0.6): Olympus as an MCP server over stdio."""

from olympus import goals, mcp_server


def _req(method, rid=1, **params):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params:
        msg["params"] = params
    return msg


def test_initialize_advertises_tools():
    res = mcp_server.handle_message(_req("initialize"))
    assert res["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert res["result"]["serverInfo"]["name"] == "olympus-council"
    assert "tools" in res["result"]["capabilities"]


def test_tools_list():
    res = mcp_server.handle_message(_req("tools/list"))
    names = [t["name"] for t in res["result"]["tools"]]
    assert "ask_olympus" in names and "olympus_goals" in names
    for t in res["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object"


def test_ask_olympus_roundtrip():
    res = mcp_server.handle_message(
        _req("tools/call", name="ask_olympus",
             arguments={"message": "what is 2+2?"}),
        ask=lambda m: f"the council says: {m} -> 4")
    content = res["result"]["content"]
    assert content == [{"type": "text",
                        "text": "the council says: what is 2+2? -> 4"}]


def test_olympus_goals_tool():
    goals.add("mcp", "keep the CI green")
    res = mcp_server.handle_message(
        _req("tools/call", name="olympus_goals", arguments={}))
    assert "keep the CI green" in res["result"]["content"][0]["text"]


def test_errors_and_notifications():
    # Notifications (no id) never get a response.
    assert mcp_server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    # Unknown method -> -32601.
    res = mcp_server.handle_message(_req("resources/list"))
    assert res["error"]["code"] == -32601
    # Unknown tool / missing args -> -32602.
    assert mcp_server.handle_message(
        _req("tools/call", name="nope"))["error"]["code"] == -32602
    assert mcp_server.handle_message(
        _req("tools/call", name="ask_olympus",
             arguments={}))["error"]["code"] == -32602


def test_tool_failure_is_an_in_band_error():
    def boom(message):
        raise RuntimeError("pipeline down")
    res = mcp_server.handle_message(
        _req("tools/call", name="ask_olympus", arguments={"message": "hi"}),
        ask=boom)
    assert res["result"]["isError"] is True
    assert "pipeline down" in res["result"]["content"][0]["text"]


def test_ping():
    assert mcp_server.handle_message(_req("ping"))["result"] == {}


# --- read-only workspace tools (scoped to OLYMPUS_MCP_USER) -----------------

def test_workspace_tools_listed():
    names = [t["name"] for t in
             mcp_server.handle_message(_req("tools/list"))["result"]["tools"]]
    for n in ("olympus_search_documents", "olympus_list_todos",
              "olympus_recall_memory"):
        assert n in names


def test_list_todos_scoped_to_mcp_user(monkeypatch, tmp_path):
    from olympus import config, todos
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_MCP_USER", "alice")
    todos.add("alice", "ship the release")
    res = mcp_server.handle_message(
        _req("tools/call", name="olympus_list_todos", arguments={}))
    assert "ship the release" in res["result"]["content"][0]["text"]
    assert not res["result"].get("isError")


def test_search_documents_read_only(monkeypatch, tmp_path):
    from olympus import config, documents, embed
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(embed, "available", lambda: False)
    monkeypatch.setenv("OLYMPUS_MCP_USER", "bob")
    documents.save("bob", "Notes", "The launch date is Tuesday in Lisbon.")
    res = mcp_server.handle_message(
        _req("tools/call", name="olympus_search_documents",
             arguments={"query": "launch date"}))
    assert "Lisbon" in res["result"]["content"][0]["text"]


def test_no_write_or_actuator_tool_is_exposed():
    names = [t["name"] for t in
             mcp_server.handle_message(_req("tools/list"))["result"]["tools"]]
    banned = ("write_document", "add_todo", "complete_todo", "send_email",
              "run_command", "prepare_action", "edit_file", "browser_act")
    assert not any(b in names for b in banned)


# --- governed Aegis / discovery reads (read/prepare only) -------------------

def test_governed_tools_listed():
    names = [t["name"] for t in
             mcp_server.handle_message(_req("tools/list"))["result"]["tools"]]
    for n in ("olympus_assess_report", "olympus_assess_scorecard",
              "olympus_discover_report"):
        assert n in names


def test_no_scan_grant_or_actuate_across_mcp():
    # An assessment (grant scope / recon / audit / validate / run) can NEVER be
    # initiated across the MCP boundary — only read/prepare surfaces are exposed.
    names = [t["name"] for t in
             mcp_server.handle_message(_req("tools/list"))["result"]["tools"]]
    banned = ("assess_authorize", "assess_scope", "assess_recon",
              "assess_http_audit", "assess_sast", "assess_secrets",
              "assess_deps", "assess_validate", "assess_run",
              "note_knowledge_gap")
    assert not any(b in names for b in banned)


def test_assess_report_scoped_and_read_only(monkeypatch, tmp_path):
    from olympus import assess, config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_MCP_USER", "carol")
    assess.record_finding(assess.Finding(
        title="Reflected XSS", severity="high", cwe="CWE-79",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
        location="https://app/x?q=1", source="sast"), user="carol")
    res = mcp_server.handle_message(
        _req("tools/call", name="olympus_assess_report",
             arguments={"format": "sarif"}))
    text = res["result"]["content"][0]["text"]
    assert not res["result"].get("isError")
    assert '"CWE-79"' in text or "CWE-79" in text     # SARIF carries the finding
    # A different user sees none of carol's findings.
    monkeypatch.setenv("OLYMPUS_MCP_USER", "dave")
    other = mcp_server.handle_message(
        _req("tools/call", name="olympus_assess_report", arguments={}))
    assert "No findings recorded" in other["result"]["content"][0]["text"]


def test_assess_scorecard_is_pure(monkeypatch, tmp_path):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    res = mcp_server.handle_message(
        _req("tools/call", name="olympus_assess_scorecard", arguments={}))
    text = res["result"]["content"][0]["text"]
    assert not res["result"].get("isError")
    assert "precision" in text.lower()               # the self-benchmark
    assert "contain" in text.lower()                 # the containment proof


def test_discover_report_read_only(monkeypatch, tmp_path):
    from olympus import config, discovery
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_MCP_USER", "erin")
    discovery.note_gap("knowledge", "how HTTP/3 flow control works", user="erin")
    res = mcp_server.handle_message(
        _req("tools/call", name="olympus_discover_report", arguments={}))
    text = res["result"]["content"][0]["text"]
    assert not res["result"].get("isError")
    assert "http/3" in text.lower()
