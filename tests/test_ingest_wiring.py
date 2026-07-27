"""A7 — the Wave-2 ingestion gate (W2-C5) wired into the LIVE ingestion paths.

`tests/test_ingestgate.py` proves the gate itself is correct. This file proves
the gate is actually *consulted* at every real trust boundary, and that the
wiring obeys the four properties the wiring itself can get wrong:

1. **Flag-off is byte-identical pass-through.** With `OLYMPUS_INGESTGATE`
   unset, `ingestgate.check` is monkeypatched to explode; every wired path
   still behaves exactly as before, which is only possible if the gate is not
   consulted at all.
2. **Flag-on happy path.** A valid artifact still installs/loads through each
   wired path.
3. **Flag-on refusal is fail-closed.** A malformed artifact — the malformation
   taken from the permanent corpus in `tests/fixtures/ingest_corpus/` — is
   REFUSED at each path: nothing is written, installed or executed, and a
   retained refusal-evidence row exists (W2-I5.3).
4. **The persistent/ephemeral split is respected by the CALL SITES.** The MCP
   path uses `sanitize_ephemeral` + `assert_schema` and never `check()`; the
   persistent paths use `check()` and never `sanitize_ephemeral`; and no
   persistent kind can be routed through `sanitize_ephemeral` at all (W2-I5.2).

Plus the partial-application guard: a refusal mid-install must leave no
half-installed state.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from olympus import (config, ingestgate, mcp_client, memory, pluginstore,
                     skillpack, skills)

CORPUS_DIR = Path(__file__).parent / "fixtures" / "ingest_corpus"


# --- corpus reuse -----------------------------------------------------------

def _corpus(name: str) -> dict:
    """One committed malformed-input case, by file stem."""
    return json.loads((CORPUS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _corpus_payload(name: str) -> dict:
    """The case's payload as a dict, whichever form the file encodes it in."""
    case = _corpus(name)
    if "payload" in case:
        return case["payload"]
    if "payload_text" in case:
        # strict=False: these fixtures deliberately carry raw control bytes.
        return json.loads(case["payload_text"], strict=False)
    if "payload_b64" in case:
        raw = base64.b64decode(case["payload_b64"])
        return json.loads(raw.decode("utf-8", "surrogateescape"), strict=False)
    raise AssertionError(f"corpus case {name} has no reusable payload")


# The exact malformations reused below, pulled from the permanent corpus so the
# wiring tests fail the day a corpus case changes meaning.
CONTROL_CHAR_INSTRUCTIONS = _corpus_payload("control_chars_skill")["instructions"]
NUL_INSTRUCTIONS = _corpus_payload("nul_byte_skill")["instructions"]
BAD_VERSION = _corpus_payload("malformed_version_skill")["version"]
UNKNOWN_MAJOR = _corpus_payload("unknown_major_skill")["version"]
WRONG_TYPE_VALUE = _corpus_payload("wrong_type_skill_instructions")["instructions"]
INVALID_UTF8_CODE = base64.b64decode(
    _corpus("invalid_utf8_plugin")["payload_b64"]).split(b'"code":"')[1][:2]


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("OLYMPUS_INGESTGATE", "1")
    yield


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_INGESTGATE", raising=False)
    yield


@pytest.fixture(autouse=True)
def plugin_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_PLUGINS_DIR", str(tmp_path / "plugins"))
    monkeypatch.delenv("OLYMPUS_PLUGIN_ALLOW", raising=False)
    monkeypatch.delenv("OLYMPUS_PLUGIN_ENFORCE", raising=False)
    yield


@pytest.fixture(autouse=True)
def mcp_isolated(monkeypatch):
    monkeypatch.setattr(mcp_client, "_KIND_CACHE", {})
    monkeypatch.setattr(mcp_client, "_SCHEMA_CACHE", {})
    yield


def _exploding_check(*a, **k):
    raise AssertionError("ingestgate.check must not be consulted here")


def _exploding_sanitize(*a, **k):
    raise AssertionError("sanitize_ephemeral must not be consulted here")


# --- helpers: a valid artifact for each wired path --------------------------

SKILL_MD = """---
name: Wired Skill
description: a valid skill pack entry
version: "1.0"
---
# Wired Skill

Do the thing, then stop.
"""


def _skill_md(*, name="Wired Skill", version='"1.0"', body="Do the thing.",
              description="a valid skill pack entry") -> str:
    return (f"---\nname: {name}\ndescription: {description}\n"
            f"version: {version}\n---\n# {name}\n\n{body}\n")


