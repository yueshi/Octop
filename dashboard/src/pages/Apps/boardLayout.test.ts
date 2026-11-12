import { describe, expect, it } from "vitest";

import {
  BOARD_FALLBACK_ROW_HEIGHT,
  BOARD_MARGIN_Y,
  BOARD_MIN_ROW_HEIGHT,
  fitBoardRowHeight,
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
