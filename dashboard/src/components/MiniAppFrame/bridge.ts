import { effectiveCapabilities, isMethodAllowed } from "./capabilities";

/** Messages the app page sends up to the host. */
export interface AppRequest {
  source?: string;
  type?: string;
  id?: number;
  method?: string;
  params?: BridgeParams;
}

export interface BridgeParams {
  limit?: number;
  offset?: number;
  table?: string | null;
  row?: Record<string, unknown>;
  url?: string;
  name?: string;
  prompt?: string;
  system?: string;
  gameId?: string;
  text?: string;
}

export type SecurityEventKind = "csp_violation" | "sandbox_integrity";

export interface AppViolation {
  kind: SecurityEventKind;
  directive: string;
  blockedURI: string;
  disposition?: string;
}

export type BridgeReply = (payload: Record<string, unknown>) => void;

/** Handles one bridge method and returns the value handed back to the app. */
export type BridgeHandler = (
  method: string,
  params: BridgeParams,
) => Promise<unknown> | unknown;

export interface BridgeContext {
  /**
   * Capabilities the page may use; omit for legacy apps (all allowed).
   *
   * A getter is accepted so a host that edits the manifest without re-mounting
   * the frame (the detail page) does not have to rebuild the bridge.
   */
  capabilities?: readonly string[] | (() => readonly string[] | undefined);
  /** Handles the request and returns its result. */
  handle: BridgeHandler;
  /** Called when the page asks for something the capability list forbids. */
  onDenied?: (method: string) => void;
  /** Called for CSP violations the page reports (throttled per frame). */
  onViolation?: (violation: AppViolation) => void;
}

function resolveCapabilities(
  capabilities: BridgeContext["capabilities"],
): readonly string[] | undefined {
  return typeof capabilities === "function" ? capabilities() : capabilities;
}

const VIOLATION_WINDOW_MS = 60_000;
const MAX_VIOLATIONS_PER_WINDOW = 20;

/** Every method the bridge implements. */
export const KNOWN_METHODS: ReadonlySet<string> = new Set([
  "me",
  "schema",
  "tables",
  "rows",
  "append",
  "generate",
  "composeMessage",
  "launchGame",
  "openExternal",
  "asset",
]);

/**
 * Whether a message is a bridge request from the app frame.
 *
 * The `event.source` check (done by the caller, which knows the frame) is what
 * keeps other windows — a popup, an embedder, another dashboard frame — from
 * driving the bridge.
 */
export function isAppRequest(data: unknown): data is AppRequest {
  if (!data || typeof data !== "object") return false;
  const request = data as AppRequest;
  return (
    request.source === "octop-app" &&
    request.type === "request" &&
    typeof request.id === "number"
  );
}

/**
 * Check a request against the app's capability manifest.
 *
 * Unknown methods are denied. `isMethodAllowed` returns true for methods it
 * does not know, which would make every future bridge method open by default.
 */
export function checkMethodAllowed(
  method: string | undefined,
  capabilities?: readonly string[],
): boolean {
  if (!method) return false;
  if (!KNOWN_METHODS.has(method)) return false;
  return isMethodAllowed(method, effectiveCapabilities(capabilities));
}

/**
 * Attach the bridge to the window.
 *
 * `getFrameWindow` returns the app frame's content window; only messages from
 * it are accepted. Returns a disposer.
 */
export function attachBridge(
  ctx: BridgeContext,
  getFrameWindow: () => Window | null,
): () => void {
  let violationCount = 0;
  let violationWindowStart = Date.now();

  const onMessage = (event: MessageEvent) => {
    const frameWindow = getFrameWindow();
    if (!frameWindow || event.source !== frameWindow) return;
    const data: unknown = event.data;
    if (!data || typeof data !== "object") return;
    const record = data as Record<string, unknown>;
    if (record.source !== "octop-app") return;

    // CSP violations are telemetry, not requests: no capability check, but
    // throttle so a broken page cannot flood the audit log.
    if (record.type === "violation") {
      if (!ctx.onViolation) return;
      const now = Date.now();
      if (now - violationWindowStart > VIOLATION_WINDOW_MS) {
        violationWindowStart = now;
        violationCount = 0;
      }
      if (violationCount >= MAX_VIOLATIONS_PER_WINDOW) return;
      violationCount += 1;
      ctx.onViolation({
        kind:
          record.kind === "sandbox_integrity"
            ? "sandbox_integrity"
            : "csp_violation",
        directive: String(record.directive ?? "").slice(0, 200),
        blockedURI: String(record.blockedURI ?? "").slice(0, 2000),
        disposition: String(record.disposition ?? "").slice(0, 32),
      });
      return;
    }

    if (!isAppRequest(data)) return;

    const reply: BridgeReply = (payload) => {
      frameWindow.postMessage(
        { source: "octop-app-host", id: data.id, ...payload },
        "*",
      );
    };

    if (
      !checkMethodAllowed(data.method, resolveCapabilities(ctx.capabilities))
    ) {
      ctx.onDenied?.(data.method ?? "");
      reply({ ok: false, error: "capability not declared" });
      return;
    }

    void (async () => {
      try {
        const result = await ctx.handle(
          data.method as string,
          data.params ?? {},
        );
        reply({ ok: true, data: result });
      } catch (err) {
        reply({
          ok: false,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    })();
  };
  window.addEventListener("message", onMessage);
  return () => window.removeEventListener("message", onMessage);
}
