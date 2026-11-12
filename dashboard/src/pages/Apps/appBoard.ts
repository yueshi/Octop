/**
 * The workbench board: the MiniApps the user pinned, their order, and the
 * size each one takes on the board grid.
 *
 * The board is a personal, per-device choice, so it lives in the browser
 * rather than in the workspace.
 */

/**
 * A board cell occupies a fixed rectangle of the 6x6 grid.
 *
 * Widths divide the canvas evenly (1, 2, 3 or 6 of six columns), heights snap
 * to 1-6 rows, so any picker combination can tile the canvas without gaps.
 * Ids are "<cols>x<rows>"; a freshly pinned MiniApp starts at
 * {@link DEFAULT_APP_BOARD_SIZE}.
 */
export type AppBoardSize =
  | "1x1"
  | "2x2"
  | "2x3"
  | "2x4"
  | "2x6"
  | "3x2"
  | "3x3"
  | "3x4"
  | "3x6"
  | "6x2"
  | "6x3"
  | "6x4"
  | "6x6";

export interface AppBoardSpan {
  cols: number;
  rows: number;
}

export const APP_BOARD_SPANS: Record<AppBoardSize, AppBoardSpan> = {
  "1x1": { cols: 1, rows: 1 },
  "2x2": { cols: 2, rows: 2 },
  "2x3": { cols: 2, rows: 3 },
  "2x4": { cols: 2, rows: 4 },
  "2x6": { cols: 2, rows: 6 },
  "3x2": { cols: 3, rows: 2 },
  "3x3": { cols: 3, rows: 3 },
  "3x4": { cols: 3, rows: 4 },
  "3x6": { cols: 3, rows: 6 },
  "6x2": { cols: 6, rows: 2 },
  "6x3": { cols: 6, rows: 3 },
  "6x4": { cols: 6, rows: 4 },
  "6x6": { cols: 6, rows: 6 },
};

export const APP_BOARD_SIZE_ORDER: AppBoardSize[] = [
  "6x2",
  "6x3",
  "6x4",
  "6x6",
  "3x2",
  "3x3",
  "3x4",
  "3x6",
  "2x2",
  "2x3",
  "2x4",
  "2x6",
  "1x1",
];

export const DEFAULT_APP_BOARD_SIZE: AppBoardSize = "2x4";

/** Size ids used by earlier builds; keep saved boards readable. */
const LEGACY_SIZE_IDS: Record<string, AppBoardSize> = {
  standard: "1x1",
  tall: "2x2",
  large: "2x2",
  tiny: "1x1",
  compact: "2x2",
  panel: "2x4",
  square: "3x3",
  strip: "2x6",
  wide: "3x6",
};

export type AppBoardStorage = Pick<Storage, "getItem" | "setItem">;

export interface AppBoard {
  /** Pinned app ids, in display order. */
  order: string[];
  /** Grid size per pinned app; missing means the default size. */
  sizes: Record<string, AppBoardSize>;
}

const STORAGE_KEY = "octop:apps-board:v1";
/** Earlier builds stored the same `{order, sizes}` shape under this key. */
const LEGACY_STORAGE_KEY = "octop:apps-layout:v1";

function defaultStorage(): AppBoardStorage | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    return window.localStorage;
  } catch {
    // Reading the property itself throws when storage is blocked.
    return undefined;
  }
}

export function isAppBoardSize(value: unknown): value is AppBoardSize {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(APP_BOARD_SPANS, value)
  );
}

/** Map a stored id (current or legacy) onto a supported size. */
function readStoredSize(value: unknown): AppBoardSize | null {
  if (isAppBoardSize(value)) return value;
  if (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(LEGACY_SIZE_IDS, value)
  ) {
    return LEGACY_SIZE_IDS[value];
  }
  return null;
}

export function emptyAppBoard(): AppBoard {
  return { order: [], sizes: {} };
}

function parseStoredBoard(raw: string | null): AppBoard | null {
  if (raw === null) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const record = parsed as { order?: unknown; sizes?: unknown };
    const seen = new Set<string>();
    const order: string[] = [];
    if (Array.isArray(record.order)) {
      for (const id of record.order) {
        if (typeof id !== "string" || !id || seen.has(id)) continue;
        seen.add(id);
        order.push(id);
      }
    }
    const sizes: Record<string, AppBoardSize> = {};
    if (record.sizes && typeof record.sizes === "object") {
      for (const [id, size] of Object.entries(
        record.sizes as Record<string, unknown>,
      )) {
        const read = readStoredSize(size);
        if (read) sizes[id] = read;
      }
    }
    return { order, sizes };
  } catch {
    return null;
  }
}

