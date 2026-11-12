import type { ChatMessage } from "../hooks/sseHelpers";
import { parseOctopToolOutput } from "../../../plugins/toolRenderers/parseToolOutput";

const APP_CARD_RENDERER = "app_card";
const APP_WRITE_TOOL = "app_write_html";

interface AppCardInfo {
  appId: string;
  toolName: string;
}

function appCardInfo(message: ChatMessage): AppCardInfo | null {
  const toolData = message.toolData;
  if (!toolData?.name || !toolData.output) return null;
  const parsed = parseOctopToolOutput(toolData.output);
  if (parsed.octopUi?.renderer !== APP_CARD_RENDERER) return null;
  const data = parsed.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const appId = (data as { app_id?: unknown }).app_id;
  if (typeof appId !== "string" || !appId) return null;
  return { appId, toolName: toolData.name };
}

/** Keep the tool message but stop its ``app_card`` from pinning to the chat. */
function withSilentCard(message: ChatMessage): ChatMessage {
  const parsed = parseOctopToolOutput(message.toolData?.output);
  const raw = parsed.raw;
  const envelope =
    raw && typeof raw === "object" && !Array.isArray(raw)
      ? { ...(raw as Record<string, unknown>) }
      : {};
  const data =
    envelope.data &&
    typeof envelope.data === "object" &&
    !Array.isArray(envelope.data)
      ? { ...(envelope.data as Record<string, unknown>), silent: true }
      : { silent: true };
  envelope.data = data;
  return {
    ...message,
    toolData: {
      ...message.toolData,
      output: JSON.stringify(envelope),
    },
  };
}

/** Refresh the kept card's snapshot without moving it in the transcript. */
function withUpdatedCard(kept: ChatMessage, latest: ChatMessage): ChatMessage {
  return {
    ...kept,
    toolData: {
      ...kept.toolData,
      output: latest.toolData?.output,
    },
  };
}

/**
 * At most one MiniApp entry card per conversation turn.
 *
 * A turn starts at each user message. ``app_open`` is the explicit "show the
 * app card" action; ``app_write_html`` only edits the shell. Keep the first
 * explicit open/template card in a turn and demote every write-html card and
 * later duplicate to the folded tool list. If the same app is opened again
 * later in the turn, the kept card refreshes its snapshot (name / row count /
 * preview availability) without adding a second card. A later turn may show
 * one fresh card.
 */
export function dedupeAppCardMessages(
  messages: readonly ChatMessage[],
): ChatMessage[] {
  const out: ChatMessage[] = [];
  let turnHasCard = false;
  let keptIndex = -1;
  let keptAppId: string | null = null;

  for (const message of messages) {
    if (message.role === "user") {
      turnHasCard = false;
      keptIndex = -1;
      keptAppId = null;
      out.push(message);
      continue;
    }
    const info = appCardInfo(message);
    if (!info) {
      out.push(message);
      continue;
    }
    if (info.toolName === APP_WRITE_TOOL) {
      out.push(withSilentCard(message));
      continue;
    }
    if (!turnHasCard) {
      turnHasCard = true;
      keptIndex = out.length;
      keptAppId = info.appId;
      out.push(message);
      continue;
    }
    if (keptIndex >= 0 && keptAppId === info.appId) {
      out[keptIndex] = withUpdatedCard(out[keptIndex], message);
    }
    out.push(withSilentCard(message));
  }
  return out;
}
