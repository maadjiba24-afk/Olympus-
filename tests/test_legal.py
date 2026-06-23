"""Privacy + terms pages: present, honest, and operator-configurable."""

from olympus import legal


def test_privacy_page_covers_the_key_topics():
    html = legal.privacy_html()
    assert html.startswith("<!doctype html>")
    for topic in ("Privacy Policy", "encrypted", "export", "delete",
                  "opt-in", "approval"):
        assert topic.lower() in html.lower()


def test_terms_page_covers_the_key_topics():
    html = legal.terms_html()
    for topic in ("Terms", "as is", "Acceptable use", "BYOK", "Liability"):
        assert topic.lower() in html.lower()


def test_operator_details_are_injected(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OPERATOR_NAME", "Acme Labs")
    monkeypatch.setenv("OLYMPUS_OPERATOR_CONTACT", "privacy@acme.test")
    privacy = legal.privacy_html()
    terms = legal.terms_html()
    assert "Acme Labs" in privacy and "privacy@acme.test" in privacy
    assert "Acme Labs" in terms and "privacy@acme.test" in terms


def test_defaults_when_operator_unset(monkeypatch):
    monkeypatch.delenv("OLYMPUS_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("OLYMPUS_OPERATOR_CONTACT", raising=False)
    html = legal.privacy_html()
    assert "the operator" in html.lower()       # graceful placeholder, no crash
