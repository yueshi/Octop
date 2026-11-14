import { AppWindow, Globe, MessageSquare, type LucideIcon } from "lucide-react";

export interface DesktopChatSeed {
  id: number;
  text: string;
  /** MiniApp this prefill belongs to, so the chat context follows the seed. */
  appId?: string;
}

export interface DesktopWindowBounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type DesktopWindowKind = "app" | "chat" | "external";

export interface DesktopWindowState extends DesktopWindowBounds {
  /** Stable window key: `app:<id>`, `chat:<id>` or `external:<space>:<url>`. */
  id: string;
  kind: DesktopWindowKind;
  appId?: string;
  agentId?: string;
  /** Desktop space this window belongs to (especially for external pages). */
  spaceId?: string;
  /** External web window. */
  url?: string;
  title?: string;
  /** MiniApp context selected inside a chat window. */
  contextAppId?: string | null;
  /** One-shot composer prefill handed to the chat window. */
  seed?: DesktopChatSeed;
  z: number;
  minimized: boolean;
  maximized: boolean;
  restore?: DesktopWindowBounds;
}

export interface DesktopWindowIdentity {
  appId?: string;
  agentId?: string;
  spaceId?: string;
  url?: string;
}

export const EXTERNAL_PROTOCOL = /^https?:\/\//i;

/**
 * One descriptor per desktop window kind. Renderers, ids, minimum sizes and
 * dock/menu labels all read from this registry so a new window kind does not
 * have to be threaded through each open/focus/persist branch.
 */
export interface DesktopWindowKindSpec {
  kind: DesktopWindowKind;
  labelKey: string;
  labelFallback: string;
  icon: LucideIcon;
  minWidth: number;
  minHeight: number;
  identity: (parts: DesktopWindowIdentity) => string | null;
  isValid: (win: DesktopWindowState) => boolean;
}

export const WINDOW_REGISTRY: Record<DesktopWindowKind, DesktopWindowKindSpec> =
  {
    app: {
      kind: "app",
      labelKey: "apps.desktop.command.appWindow",
      labelFallback: "MiniApp window",
      icon: AppWindow,
      minWidth: 300,
      minHeight: 220,
      identity: ({ appId }) => (appId ? `app:${appId}` : null),
      isValid: (win) => Boolean(win.appId),
    },
    chat: {
      kind: "chat",
      labelKey: "apps.desktop.command.chatWindow",
      labelFallback: "Expert chat",
      icon: MessageSquare,
      minWidth: 420,
      minHeight: 360,
      identity: ({ agentId }) => (agentId ? `chat:${agentId}` : null),
      isValid: (win) => Boolean(win.agentId),
    },
    external: {
      kind: "external",
      labelKey: "apps.desktop.command.externalWindow",
      labelFallback: "Web page",
      icon: Globe,
      minWidth: 340,
      minHeight: 520,
      identity: ({ spaceId, url }) =>
        url && EXTERNAL_PROTOCOL.test(url)
          ? `external:${spaceId ?? "__unassigned__"}:${url}`
          : null,
      isValid: (win) => Boolean(win.url && EXTERNAL_PROTOCOL.test(win.url)),
    },
  };

export const MENU_BAR_HEIGHT = 38;
/** Default gap above the visible dock surface before it is measured. */
export const DOCK_SPACE = 80;

export const WINDOWS_STORE_KEY = "octop:apps-desktop-windows:v1";
const WINDOWS_STORE_LIMIT = 30;

export function appWindowId(appId: string): string {
  return WINDOW_REGISTRY.app.identity({ appId })!;
}

export function chatWindowId(agentId: string): string {
  return WINDOW_REGISTRY.chat.identity({ agentId })!;
}

export function externalWindowId(spaceId: string, url: string): string | null {
  return WINDOW_REGISTRY.external.identity({ spaceId, url });
}

export function externalWindowTitle(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./i, "");
  } catch {
    return url;
  }
}

export function windowSpaceId(
  win: DesktopWindowState,
  appSpace: ReadonlyMap<string, string>,
): string | undefined {
  if (win.spaceId) return win.spaceId;
  if (win.kind === "chat") return win.agentId;
  return win.appId ? appSpace.get(win.appId) : undefined;
}

export interface DesktopWindowTitleContext {
  apps: ReadonlyMap<string, { name: string }>;
  spaces: ReadonlyMap<string, { name: string }>;
}

/** Resolve the one title used by window chrome, Mission Control and the dock. */
export function desktopWindowTitle(
  win: DesktopWindowState,
  ctx: DesktopWindowTitleContext,
): string {
  const app = win.kind === "app" && win.appId ? ctx.apps.get(win.appId) : null;
  if (app) return app.name;
  if (win.kind === "external") {
    return win.title || externalWindowTitle(win.url ?? "");
  }
  const space = ctx.spaces.get(win.agentId ?? win.spaceId ?? "");
  return space?.name ?? win.title ?? win.agentId ?? "";
}

