"""Adversarial unit tests for the GHCR runtime-image digest gate (v7).

WHY THIS GATE EXISTS
--------------------
`pypa/gh-action-pypi-publish` is a COMPOSITE action. At the pinned commit
its `create-docker-action.py` computes the image it will run:

    if repo_id == REPO_ID_GH_ACTION ('178055147'):   # the action's OWN repo
        return <checked-out Dockerfile>
    return f'docker://ghcr.io/{repo}:{ref}'          # EVERY other consumer

`REPO_ID` is the CONSUMER's `github.repository_id`, so this repository takes
the second branch: it pulls a PREBUILT GHCR image addressed by a SHA-NAMED
TAG. It does not build the Dockerfile and does not resolve `python:3.13-slim`
at consumer run time — that base is baked in at PyPA's image-build time.

The residual risk is therefore a MUTABLE TAG, not a floating base image: a
tag is a pointer, and whoever controls the GHCR package can repoint it. This
gate resolves that tag to its manifest digest and fails closed on any change.

NO TEST HERE TOUCHES THE NETWORK. Every case drives an injected transport,
because a gate that only works when GHCR is reachable cannot be proven in CI
and a silently skipped supply-chain test is indistinguishable from a passing
one.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import release_pipeline as rp  # noqa: E402

# The audited identity, restated literally so a silent edit to the module
# constants fails here rather than shipping.
_IMAGE_REPO = "pypa/gh-action-pypi-publish"
_IMAGE_TAG = "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
_GOOD_DIGEST = ("sha256:a68d05519f6d7e47372aeaddab80b851b69afa89be179ec4"
                "1775c72c4e3ab2d5")
_OTHER_DIGEST = ("sha256:5c2f7030fbef8308068eb4cc9080fd3c9e157ccf6d511924"
                 "d69f8f4b23dc95c1")
_TOKEN = "ghcr-anonymous-pull-token-DISPOSABLE-abcdef0123456789"


# --- injected transport --------------------------------------------------------

class _FakeHeaders:
    def __init__(self, pairs):
        self._pairs = list(pairs)

    def get_all(self, name, failobj=None):
        hits = [v for k, v in self._pairs if k.lower() == name.lower()]
        return hits or failobj

    def get(self, name, failobj=None):
        hits = self.get_all(name)
        return hits[0] if hits else failobj


class _FakeResponse:
    def __init__(self, *, status=200, headers=(), body=b""):
        self.status = status
        self.headers = _FakeHeaders(headers)
        self._body = body

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Transport:
    """Records every request and replies from a scripted queue."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.replies:
            raise AssertionError("transport called more times than scripted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _token_reply(token=_TOKEN):
    return _FakeResponse(body=json.dumps({"token": token}).encode())


def _manifest_reply(digest=_GOOD_DIGEST, *, status=200, extra=()):
    headers = [("Content-Type",
                "application/vnd.docker.distribution.manifest.v2+json")]
    if digest is not None:
        headers.append(("Docker-Content-Digest", digest))
    headers.extend(extra)
    return _FakeResponse(status=status, headers=headers, body=b"{}")


def _install(monkeypatch, *replies):
    transport = _Transport(*replies)
    monkeypatch.setattr(rp, "_open_url", transport)
    return transport


# --- the pinned identity is exactly the audited one ----------------------------

def test_the_module_pins_the_audited_image_identity():
    assert rp.RUNTIME_IMAGE_REGISTRY == "ghcr.io"
    assert rp.RUNTIME_IMAGE_REPOSITORY == _IMAGE_REPO
    assert rp.RUNTIME_IMAGE_REFERENCE == _IMAGE_TAG
    assert rp.RUNTIME_IMAGE_DIGEST == _GOOD_DIGEST


def test_the_pinned_reference_is_the_pinned_action_commit():
    """The GHCR tag IS the action commit — that is why pinning the `uses:`
    SHA determines which image a consumer run pulls."""
    workflow = (_REPO / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8")
    assert f"pypa/gh-action-pypi-publish@{_IMAGE_TAG}" in workflow


# --- 1. the happy path ---------------------------------------------------------

def test_correct_digest_is_accepted(monkeypatch):
    transport = _install(monkeypatch, _token_reply(), _manifest_reply())
    assert rp.check_runtime_image() == _GOOD_DIGEST
    assert len(transport.requests) == 2


def test_the_manifest_request_sends_explicit_oci_and_docker_media_types(
        monkeypatch):
    transport = _install(monkeypatch, _token_reply(), _manifest_reply())
    rp.check_runtime_image()
    manifest_request = transport.requests[1][0]
    accept = manifest_request.get_header("Accept") or ""
    for media_type in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ):
        assert media_type in accept, f"missing accepted media type: {media_type}"


def test_the_request_targets_only_the_allowed_registry_and_repository(
        monkeypatch):
    transport = _install(monkeypatch, _token_reply(), _manifest_reply())
    rp.check_runtime_image()
    token_url = transport.requests[0][0].full_url
    manifest_url = transport.requests[1][0].full_url
    assert token_url.startswith("https://ghcr.io/")
    assert manifest_url == (
        f"https://ghcr.io/v2/{_IMAGE_REPO}/manifests/{_IMAGE_TAG}")


def test_both_requests_are_bounded_by_a_timeout(monkeypatch):
    transport = _install(monkeypatch, _token_reply(), _manifest_reply())
    rp.check_runtime_image()
    for _request, timeout in transport.requests:
        assert isinstance(timeout, (int, float))
        assert 0 < timeout <= 60, "the registry timeout must be bounded"


def test_the_pull_token_is_sent_as_a_bearer_credential(monkeypatch):
    transport = _install(monkeypatch, _token_reply(), _manifest_reply())
    rp.check_runtime_image()
    assert transport.requests[1][0].get_header(
        "Authorization") == f"Bearer {_TOKEN}"


# --- 2. wrong digest -----------------------------------------------------------

def test_wrong_digest_is_rejected(monkeypatch):
    _install(monkeypatch, _token_reply(), _manifest_reply(_OTHER_DIGEST))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert "repointed" in str(err.value).lower()


def test_a_repointed_tag_never_echoes_either_digest(monkeypatch):
    """Module convention: messages name the KIND, never the value."""
    _install(monkeypatch, _token_reply(), _manifest_reply(_OTHER_DIGEST))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert _OTHER_DIGEST not in message
    assert _GOOD_DIGEST not in message


# --- 3. missing digest ---------------------------------------------------------

def test_missing_digest_header_is_rejected(monkeypatch):
    _install(monkeypatch, _token_reply(), _manifest_reply(None))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert "docker-content-digest" in str(err.value).lower()


# --- 4. malformed digests ------------------------------------------------------

@pytest.mark.parametrize("digest", [
    "",
    "sha256:",
    "sha256:deadbeef",                                   # short
    "sha256:" + "a" * 63,                                # one short
    "sha256:" + "a" * 65,                                # one long
    "sha256:A68D05519F6D7E47372AEADDAB80B851B69AFA89BE179EC41775C72C4E3AB2D5",
    _GOOD_DIGEST.upper(),
    "SHA256:" + "a" * 64,                                # uppercase algorithm
    "sha512:" + "a" * 64,                                # wrong algorithm
    "a" * 64,                                            # no algorithm prefix
    "sha256:" + "g" * 64,                                # non-hex
    f" {_GOOD_DIGEST} extra",
])
def test_malformed_digest_is_rejected(monkeypatch, digest):
    _install(monkeypatch, _token_reply(), _manifest_reply(digest))
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_runtime_image()


def test_a_malformed_digest_is_not_echoed(monkeypatch):
    poison = "sha256:" + "Z" * 64
    _install(monkeypatch, _token_reply(), _manifest_reply(poison))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert poison not in str(err.value)


# --- 5. conflicting digest headers ---------------------------------------------

def test_conflicting_digest_headers_are_rejected(monkeypatch):
    _install(monkeypatch, _token_reply(),
             _manifest_reply(_GOOD_DIGEST,
                             extra=[("Docker-Content-Digest", _OTHER_DIGEST)]))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert "conflicting" in str(err.value).lower()


def test_duplicate_but_identical_digest_headers_are_accepted(monkeypatch):
    """Not a conflict: the same value twice still names one manifest."""
    _install(monkeypatch, _token_reply(),
             _manifest_reply(_GOOD_DIGEST,
                             extra=[("Docker-Content-Digest", _GOOD_DIGEST)]))
    assert rp.check_runtime_image() == _GOOD_DIGEST


# --- 6. redirects: EVERY one is refused ----------------------------------------
#
# A host-equality check is not enough. `https -> http` on the same host
# strips TLS; a port change reaches a different listener; even an identical
# URL is a second request we did not audit. The Authorization header must
# never travel to any of them, so nothing is followed.

_SOURCE_URL = "https://ghcr.io/v2/pypa/gh-action-pypi-publish/manifests/abc"

_REDIRECT_TARGETS = {
    "cross-host": "https://evil.example.com/v2/x/manifests/y",
    "cross-host-lookalike": "https://ghcr.io.evil.example.com/v2/x",
    "https-to-http": "http://ghcr.io/v2/pypa/gh-action-pypi-publish/manifests/abc",
    "alternate-port": "https://ghcr.io:8443/v2/pypa/gh-action-pypi-publish/manifests/abc",
    "same-host-different-path": "https://ghcr.io/v2/other/manifests/abc",
    "same-host-same-path": _SOURCE_URL,
    "scheme-downgrade-other-host": "http://evil.example.com/x",
    "protocol-relative-ish": "https://ghcr.io@evil.example.com/v2/x",
}

_REDIRECT_CODES = (301, 302, 303, 307, 308)


@pytest.mark.parametrize("code", _REDIRECT_CODES)
@pytest.mark.parametrize("label", sorted(_REDIRECT_TARGETS))
def test_every_redirect_status_and_target_is_rejected(code, label):
    handler = rp._RejectAllRedirects()
    request = urllib.request.Request(_SOURCE_URL)
    with pytest.raises(rp._RedirectRejected):
        handler.redirect_request(request, None, code, "Redirect", {},
                                 _REDIRECT_TARGETS[label])


@pytest.mark.parametrize("code", _REDIRECT_CODES)
def test_each_http_error_handler_refuses_rather_than_following(code):
    """urllib dispatches by status; every entry point must refuse."""
    handler = rp._RejectAllRedirects()
    request = urllib.request.Request(_SOURCE_URL)
    method = getattr(handler, f"http_error_{code}")
    with pytest.raises(rp._RedirectRejected):
        method(request, None, code, "Redirect", {})


def test_the_redirect_exception_carries_no_target():
    handler = rp._RejectAllRedirects()
    request = urllib.request.Request(_SOURCE_URL)
    try:
        handler.redirect_request(request, None, 302, "Found", {},
                                 _REDIRECT_TARGETS["cross-host"])
    except rp._RedirectRejected as exc:
        assert exc.args == (), "the redirect target must not be captured"
        assert "evil.example.com" not in repr(exc)
    else:
        raise AssertionError("the redirect was not rejected")


def test_no_same_host_redirect_escape_hatch_remains():
    """The v7 helpers permitted same-host redirects; they must be gone."""
    assert not hasattr(rp, "_SameHostOnlyRedirect")
    assert not hasattr(rp, "_CrossHostRedirect")


@pytest.mark.parametrize("label", sorted(_REDIRECT_TARGETS))
def test_a_redirect_becomes_a_sanitized_release_error(monkeypatch, label):
    target = _REDIRECT_TARGETS[label]
    _install(monkeypatch, rp._RedirectRejected())
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert "redirect" in message.lower()
    for fragment in ("evil.example.com", "ghcr.io", "8443", target):
        assert fragment not in message, f"{fragment!r} leaked into the error"


def test_authorization_is_never_forwarded_after_a_redirect(monkeypatch):
    """The token request succeeds; the manifest request is redirected. No
    second request may be issued, so the credential cannot follow."""
    transport = _install(monkeypatch, _token_reply(),
                         rp._RedirectRejected())
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_runtime_image()
    assert len(transport.requests) == 2, "exactly the two audited requests"
    assert not transport.replies, "no further request may be attempted"
    # Every request that was issued went to the audited endpoint; none was
    # re-issued at a redirect target.
    for request, _timeout in transport.requests:
        assert request.full_url.startswith(
            "https://ghcr.io/"), "a request escaped the audited registry"
    for label, target in _REDIRECT_TARGETS.items():
        assert all(request.full_url != target
                   for request, _ in transport.requests), (
            f"a request was issued to the {label} redirect target")


def test_the_opener_installs_the_reject_all_handler():
    import inspect as _inspect
    source = _inspect.getsource(rp._open_url)
    assert "_RejectAllRedirects" in source


# --- 7. wrong registry / repository / tag --------------------------------------

@pytest.mark.parametrize("repository", [
    "pypa/gh-action-pypi-publish-evil",
    "evil/gh-action-pypi-publish",
    "PyPA/gh-action-pypi-publish",
    "pypa/gh-action-pypi-publish/../../evil",
    "",
])
def test_a_repository_other_than_the_pinned_one_is_refused(monkeypatch,
                                                           repository):
    _install(monkeypatch)                       # transport must never be used
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image(repository=repository)
    assert "repositor" in str(err.value).lower()


@pytest.mark.parametrize("reference", [
    "v1.14.2",
    "latest",
    "main",
    "dc37677b2e1c63e2034f94d8a5b11f265b73ba34",      # one character off
    "",
])
def test_a_reference_other_than_the_pinned_commit_is_refused(monkeypatch,
                                                             reference):
    _install(monkeypatch)
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_runtime_image(reference=reference)


def test_the_registry_host_is_not_caller_controlled():
    """There is no parameter that could redirect this check at another
    registry: the host is a module constant."""
    import inspect as _inspect
    signature = _inspect.signature(rp.check_runtime_image)
    assert "registry" not in signature.parameters
    assert "host" not in signature.parameters
    assert "url" not in signature.parameters


def test_an_expected_digest_that_is_not_a_lowercase_sha256_is_refused(
        monkeypatch):
    _install(monkeypatch)
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_runtime_image(expected_digest="sha256:nope")


# --- 8. no token or authorization material may reach an error ------------------

def _assert_no_credential(message: str) -> None:
    lowered = message.lower()
    assert _TOKEN not in message, "the pull token leaked into an error"
    assert "bearer" not in lowered, "an Authorization header leaked"
    assert "authorization" not in lowered, "a header name leaked"


@pytest.mark.parametrize("reply", [
    _manifest_reply(_OTHER_DIGEST),
    _manifest_reply(None),
    _manifest_reply("sha256:bad"),
    _manifest_reply(status=500),
])
def test_no_error_after_token_acquisition_leaks_the_token(monkeypatch, reply):
    _install(monkeypatch, _token_reply(), reply)
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    _assert_no_credential(str(err.value))


def test_a_registry_failure_after_authentication_leaks_no_credential(
        monkeypatch):
    _install(monkeypatch, _token_reply(),
             urllib.error.HTTPError("https://ghcr.io/v2/x", 403, "Forbidden",
                                    {}, None))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    _assert_no_credential(str(err.value))


def test_the_token_is_never_returned_or_printed(monkeypatch, capsys):
    _install(monkeypatch, _token_reply(), _manifest_reply())
    result = rp.check_runtime_image()
    captured = capsys.readouterr()
    assert _TOKEN not in result
    assert _TOKEN not in captured.out and _TOKEN not in captured.err


# --- 9. network, TLS, JSON and HTTP failures are sanitized ---------------------

@pytest.mark.parametrize("failure", [
    TimeoutError("timed out"),
    urllib.error.URLError(TimeoutError("timed out")),
    urllib.error.URLError(ssl.SSLError("CERTIFICATE_VERIFY_FAILED")),
    ssl.SSLError("handshake failure for ghcr.io"),
    ssl.SSLCertVerificationError("self signed certificate"),
    ConnectionResetError("connection reset by peer"),
    OSError("network is unreachable"),
])
def test_transport_failures_become_release_check_errors(monkeypatch, failure):
    _install(monkeypatch, failure)
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert "ghcr.io" not in message, "the endpoint must not be echoed"
    assert "certificate" not in message.lower()


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 503])
def test_non_success_http_status_fails_closed(monkeypatch, status):
    _install(monkeypatch, _token_reply(), _manifest_reply(status=status))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert str(status) in str(err.value)


