import { describe, expect, it } from "vitest";

import { buildWrapperDoc } from "./wrapper";

describe("buildWrapperDoc", () => {
  it("stretches the app frame to fill the wrapper", () => {
    // Without this the inner iframe uses the default 300×150 box and the app
    // renders in the top-left corner of a large board cell.
    const doc = buildWrapperDoc("<html></html>");
    expect(doc).toContain(
      "iframe { position: fixed; inset: 0; width: 100%; height: 100%;",
    );
    expect(doc).toMatch(/html,\s*body\s*\{[^}]*height:\s*100%/);
    // A white wrapper base leaks through rounded window corners/seams; the
    // app document paints its own themed background instead.
    expect(doc).toContain("background: transparent");
  });

  it("denies the app frame any navigation", () => {
    // The app frame's own navigation is checked against its embedder's
    // frame-src, so this is the directive that stops the escape.
    expect(buildWrapperDoc("<html></html>")).toContain("frame-src 'none'");
  });

  it("keeps the wrapper free of network and plugin channels", () => {
    const doc = buildWrapperDoc("<html></html>");
    expect(doc).toContain("default-src 'none'");
    expect(doc).toContain("object-src 'none'");
    expect(doc).not.toContain("connect-src 'unsafe");
  });

  it("runs the app in a script-only sandbox without same-origin", () => {
    const doc = buildWrapperDoc("<html></html>");
    expect(doc).toContain('inner.setAttribute("sandbox", "allow-scripts")');
    // allow-same-origin next to allow-scripts would let the app remove its
    // own sandbox attribute.
    expect(doc).not.toContain("allow-same-origin");
  });

  it("escapes a script tag in the app so it cannot close the wrapper", () => {
    const doc = buildWrapperDoc("<html><script>alert(1)</script></html>");
    expect(doc).not.toContain("</script>alert(1)</script>");
    expect(doc).toContain("<\\/script>");
  });

  it("treats a second load as an escape and stops relaying", () => {
    const doc = buildWrapperDoc("<html></html>");
    expect(doc).toContain("loads > 1");
    expect(doc).toContain('type: "escaped"');
  });

  it("relays messages in both directions", () => {
    const doc = buildWrapperDoc("<html></html>");
    expect(doc).toContain("event.source === inner.contentWindow");
    expect(doc).toContain("event.source === parent");
  });
});