def _plugin_file(tmp_path, body=b"# a plugin\n", name="myplugin.py"):
    f = tmp_path / name
    f.write_bytes(body)
    return f, hashlib.sha256(body).hexdigest()


def _seed_memory(user="alice") -> dict:
    memory.set_user(user)
    memory.save("lessons", "First lesson", "remember the moat")
    memory.set_user("shared")
    return {p.relative_to(config.MEMORY_DIR).as_posix(): p.read_bytes()
            for r in memory._memory_roots(user) if r.exists()
            for p in sorted(r.rglob("*")) if p.is_file()}


def _make_archive(path: Path, manifest: dict, files=()) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in files:
            info = tarfile.TarInfo(f"data/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        md = json.dumps(manifest).encode("utf-8")
        mi = tarfile.TarInfo("manifest.json")
        mi.size = len(md)
        tar.addfile(mi, io.BytesIO(md))
    path.write_bytes(buf.getvalue())


class _FakeTool:
    def __init__(self, name="do_thing", description="a tool"):
        self.name = name
        self.description = description
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeResult:
    def __init__(self, text="ok", is_error=False):
        self.isError = is_error
        self.content = [type("C", (), {"text": text})()]


def _fake_server(name="files"):
    return type("S", (), {"name": name, "transport": "sse",
                          "url": "https://example.invalid/mcp",
                          "type": "data"})()


def _wire_mcp(monkeypatch, tools=None, result=None):
    monkeypatch.setattr(mcp_client, "have_sdk", lambda: True)
    monkeypatch.setattr(mcp_client, "_resolve", lambda n: _fake_server(n))
    payload = {"tools": tools if tools is not None else [_FakeTool()],
               "result": result if result is not None else _FakeResult()}
    seen = {"n": 0}

    def fake_run_sync(coro_factory, timeout):
        seen["n"] += 1
        return payload["tools"] if timeout == mcp_client.DISCOVER_TIMEOUT \
            else payload["result"]

    monkeypatch.setattr(mcp_client, "_run_sync", fake_run_sync)
    return seen


def _refusal_kinds() -> list[str]:
    return [r.get("kind") for r in ingestgate.refusals(200)]


# ===========================================================================
# 1. FLAG OFF => pass-through. The gate is not consulted at all.
# ===========================================================================

def test_flag_off_skillpack_import_never_consults_the_gate(
        gate_off, monkeypatch, tmp_path):
    monkeypatch.setattr(ingestgate, "check", _exploding_check)
    f = tmp_path / "SKILL.md"
    f.write_text(SKILL_MD, encoding="utf-8")
    assert "Wired Skill" in skillpack.import_file(str(f))
    assert "Do the thing" in skills.read("Wired Skill")


def test_flag_off_skill_packs_never_consult_the_gate(gate_off, monkeypatch):
    monkeypatch.setattr(ingestgate, "check", _exploding_check)
    assert len(skillpack.install_starter_pack()) == len(skillpack.STARTER_PACK)
    assert len(skillpack.install_security_pack()) == len(skillpack.SECURITY_PACK)


def test_flag_off_pluginstore_never_consults_the_gate(
        gate_off, monkeypatch, tmp_path):
    monkeypatch.setattr(ingestgate, "check", _exploding_check)
    f, digest = _plugin_file(tmp_path)
    res = pluginstore.install(str(f), sha256=digest)
    assert (pluginstore.plugins_dir() / f"{res['name']}.py").exists()


def test_flag_off_mcp_never_consults_the_gate(gate_off, monkeypatch):
    monkeypatch.setattr(ingestgate, "check", _exploding_check)
    monkeypatch.setattr(ingestgate, "sanitize_ephemeral", _exploding_sanitize)
    _wire_mcp(monkeypatch)
    assert mcp_client.call("files", "read") == "ok"
    assert [d["name"] for d in mcp_client.discover(_fake_server())] == \
        ["mcp__files__do_thing"]


def test_flag_off_memory_import_never_consults_the_gate(
        gate_off, monkeypatch, tmp_path):
    monkeypatch.setattr(ingestgate, "check", _exploding_check)
    before = _seed_memory()
    archive = tmp_path / "mem.tar.gz"
    memory.export_memory(archive, user="alice")
    memory.delete_memory("alice")
    assert memory.import_memory(archive)["count"] == len(before)


def test_flag_off_records_no_refusal_evidence(gate_off, tmp_path):
    """Pass-through means *nothing* is recorded, not even for a payload the
    enforcing gate would refuse."""
    f = tmp_path / "SKILL.md"
    f.write_text(_skill_md(version=BAD_VERSION), encoding="utf-8")
    assert "Error" not in skillpack.import_file(str(f))
    assert ingestgate.refusals() == []


# ===========================================================================
# 2. FLAG ON => a valid artifact still installs/loads through every path.
# ===========================================================================

def test_flag_on_skill_import_still_works(gate_on, tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(SKILL_MD, encoding="utf-8")
    msg = skillpack.import_file(str(f))
    assert "Error" not in msg
    assert "Do the thing" in skills.read("Wired Skill")
    assert ingestgate.refusals() == []


def test_flag_on_remote_skill_text_import_still_works(gate_on):
    msg = skillpack._import_text(SKILL_MD, "https://example.test/SKILL.md",
                                 provisional=True)
    assert "Error" not in msg
    assert "<!-- provisional -->" in skills.read("Wired Skill")


def test_flag_on_skill_without_a_declared_version_still_installs(gate_on,
                                                                 tmp_path):
    """SKILL.md has no mandatory version field; the call site supplies the
    envelope version, so an ordinary community skill still imports."""
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: Plain\ndescription: d\n---\n# Plain\n\nbody\n",
                 encoding="utf-8")
    assert "Error" not in skillpack.import_file(str(f))


def test_flag_on_curated_packs_still_install(gate_on):
    assert len(skillpack.install_starter_pack()) == len(skillpack.STARTER_PACK)
    assert len(skillpack.install_security_pack()) == len(skillpack.SECURITY_PACK)
    assert ingestgate.refusals() == []


def test_flag_on_plugin_install_still_works(gate_on, tmp_path):
    f, digest = _plugin_file(tmp_path)
    res = pluginstore.install(str(f), sha256=digest)
    assert res["sha256"] == digest
    assert (pluginstore.plugins_dir() / f"{res['name']}.py").exists()
    assert pluginstore.list_installed()[0]["source"] == str(f)
    assert ingestgate.refusals() == []


def test_flag_on_mcp_call_and_discovery_still_work(gate_on, monkeypatch):
    _wire_mcp(monkeypatch)
    assert mcp_client.call("files", "read") == "ok"
    schemas = mcp_client.discover(_fake_server())
    assert [d["name"] for d in schemas] == ["mcp__files__do_thing"]
    assert schemas[0]["input_schema"] == {"type": "object", "properties": {}}


def test_flag_on_memory_import_still_restores_byte_for_byte(gate_on, tmp_path):
    before = _seed_memory()
    archive = tmp_path / "mem.tar.gz"
    memory.export_memory(archive, user="alice")
    memory.delete_memory("alice")
    result = memory.import_memory(archive)
    assert result["count"] == len(before) and result["verified"] == len(before)
    after = {p.relative_to(config.MEMORY_DIR).as_posix(): p.read_bytes()
             for r in memory._memory_roots("alice") if r.exists()
             for p in sorted(r.rglob("*")) if p.is_file()}
    assert after == before


# ===========================================================================
# 3. FLAG ON => a malformed artifact is REFUSED at every path (fail-closed),
#    with retained evidence and nothing written.
# ===========================================================================

@pytest.mark.parametrize("bad,marker", [
    (dict(body=CONTROL_CHAR_INSTRUCTIONS), "control_chars"),
    (dict(body=NUL_INSTRUCTIONS), "nul_byte"),
    (dict(version=BAD_VERSION), "malformed_version"),
    (dict(version=UNKNOWN_MAJOR), "unknown_major_version"),
])
def test_flag_on_skill_import_is_refused(gate_on, tmp_path, bad, marker):
    f = tmp_path / "SKILL.md"
    f.write_text(_skill_md(name="Bad Skill", **bad), encoding="utf-8")
    msg = skillpack.import_file(str(f))
    assert msg.startswith("Error: refused to import")
    assert "ingestion gate refused it" in msg and marker in msg
    # nothing written: the skill does not exist
    assert "No skill named" in skills.read("Bad Skill")
    assert skills.list_all() == []
    # retained refusal evidence (W2-I5.3)
    assert "skill" in _refusal_kinds()


def test_refused_skill_import_evidence_is_retained_and_referenced(gate_on,
                                                                  tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(_skill_md(name="Bad Skill", version=BAD_VERSION),
                 encoding="utf-8")
    msg = skillpack.import_file(str(f))
    rows = ingestgate.refusals()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "skill" and row["reasons"] == ["malformed_version"]
    assert row["evidence_ref"] in msg            # the record is the receipt
    assert row["sha256"] and "signed" in row


def test_flag_on_remote_skill_text_import_is_refused(gate_on):
    msg = skillpack._import_text(_skill_md(name="Bad", body=NUL_INSTRUCTIONS),
                                 "https://example.test/SKILL.md",
                                 provisional=True)
    assert msg.startswith("Error: refused to import")
    assert skills.list_all() == []
    assert "skill" in _refusal_kinds()


def test_flag_on_plugin_install_is_refused(gate_on, tmp_path):
    """Non-UTF-8 plugin bytes (corpus: invalid_utf8_plugin) pass every existing
    check — the hash matches — and are stopped by the gate."""
    f, digest = _plugin_file(tmp_path, body=b"# x\n" + INVALID_UTF8_CODE)
    with pytest.raises(ValueError, match="ingestion gate refused"):
        pluginstore.install(str(f), sha256=digest)
    assert pluginstore.list_installed() == []
    assert list(pluginstore.plugins_dir().glob("*.py")) == []
    assert "plugin" in _refusal_kinds()


def test_flag_on_plugin_pinned_hash_is_verified_by_the_gate(gate_on, tmp_path):
    """The operator's pinned sha256 is handed to the gate as the artifact's
    declared hash (corpus: declared_hash_mismatch_plugin) — the gate does the
    cross-check, the wiring does not duplicate it."""
    f, digest = _plugin_file(tmp_path)
    body = f.read_bytes()
    artifact = {"version": "1", "name": "myplugin",
                "code": body.decode("utf-8"), "sha256": "0" * 64,
                "size": len(body)}
    with pytest.raises(ingestgate.IngestRefused) as err:
        ingestgate.check("plugin", artifact, source=str(f),
                         provenance={"source": str(f),
                                     "sha256": ingestgate.payload_sha256(artifact)})
    assert "declared_hash_mismatch" in err.value.reasons
    # and the live path still refuses a mismatched pin before any write
    with pytest.raises(ValueError):
        pluginstore.install(str(f), sha256="deadbeef")
    assert pluginstore.list_installed() == []
    assert digest


def test_flag_on_memory_import_is_refused(gate_on, tmp_path):
    """A manifest whose `files` is the wrong type (corpus discipline:
    wrong_type_skill_instructions) is refused before a byte is restored."""
    archive = tmp_path / "bad.tar.gz"
    _make_archive(archive, {"schema_version": memory.ARCHIVE_SCHEMA_VERSION,
                            "files": WRONG_TYPE_VALUE},
                  files=[("lessons/x.md", b"payload")])
    with pytest.raises(ValueError, match="ingestion gate refused"):
        memory.import_memory(archive)
    assert not (config.MEMORY_DIR / "lessons").exists()
    assert "memory_import" in _refusal_kinds()


def test_flag_on_memory_import_refuses_a_nul_bearing_manifest(gate_on, tmp_path):
    archive = tmp_path / "nul.tar.gz"
    _make_archive(archive,
                  {"schema_version": memory.ARCHIVE_SCHEMA_VERSION,
                   "files": [{"path": "lessons/x.md", "sha256": "",
                              "note": NUL_INSTRUCTIONS}]},
                  files=[("lessons/x.md", b"payload")])
    with pytest.raises(ValueError, match="ingestion gate refused"):
        memory.import_memory(archive)
    assert not (config.MEMORY_DIR / "lessons").exists()
    assert ingestgate.refusals()[-1]["reasons"] == ["nul_byte"]


def test_flag_on_mcp_payload_failing_the_schema_is_never_executed(
        gate_on, monkeypatch):
    """An out-of-contract payload shape (a non-str/dict/list `result`, the
    corpus's wrong-type discipline) is refused before it is acted on: the
    caller gets a refusal string, never the payload."""
    with pytest.raises(ingestgate.IngestRefused) as err:
        mcp_client._gate_ephemeral("files", "read", WRONG_TYPE_VALUE)
    assert "wrong_type:result" in err.value.reasons

    _wire_mcp(monkeypatch)
    monkeypatch.setattr(mcp_client, "_render", lambda r: WRONG_TYPE_VALUE)
    out = mcp_client.call("files", "read")
    assert out.startswith("Error: the payload from MCP tool")
    assert "refused by the ingestion gate" in out
    assert str(WRONG_TYPE_VALUE) not in out.split("refused by")[0]


def test_flag_on_refused_discovery_registers_no_tools(gate_on, monkeypatch):
    """A refusal on the discovery payload is fail-closed: no tools, no cached
    schemas, and no half-registered trust kinds."""
    _wire_mcp(monkeypatch)

    def _refuse(kind, payload, **kw):
        raise ingestgate.IngestRefused(kind, ["simulated_refusal"], "ing-x")

    monkeypatch.setattr(ingestgate, "sanitize_ephemeral", _refuse)
    assert mcp_client.discover(_fake_server()) == []
    assert mcp_client._KIND_CACHE == {}
    assert mcp_client._SCHEMA_CACHE == {}
    assert mcp_client.kind_of("mcp__files__do_thing") is None


# ===========================================================================
# 4. Persistent vs ephemeral: the call sites route to the right half.
# ===========================================================================

def test_mcp_path_never_uses_the_persistent_check(gate_on, monkeypatch):
    """The MCP payload is ephemeral: sanitize + authoritative schema, never the
    reject-never-repair path."""
    monkeypatch.setattr(ingestgate, "check", _exploding_check)
    _wire_mcp(monkeypatch)
    assert mcp_client.call("files", "read") == "ok"
    assert len(mcp_client.discover(_fake_server())) == 1


def test_mcp_path_uses_sanitize_then_assert_schema(gate_on, monkeypatch):
    """Both ephemeral halves are used, in order: sanitation makes the payload
    safe to look at, the authoritative schema makes it safe to act on."""
    order: list[str] = []
    real_sanitize = ingestgate.sanitize_ephemeral
    real_assert = ingestgate.assert_schema

    def spy_sanitize(kind, payload, *, rules):
        order.append(f"sanitize:{kind}")
        return real_sanitize(kind, payload, rules=rules)

    def spy_assert(value, schema, *, kind=""):
        order.append(f"schema:{kind}")
        return real_assert(value, schema, kind=kind)

    monkeypatch.setattr(ingestgate, "sanitize_ephemeral", spy_sanitize)
    monkeypatch.setattr(ingestgate, "assert_schema", spy_assert)
    _wire_mcp(monkeypatch)
    mcp_client.call("files", "read")
    assert order == ["sanitize:mcp_payload", "schema:mcp_payload"]


def test_mcp_sanitation_continues_rather_than_refusing(gate_on, monkeypatch):
    """Ephemeral doctrine: a control character in a provider response is
    sanitized and the call continues (the durable half would refuse it)."""
    _wire_mcp(monkeypatch, result=_FakeResult(text=f"a{chr(7)}b"))
    assert mcp_client.call("files", "read") == "ab"
    assert ingestgate.refusals() == []
    # the same bytes in a DURABLE artifact are refused, not repaired
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.check("skill", {"version": "1", "name": "x",
                                   "instructions": f"a{chr(7)}b"})


@pytest.mark.parametrize("kind", ["skill", "plugin", "memory_import",
                                  "config_bundle", "session_import"])
def test_persistent_kinds_can_never_be_sanitized(gate_on, kind):
    """W2-I5.2 at the wiring level: no call site can route a durable kind
    through the ephemeral repair path — the gate refuses it outright."""
    with pytest.raises(ingestgate.IngestRefused) as err:
        ingestgate.sanitize_ephemeral(kind, {"version": "1"},
                                      rules={"strip_control": True})
    assert "ephemeral_sanitation_on_persistent_kind" in err.value.reasons
    assert kind in _refusal_kinds()


def test_persistent_paths_never_use_the_ephemeral_route(
        gate_on, monkeypatch, tmp_path):
    """Skills, plugins and memory imports go through check() only."""
    monkeypatch.setattr(ingestgate, "sanitize_ephemeral", _exploding_sanitize)

    f = tmp_path / "SKILL.md"
    f.write_text(SKILL_MD, encoding="utf-8")
    assert "Error" not in skillpack.import_file(str(f))

    pf, digest = _plugin_file(tmp_path)
    assert pluginstore.install(str(pf), sha256=digest)["sha256"] == digest

    _seed_memory()
    archive = tmp_path / "mem.tar.gz"
    memory.export_memory(archive, user="alice")
    memory.delete_memory("alice")
    assert memory.import_memory(archive)["count"] >= 1


@pytest.mark.parametrize("kind", ["skill", "plugin", "memory_import"])
def test_each_wired_persistent_kind_is_registered(kind):
    """W2-I5.1 fail-closed: a wired kind that is not in the registry would be
    refused wholesale, so the wiring's kind names are asserted, not assumed."""
    assert ingestgate.is_registered(kind) and ingestgate.is_persistent(kind)


def test_the_mcp_kind_is_registered_and_ephemeral():
    assert ingestgate.is_registered("mcp_payload")
    assert not ingestgate.is_persistent("mcp_payload")


# ===========================================================================
# 5. Partial-application guard: a refusal leaves no half-applied state.
# ===========================================================================

def test_a_refused_pack_entry_installs_nothing(gate_on, monkeypatch):
    """One bad skill in a curated pack means the WHOLE pack is refused — a
    refusal mid-install must never leave a half-installed pack."""
    good = dict(skillpack.STARTER_PACK[0])
    bad = {"name": "poisoned", "description": "d",
           "instructions": CONTROL_CHAR_INSTRUCTIONS}
    monkeypatch.setattr(skillpack, "STARTER_PACK", (good, bad))
    msgs = skillpack.install_starter_pack()
    assert len(msgs) == 1 and msgs[0].startswith("Error: refused to install")
    assert "Nothing was installed" in msgs[0]
    assert skills.list_all() == []                      # not even the good one
    assert "skill" in _refusal_kinds()


def test_a_refused_security_pack_entry_installs_nothing(gate_on, monkeypatch):
    bad = {"name": "poisoned", "description": "d",
           "instructions": "fine", "version": BAD_VERSION}
    monkeypatch.setattr(skillpack, "SECURITY_PACK",
                        (dict(skillpack.SECURITY_PACK[0]), bad))
    msgs = skillpack.install_security_pack()
    assert len(msgs) == 1 and "Nothing was installed" in msgs[0]
    assert skills.list_all() == []


def test_a_refused_plugin_leaves_no_file_and_no_manifest_entry(gate_on,
                                                               tmp_path):
    f, digest = _plugin_file(tmp_path, body=b"# x\n" + INVALID_UTF8_CODE)
    with pytest.raises(ValueError):
        pluginstore.install(str(f), sha256=digest)
    assert list(pluginstore.plugins_dir().glob("*.py")) == []
    assert pluginstore.list_installed() == []
    assert not (pluginstore.plugins_dir() / ".manifest.json").exists()


def test_a_refused_plugin_does_not_disturb_an_earlier_install(gate_on,
                                                              tmp_path):
    good, good_digest = _plugin_file(tmp_path, body=b"# good\n",
                                     name="good.py")
    pluginstore.install(str(good), sha256=good_digest)
    bad, bad_digest = _plugin_file(tmp_path, body=b"# x\n" + INVALID_UTF8_CODE,
                                   name="bad.py")
    with pytest.raises(ValueError):
        pluginstore.install(str(bad), sha256=bad_digest)
    installed = pluginstore.list_installed()
    assert [p["name"] for p in installed] == ["good"]
    assert pluginstore.verify() == [{"name": "good", "status": "ok",
                                     "expected": good_digest,
                                     "actual": good_digest}]


def test_a_refused_memory_import_restores_nothing(gate_on, tmp_path):
    """The manifest is gated before the restore loop, so a refusal cannot leave
    a partially restored MEMORY_DIR."""
    archive = tmp_path / "bad.tar.gz"
    _make_archive(archive,
                  {"schema_version": memory.ARCHIVE_SCHEMA_VERSION,
                   "files": [{"path": "lessons/a.md", "sha256": ""},
                             {"path": "lessons/b.md", "sha256": "",
                              "note": NUL_INSTRUCTIONS}]},
                  files=[("lessons/a.md", b"one"), ("lessons/b.md", b"two")])
    with pytest.raises(ValueError, match="ingestion gate refused"):
        memory.import_memory(archive)
    assert not (config.MEMORY_DIR / "lessons" / "a.md").exists()
    assert not (config.MEMORY_DIR / "lessons" / "b.md").exists()


def test_a_refused_skill_import_writes_no_partial_file(gate_on, tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(_skill_md(name="Half Written", body=NUL_INSTRUCTIONS),
                 encoding="utf-8")
    assert skillpack.import_file(str(f)).startswith("Error")
    assert skills.count() == 0
