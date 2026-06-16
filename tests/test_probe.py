from app.worldpanel.probe import (
    is_network_candidate,
    redact_dom_html,
    redact_payload,
    redact_text,
    redact_url,
    sanitize_headers,
)


def test_redact_text_removes_credentials_state_tokens_emails_and_business_numbers():
    raw = """
    <input type="password" value="secret-password">
    <input name="__VIEWSTATE" value="very-long-aspnet-state-token">
    analyst@example.com Authorization: Bearer abcdefghijklmnopqrstuvwxyz
    Spend 123,456.78 and Penetration 42.5%
    <span class="rtPlus" id="ctl00_tree_plus">+</span>
    """

    redacted = redact_text(raw)

    assert "secret-password" not in redacted
    assert "very-long-aspnet-state-token" not in redacted
    assert "analyst@example.com" not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "123,456.78" not in redacted
    assert "42.5" not in redacted
    assert "rtPlus" in redacted
    assert "ctl00_tree_plus" in redacted


def test_redact_url_preserves_endpoint_shape_but_removes_query_values():
    redacted = redact_url(
        "https://eu.worldpanelonline.com/ReportingCS/TreeHandler.aspx"
        "?reportId=12345&member=Coke%20TM&__VIEWSTATE=secret"
    )

    assert redacted == (
        "https://eu.worldpanelonline.com/ReportingCS/TreeHandler.aspx"
        "?reportId=%5BREDACTED%5D&member=%5BREDACTED%5D&__VIEWSTATE=%5BREDACTED%5D"
    )


def test_redact_dom_preserves_telerik_structure_but_removes_business_values():
    redacted = redact_dom_html(
        '<span class="rtPlus" title="Secret Brand">Secret Brand</span>'
        '<script>const member = "Secret Brand";</script>'
    )

    assert 'class="rtPlus"' in redacted
    assert "Secret Brand" not in redacted
    assert "<script>[REDACTED_TEXT]</script>" in redacted


def test_redact_payload_preserves_keys_but_removes_values():
    redacted = redact_payload('{"member":"Secret Brand","items":[1,2]}')

    assert '"member"' in redacted
    assert '"items"' in redacted
    assert "Secret Brand" not in redacted
    assert "1" not in redacted


def test_sanitize_headers_keeps_diagnostic_headers_and_drops_secrets():
    sanitized = sanitize_headers(
        {
            "content-type": "application/json",
            "x-requested-with": "XMLHttpRequest",
            "authorization": "Bearer secret",
            "cookie": "session=secret",
            "referer": "https://example.test/page?token=secret",
        }
    )

    assert sanitized == {
        "content-type": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://example.test/page?token=%5BREDACTED%5D",
    }


def test_network_candidate_accepts_xhr_fetch_and_telerik_postbacks():
    assert is_network_candidate("xhr", "https://example.test/api/tree", "GET")
    assert is_network_candidate("fetch", "https://example.test/api/tree", "GET")
    assert is_network_candidate("document", "https://example.test/Telerik.Web.UI.WebResource.axd", "POST")
    assert not is_network_candidate("image", "https://example.test/logo.png", "GET")