export function loadAppBoard(
  storage: AppBoardStorage | undefined = defaultStorage(),
): AppBoard {
  if (!storage) return emptyAppBoard();
  try {
    const fresh = parseStoredBoard(storage.getItem(STORAGE_KEY));
    if (fresh) return fresh;
    // Migrate a board saved by the pre-6x6 workbench. The next save writes
    // the new key, so this fallback only runs once per browser.
    return (
      parseStoredBoard(storage.getItem(LEGACY_STORAGE_KEY)) ?? emptyAppBoard()
    );
  } catch {
    return emptyAppBoard();
  }
}

export function saveAppBoard(
  board: AppBoard,
  storage: AppBoardStorage | undefined = defaultStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(board));
  } catch {
    /* localStorage can be unavailable (private mode / quota). */
  }
}

export function isAppOnBoard(board: AppBoard, appId: string): boolean {
  return board.order.includes(appId);
}

/** Pin an app at the end of the board. Pinning twice is a no-op. */
export function addAppToBoard(board: AppBoard, appId: string): AppBoard {
  if (!appId || isAppOnBoard(board, appId)) return board;
  return { ...board, order: [...board.order, appId] };
}

export function removeAppFromBoard(board: AppBoard, appId: string): AppBoard {
  if (!isAppOnBoard(board, appId)) return board;
  const sizes = { ...board.sizes };
  delete sizes[appId];
  return {
    order: board.order.filter((id) => id !== appId),
    sizes,
  };
}

/**
 * Move a pinned app one slot up (-1) or down (+1). Cells are not dragged
 * around, so ordering goes through explicit buttons.
 */
export function moveAppInBoard(
  board: AppBoard,
  appId: string,
  offset: -1 | 1,
): AppBoard {
  const from = board.order.indexOf(appId);
  if (from < 0) return board;
  const to = from + offset;
  if (to < 0 || to >= board.order.length) return board;
  const order = [...board.order];
  const [moved] = order.splice(from, 1);
  order.splice(to, 0, moved);
  return { ...board, order };
}

/** Drop pinned apps that are gone (deleted, or access revoked). */
export function reconcileAppBoard(
  board: AppBoard,
  appIds: readonly string[],
): AppBoard {
  const present = new Set(appIds);
  const order = board.order.filter((id) => present.has(id));
  const sizes = Object.fromEntries(
    Object.entries(board.sizes).filter(([id]) => present.has(id)),
  );
  const unchanged =
    order.length === board.order.length &&
    Object.keys(sizes).length === Object.keys(board.sizes).length;
  return unchanged ? board : { order, sizes };
}

export function sizeOfApp(board: AppBoard, appId: string): AppBoardSize {
  return board.sizes[appId] ?? DEFAULT_APP_BOARD_SIZE;
}

/** Set the grid size of a pinned app. The default size clears the override. */
export function withAppSize(
  board: AppBoard,
  appId: string,
  size: AppBoardSize,
): AppBoard {
  const sizes = { ...board.sizes };
  if (size === DEFAULT_APP_BOARD_SIZE) delete sizes[appId];
  else sizes[appId] = size;
  return { ...board, sizes };
}

/**
 * Snap a dragged cell span to the closest supported shape.
 *
 * Both dimensions matter: the nearest shape is the one whose relative
 * distance in width and height is smallest. Ties prefer the shape that keeps
 * the dragged width, then height, then the smaller cell — dragging just past
 * three columns should not jump straight to a full-width band.
 */
export function snapAppBoardSize(cols: number, rows: number): AppBoardSize {
  const EPS = 1e-9;
  let best: AppBoardSize = DEFAULT_APP_BOARD_SIZE;
  let bestScore = Infinity;
  let bestW = Infinity;
  let bestH = Infinity;
  let bestArea = Infinity;
  for (const size of APP_BOARD_SIZE_ORDER) {
    const span = APP_BOARD_SPANS[size];
    const w = Math.abs(cols - span.cols) / span.cols;
    const h = Math.abs(rows - span.rows) / span.rows;
    const score = Math.max(w, h);
    const area = span.cols * span.rows;
    const better =
      score < bestScore - EPS ||
      (Math.abs(score - bestScore) <= EPS &&
        (w < bestW - EPS ||
          (Math.abs(w - bestW) <= EPS &&
            (h < bestH - EPS ||
              (Math.abs(h - bestH) <= EPS && area < bestArea)))));
    if (better) {
      best = size;
      bestScore = score;
      bestW = w;
      bestH = h;
      bestArea = area;
    }
  }
  return best;
}
