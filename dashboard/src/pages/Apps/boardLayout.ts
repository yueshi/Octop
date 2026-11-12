/** Grid geometry shared by the board canvas and its tests. */
export const BOARD_GRID_COLS = 12;
export const BOARD_MARGIN_Y = 12;
export const BOARD_MIN_ROW_HEIGHT = 48;
/** Row height before the canvas has been measured (also the jsdom default). */
export const BOARD_FALLBACK_ROW_HEIGHT = 64;

/**
 * Pixel height of one grid row so `rowCount` rows (plus the gaps between
 * them) fill `fillHeight` exactly — the board leaves no blank strip at the
 * bottom. Very dense boards clamp to a readable minimum and scroll instead.
 */
export function fitBoardRowHeight(
  fillHeight: number,
  rowCount: number,
): number {
  if (fillHeight <= 0) return BOARD_FALLBACK_ROW_HEIGHT;
  const rows = Math.max(1, rowCount);
  return Math.max(
    BOARD_MIN_ROW_HEIGHT,
    (fillHeight - (rows - 1) * BOARD_MARGIN_Y) / rows,
  );
}
