import { describe, expect, it } from "vitest";

import {
  addAppToBoard,
  APP_BOARD_SIZE_ORDER,
  APP_BOARD_SPANS,
  clearAppBoard,
  emptyAppBoard,
  isAppOnBoard,
  isAppBoardSize,
  loadAppBoard,
  moveAppInBoard,
  reconcileAppBoard,
  removeAppFromBoard,
  saveAppBoard,
  sizeOfApp,
  snapAppBoardSize,
  withAppSize,
  type AppBoardStorage,
} from "./appBoard";

function memoryStorage(): AppBoardStorage & { value: () => string | null } {
  let value: string | null = null;
  return {
    getItem: () => value,
    setItem: (_key: string, next: string) => {
      value = next;
    },
    value: () => value,
  };
}

describe("appBoard", () => {
  it("starts empty", () => {
    expect(emptyAppBoard()).toEqual({ order: [], sizes: {} });
  });

  it("pins an app at the end and ignores a second pin", () => {
    const board = addAppToBoard(emptyAppBoard(), "app_a");
    expect(board.order).toEqual(["app_a"]);
    expect(addAppToBoard(board, "app_a")).toBe(board);
    expect(addAppToBoard(board, "app_b").order).toEqual(["app_a", "app_b"]);
  });

  it("unpins an app and ignores unknown ids", () => {
    const board = { order: ["app_a", "app_b"], sizes: {} };
    expect(removeAppFromBoard(board, "app_a").order).toEqual(["app_b"]);
    expect(removeAppFromBoard(board, "app_zz")).toBe(board);
    expect(isAppOnBoard(board, "app_b")).toBe(true);
    expect(isAppOnBoard(board, "app_zz")).toBe(false);
  });

  it("drops the size of an unpinned app", () => {
    const board = withAppSize({ order: ["app_a"], sizes: {} }, "app_a", "2x6");
    expect(sizeOfApp(board, "app_a")).toBe("2x6");
    const cleared = removeAppFromBoard(board, "app_a");
    expect(cleared.sizes).toEqual({});
  });

  it("moves a pinned app one slot at a time", () => {
    const board = { order: ["app_a", "app_b", "app_c"], sizes: {} };
    expect(moveAppInBoard(board, "app_b", -1).order).toEqual([
      "app_b",
      "app_a",
      "app_c",
    ]);
    expect(moveAppInBoard(board, "app_b", 1).order).toEqual([
      "app_a",
      "app_c",
      "app_b",
    ]);
    // Out of range moves leave the board untouched.
    expect(moveAppInBoard(board, "app_a", -1)).toBe(board);
    expect(moveAppInBoard(board, "app_c", 1)).toBe(board);
    expect(moveAppInBoard(board, "app_zz", 1)).toBe(board);
  });

  it("stores the grid size of each pinned app", () => {
    const board = withAppSize({ order: ["app_a"], sizes: {} }, "app_a", "3x3");
    expect(sizeOfApp(board, "app_a")).toBe("3x3");
    // An app without an override takes the default size.
    expect(sizeOfApp(board, "app_b")).toBe("2x4");
    // The default size clears the override instead of storing it.
    expect(withAppSize(board, "app_a", "2x4").sizes).toEqual({});
  });

  it("describes every size by its span", () => {
    for (const size of APP_BOARD_SIZE_ORDER) {
      const span = APP_BOARD_SPANS[size];
      expect(size).toBe(`${span.cols}x${span.rows}`);
      expect(span.cols).toBeLessThanOrEqual(6);
      expect(span.rows).toBeLessThanOrEqual(6);
    }
  });

  it("offers widths that divide the six-column canvas", () => {
    const widths = new Set(
      APP_BOARD_SIZE_ORDER.map((size) => APP_BOARD_SPANS[size].cols),
    );
    expect([...widths].sort((a, b) => a - b)).toEqual([1, 2, 3, 6]);
  });

  it("snaps a dragged span to the closest supported shape", () => {
    expect(snapAppBoardSize(1, 1)).toBe("1x1");
    expect(snapAppBoardSize(2, 2)).toBe("2x2");
    expect(snapAppBoardSize(2, 4)).toBe("2x4");
    expect(snapAppBoardSize(3, 3)).toBe("3x3");
    expect(snapAppBoardSize(2, 6)).toBe("2x6");
    expect(snapAppBoardSize(3, 6)).toBe("3x6");
    expect(snapAppBoardSize(6, 6)).toBe("6x6");
    // One row past a 2x4 panel is closer to the full-height strip.
    expect(snapAppBoardSize(2, 5)).toBe("2x6");
    // A taller half stays on the half width.
    expect(snapAppBoardSize(3, 4)).toBe("3x4");
    // Four columns: three is as close as six, and the smaller cell wins.
    expect(snapAppBoardSize(4, 4)).toBe("3x4");
  });

  it("keeps every stored size readable", () => {
    expect(isAppBoardSize("6x6")).toBe(true);
    expect(isAppBoardSize("4x4")).toBe(false);
    expect(isAppBoardSize("toString")).toBe(false);
    expect(isAppBoardSize(null)).toBe(false);
  });

  it("drops pinned apps that no longer exist", () => {
    const board = { order: ["app_a", "gone", "app_b"], sizes: {} };
    expect(reconcileAppBoard(board, ["app_a", "app_b"]).order).toEqual([
      "app_a",
      "app_b",
    ]);
    // Nothing to drop: the same board object is returned.
    expect(reconcileAppBoard(board, ["app_a", "gone", "app_b"])).toBe(board);
  });

  it("round-trips through storage", () => {
    const storage = memoryStorage();
    saveAppBoard(
      { order: ["app_a", "app_b"], sizes: { app_b: "6x6" } },
      storage,
    );
    expect(loadAppBoard(storage)).toEqual({
      order: ["app_a", "app_b"],
      sizes: { app_b: "6x6" },
    });
  });

  it("migrates size ids from earlier builds", () => {
    const storage = memoryStorage();
    storage.setItem(
      "octop:apps-board:v1",
      JSON.stringify({
        order: ["a", "b", "c"],
        sizes: { a: "panel", b: "square", c: "strip" },
      }),
    );
    expect(loadAppBoard(storage).sizes).toEqual({
      a: "2x4",
      b: "3x3",
      c: "2x6",
    });
  });

  it("migrates a board saved under the old storage key", () => {
    const storage = memoryStorage();
    storage.setItem(
      "octop:apps-layout:v1",
      JSON.stringify({
        order: ["app_a", "app_b"],
        sizes: { app_a: "standard", app_b: "large" },
      }),
    );
    expect(loadAppBoard(storage)).toEqual({
      order: ["app_a", "app_b"],
      sizes: { app_a: "1x1", app_b: "2x2" },
    });
  });

  it("ignores a corrupted payload", () => {
    const storage = memoryStorage();
    storage.setItem("octop:apps-board:v1", "not json");
    expect(loadAppBoard(storage)).toEqual({ order: [], sizes: {} });
    storage.setItem(
      "octop:apps-board:v1",
      JSON.stringify({ order: [1, "a", "a"], sizes: { a: "huge" } }),
    );
    expect(loadAppBoard(storage)).toEqual({ order: ["a"], sizes: {} });
  });

  it("clears both browser-local keys after server migration", () => {
    const removed: string[] = [];
    clearAppBoard({
      getItem: () => null,
      setItem: () => undefined,
      removeItem: (key) => {
        removed.push(key);
      },
    });
    expect(removed).toEqual(["octop:apps-board:v1", "octop:apps-layout:v1"]);
  });

  it("reads a board saved before sizes existed", () => {
    const storage = memoryStorage();
    storage.setItem(
      "octop:apps-board:v1",
      JSON.stringify({ order: ["app_a"] }),
    );
    expect(loadAppBoard(storage)).toEqual({ order: ["app_a"], sizes: {} });
  });
});