def test_an_http_error_is_reported_by_status_only(monkeypatch):
    _install(monkeypatch,
             urllib.error.HTTPError("https://ghcr.io/token?scope=secret", 418,
                                    "I am a teapot", {}, None))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert "418" in message
    assert "scope=secret" not in message and "teapot" not in message.lower()


@pytest.mark.parametrize("body", [
    b"not json at all",
    b"{",
    b"[]",
    b'{"no_token_here": 1}',
    b'{"token": ""}',
    b'{"token": null}',
    b'{"token": 12345}',
    b"\xff\xfe\x00bad utf-8",
])
def test_a_malformed_token_response_fails_closed(monkeypatch, body):
    _install(monkeypatch, _FakeResponse(body=body))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert "token" in str(err.value).lower() or "json" in str(err.value).lower()


def test_an_oversized_token_response_is_refused(monkeypatch):
    huge = json.dumps({"token": "a" * 200_000}).encode()
    _install(monkeypatch, _FakeResponse(body=huge))
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_runtime_image()


def test_the_check_never_shells_out_or_pulls_the_image():
    """`docker pull` would EXECUTE the image this gate exists to distrust."""
    source = (_REPO / "scripts" / "release_pipeline.py").read_text(
        encoding="utf-8")
    section = source[source.index("RUNTIME_IMAGE_REGISTRY"):]
    for forbidden in ("docker pull", "docker run", "subprocess.run(['docker'",
                      'subprocess.run(["docker"'):
        assert forbidden not in section


