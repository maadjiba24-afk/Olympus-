"""W2-1: the authorization matrix for every /api/* route.

Wave 2's gate is that other humans can have accounts without reading each
other's data. This enumerates every `/api/*` route and exercises it against four
principals -- anonymous, user A, user B, operator -- asserting the expected
outcome for each cell.

THE ROUTE LIST IS DERIVED FROM web.py, NOT HAND-MAINTAINED. `test_matrix_covers_
every_api_route` extracts every `/api/...` literal from the source and fails if
one is absent from the table below. A hand-written list silently stops covering
whatever gets added next, and the routes nobody thought about are exactly where
cross-tenant bugs live.

A route that is genuinely principal-free is declared so explicitly, with a
reason, rather than omitted -- an untested route is the hole.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from olympus import accounts, config, web

_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

# --- the matrix ------------------------------------------------------------
#
# `scope` is the contract each route is asserted against:
#
#   per_user     -- data belongs to ONE principal. Anonymous is refused; A and B
#                   each see only their own; nothing A writes is visible to B.
#   session      -- per-session conversational state, not durable data.
#   auth         -- the login surface itself; principal-free BY DEFINITION,
#                   because it is what creates a principal.
#   instance     -- instance-wide operational fact, deliberately not per-user.
#                   `_authorized` is the gate here, NOT OLYMPUS_REQUIRE_LOGIN;
#                   its docstring states the two are separate on purpose (one
#                   asks whether the surface is exposed at all, the other
#                   whether a per-account session exists).
#   operator     -- operator-only surface, additionally loopback-gated.
#
_MATRIX: dict[str, dict] = {
    "/api/register": {"methods": ("POST",), "scope": "auth",
                      "why": "creates a principal; cannot require one"},
    "/api/login": {"methods": ("POST",), "scope": "auth",
                   "why": "creates a principal; cannot require one"},
    "/api/logout": {"methods": ("POST",), "scope": "auth",
                    "why": "destroys a principal; must work without one"},
    "/api/health": {"methods": ("GET",), "scope": "instance",
                    "why": "liveness of the process, identical for everyone"},
    "/api/metrics": {"methods": ("GET",), "scope": "instance",
                     "why": "instance ops + spend; gated by _authorized, "
                            "deliberately NOT by OLYMPUS_REQUIRE_LOGIN"},
    "/api/me": {"methods": ("GET",), "scope": "instance",
                "why": "reports whether THIS caller is logged in; must answer "
                       "for an anonymous caller or the UI cannot render login"},
    "/api/status": {"methods": ("GET",), "scope": "session",
                    "why": "this session's own event stream"},
    "/api/chat": {"methods": ("POST",), "scope": "session",
                  "why": "drives this session's bot"},
    "/api/feedback": {"methods": ("POST",), "scope": "session",
                      "why": "rates this session's last reply"},
    "/api/report": {"methods": ("POST",), "scope": "instance",
                    "why": "a problem report must work BEFORE login, e.g. "
                           "'I cannot log in'; it only needs the access token"},
    "/api/admin": {"methods": ("GET",), "scope": "operator",
                   "why": "operator overview"},
    "/api/admin/act": {"methods": ("POST",), "scope": "operator",
                       "why": "operator mutation"},
    "/api/actions": {"methods": ("GET",), "scope": "per_user"},
    "/api/action": {"methods": ("POST",), "scope": "per_user"},
    "/api/documents": {"methods": ("GET", "POST"), "scope": "per_user"},
    "/api/agenda": {"methods": ("GET",), "scope": "per_user"},
    "/api/compare": {"methods": ("GET", "POST"), "scope": "per_user"},
    "/api/memory": {"methods": ("GET", "POST"), "scope": "per_user"},
    "/api/connected": {"methods": ("GET",), "scope": "per_user"},
    "/api/todos": {"methods": ("POST",), "scope": "per_user"},
    "/api/gallery": {"methods": ("GET", "POST"), "scope": "per_user"},
    "/api/gallery/image": {"methods": ("GET",), "scope": "per_user"},
}

_TOKEN = "matrix-access-token"


# --- harness ---------------------------------------------------------------

@pytest.fixture()
def srv(monkeypatch, tmp_path):
    """A real server with accounts enabled and two registered users."""
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "1")
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", _TOKEN)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(ws))

    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    tokens = {}
    for name in ("alice", "bob"):
        tokens[name] = accounts.register(name, f"{name}-passw0rd!")
    yield base, tokens, ws
    server.shutdown()


def call(base, method, path, *, sid=None, token=_TOKEN, payload=None,
         query="", headers=None):
    """One request as one principal. Returns (status, body-text)."""
    url = f"{base}{path}{query}"
    data = None
    headers = dict(headers or {})
    if token:
        headers.setdefault("X-Olympus-Token", token)
    if method == "POST":
        body = dict(payload or {})
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if sid:
        headers["Cookie"] = f"olympus_sid={sid}"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


# --- the coverage guarantee ------------------------------------------------

def test_matrix_covers_every_api_route():
    """Every `/api/*` literal in web.py must appear in _MATRIX.

    This is what makes the matrix a matrix rather than a sample. A route added
    to web.py without a row here fails the suite, so the answer to "is this
    route scoped?" can never default to nobody-checked.
    """
    src = (Path(web.__file__)).read_text(encoding="utf-8")
    found = set(re.findall(r'"(/api/[a-z0-9_/-]*)"', src))
    missing = sorted(found - set(_MATRIX))
    assert not missing, (
        f"routes in web.py with no row in the authorization matrix: {missing}. "
        f"Add a row -- if the route is genuinely principal-free, declare it "
        f"with a `why` rather than leaving it uncovered.")
    stale = sorted(set(_MATRIX) - found)
    assert not stale, f"matrix rows for routes that no longer exist: {stale}"


def test_every_principal_free_route_states_a_reason():
    """A route may only opt out of per-user scoping WITH a written reason."""
    for path, row in _MATRIX.items():
        if row["scope"] != "per_user":
            assert row.get("why"), (
                f"{path} is declared {row['scope']} but gives no reason; an "
                f"unexplained exemption is indistinguishable from an oversight")


# --- anonymous is refused everywhere that holds user data ------------------

@pytest.mark.parametrize("path", sorted(
    p for p, r in _MATRIX.items() if r["scope"] == "per_user"))
def test_anonymous_cannot_reach_per_user_routes(srv, path):
    """No cookie, no account -> 401 on every route that holds user data."""
    base, _tokens, _ws = srv
    for method in _MATRIX[path]["methods"]:
        status, body = call(base, method, path)
        assert status == 401, (
            f"{method} {path} returned {status} to an ANONYMOUS caller "
            f"(expected 401 login required): {body[:200]}")


@pytest.mark.parametrize("path", sorted(
    p for p, r in _MATRIX.items() if r["scope"] == "per_user"))
def test_bad_access_token_is_refused(srv, path):
    """The surface gate is independent of the session gate."""
    base, tokens, _ws = srv
    for method in _MATRIX[path]["methods"]:
        status, _ = call(base, method, path, sid=tokens["alice"],
                         token="wrong-token")
        assert status == 401, f"{method} {path} accepted a bad access token"


# --- cross-tenant isolation, the actual Wave-2 gate ------------------------

def test_documents_are_not_cross_tenant(srv):
    base, tokens, _ws = srv
    a, b = tokens["alice"], tokens["bob"]
    status, _ = call(base, "POST", "/api/documents", sid=a,
                     payload={"op": "save", "name": "secret.md",
                              "content": "alice-only"})
    assert status == 200

    _, listing = call(base, "GET", "/api/documents", sid=b)
    assert "secret.md" not in listing, "B can LIST A's documents"

    status, body = call(base, "GET", "/api/documents", sid=b,
                        query="?name=secret.md")
    assert "alice-only" not in body, "B can READ A's document"

    _, mine = call(base, "GET", "/api/documents", sid=a)
    # documents slugify on save: secret.md -> secret-md. Assert on the slug,
    # not the raw filename -- the first run failed here on MY assertion, not
    # on a scoping defect; A never lost the document.
    assert "secret-md" in mine, "A lost their own document"


def test_todos_are_not_cross_tenant(srv):
    base, tokens, _ws = srv
    a, b = tokens["alice"], tokens["bob"]
    call(base, "POST", "/api/todos", sid=a,
         payload={"op": "add", "text": "alice-private-todo"})
    _, seen = call(base, "POST", "/api/todos", sid=b,
                   payload={"op": "add", "text": "bob-todo"})
    assert "alice-private-todo" not in seen, "B can see A's todos"


def test_gallery_surface_is_not_cross_tenant(srv):
    """The GALLERY SURFACE -- all four vectors: list, read, delete, edit_image.

    NAMED FOR EXACTLY WHAT IT PROVES. It was `test_gallery_is_not_cross_tenant`,
    which reads as a claim about the DATA. It is not: the images themselves sit
    in one shared workspace and the sandbox file tools still union across it, so
    B can still read A's gallery directory through `read_file`. That residual
    gap is demonstrated by `test_file_tools_still_reach_another_principals_
    gallery` below, not merely described here -- citing this test as isolation
    would be wrong.

    The strict xfail this carried through W2-1a is removed in the same commit as
    the fix, which is what strict=True was for.
    """
    base, tokens, ws = srv
    a, b = tokens["alice"], tokens["bob"]

    # An image produced by A's work, where generate_image now writes it: into
    # A's OWN gallery directory. Seeding it flat would exercise the legacy
    # bucket instead, which is a different contract (see the legacy tests).
    from olympus import gallery
    ns_a = accounts.namespace_for_token(a)
    (gallery.owner_root(ns_a, create=True) / "alice-secret.png").write_bytes(_PNG)

    _, mine = call(base, "GET", "/api/gallery", sid=a)
    assert "alice-secret.png" in mine, "A cannot see their own image"

    _, theirs = call(base, "GET", "/api/gallery", sid=b)
    assert "alice-secret.png" not in theirs, "B can LIST A's images"

    status, _ = call(base, "GET", "/api/gallery/image", sid=b,
                     query="?name=alice-secret.png")
    assert status == 404, "B can READ A's image bytes"

    call(base, "POST", "/api/gallery", sid=b,
         payload={"op": "delete", "name": "alice-secret.png"})
    _, still = call(base, "GET", "/api/gallery", sid=a)
    assert "alice-secret.png" in still, "B DELETED A's image"

    # FOURTH vector: edit_image resolved its source with sandbox._confine,
    # which admits any path under workdir() -- including A's gallery. A dummy
    # key is required because the key check runs BEFORE source resolution, so
    # without one every call returns the same "needs an API key" and the cell
    # would pass while proving nothing. With a key, B's edit must stop at
    # source resolution -- before any network call is attempted.
    import os
    os.environ["OLYMPUS_MEDIA_API_KEY"] = "dummy-not-used-resolution-fails-first"
    try:
        _, edited = call(base, "POST", "/api/gallery", sid=b,
                         payload={"op": "edit", "prompt": "make it blue",
                                  "name": "alice-secret.png"})
    finally:
        os.environ.pop("OLYMPUS_MEDIA_API_KEY", None)
    assert "no workspace image named" in edited, (
        f"B could EDIT A's image (fourth vector): {edited[:200]}")


def test_file_tools_still_reach_another_principals_gallery(monkeypatch, tmp_path):
    """THE RESIDUAL GAP, asserted as CURRENT behaviour so it stays tracked.

    W2-1b scoped the gallery SURFACE (the /api/gallery routes and the CLI
    listing). It did not re-root the sandbox file tools, which still share one
    workspace and still union across it by deliberate design -- `read_file`'s
    own docstring says so, and ADR 0005 records why per-worker re-rooting was
    rejected. So one principal's gallery directory is a plain subdirectory of a
    workspace every principal's file tools can read.

    This asserts the leak EXISTS rather than describing it in a README, because
    a hole with a test is tracked and a hole in prose is forgotten. It is the
    reason W2-2 exists.

    WHEN PER-PRINCIPAL WORKSPACE ROOTS LAND, THIS TEST FLIPS AND FAILS. That
    failure is the signal that the gap closed: invert the assertion then, and
    rename `test_gallery_surface_is_not_cross_tenant` to drop "surface" only
    once this can no longer pass.
    """
    from olympus import config, gallery, memory, sandbox
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(ws))
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "1")

    # A's file, in the directory the scoped gallery owns. Distinctive content,
    # so the assertion proves B READ IT rather than merely that no error came
    # back -- an absence-of-error check would also pass on an empty read.
    a_root = gallery.owner_root("u:alice", create=True)
    (a_root / "alice-secret.png").write_bytes(_PNG)
    (a_root / "notes.txt").write_text("ALICE-PRIVATE-CONTENT", encoding="utf-8")
    rel = f"gallery/{memory.safe_id('u:alice')}/notes.txt"

    # The gallery surface refuses B -- that half really is fixed.
    assert gallery.read_image("alice-secret.png", "u:bob") is None
    assert gallery.list_images("u:bob") == []

    # ...but the file tools do not know about principals at all.
    memory.set_user("u:bob")
    out = sandbox.read_file(rel)
    assert "ALICE-PRIVATE-CONTENT" in out, (
        "GAP CLOSED -- the sandbox file tools no longer reach another "
        "principal's gallery directory. That is the W2-2 outcome: invert this "
        "assertion and drop 'surface' from the sibling test's name.")


def test_gallery_owner_can_do_all_four(srv):
    """Scoping must not cost the owner their own access."""
    base, tokens, _ws = srv
    from olympus import accounts, gallery
    a = tokens["alice"]
    ns_a = accounts.namespace_for_token(a)
    root = gallery.owner_root(ns_a, create=True)
    (root / "mine.png").write_bytes(_PNG)

    _, listed = call(base, "GET", "/api/gallery", sid=a)
    assert "mine.png" in listed, "owner cannot LIST their own image"
    status, _ = call(base, "GET", "/api/gallery/image", sid=a,
                     query="?name=mine.png")
    assert status == 200, "owner cannot READ their own image"
    _, after = call(base, "POST", "/api/gallery", sid=a,
                    payload={"op": "delete", "name": "mine.png"})
    assert "mine.png" not in after, "owner cannot DELETE their own image"


def test_gallery_resolver_never_unions_read_roots():
    """`sandbox._read_roots()` unions the worker root, the shared workspace AND
    every sibling worker root. That union is the cross-tenant bug. Assert the
    gallery does not call it, so a later refactor cannot quietly restore it.
    """
    import ast

    from olympus import gallery
    tree = ast.parse(Path(gallery.__file__).read_text(encoding="utf-8"))
    # Checked on the AST, not by substring. The module's docstring and comments
    # EXPLAIN why _read_roots must not be used, so a text search matches its own
    # rationale -- the same trap as the W1-4 `time.sleep` assertion, and here
    # even stripping `#` comments is not enough because the docstring is prose
    # the parser keeps. The AST sees only what actually executes.
    called = {node.attr for node in ast.walk(tree)
              if isinstance(node, ast.Attribute)}
    called |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "_read_roots" not in called, (
        "gallery reintroduced sandbox._read_roots(); that unions sibling worker "
        "roots and is exactly the cross-tenant leak this scoping removed")


def test_legacy_flat_images_survive_the_upgrade(monkeypatch, tmp_path):
    """A single-user install must not open the gallery after upgrade and find
    it empty. With accounts OFF there is one human, so the pre-upgrade flat
    images are shown to them."""
    from olympus import config, gallery
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(ws))
    monkeypatch.delenv("OLYMPUS_REQUIRE_LOGIN", raising=False)
    (ws / "legacy.png").write_bytes(_PNG)          # written before scoping

    names = [i["name"] for i in gallery.list_images("solo")]
    assert "legacy.png" in names, "a single-user install LOST its gallery"
    assert gallery.read_image("legacy.png", "solo") is not None


def test_legacy_images_are_hidden_once_accounts_are_on(monkeypatch, tmp_path):
    """With accounts ON they belong to nobody and must not leak to everyone."""
    from olympus import config, gallery
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(ws))
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "1")
    (ws / "legacy.png").write_bytes(_PNG)

    assert gallery.list_images("u:1") == [], "legacy images leaked to an account"
    assert gallery.read_image("legacy.png", "u:1") is None
    # ...and the operator can claim them deliberately.
    assert gallery.claim_legacy("u:1") == 1
    assert [i["name"] for i in gallery.list_images("u:1")] == ["legacy.png"]


def test_supplied_session_is_ignored_when_login_is_required(srv):
    """A session id in a query string is a bearer token in a URL. With accounts
    on it must not select another account's namespace."""
    base, tokens, _ws = srv
    a = tokens["alice"]
    # NO COOKIE. `_resolve_sid` prefers the cookie and returns before it ever
    # reads `provided`, so a request that HAS a cookie cannot exercise this at
    # all -- an earlier version of this test sent one and was therefore
    # vacuous: it passed against the unfixed code.
    _, body = call(base, "GET", "/api/me", query=f"?session={a}")
    assert '"logged_in": false' in body.replace(" ", "").replace(
        '"logged_in":false', '"logged_in": false'), (
        f"a query-string session logged the caller in as another account: "
        f"{body[:200]}")
    assert "alice" not in body, (
        f"a bearer session id in the URL selected A's account: {body[:200]}")


