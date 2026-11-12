import type { BoardItem } from "../../api/modules/boards";

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

/** Two grid rows per auto-fill band keeps cells readable on dense boards. */
const AUTO_FILL_BAND = 2;
const AUTO_FILL_MAX_COLS = 4;

/** Split 12 columns across `parts` cells as evenly as possible. */
export function splitBoardColumns(parts: number): number[] {
  const count = Math.max(1, parts);
  const base = Math.floor(BOARD_GRID_COLS / count);
  const remainder = BOARD_GRID_COLS % count;
  return Array.from(
    { length: count },
    (_, index) => base + (index < remainder ? 1 : 0),
  );
}

/**
 * Rearrange every MiniApp into full-width bands so the board is tiled without
 * gaps. The last band spreads its cells across the full twelve columns.
 *
 * 1 app: 12×2 · 2 apps: 6+6 · 3 apps: 4+4+4 · 4 apps: 3+3+3+3 · 5 apps: a
 * row of 3 then 6+6 · 9 apps: 4+4+4 per row.
 */
export function autoFillBoardLayout(items: BoardItem[]): BoardItem[] {
  const count = items.length;
  if (count === 0) return [];
  const ordered = [...items].sort(
    (left, right) =>
      left.y - right.y ||
      left.x - right.x ||
      left.app_id.localeCompare(right.app_id),
  );

  let bestCols = 1;
  let bestScore = Number.POSITIVE_INFINITY;
  for (let cols = 1; cols <= AUTO_FILL_MAX_COLS; cols += 1) {
    const rows = Math.ceil(count / cols);
    const waste = cols * rows - count;
    // Prefer fewer rows, then fewer empty slots, then more columns.
    const score = rows * 100 + waste;
    if (score < bestScore || (score === bestScore && cols > bestCols)) {
      bestCols = cols;
      bestScore = score;
    }
  }

  const rows = Math.ceil(count / bestCols);
  const result: BoardItem[] = [];
  let index = 0;
  for (let row = 0; row < rows; row += 1) {
    const inRow = Math.min(bestCols, count - row * bestCols);
    const widths = splitBoardColumns(inRow);
    let x = 0;
    for (let column = 0; column < inRow; column += 1) {
      const source = ordered[index];
      index += 1;
      const w = widths[column];
      result.push({
        app_id: source.app_id,
        x,
        y: row * AUTO_FILL_BAND,
        w,
        h: AUTO_FILL_BAND,
      });
      x += w;
    }
  }
  return result;
}
