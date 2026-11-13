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

function clampSize(item: BoardItem): BoardItem {
  return {
    ...item,
    w: Math.min(BOARD_GRID_COLS, Math.max(1, item.w)),
    h: Math.min(BOARD_GRID_COLS, Math.max(1, item.h)),
  };
}

/** Add a new cell at the first free slot instead of the very bottom. */
export function addBoardItem(
  items: BoardItem[],
  appId: string,
  w = 4,
  h = 4,
): BoardItem[] {
  if (items.some((item) => item.app_id === appId)) return items;
  const nextW = Math.min(BOARD_GRID_COLS, Math.max(1, w));
  const nextH = Math.min(BOARD_GRID_COLS, Math.max(1, h));
  const spot = firstFreeSpot(items, nextW, nextH);
  return [...items, { app_id: appId, ...spot, w: nextW, h: nextH }];
}

/** First free rectangle scanning top-to-bottom, then left-to-right. */
function firstFreeSpot(
  placed: BoardItem[],
  w: number,
  h: number,
): { x: number; y: number } {
  const maxY = placed.reduce((max, item) => Math.max(max, item.y + item.h), 0);
  for (let y = 0; y <= maxY; y += 1) {
    for (let x = 0; x <= BOARD_GRID_COLS - w; x += 1) {
      const candidate = { app_id: "", x, y, w, h };
      if (!placed.some((other) => overlaps(candidate, other))) {
        return { x, y };
      }
    }
  }
  return { x: 0, y: maxY };
}

/**
 * Pack every cell at its chosen size. Locked cells are anchors; unlocked
 * cells are placed largest-first into the first free rectangle, so a user's
 * small/large mix survives the arrange instead of being flattened to a
 * uniform grid. The result is undoable from the toolbar.
 */
export function autoArrangeBoardLayout(items: BoardItem[]): BoardItem[] {
  if (items.length === 0) return [];
  const placed = sortByPosition(
    items.filter((item) => item.locked).map(clampSize),
  );
  const unlocked = items
    .filter((item) => !item.locked)
    .map(clampSize)
    .sort(
      (left, right) =>
        right.w * right.h - left.w * left.h ||
        left.y - right.y ||
        left.x - right.x ||
        left.app_id.localeCompare(right.app_id),
    );
  for (const item of unlocked) {
    placed.push({ ...item, ...firstFreeSpot(placed, item.w, item.h) });
  }
  return sortByPosition(placed);
}
