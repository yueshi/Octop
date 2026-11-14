"""Unit tests for the app sandbox wrapper document."""

from __future__ import annotations

from octop.infra.apps.sandbox import (
    APP_CSP,
    WEBRTC_FREEZE,
    WRAPPER_CSP,
    build_app_doc,
    build_wrapper_doc,
    sanitize_blocked_uri,
)


def test_wrapper_denies_the_app_frame_any_navigation() -> None:
    """A child frame's navigation is checked against its embedder's frame-src."""
    doc = build_wrapper_doc("<html></html>")
    assert "frame-src 'none'" in doc
    assert WRAPPER_CSP in doc


def test_wrapper_keeps_network_and_plugin_channels_closed() -> None:
    doc = build_wrapper_doc("<html></html>")
    assert "default-src 'none'" in doc
    assert "object-src 'none'" in doc
    assert "connect-src 'none'" in doc


def test_wrapper_keeps_the_inline_resource_allowances() -> None:
    """A srcdoc child inherits the wrapper policy, so it must allow data: media.

    Dropping these would break inline images, fonts and media inside every app.
    """
    assert "img-src data: blob:" in WRAPPER_CSP
    assert "font-src data:" in WRAPPER_CSP
    assert "media-src data: blob:" in WRAPPER_CSP
    assert WRAPPER_CSP == APP_CSP + "; frame-src 'none'"


def test_wrapper_runs_the_app_without_same_origin() -> None:
    doc = build_wrapper_doc("<html></html>")
    assert 'inner.setAttribute("sandbox", "allow-scripts")' in doc
    # allow-scripts together with allow-same-origin would let the app remove
    # its own sandbox attribute.
    assert "allow-same-origin" not in doc


def test_wrapper_escapes_a_script_tag_in_the_app() -> None:
    doc = build_wrapper_doc("<html><script>alert(1)</script></html>")
    assert "</script>alert(1)</script>" not in doc
    assert "<\\/script>" in doc


def test_wrapper_drops_the_frame_after_a_second_load() -> None:
    doc = build_wrapper_doc("<html></html>")
    assert "loads > 1" in doc
    assert "escaped" in doc


def test_wrapper_relays_both_directions() -> None:
    doc = build_wrapper_doc("<html></html>")
    assert "event.source === inner.contentWindow" in doc
    assert "event.source === parent" in doc


def test_app_csp_blocks_remote_resources_and_plugins() -> None:
    assert "connect-src 'none'" in APP_CSP
    assert "object-src 'none'" in APP_CSP
    assert "https:" not in APP_CSP
    # An explicit frame-src in the app document makes Chrome skip creating the
    # app's own srcdoc frame. The wrapper carries the navigation ban instead.
    assert "frame-src" not in APP_CSP
    assert "frame-src 'none'" in WRAPPER_CSP


def test_wrapper_stretches_the_app_frame() -> None:
    doc = build_wrapper_doc("<html></html>")
    assert "position: fixed" in doc
    assert "inset: 0" in doc
    assert "width: 100%" in doc
    assert "height: 100%" in doc
    assert 'name="viewport"' in doc


def test_webrtc_freeze_covers_the_rtc_and_frame_globals() -> None:
    assert "RTCPeerConnection" in WEBRTC_FREEZE
    assert "webkitRTCPeerConnection" in WEBRTC_FREEZE
    assert "HTMLIFrameElement" in WEBRTC_FREEZE
    assert "ShadowRoot" in WEBRTC_FREEZE
    assert "MutationObserver" in WEBRTC_FREEZE
    assert "setHTMLUnsafe" in WEBRTC_FREEZE
    assert "execCommand" in WEBRTC_FREEZE
    assert "Worker" in WEBRTC_FREEZE
    assert "WebTransport" in WEBRTC_FREEZE
    assert "serviceWorker" in WEBRTC_FREEZE


def test_build_app_doc_puts_the_freeze_before_app_scripts() -> None:
    doc = build_app_doc(
        '<!doctype html><html lang="zh" class="x"><head>'
        '<script id="app">1</script></head><body></body></html>',
        '<meta name="csp">',
    )

    assert doc.startswith('<!doctype html><html lang="zh" class="x"><script>')
    assert doc.count("<!doctype") == 1
    assert doc.index("RTCPeerConnection") < doc.index('<script id="app">')
    assert '<meta name="csp">' in doc


def test_build_app_doc_refuses_an_old_app_with_a_frame() -> None:
    """Write-time validation covers new apps; old ones must not render either."""
    doc = build_app_doc(
        '<!doctype html><html><body><iframe srcdoc="x"></iframe></body></html>',
        "",
    )
    assert "embeds a frame" in doc
    assert "<iframe" not in doc
    assert "RTCPeerConnection" not in doc


def test_build_app_doc_puts_the_freeze_before_a_script_before_head() -> None:
    doc = build_app_doc(
        '<script id="early">1</script><html><head></head><body></body></html>',
        "",
    )
    assert doc.index("RTCPeerConnection") < doc.index('<script id="early">')


def test_sanitize_blocked_uri_drops_the_payload() -> None:
    """A blocked URL may carry the data the page tried to exfiltrate."""
    assert (
        sanitize_blocked_uri("https://evil.example/collect?secret=ROW-1#frag")
        == "https://evil.example/collect"
    )


def test_sanitize_blocked_uri_keeps_only_the_scheme_for_inline_data() -> None:
    assert sanitize_blocked_uri("data:image/png;base64,AAAA") == "data:"
    assert sanitize_blocked_uri("blob:https://x/y") == "blob:"
    assert sanitize_blocked_uri("javascript:alert(1)") == "javascript:"


def test_sanitize_blocked_uri_truncates_and_handles_empty() -> None:
    assert sanitize_blocked_uri("") == ""
    assert len(sanitize_blocked_uri("x" * 500)) == 80
    assert sanitize_blocked_uri("inline") == "inline"
