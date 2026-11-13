import { describe, expect, it } from "vitest";

import { APP_CSP, WRAPPER_CSP } from "./appCsp";

describe("APP_CSP", () => {
  it("blocks remote resources so app rows cannot be exfiltrated", () => {
    expect(APP_CSP).not.toContain("https:");
    expect(APP_CSP).not.toContain("http:");
    expect(APP_CSP).toContain("connect-src 'none'");
    expect(APP_CSP).toContain("form-action 'none'");
    expect(APP_CSP).toContain("base-uri 'none'");
  });

  it("blocks nested frames and plugin objects", () => {
    expect(APP_CSP).toContain("frame-src 'none'");
    expect(APP_CSP).toContain("object-src 'none'");
  });
});

describe("WRAPPER_CSP", () => {
  it("denies the app frame any navigation", () => {
    // A child frame's navigation is checked against its embedder's frame-src;
    // this is what stops an app from leaving the sandbox with its rows.
    expect(WRAPPER_CSP).toContain("frame-src 'none'");
    expect(WRAPPER_CSP).toContain("object-src 'none'");
    expect(WRAPPER_CSP).toContain("default-src 'none'");
  });

  it("keeps the inline resource allowances the app needs", () => {
    // A srcdoc child inherits the wrapper's policy, so leaving these out would
    // break data: images, fonts and media inside every app.
    expect(WRAPPER_CSP).toContain("img-src data: blob:");
    expect(WRAPPER_CSP).toContain("font-src data:");
    expect(WRAPPER_CSP).toContain("media-src data: blob:");
    expect(WRAPPER_CSP).toBe(APP_CSP);
  });
});
