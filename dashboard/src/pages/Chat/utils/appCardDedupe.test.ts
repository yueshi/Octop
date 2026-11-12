import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../hooks/sseHelpers";
import { parseOctopToolOutput } from "../../../plugins/toolRenderers/parseToolOutput";
import { isPinnedToolUiMessage } from "../../../plugins/toolRenderers/isPinnedToolUi";
import { dedupeAppCardMessages } from "./appCardDedupe";

function appCardMessage(
  id: string,
  toolName: string,
  appId: string,
  data: Record<string, unknown> = {},
): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    timestamp: 0,
    status: "done",
    toolData: {
      name: toolName,
      output: JSON.stringify({
        octop_ui: { renderer: "app_card", version: 1 },
        data: { app_id: appId, name: appId, has_html: true, ...data },
      }),
    },
  };
}

function isSilent(message: ChatMessage): boolean {
  const parsed = parseOctopToolOutput(message.toolData?.output);
  return (parsed.data as { silent?: boolean } | undefined)?.silent === true;
}

describe("dedupeAppCardMessages", () => {
  it("drops the write-html card and keeps the first app_open card", () => {
    const write = appCardMessage("write", "app_write_html", "app_1");
    const open = appCardMessage("open", "app_open", "app_1");
    const result = dedupeAppCardMessages([write, open]);

    expect(result[0]).not.toBe(write);
    expect(isSilent(result[0])).toBe(true);
    expect(isSilent(result[1])).toBe(false);
    expect(isPinnedToolUiMessage(result[0])).toBe(false);
    expect(isPinnedToolUiMessage(result[1])).toBe(true);
  });

  it("keeps only the first open card in a turn", () => {
    const first = appCardMessage("first", "app_open", "app_1");
    const second = appCardMessage("second", "app_open", "app_1");
    const result = dedupeAppCardMessages([first, second]);

    expect(isSilent(result[0])).toBe(false);
    expect(isSilent(result[1])).toBe(true);
  });

  it("refreshes the kept card from a later same-app open in the same turn", () => {
    const first = appCardMessage("first", "app_open", "app_1", {
      row_count: 0,
      has_preview: false,
    });
    const latest = appCardMessage("latest", "app_open", "app_1", {
      row_count: 9,
      has_preview: true,
    });
    const result = dedupeAppCardMessages([first, latest]);

    const kept = parseOctopToolOutput(result[0].toolData?.output).data as {
      row_count?: number;
      has_preview?: boolean;
    };
    expect(kept.row_count).toBe(9);
    expect(kept.has_preview).toBe(true);
    expect(isSilent(result[1])).toBe(true);
  });

  it("allows one fresh card in the next user turn", () => {
    const user: ChatMessage = {
      id: "user",
      role: "user",
      content: "open it again",
      timestamp: 0,
    };
    const first = appCardMessage("first", "app_open", "app_1");
    const second = appCardMessage("second", "app_open", "app_1");
    const result = dedupeAppCardMessages([first, user, second]);

    expect(isSilent(result[0])).toBe(false);
    expect(result[1]).toBe(user);
    expect(isSilent(result[2])).toBe(false);
  });

  it("keeps one card and leaves other tools alone", () => {
    const first = appCardMessage("first", "app_open", "app_1");
    const other = appCardMessage("other", "app_open", "app_2");
    const plain: ChatMessage = {
      id: "plain",
      role: "assistant",
      content: "",
      timestamp: 0,
      toolData: { name: "app_list", output: '{"apps":[]}' },
    };
    const result = dedupeAppCardMessages([first, other, plain]);

    expect(isSilent(result[0])).toBe(false);
    expect(isSilent(result[1])).toBe(true);
    expect(result[2]).toBe(plain);
  });
});