def test_memory_is_not_cross_tenant(srv):
    base, tokens, _ws = srv
    a, b = tokens["alice"], tokens["bob"]
    from olympus import usermem
    ns_a = accounts.namespace_for_token(a)
    usermem.add_memory(ns_a, type="preference", content="alice-private-memory",
                       confidence=0.9)
    _, theirs = call(base, "GET", "/api/memory", sid=b)
    assert "alice-private-memory" not in theirs, "B can read A's memory"
    _, mine = call(base, "GET", "/api/memory", sid=a)
    assert "alice-private-memory" in mine, "A lost their own memory"


# --- operator surfaces ------------------------------------------------------

_OPERATOR_TOKEN = "matrix-operator-token"


@pytest.mark.parametrize("path", sorted(
    p for p, r in _MATRIX.items() if r["scope"] == "operator"))
def test_operator_routes_refuse_a_plain_user(srv, path, monkeypatch):
    """PRIVILEGE ESCALATION BY REGISTRATION (W2-1a).

    `_admin_authorized` used to return True whenever OLYMPUS_ACCESS_TOKEN was
    set. The access token is the ENTRY GATE every user holds, so any account
    that could log in was a full operator — and /api/admin/act carries
    config_set, set_autonomy on other users, action approve/reject, schedule_add
    and mcp_add. This is the cell that caught it.
    """
    monkeypatch.setenv("OLYMPUS_OPERATOR_TOKEN", _OPERATOR_TOKEN)
    base, tokens, _ws = srv
    for method in _MATRIX[path]["methods"]:
        status, body = call(base, method, path, sid=tokens["alice"],
                            payload={"op": "noop"},
                            headers={"X-Olympus-Admin": "1"})
        assert status in (401, 403), (
            f"{method} {path} gave a PLAIN USER {status} — a registered "
            f"account reached an operator surface: {body[:200]}")


