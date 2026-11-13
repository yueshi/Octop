import { describe, expect, it } from "vitest";

import { APP_SDK, WEBRTC_FREEZE, buildSrcDoc, hasEmbeddedFrame } from "./sdk";

describe("APP_SDK", () => {
  it("reports CSP violations to the host", () => {
    expect(APP_SDK).toContain("securitypolicyviolation");
    expect(APP_SDK).toContain('type: "violation"');
    expect(APP_SDK).toContain("blockedURI");
  });

  it("refuses bridge calls when the freeze invariants fail", () => {
    expect(APP_SDK).toContain("sandbox-integrity");
    expect(APP_SDK).toContain("RTCPeerConnection");
    expect(APP_SDK).toContain("iframe,frame,object,embed,portal");
  });
});

describe("WEBRTC_FREEZE", () => {
  it("blocks WebRTC and the frame constructors", () => {
    expect(WEBRTC_FREEZE).toContain("RTCPeerConnection");
    expect(WEBRTC_FREEZE).toContain("webkitRTCPeerConnection");
    expect(WEBRTC_FREEZE).toContain("HTMLIFrameElement");
    expect(WEBRTC_FREEZE).toContain("ShadowRoot");
    expect(WEBRTC_FREEZE).toContain("MutationObserver");
    expect(WEBRTC_FREEZE).toContain("setHTMLUnsafe");
    expect(WEBRTC_FREEZE).toContain("execCommand");
    expect(WEBRTC_FREEZE).toContain("Worker");
    expect(WEBRTC_FREEZE).toContain("WebTransport");
    expect(WEBRTC_FREEZE).toContain("serviceWorker");
  });
});

describe("hasEmbeddedFrame", () => {
  it("detects static frame and plugin tags", () => {
    expect(hasEmbeddedFrame("<iframe></iframe>")).toBe(true);
    expect(hasEmbeddedFrame("<object data='x'></object>")).toBe(true);
    expect(hasEmbeddedFrame("<p>ok</p>")).toBe(false);
  });
});

describe("buildSrcDoc", () => {
  it("refuses an old app with a static frame", () => {
    const doc = buildSrcDoc("<iframe srcdoc='x'></iframe>");
    expect(doc).toContain("embeds a frame");
    expect(doc).not.toContain("<iframe");
  });

  it("puts the freeze before every app script and keeps html attributes", () => {
    const doc = buildSrcDoc(
      `<!doctype html><html lang="zh" class="x"><head><script id="app">1</script></head><body></body></html>`,
    );

    const freezeAt = doc.indexOf("RTCPeerConnection");
    const appAt = doc.indexOf('<script id="app">');
    expect(freezeAt).toBeGreaterThan(-1);
    expect(appAt).toBeGreaterThan(-1);
    expect(freezeAt).toBeLessThan(appAt);
    expect(doc).toContain('<html lang="zh" class="x">');
    expect(doc.match(/<!doctype/gi)).toHaveLength(1);
  });

  it("puts the freeze before a script that precedes <head>", () => {
    const doc = buildSrcDoc(
      `<script id="early">1</script><html><head></head><body></body></html>`,
    );
    expect(doc.indexOf("RTCPeerConnection")).toBeLessThan(
      doc.indexOf('<script id="early">'),
    );
  });
});
