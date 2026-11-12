/**
 * Page-facing capabilities for a MiniApp.
 *
 * The host bridge is the enforcement point for page actions: the iframe has
 * no JWT and no network (`connect-src 'none'`), so every request it makes
 * goes through this allowlist. A missing list means an older payload; treat
 * it as the legacy all-on set rather than breaking existing apps.
 */
export const CAP = {
  DATA_READ: "data.read",
  DATA_WRITE: "data.write",
  ASSET_READ: "asset.read",
  EXTERNAL_OPEN: "external.open",
  CHAT_COMPOSE: "chat.compose",
  EVENTS_READ: "events.read",
  AI_GENERATE: "ai.generate",
  GAME_LAUNCH: "game.launch",
  EXTERNAL_FETCH: "external.fetch",
  EVENTS_EMIT: "events.emit",
  SCHEDULES_MANAGE: "schedules.manage",
} as const;

export const ALL_CAPABILITIES: readonly string[] = Object.values(CAP);

const METHOD_CAPABILITY: Record<string, string> = {
  schema: CAP.DATA_READ,
  tables: CAP.DATA_READ,
  rows: CAP.DATA_READ,
  append: CAP.DATA_WRITE,
  generate: CAP.AI_GENERATE,
  launchGame: CAP.GAME_LAUNCH,
  asset: CAP.ASSET_READ,
  openExternal: CAP.EXTERNAL_OPEN,
  composeMessage: CAP.CHAT_COMPOSE,
};

export function effectiveCapabilities(
  capabilities?: readonly string[],
): readonly string[] {
  return capabilities ?? ALL_CAPABILITIES;
}

export function isMethodAllowed(
  method: string | undefined,
  capabilities?: readonly string[],
): boolean {
  if (!method) return false;
  const required = METHOD_CAPABILITY[method];
  if (!required || !capabilities) return true;
  return capabilities.includes(required);
}
