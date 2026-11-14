import { afterEach, describe, expect, it } from "vitest";

import { fullscreenContainer } from "./overlayContainer";

function setFullscreenElement(value: Element | null): void {
  Object.defineProperty(document, "fullscreenElement", {
    configurable: true,
    value,
  });
}

describe("fullscreenContainer", () => {
  afterEach(() => {
    setFullscreenElement(null);
  });

  it("mounts overlays inside the current fullscreen element", () => {
    const fullscreen = document.createElement("div");
    setFullscreenElement(fullscreen);

    expect(fullscreenContainer()).toBe(fullscreen);
  });

  it("falls back to body outside fullscreen", () => {
    setFullscreenElement(null);

    expect(fullscreenContainer()).toBe(document.body);
  });
});