export interface NewDesktopWindowInput extends DesktopWindowIdentity {
  kind: DesktopWindowKind;
  title?: string;
  contextAppId?: string | null;
  seed?: DesktopChatSeed;
  z: number;
  list: readonly DesktopWindowState[];
  appSpace: ReadonlyMap<string, string>;
  viewportWidth: number;
  viewportHeight: number;
  /** Pixels reserved at the bottom for the dock (measured by the desktop). */
  bottomInset?: number;
}

/**
 * Build a window with the registry's size rules and a per-space cascade.
 * Returns ``null`` when the identity is incomplete (e.g. external without URL).
 */
export function createDesktopWindow(
  input: NewDesktopWindowInput,
): DesktopWindowState | null {
  const spec = WINDOW_REGISTRY[input.kind];
  const id = spec.identity(input);
  if (!id) return null;
  const maxW = input.viewportWidth || 1280;
  const maxH = input.viewportHeight || 800;
  const bottomInset = input.bottomInset ?? DOCK_SPACE;
  const maxWindowH = Math.max(
    spec.minHeight,
    maxH - MENU_BAR_HEIGHT - 8 - bottomInset,
  );
  const peers = input.list.filter(
    (win) => windowSpaceId(win, input.appSpace) === input.spaceId,
  ).length;
  const cascade = peers % 6;

  let width: number;
  let height: number;
  let x: number;
  let y: number;
  if (input.kind === "chat") {
    width = Math.max(spec.minWidth, Math.min(920, maxW - 56));
    height = Math.max(spec.minHeight, Math.min(680, maxWindowH));
    x = Math.round((maxW - width) / 2) + cascade * 20;
    y = 48 + cascade * 20;
  } else if (input.kind === "external") {
    width = Math.max(spec.minWidth, Math.min(430, maxW - 64));
    height = Math.max(spec.minHeight, Math.min(780, maxWindowH));
    x = Math.round((maxW - width) / 2) + cascade * 22;
    y = 44 + cascade * 22;
  } else {
    width = Math.max(spec.minWidth, Math.min(760, maxW - 40));
    height = Math.max(spec.minHeight, Math.min(560, maxWindowH));
    x = 64 + cascade * 28;
    y = 56 + cascade * 24;
  }

  return {
    id,
    kind: input.kind,
    appId: input.appId,
    agentId: input.agentId,
    spaceId: input.spaceId,
    url: input.url,
    title:
      input.kind === "external"
        ? input.title || externalWindowTitle(input.url ?? "")
        : input.title,
    contextAppId: input.contextAppId,
    seed: input.seed,
    x: Math.max(8, Math.min(maxW - width - 8, x)),
    y: Math.max(MENU_BAR_HEIGHT + 8, Math.min(maxH - bottomInset - height, y)),
    w: width,
    h: height,
    z: input.z,
    minimized: false,
    maximized: false,
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asFinite(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function readBounds(value: unknown): DesktopWindowBounds | undefined {
  const row = asRecord(value);
  if (!row) return undefined;
  const x = asFinite(row.x, Number.NaN);
  const y = asFinite(row.y, Number.NaN);
  const w = asFinite(row.w, Number.NaN);
  const h = asFinite(row.h, Number.NaN);
  if (![x, y, w, h].every(Number.isFinite)) return undefined;
  return { x, y, w, h };
}

/** Restore last session's windows; malformed rows are discarded. */
export function loadDesktopWindows(): DesktopWindowState[] {
  try {
    const raw = localStorage.getItem(WINDOWS_STORE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.slice(0, WINDOWS_STORE_LIMIT).flatMap((entry) => {
      const row = asRecord(entry);
      if (!row) return [];
      const kind = row.kind;
      if (kind !== "app" && kind !== "chat" && kind !== "external") {
        return [];
      }
      const id = asString(row.id);
      if (!id) return [];
      const spec = WINDOW_REGISTRY[kind];
      const candidate = {
        id,
        kind,
        appId: asString(row.appId),
        agentId: asString(row.agentId),
        spaceId: asString(row.spaceId),
        url: asString(row.url),
        title: asString(row.title),
        contextAppId: asString(row.contextAppId),
        x: asFinite(row.x, 0),
        y: asFinite(row.y, MENU_BAR_HEIGHT),
        w: asFinite(row.w, spec.minWidth),
        h: asFinite(row.h, spec.minHeight),
        z: Math.max(1, asFinite(row.z, 20)),
        minimized: row.minimized === true,
        maximized: row.maximized === true,
        restore: readBounds(row.restore),
      } satisfies DesktopWindowState;
      if (!spec.isValid(candidate)) return [];
      return [
        {
          ...candidate,
          w: Math.max(spec.minWidth, candidate.w),
          h: Math.max(spec.minHeight, candidate.h),
        },
      ];
    });
  } catch {
    return [];
  }
}

export function saveDesktopWindows(windows: DesktopWindowState[]): void {
  try {
    const rows = windows.slice(0, WINDOWS_STORE_LIMIT).map((win) => {
      const { seed: _seed, ...rest } = win;
      return rest;
    });
    localStorage.setItem(WINDOWS_STORE_KEY, JSON.stringify(rows));
  } catch {
    // Session restore is best-effort; private mode or quota failures are fine.
  }
}