# --- 10. the bearer token is validated BEFORE it can reach a header ------------

# Every hostile token embeds a distinctive sentinel, so "did the value
# leak?" is a meaningful substring test. (A plain word like "token" is a
# substring of the error prose itself and would false-positive.)
_SENTINEL = "ZQSECRETQZ"

_HOSTILE_TOKENS = {
    "newline-header-injection": f"{_SENTINEL}\r\nX-Injected: 1",
    "bare-newline": f"{_SENTINEL}\n{_SENTINEL}",
    "carriage-return": f"{_SENTINEL}\r",
    "leading-space": f" {_SENTINEL}",
    "trailing-space": f"{_SENTINEL} ",
    "inner-space": f"{_SENTINEL} {_SENTINEL}",
    "tab": f"{_SENTINEL}\t",
    "form-feed": f"{_SENTINEL}\x0c",
    "null-byte": f"{_SENTINEL}\x00",
    "control-char": f"{_SENTINEL}\x07",
    "vertical-tab": f"{_SENTINEL}\x0b",
    "non-ascii": f"{_SENTINEL}é",
    "rtl-override": f"{_SENTINEL}‮",
    "comma": f"{_SENTINEL},",
    "quote": f'{_SENTINEL}"',
    "semicolon": f"{_SENTINEL};",
    "backslash": f"{_SENTINEL}\\",
    "empty": "",
    "equals-first": f"={_SENTINEL}",
    "inner-equals": f"{_SENTINEL}={_SENTINEL}",
}


