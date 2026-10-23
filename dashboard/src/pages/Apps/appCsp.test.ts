import { describe, expect, it } from "vitest";

import { APP_CSP } from "./appCsp";

describe("APP_CSP", () => {
  it("blocks remote resources so app rows cannot be exfiltrated", () => {
    expect(APP_CSP).not.toContain("https:");
    expect(APP_CSP).not.toContain("http:");
    expect(APP_CSP).toContain("connect-src 'none'");
    expect(APP_CSP).toContain("form-action 'none'");
    expect(APP_CSP).toContain("base-uri 'none'");
  });
});
