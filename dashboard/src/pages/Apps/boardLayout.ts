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

/** Quick size choices offered per cell. Labels live in the dashboard locale. */
export interface BoardSizePreset {
  key: string;
  w: number;
  h: number;
}

export const BOARD_SIZE_PRESETS: readonly BoardSizePreset[] = [
  { key: "small", w: 3, h: 2 },
  { key: "medium", w: 4, h: 3 },
  { key: "large", w: 6, h: 4 },
  { key: "wide", w: 12, h: 3 },
  { key: "tall", w: 3, h: 6 },
];

function overlaps(left: BoardItem, right: BoardItem): boolean {
  return (
    left.x < right.x + right.w &&
    left.x + left.w > right.x &&
    left.y < right.y + right.h &&
    left.y + left.h > right.y
  );
}

function sortByPosition(items: BoardItem[]): BoardItem[] {
  return [...items].sort(
    (left, right) =>
      left.y - right.y ||
      left.x - right.x ||
      left.app_id.localeCompare(right.app_id),
  );
}

/**
 * Resize one cell to a preset, keeping it as close to its current position as
 * possible. If the new rectangle would overlap a neighbour it moves down until
 * it fits; RGL then compacts it upward normally.
 */
export function placeBoardItem(
  items: BoardItem[],
  appId: string,
  w: number,
  h: number,
): BoardItem[] {
  const target = items.find((item) => item.app_id === appId);
  if (!target) return items;
  const nextW = Math.min(BOARD_GRID_COLS, Math.max(1, Math.round(w)));
  const nextH = Math.min(BOARD_GRID_COLS, Math.max(1, Math.round(h)));
  const x = Math.max(0, Math.min(target.x, BOARD_GRID_COLS - nextW));
  const others = items.filter((item) => item.app_id !== appId);
  let y = Math.max(0, target.y);
  let candidate: BoardItem = { ...target, x, y, w: nextW, h: nextH };
  while (others.some((other) => overlaps(candidate, other))) {
    y += 1;
    candidate = { ...candidate, y };
  }
  return items.map((item) => (item.app_id === appId ? candidate : item));
}

/**
 * Locked cells stay exactly where they are; unlocked cells keep their chosen
 * sizes and fill the first free rectangle around those anchors.
 */
function packUnlockedAroundLocks(items: BoardItem[]): BoardItem[] {
  const locked = sortByPosition(items.filter((item) => item.locked));
  const placed: BoardItem[] = locked.map((item) => ({
    ...item,
    w: Math.min(BOARD_GRID_COLS, Math.max(1, item.w)),
    h: Math.min(BOARD_GRID_COLS, Math.max(1, item.h)),
  }));
  const unlocked = sortByPosition(items.filter((item) => !item.locked));
  for (const item of unlocked) {
    const w = Math.min(BOARD_GRID_COLS, Math.max(1, item.w));
    const h = Math.min(BOARD_GRID_COLS, Math.max(1, item.h));
    let spot: { x: number; y: number } | null = null;
    for (let y = 0; !spot && y < 256; y += 1) {
      for (let x = 0; x <= BOARD_GRID_COLS - w; x += 1) {
        const candidate = { ...item, x, y, w, h };
        if (!placed.some((other) => overlaps(candidate, other))) {
          spot = { x, y };
          break;
        }
      }
    }
    placed.push({ ...item, ...(spot ?? { x: 0, y: 256 }), w, h });
  }
  return sortByPosition(placed);
}

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
  if (items.some((item) => item.locked)) {
    return packUnlockedAroundLocks(items);
  }
  const ordered = sortByPosition(items);

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
