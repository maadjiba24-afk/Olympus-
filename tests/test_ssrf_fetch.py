"""SSRF: browse_page / _http_get / _web_fetch must refuse internal/metadata
hosts on the initial request AND on redirects. Regression for the audit finding
that browse_page bypassed the gate entirely and _web_fetch followed redirects.
"""

import io

import pytest

from olympus import media, tools

_INTERNAL = "http://169.254.169.254/latest/meta-data/"   # link-local, no DNS


def test_http_get_blocks_internal():
    with pytest.raises(ValueError):
        tools._http_get(_INTERNAL)


def test_web_fetch_blocks_internal():
    assert tools._web_fetch(_INTERNAL).startswith("Error")


def test_browse_page_blocks_internal():
    assert media.browse_page(_INTERNAL).startswith("Error")


def test_redirect_to_internal_is_refused():
    h = tools._SafeRedirectHandler()
    req = tools._urlreq.Request("http://93.184.216.34/")     # public literal IP
    with pytest.raises(tools._urlerr.HTTPError):
        h.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, _INTERNAL)


def test_non_http_scheme_blocked():
    with pytest.raises(ValueError):
        tools._http_get("file:///etc/passwd")
