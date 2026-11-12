import type { AppSummary } from "../../../api/modules/apps";
import type { ChatMessage } from "../hooks/sseHelpers";
import { parseOctopToolOutput } from "../../../plugins/toolRenderers/parseToolOutput";

const APP_CARD_RENDERER = "app_card";

/** A MiniApp that an app card in this conversation pointed at. */
export interface SessionApp {
  app_id: string;
  name: string;
  description?: string;
  row_count?: number;
  has_html?: boolean;
  has_preview?: boolean;
}

function readAppCard(message: ChatMessage): SessionApp | null {
  const output = message.toolData?.output;
  // Cheap pre-filter: the transcript also holds large bash/file outputs, and
  // re-parsing every one of them on each streamed token is wasteful.
  if (typeof output === "string" && !output.includes(APP_CARD_RENDERER)) {
    return null;
  }
  const parsed = parseOctopToolOutput(output);
  if (parsed.octopUi?.renderer !== APP_CARD_RENDERER) return null;
  const data = parsed.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const record = data as Record<string, unknown>;
  const appId = record.app_id;
  if (typeof appId !== "string" || !appId) return null;

  const app: SessionApp = {
    app_id: appId,
    name: typeof record.name === "string" && record.name ? record.name : appId,
  };
  if (typeof record.description === "string") {
    app.description = record.description;
  }
  if (typeof record.row_count === "number") {
    app.row_count = record.row_count;
  }
  if (typeof record.has_html === "boolean") {
    app.has_html = record.has_html;
  }
  if (typeof record.has_preview === "boolean") {
    app.has_preview = record.has_preview;
  }
  return app;
}

/**
 * MiniApps created or opened in this conversation, most recent first.
 *
 * The session has no server-side app list — the transcript is the record.
 * Every `app_card` output contributes an entry; a later card for the same app
 * refreshes its snapshot (name / row count / preview) instead of adding a row.
 */
export function collectSessionApps(
  messages: readonly ChatMessage[],
): SessionApp[] {
  const byId = new Map<string, SessionApp>();
  for (const message of messages) {
    const app = readAppCard(message);
    if (!app) continue;
    // Re-insert so the most recently mentioned app ends up last.
    byId.delete(app.app_id);
    byId.set(app.app_id, app);
  }
  return Array.from(byId.values()).reverse();
}

/** Server summaries for the whole thread → the dock list rows. */
export function sessionAppsFromSummaries(
  rows: readonly AppSummary[],
): SessionApp[] {
  return rows.map((row) => ({
    app_id: row.app_id,
    name: row.name,
    ...(row.description ? { description: row.description } : {}),
    row_count: row.row_count,
    has_html: row.has_html,
    has_preview: row.has_preview,
  }));
}

/**
 * Whole-thread server results with live transcript cards on top.
 *
 * The loaded history page is the freshest source for a card that has not been
 * persisted yet; the server scan is the only complete source for older ones.
 * Server fields win for an app both know about (current name / row count).
 */
export function mergeSessionApps(
  serverApps: readonly SessionApp[] | null,
  transcriptApps: readonly SessionApp[],
): SessionApp[] {
  if (!serverApps) return [...transcriptApps];
  const known = new Set(serverApps.map((app) => app.app_id));
  return [
    ...transcriptApps.filter((app) => !known.has(app.app_id)),
    ...serverApps,
  ];
}
