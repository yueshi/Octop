import { APP_CSP } from "./appCsp";

/**
 * Bridge exposed to app HTML as `window.octopApp`. Every call goes through the
 * parent dashboard over postMessage so the iframe never sees a JWT and never
 * needs `allow-same-origin`.
 */
export const APP_SDK = `(function () {
  var pending = new Map();
  var seq = 0;
  var TIMEOUT_MS = 30000;
  // Captured before any app script runs; the freeze installs first.
  var find = Document.prototype.querySelector;
  function intact() {
    try {
      if (typeof window.RTCPeerConnection !== "undefined") return false;
      if (typeof window.webkitRTCPeerConnection !== "undefined") return false;
      if (typeof window.mozRTCPeerConnection !== "undefined") return false;
      return !find.call(document, "iframe,frame,object,embed,portal");
    } catch (e) {
      return false;
    }
  }
  function reportIntegrity() {
    try {
      parent.postMessage(
        {
          source: "octop-app",
          type: "violation",
          kind: "sandbox_integrity",
          directive: "sandbox-integrity",
          blockedURI: "",
          disposition: "enforce"
        },
        "*"
      );
    } catch (e) {}
  }
  function call(method, params) {
    if (!intact()) {
      reportIntegrity();
      return Promise.reject(new Error("Sandbox integrity check failed"));
    }
    return new Promise(function (resolve, reject) {
      var id = ++seq;
      var entry = { resolve: resolve, reject: reject, timer: null };
      pending.set(id, entry);
      // The host may go away mid-request (the frame was remounted); reject
      // instead of leaving the caller's promise pending forever.
      entry.timer = setTimeout(function () {
        pending.delete(id);
        reject(new Error("Request timed out"));
      }, TIMEOUT_MS);
      parent.postMessage(
        { source: "octop-app", type: "request", id: id, method: method, params: params || {} },
        "*"
      );
    });
  }
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.source !== "octop-app-host") return;
    var entry = pending.get(data.id);
    if (!entry) return;
    pending.delete(data.id);
    if (entry.timer) clearTimeout(entry.timer);
    if (data.ok) entry.resolve(data.data);
    else entry.reject(new Error(data.error || "Request failed"));
  });
  // Report what the browser blocked. A page that tries to reach a remote
  // origin after the fact is worth seeing in the audit log even though the
  // CSP already stopped it.
  window.addEventListener("securitypolicyviolation", function (event) {
    parent.postMessage(
      {
        source: "octop-app",
        type: "violation",
        directive: event.violatedDirective || "",
        blockedURI: event.blockedURI || "",
        disposition: event.disposition || ""
      },
      "*"
    );
  });
  window.octopApp = {
    me: function () { return call("me"); },
    schema: function (table) { return call("schema", { table: table || null }); },
    tables: function () { return call("tables"); },
    listRows: function (options) { return call("rows", options || {}); },
    appendRow: function (row, options) {
      return call("append", { row: row, table: (options || {}).table || null });
    },
    generate: function (prompt, options) {
      return call("generate", {
        prompt: prompt || "",
        system: (options || {}).system || ""
      });
    },
    launchGame: function (gameId) {
      return call("launchGame", { gameId: gameId || "" });
    },
    composeMessage: function (text) {
      return call("composeMessage", { text: text || "" });
    },
    openExternal: function (url) { return call("openExternal", { url: url || "" }); },
    asset: function (name) { return call("asset", { name: name || "" }); }
  };
})();`;

/**
 * Run before any app script.
 *
 * WebRTC is the one network channel CSP does not cover: an app that reads rows
 * through the bridge could otherwise open an `RTCPeerConnection` and send them
 * to a STUN/TURN server. The globals are replaced with non-configurable
 * `undefined` values on both the window and its prototype, so `delete` and
 * prototype lookups do not bring them back.
 *
 * A nested `srcdoc` frame would get a fresh, unfrozen realm (and `frame-src`
 * does not stop the initial srcdoc load), so this also rejects frame creation
 * and removes any frame that slips through. This is defence in depth: the
 * wrapper's `frame-src 'none'` and the write-time scan remain in place.
 */
export const WEBRTC_FREEZE = `(function () {
  var RTC = ["RTCPeerConnection", "webkitRTCPeerConnection", "mozRTCPeerConnection", "RTCDataChannel"];
  var EMBED = { IFRAME: 1, FRAME: 1, OBJECT: 1, EMBED: 1, PORTAL: 1 };
  var FRAME_MARKUP = /<\\s*(iframe|frame|object|embed|portal)\\b/i;

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
})();`;

/** Whether the app document embeds a frame/plugin element. */
export function hasEmbeddedFrame(html: string): boolean {
  return /<\s*(iframe|frame|object|embed|portal)\b/i.test(html);
}

/**
 * Host-level presentation reset for every MiniApp document.
 *
 * Generated apps often theme `body` but leave the root scroll container and
 * native scrollbars unthemed. Inside a dark desktop window that shows up as a
 * white strip on the right/bottom edge and white rubber-band gaps while
 * scrolling. Keep the scrollbars transparent on the app background, and stop
 * overscroll from revealing the iframe's default white canvas.
 */
const APP_HOST_STYLE = `<style>
  html, body { overscroll-behavior: none; }
  html {
    background: var(--bg, #fff);
    scrollbar-color: rgb(148 163 184 / 55%) var(--bg, transparent);
  }
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: var(--bg, transparent); }
  ::-webkit-scrollbar-corner { background: var(--bg, #fff); }
  ::-webkit-scrollbar-thumb {
    background: rgb(148 163 184 / 55%);
    border-radius: 999px;
  }
  ::-webkit-scrollbar-thumb:hover { background: rgb(148 163 184 / 70%); }
</style>`;

export function buildSrcDoc(html: string): string {
  // Old apps may predate the write-time rule; never run one whose static HTML
  // would create a fresh realm before the freeze can protect it.
  if (hasEmbeddedFrame(html)) {
    return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${APP_CSP}">${APP_HOST_STYLE}</head><body><p>This MiniApp embeds a frame and cannot be displayed.</p></body></html>`;
  }
  const head = `<meta http-equiv="Content-Security-Policy" content="${APP_CSP}"><script>${APP_SDK}</script>${APP_HOST_STYLE}`;
  // Pull the first <html ...> tag out so the freeze script can precede every
  // app script while keeping lang/class/style on the root element.
  const htmlMatch = /<html\b([^>]*)>/i.exec(html);
  const htmlAttrs = htmlMatch ? htmlMatch[1] : "";
  let body = html;
  if (htmlMatch?.index !== undefined) {
    body =
      html.slice(0, htmlMatch.index) +
      html.slice(htmlMatch.index + htmlMatch[0].length);
  }
  body = body.replace(/^\s*<!doctype[^>]*>/i, "");
  const headMatch = /<head[^>]*>/i.exec(body);
  if (headMatch?.index !== undefined) {
    const at = headMatch.index + headMatch[0].length;
    body = `${body.slice(0, at)}${head}${body.slice(at)}`;
  } else {
    const bodyMatch = /<body[^>]*>/i.exec(body);
    if (bodyMatch?.index !== undefined) {
      body = `${body.slice(0, bodyMatch.index)}<head>${head}</head>${body.slice(
        bodyMatch.index,
      )}`;
    } else {
      body = `<head>${head}</head><body>${body}</body>`;
    }
  }
  return `<!doctype html><html${htmlAttrs}><script>${WEBRTC_FREEZE}</script>${body}`;
}