@pytest.mark.parametrize("label", sorted(_HOSTILE_TOKENS))
def test_a_hostile_token_is_rejected_before_the_manifest_request(monkeypatch,
                                                                 label):
    hostile = _HOSTILE_TOKENS[label]
    transport = _install(monkeypatch, _token_reply(hostile))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    # Only the TOKEN request was made — the credential never reached a header.
    assert len(transport.requests) == 1, (
        "a rejected token must not produce an authenticated request")
    message = str(err.value)
    assert "token" in message.lower()
    assert _SENTINEL not in message, "the rejected token leaked"
    assert "X-Injected" not in message


@pytest.mark.parametrize("value", [None, 12345, 1.5, True, [], {}])
def test_a_non_string_token_is_rejected(monkeypatch, value):
    """These are the only non-string shapes JSON can actually deliver."""
    transport = _install(
        monkeypatch, _FakeResponse(body=json.dumps({"token": value}).encode()))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert "token" in str(err.value).lower()
    assert len(transport.requests) == 1, "no authenticated request may follow"


def test_an_over_long_token_is_rejected_without_echoing_it(monkeypatch):
    huge = "a" * (rp._MAX_TOKEN_CHARS + 1)
    transport = _install(monkeypatch, _token_reply(huge))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert str(rp._MAX_TOKEN_CHARS) in message
    assert huge not in message
    assert len(transport.requests) == 1