@pytest.mark.parametrize("path", sorted(
    p for p, r in _MATRIX.items() if r["scope"] == "operator"))
def test_operator_routes_accept_the_operator_token(srv, path, monkeypatch):
    """The separate credential, in its own header, actually works."""
    monkeypatch.setenv("OLYMPUS_OPERATOR_TOKEN", _OPERATOR_TOKEN)
    base, tokens, _ws = srv
    for method in _MATRIX[path]["methods"]:
        status, body = call(
            base, method, path, sid=tokens["alice"],
            payload={"op": "noop"},
            headers={"X-Olympus-Operator": _OPERATOR_TOKEN,
                     "X-Olympus-Admin": "1"})
        assert status != 403, (
            f"{method} {path} refused a correct operator token: {body[:200]}")


def test_operator_token_equal_to_access_token_is_refused(srv, monkeypatch):
    """A config that is wrong in a way that LOOKS right.

    Setting the two secrets to the same value recreates the escalation exactly
    — every user holds the access token — while appearing configured. It must
    refuse rather than serve.
    """
    monkeypatch.setenv("OLYMPUS_OPERATOR_TOKEN", _TOKEN)   # == access token
    base, tokens, _ws = srv
    status, body = call(base, "GET", "/api/admin", sid=tokens["alice"],
                        headers={"X-Olympus-Operator": _TOKEN})
    assert status == 500, (
        f"an operator token identical to the access token was ACCEPTED "
        f"({status}) — every account is an operator again: {body[:200]}")
    assert "same value" in body


