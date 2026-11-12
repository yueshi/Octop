import { describe, expect, it } from "vitest";

import type { BoardItem } from "../../api/modules/boards";
import {
  autoFillBoardLayout,
  BOARD_FALLBACK_ROW_HEIGHT,
  BOARD_MARGIN_Y,
  BOARD_MIN_ROW_HEIGHT,
  fitBoardRowHeight,
  placeBoardItem,
  splitBoardColumns,
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

/** Every band starts at x=0 and its cells sum to the full 12 columns. */
function expectFilledBands(items: BoardItem[]): void {
  const bands = new Map<number, BoardItem[]>();
  for (const item of items) {
    bands.set(item.y, [...(bands.get(item.y) ?? []), item]);
  }
  for (const band of bands.values()) {
    let expectedX = 0;
    for (const item of [...band].sort((a, b) => a.x - b.x)) {
      expect(item.x).toBe(expectedX);
      expectedX += item.w;
    }
    expect(expectedX).toBe(12);
  }
}

describe("autoFillBoardLayout", () => {
  it("gives a single app the full width", () => {
    expect(autoFillBoardLayout(apps(1))).toEqual([
      { app_id: "app_0", x: 0, y: 0, w: 12, h: 2 },
    ]);
  });

  it("splits two apps into halves", () => {
    expect(autoFillBoardLayout(apps(2)).map((item) => item.w)).toEqual([6, 6]);
  });

  it("puts three apps in one full band", () => {
    const result = autoFillBoardLayout(apps(3));
    expect(result.map((item) => item.w)).toEqual([4, 4, 4]);
    expect(new Set(result.map((item) => item.y))).toEqual(new Set([0]));
  });

  it("puts four apps in one row of thirds", () => {
    expect(autoFillBoardLayout(apps(4)).map((item) => item.w)).toEqual([
      3, 3, 3, 3,
    ]);
  });

  it("spreads the last band across all columns", () => {
    const result = autoFillBoardLayout(apps(5));
    expect(result.map((item) => [item.y, item.w])).toEqual([
      [0, 4],
      [0, 4],
      [0, 4],
      [2, 6],
      [2, 6],
    ]);
    expectFilledBands(result);
  });

  it("tiles nine apps into a 3×3 grid", () => {
    const result = autoFillBoardLayout(apps(9));
    expect(result).toHaveLength(9);
    expect(new Set(result.map((item) => item.y))).toEqual(new Set([0, 2, 4]));
    expect(result.every((item) => item.w === 4)).toBe(true);
  });

  it("keeps every app and never overlaps within a band", () => {
    const result = autoFillBoardLayout(apps(12));
    expect(new Set(result.map((item) => item.app_id)).size).toBe(12);
    expectFilledBands(result);
  });

  it("splits columns evenly", () => {
    expect(splitBoardColumns(1)).toEqual([12]);
    expect(splitBoardColumns(2)).toEqual([6, 6]);
    expect(splitBoardColumns(3)).toEqual([4, 4, 4]);
    expect(splitBoardColumns(4)).toEqual([3, 3, 3, 3]);
  });

  it("keeps locked cells fixed while filling around them", () => {
    const result = autoFillBoardLayout([
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