@pytest.mark.parametrize("token", [
    "abcDEF123", "a-b_c.d~e", "tok+en/slash", "padded==", "A" * 8192,
])
def test_well_formed_bearer_tokens_are_accepted(token):
    assert rp._validated_bearer_token(token) == token


def test_the_token_validator_never_echoes_the_value():
    for label, hostile in _HOSTILE_TOKENS.items():
        with pytest.raises(rp.ReleaseCheckError) as err:
            rp._validated_bearer_token(hostile)
        assert _SENTINEL not in str(err.value), f"{label} leaked"


# --- 11. failures raised while reading the response are sanitized --------------

class _ExplodingResponse(_FakeResponse):
    """Enters fine, then fails at the point the caller reads or inspects."""

    def __init__(self, failure, *, where="read", **kwargs):
        super().__init__(**kwargs)
        self._failure = failure
        self._where = where

    def read(self, amount=None):
        if self._where == "read":
            raise self._failure
        return super().read(amount)

    @property
    def headers(self):
        if self._where == "headers":
            raise self._failure
        return self._headers

    @headers.setter
    def headers(self, value):
        self._headers = value

    def close(self):
        if self._where == "close":
            raise self._failure


_READ_FAILURES = [
    TimeoutError("read timed out"),
    ConnectionResetError("peer reset mid-body"),
    ssl.SSLError("decryption failed for https://ghcr.io/token?scope=secret"),
    OSError("I/O error on https://ghcr.io"),
    RecursionError("maximum recursion depth exceeded"),
    MemoryError(),
    ValueError("unexpected body: BearerSECRET"),
]


