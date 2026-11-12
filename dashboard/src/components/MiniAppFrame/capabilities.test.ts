import { describe, expect, it } from "vitest";

import { CAP, effectiveCapabilities, isMethodAllowed } from "./capabilities";

describe("MiniApp capabilities", () => {
  it("treats an older payload without a manifest as legacy all-on", () => {
    expect(effectiveCapabilities(undefined)).toContain(CAP.AI_GENERATE);
    expect(isMethodAllowed("generate", undefined)).toBe(true);
  });

  it("gates page methods by the declared manifest", () => {
    expect(isMethodAllowed("rows", [CAP.DATA_READ])).toBe(true);
    expect(isMethodAllowed("append", [CAP.DATA_READ])).toBe(false);
    expect(isMethodAllowed("generate", [CAP.AI_GENERATE])).toBe(true);
    expect(isMethodAllowed("launchGame", [CAP.AI_GENERATE])).toBe(false);
    expect(isMethodAllowed("asset", [CAP.DATA_READ])).toBe(false);
    expect(isMethodAllowed("composeMessage", [CAP.CHAT_COMPOSE])).toBe(true);
    expect(isMethodAllowed("composeMessage", [CAP.DATA_READ])).toBe(false);
  });

  it("leaves untracked methods to the bridge switch", () => {
    expect(isMethodAllowed("me", [])).toBe(true);
    expect(isMethodAllowed("unknownMethod", [])).toBe(true);
  });
});
