"""Matrix channel: /sync message extraction and access wiring. All HTTP is
avoided — the pure parse core is tested directly."""

from olympus import chanbase, matrix


def _sync(events, room="!r:s"):
    return {"next_batch": "s2", "rooms": {"join": {
        room: {"timeline": {"events": events}}}}}


def test_extract_text_messages():
    sync = _sync([
        {"type": "m.room.message", "sender": "@alice:s",
         "content": {"msgtype": "m.text", "body": "hello"}},
        {"type": "m.room.message", "sender": "@bot:s",       # self — skipped
         "content": {"msgtype": "m.text", "body": "my own"}},
        {"type": "m.room.message", "sender": "@bob:s",       # image — skipped
         "content": {"msgtype": "m.image", "body": "pic"}},
        {"type": "m.reaction", "sender": "@c:s", "content": {}},  # not a msg
    ])
    out = matrix.extract_messages(sync, self_id="@bot:s")
    assert out == [("!r:s", "@alice:s", "hello")]


def test_extract_skips_empty_and_missing_sender():
    sync = _sync([
        {"type": "m.room.message", "sender": "",
         "content": {"msgtype": "m.text", "body": "x"}},
        {"type": "m.room.message", "sender": "@a:s",
         "content": {"msgtype": "m.text", "body": "   "}},
    ])
    assert matrix.extract_messages(sync, self_id="@bot:s") == []


def test_extract_handles_empty_sync():
    assert matrix.extract_messages({}, self_id="@bot:s") == []
    assert matrix.extract_messages({"rooms": {}}, self_id="@bot:s") == []


def test_notify_unconfigured_returns_false(monkeypatch):
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
    assert matrix.notify("hi") is False


def test_matrix_uses_untrusted_default(monkeypatch):
    # A Matrix sender is subject to the same untrusted-by-default gate.
    monkeypatch.delenv("OLYMPUS_MATRIX_DM_POLICY", raising=False)
    monkeypatch.delenv("OLYMPUS_CHANNEL_DM_POLICY", raising=False)
    assert chanbase.allowed("matrix", "@stranger:server") is False


def test_token_reads_secretref(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text("syt_secret\n", encoding="utf-8")
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://h")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", f"file:{f}")
    base, token = matrix._cfg()
    assert base == "https://h" and token == "syt_secret"