@pytest.mark.parametrize("failure", _READ_FAILURES,
                         ids=lambda f: type(f).__name__)
def test_a_failure_during_read_is_sanitized(monkeypatch, failure):
    _install(monkeypatch, _ExplodingResponse(failure, where="read"))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert "ghcr.io" not in message, "the endpoint leaked from a read failure"
    assert "scope=secret" not in message
    assert "BearerSECRET" not in message
    _assert_no_credential(message)


@pytest.mark.parametrize("failure", _READ_FAILURES,
                         ids=lambda f: type(f).__name__)
def test_a_failure_while_reading_headers_is_sanitized(monkeypatch, failure):
    _install(monkeypatch, _token_reply(),
             _ExplodingResponse(failure, where="headers"))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert "ghcr.io" not in message
    _assert_no_credential(message)


def test_a_failure_while_closing_does_not_mask_or_leak(monkeypatch):
    """close() must never replace the real outcome nor raise on its own."""
    _install(monkeypatch,
             _ExplodingResponse(OSError("close failed for https://ghcr.io"),
                                where="close",
                                body=json.dumps({"token": _TOKEN}).encode()),
             _manifest_reply())
    assert rp.check_runtime_image() == _GOOD_DIGEST


def test_a_request_that_cannot_be_constructed_is_sanitized(monkeypatch):
    def explode(*_args, **_kwargs):
        raise ValueError("bad url https://ghcr.io/token?scope=secret")
    monkeypatch.setattr(rp.urllib.request, "Request", explode)
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    message = str(err.value)
    assert "scope=secret" not in message and "ghcr.io" not in message


def test_no_sanitized_failure_carries_a_chained_cause(monkeypatch):
    """`raise ... from None`: a chained traceback would print the original
    exception, whose text may quote the URL or the credential."""
    _install(monkeypatch, _token_reply(),
             urllib.error.HTTPError("https://ghcr.io/v2/x?scope=secret", 403,
                                    f"Bearer {_TOKEN}", {}, None))
    with pytest.raises(rp.ReleaseCheckError) as err:
        rp.check_runtime_image()
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__ is True


# --- the CLI surface -----------------------------------------------------------

def test_the_cli_exposes_the_check_and_fails_closed(monkeypatch, capsys):
    _install(monkeypatch, _token_reply(), _manifest_reply(_OTHER_DIGEST))
    assert rp.main(["check-runtime-image"]) == 1
    assert "RELEASE CHECK FAILED" in capsys.readouterr().err


def test_the_cli_succeeds_on_the_audited_digest(monkeypatch, capsys):
    _install(monkeypatch, _token_reply(), _manifest_reply())
    assert rp.main(["check-runtime-image"]) == 0
    out = capsys.readouterr().out
    assert _TOKEN not in out


@pytest.mark.parametrize("scenario", [
    "redirect", "hostile-token", "read-failure", "http-error", "wrong-digest",
])
def test_the_cli_returns_1_without_credential_or_endpoint_leakage(
        monkeypatch, capsys, scenario):
    if scenario == "redirect":
        _install(monkeypatch, _token_reply(), rp._RedirectRejected())
    elif scenario == "hostile-token":
        _install(monkeypatch, _token_reply("bad\r\nX-Injected: 1"))
    elif scenario == "read-failure":
        _install(monkeypatch,
                 _ExplodingResponse(OSError("boom https://ghcr.io/token"),
                                    where="read"))
    elif scenario == "http-error":
        _install(monkeypatch,
                 urllib.error.HTTPError("https://ghcr.io/token?scope=secret",
                                        403, "Forbidden", {}, None))
    else:
        _install(monkeypatch, _token_reply(), _manifest_reply(_OTHER_DIGEST))

    assert rp.main(["check-runtime-image"]) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "RELEASE CHECK FAILED" in captured.err
    for fragment in (_TOKEN, "ghcr.io", "scope=secret", "Bearer",
                     "Authorization", "X-Injected"):
        assert fragment not in combined, f"{fragment!r} leaked from the CLI"
