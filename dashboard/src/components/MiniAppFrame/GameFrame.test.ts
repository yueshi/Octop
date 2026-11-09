import { beforeAll, describe, expect, it, vi } from "vitest";

import { buildGameUrl } from "./GameFrame";

beforeAll(() => {
  // Vite normally defines this; the test runner does not.
  vi.stubGlobal("BASE_URL", "");
});

const session = {
  session_id: "game_abc",
  game_id: "duck",
  name: "小鸭养成",
  url: "https://games.example.com/duck/index.html?theme=light",
  token: "scoped-token",
  expires_at: 2_000_000_000,
  sandbox: "allow-scripts allow-same-origin",
};

describe("buildGameUrl", () => {
  it("keeps the game URL and puts the session in the fragment", () => {
    const url = new URL(buildGameUrl("app_1", session));
    expect(url.origin).toBe("https://games.example.com");
    expect(url.pathname).toBe("/duck/index.html");
    expect(url.search).toBe("?theme=light");
    const fragment = new URLSearchParams(url.hash.slice(1));
    expect(fragment.get("app_id")).toBe("app_1");
    expect(fragment.get("game_id")).toBe("duck");
    expect(fragment.get("session_id")).toBe("game_abc");
    expect(fragment.get("token")).toBe("scoped-token");
    const expectedApi = new URL("/api", window.location.origin)
      .toString()
      .replace(/\/$/, "");
    expect(fragment.get("api")).toBe(expectedApi);
  });
});
