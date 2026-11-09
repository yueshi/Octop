/** Card sizes and persisted layout for the Workbench MiniApp grid. */

export type AppCardSize = "standard" | "tall" | "large";

export interface AppCardSpan {
  cols: number;
  rows: number;
}

export const APP_CARD_SIZE_ORDER: AppCardSize[] = ["standard", "tall", "large"];

/** standard = 1×1, tall = 1×2, large = 2×2 (width × height cells). */
export const APP_CARD_SPANS: Record<AppCardSize, AppCardSpan> = {
  standard: { cols: 1, rows: 1 },
  tall: { cols: 1, rows: 2 },
  large: { cols: 2, rows: 2 },
};

export interface AppCardLayout {
  order: string[];
  sizes: Record<string, AppCardSize>;
}

export type AppCardLayoutStorage = Pick<Storage, "getItem" | "setItem">;

const STORAGE_KEY = "octop:apps-layout:v1";

function defaultStorage(): AppCardLayoutStorage | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    return window.localStorage;
  } catch {
    // Reading the property itself throws when storage is blocked.
    return undefined;
  }
}

export function isAppCardSize(value: unknown): value is AppCardSize {
  return value === "standard" || value === "tall" || value === "large";
}

export function emptyAppCardLayout(): AppCardLayout {
  return { order: [], sizes: {} };
}

export function loadAppCardLayout(
  storage: AppCardLayoutStorage | undefined = defaultStorage(),
): AppCardLayout {
  if (!storage) return emptyAppCardLayout();
  try {
    const raw: unknown = JSON.parse(storage.getItem(STORAGE_KEY) ?? "null");
    if (!raw || typeof raw !== "object") return emptyAppCardLayout();
    const record = raw as { order?: unknown; sizes?: unknown };
    const order = Array.isArray(record.order)
      ? record.order.filter((id): id is string => typeof id === "string")
      : [];
    const sizes: Record<string, AppCardSize> = {};
    if (record.sizes && typeof record.sizes === "object") {
      for (const [id, size] of Object.entries(
        record.sizes as Record<string, unknown>,
      )) {
        if (isAppCardSize(size)) sizes[id] = size;
      }
    }
    return { order, sizes };
  } catch {
    return emptyAppCardLayout();
  }
}

export function saveAppCardLayout(
  layout: AppCardLayout,
  storage: AppCardLayoutStorage | undefined = defaultStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(layout));
  } catch {
    /* localStorage can be unavailable (private mode / quota). */
  }
}

/** Keep the saved order, drop vanished apps, append newly created ones. */
export function reconcileAppOrder(
  appIds: readonly string[],
  order: readonly string[],
): string[] {
  const present = new Set(appIds);
  const seen = new Set<string>();
  const result: string[] = [];
  for (const id of order) {
    if (present.has(id) && !seen.has(id)) {
      result.push(id);
      seen.add(id);
    }
  }
  for (const id of appIds) {
    if (!seen.has(id)) {
      result.push(id);
      seen.add(id);
    }
  }
  return result;
}

export function moveAppInOrder(
  ids: readonly string[],
  from: number,
  to: number,
): string[] {
  if (from < 0 || from >= ids.length || to < 0 || to >= ids.length) {
    return [...ids];
  }
  const next = [...ids];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}

export function withAppCardSize(
  layout: AppCardLayout,
  appId: string,
  size: AppCardSize,
): AppCardLayout {
  const sizes = { ...layout.sizes };
  if (size === "standard") delete sizes[appId];
  else sizes[appId] = size;
  return { ...layout, sizes };
}

export function nextAppCardSize(size: AppCardSize): AppCardSize {
  const index = APP_CARD_SIZE_ORDER.indexOf(size);
  return APP_CARD_SIZE_ORDER[(index + 1) % APP_CARD_SIZE_ORDER.length];
}

/**
 * Snap a drag-resized cell span to one of the three specs.
 *
 * There is no 2×1 size: a wider card must also be taller, so a horizontal
 * drag alone stays standard.
 */
export function snapAppCardSize(cols: number, rows: number): AppCardSize {
  if (cols >= 2 && rows >= 2) return "large";
  if (rows >= 2) return "tall";
  return "standard";
}
