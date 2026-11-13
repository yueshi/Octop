import { describe, expect, it } from "vitest";

import type { BoardItem } from "../../api/modules/boards";
import {
  addBoardItem,
  autoArrangeBoardLayout,
  BOARD_FALLBACK_ROW_HEIGHT,
  BOARD_MARGIN_Y,
  BOARD_MIN_ROW_HEIGHT,
  fitBoardRowHeight,
  placeBoardItem,
} from "./boardLayout";

describe("fitBoardRowHeight", () => {
  it("stretches rows to fill the canvas exactly", () => {
    const height = fitBoardRowHeight(800, 4);
    expect(height * 4 + BOARD_MARGIN_Y * 3).toBeCloseTo(800);
    expect(height).toBeGreaterThan(BOARD_MIN_ROW_HEIGHT);
  });

  it("grows a sparse board instead of leaving a blank bottom", () => {
    expect(fitBoardRowHeight(900, 2)).toBe(444);
    expect(fitBoardRowHeight(900, 1)).toBe(900);
  });

  it("keeps a minimum row height, letting dense boards scroll", () => {
    expect(fitBoardRowHeight(120, 6)).toBe(BOARD_MIN_ROW_HEIGHT);
  });

  it("falls back before the canvas is measured", () => {
    expect(fitBoardRowHeight(0, 4)).toBe(BOARD_FALLBACK_ROW_HEIGHT);
    expect(fitBoardRowHeight(0, 0)).toBe(BOARD_FALLBACK_ROW_HEIGHT);
  });

  it("treats an empty layout as one row", () => {
    expect(fitBoardRowHeight(600, 0)).toBe(600);
  });
});

function apps(count: number): BoardItem[] {
  return Array.from({ length: count }, (_, index) => ({
    app_id: `app_${index}`,
    x: index % 2,
    y: index * 2,
    w: 2,
    h: 2,
  }));
}

function cells(specs: Array<[id: string, w: number, h: number]>): BoardItem[] {
  return specs.map(([app_id, w, h], index) => ({
    app_id,
    x: 0,
    y: index * 2,
    w,
    h,
  }));
}

function expectNoOverlap(items: BoardItem[]): void {
  for (let left = 0; left < items.length; left += 1) {
    for (let right = left + 1; right < items.length; right += 1) {
      const a = items[left];
      const b = items[right];
      const overlap =
        a.x < b.x + b.w &&
        a.x + a.w > b.x &&
        a.y < b.y + b.h &&
        a.y + a.h > b.y;
      expect(overlap).toBe(false);
    }
  }
}

describe("autoArrangeBoardLayout", () => {
  it("keeps the chosen size of a single cell", () => {
    expect(
      autoArrangeBoardLayout([{ app_id: "app_0", x: 3, y: 2, w: 3, h: 2 }]),
    ).toEqual([{ app_id: "app_0", x: 0, y: 0, w: 3, h: 2 }]);
  });

  it("packs six 4×4 cells three per row", () => {
    const result = autoArrangeBoardLayout(
      cells(Array.from({ length: 6 }, (_, index) => [`app_${index}`, 4, 4])),
    );
    expect(result.map((item) => [item.x, item.y])).toEqual([
      [0, 0],
      [4, 0],
      [8, 0],
      [0, 4],
      [4, 4],
      [8, 4],
    ]);
  });

  it("preserves a mixed size layout instead of flattening it", () => {
    const result = autoArrangeBoardLayout(
      cells([
        ["wide", 8, 4],
        ["tall", 6, 3],
        ["square", 4, 4],
        ["small", 4, 2],
      ]),
    );
    const byId = Object.fromEntries(result.map((item) => [item.app_id, item]));
    expect([byId.wide.x, byId.wide.y]).toEqual([0, 0]);
    expect([byId.square.x, byId.square.y]).toEqual([8, 0]);
    expect([byId.tall.x, byId.tall.y]).toEqual([0, 4]);
    expect([byId.small.x, byId.small.y]).toEqual([6, 4]);
    expectNoOverlap(result);
  });

  it("keeps locked cells fixed while filling around them", () => {
    const result = autoArrangeBoardLayout([
      { app_id: "locked", x: 0, y: 0, w: 6, h: 4, locked: true },
      { app_id: "free", x: 0, y: 4, w: 4, h: 2 },
    ]);
    const locked = result.find((item) => item.app_id === "locked");
    const free = result.find((item) => item.app_id === "free");
    expect(locked).toEqual({
      app_id: "locked",
      x: 0,
      y: 0,
      w: 6,
      h: 4,
      locked: true,
    });
    expect(free).toEqual({ app_id: "free", x: 6, y: 0, w: 4, h: 2 });
  });
});

describe("addBoardItem", () => {
  it("fills a hole in the top row", () => {
    const result = addBoardItem(
      [
        { app_id: "a", x: 0, y: 0, w: 4, h: 4 },
        { app_id: "b", x: 8, y: 0, w: 4, h: 4 },
      ],
      "new",
    );
    expect(result.find((item) => item.app_id === "new")).toEqual({
      app_id: "new",
      x: 4,
      y: 0,
      w: 4,
      h: 4,
    });
  });

  it("starts a new row when the top band is full", () => {
    const items = [0, 4, 8].map((x, index) => ({
      app_id: `a${index}`,
      x,
      y: 0,
      w: 4,
      h: 4,
    }));
    expect(
      addBoardItem(items, "new").find((item) => item.app_id === "new"),
    ).toEqual({ app_id: "new", x: 0, y: 4, w: 4, h: 4 });
  });
});

describe("placeBoardItem", () => {
  it("resizes the cell in place when there is room", () => {
    const result = placeBoardItem(apps(1), "app_0", 6, 4);
    expect(result[0]).toEqual({ app_id: "app_0", x: 0, y: 0, w: 6, h: 4 });
  });

  it("moves the cell down when the new size would overlap", () => {
    const result = placeBoardItem(
      [
        { app_id: "a", x: 0, y: 0, w: 4, h: 4 },
        { app_id: "b", x: 4, y: 0, w: 4, h: 4 },
      ],
      "a",
      6,
      4,
    );
    expect(result[0]).toEqual({ app_id: "a", x: 0, y: 4, w: 6, h: 4 });
    expect(result[1]).toEqual({ app_id: "b", x: 4, y: 0, w: 4, h: 4 });
  });

  it("clamps a full-width cell back inside the grid", () => {
    const result = placeBoardItem(
      [{ app_id: "a", x: 8, y: 0, w: 3, h: 2 }],
      "a",
      12,
      3,
    );
    expect(result[0]).toEqual({ app_id: "a", x: 0, y: 0, w: 12, h: 3 });
  });
});
