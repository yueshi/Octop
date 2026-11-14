import { describe, expect, it } from "vitest";

import {
  DOCK_SPACE,
  MENU_BAR_HEIGHT,
  createDesktopWindow,
} from "./desktopWindows";

const appSpace = new Map([["app_1", "agent_1"]]);

function create(kind: "app" | "chat" | "external", bottomInset?: number) {
  return createDesktopWindow({
    kind,
    appId: kind === "app" ? "app_1" : undefined,
    agentId: kind === "chat" ? "agent_1" : undefined,
    spaceId: kind === "external" ? "agent_1" : undefined,
    url: kind === "external" ? "https://example.com" : undefined,
    z: 1,
    list: [],
    appSpace,
    viewportWidth: 1280,
    viewportHeight: 800,
    bottomInset,
  })!;
}

describe("desktopWindows", () => {
  it("keeps every window kind above the dock surface", () => {
    const dockTop = 800 - DOCK_SPACE;
    for (const kind of ["app", "chat", "external"] as const) {
      const win = create(kind);
      expect(win.y + win.h).toBeLessThanOrEqual(dockTop);
    }
  });

  it("uses the measured dock inset for placement", () => {
    const win = create("chat", 140);
    expect(win.y + win.h).toBeLessThanOrEqual(800 - 140);
    expect(win.y).toBeGreaterThanOrEqual(MENU_BAR_HEIGHT);
  });

  it("shrinks a tall window instead of letting it cross the dock", () => {
    const win = create("external", 200);
    expect(win.h).toBeLessThanOrEqual(800 - MENU_BAR_HEIGHT - 200);
    expect(win.y + win.h).toBeLessThanOrEqual(800 - 200);
  });
});