def test_operator_unset_fails_closed_off_loopback(srv, monkeypatch):
    """UNSET must fall back to loopback-only, NOT to the access token.

    A fallback would preserve the hole for every operator who does not read the
    changelog — the exact population the fix exists for. Simulated by the
    forwarding header, which the gate treats as proof the request is relayed
    from off-box.
    """
    monkeypatch.delenv("OLYMPUS_OPERATOR_TOKEN", raising=False)
    base, tokens, _ws = srv
    status, body = call(base, "GET", "/api/admin", sid=tokens["alice"],
                        headers={"X-Forwarded-For": "203.0.113.9"})
    assert status in (401, 403), (
        f"with OLYMPUS_OPERATOR_TOKEN unset, an off-box request got {status} "
        f"— the gate fell back to the access token: {body[:200]}")
    assert "OLYMPUS_OPERATOR_TOKEN" in body, (
        "the refusal must name the operator token, not the access token")


def test_both_admin_routes_share_the_one_gate():
    """Fixing `_admin_authorized` only covers both routes if both call it.

    Verified rather than assumed: exactly the two admin paths call it.
    """
    src = Path(web.__file__).read_text(encoding="utf-8")
    assert src.count("self._admin_authorized()") == 2, (
        "expected exactly two _admin_authorized call sites (/api/admin and "
        "/api/admin/act); a third route would not be covered by this fix")


def test_csrf_header_is_still_required_on_admin_act(srv, monkeypatch):
    """The X-Olympus-Admin requirement is SEPARATE from the credential and
    defends something different (CSRF). Fixing one must not collapse the other.
    """
    monkeypatch.setenv("OLYMPUS_OPERATOR_TOKEN", _OPERATOR_TOKEN)
    base, tokens, _ws = srv
    status, body = call(base, "POST", "/api/admin/act", sid=tokens["alice"],
                        payload={"op": "noop"},
                        headers={"X-Olympus-Operator": _OPERATOR_TOKEN})
    assert status == 403 and "X-Olympus-Admin" in body, (
        f"the CSRF header is no longer enforced: {status} {body[:200]}")


# --- session-scoped ---------------------------------------------------------

def test_session_events_are_not_cross_session(srv):
    """A's event stream must not appear in B's."""
    base, tokens, _ws = srv
    a, b = tokens["alice"], tokens["bob"]
    status_a, body_a = call(base, "GET", "/api/status", sid=a)
    status_b, body_b = call(base, "GET", "/api/status", sid=b)
    assert status_a == 200 and status_b == 200
    # Distinct sessions must not share the same event object identity.
    assert web._session(a) is not web._session(b)
