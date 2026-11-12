import { describe, expect, it } from "vitest";

import type { AppSummary } from "../../../api/modules/apps";
import type { ChatMessage } from "../hooks/sseHelpers";
import {
  collectSessionApps,
  mergeSessionApps,
  sessionAppsFromSummaries,
} from "./sessionApps";

function appCardMessage(
  id: string,
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
      name: "app_open",
      output: JSON.stringify({
        octop_ui: { renderer: "app_card", version: 1 },
        data: { app_id: appId, name: appId, ...data },
      }),
    },
  };
}

function plainMessage(id: string, role: ChatMessage["role"]): ChatMessage {
  return { id, role, content: "hi", timestamp: 0, status: "done" };
}

describe("collectSessionApps", () => {
  it("returns nothing without app cards", () => {
    expect(
      collectSessionApps([
        plainMessage("u", "user"),
        plainMessage("a", "assistant"),
      ]),
    ).toEqual([]);
  });

  it("collects per-app snapshots, newest mention first", () => {
    const apps = collectSessionApps([
      appCardMessage("a1", "app_a", { name: "台账", row_count: 3 }),
      appCardMessage("a2", "app_b", { name: "看板" }),
    ]);
    expect(apps.map((app) => app.app_id)).toEqual(["app_b", "app_a"]);
    expect(apps[1]).toMatchObject({
      name: "台账",
      row_count: 3,
    });
  });

  it("refreshes the snapshot of a repeated app instead of duplicating it", () => {
    const apps = collectSessionApps([
      appCardMessage("a1", "app_a", {
        name: "旧名字",
        row_count: 1,
        has_html: false,
      }),
      appCardMessage("a2", "app_a", {
        name: "新名字",
        row_count: 9,
        has_html: true,
        has_preview: true,
        description: "现在有描述",
      }),
    ]);
    expect(apps).toHaveLength(1);
    expect(apps[0]).toMatchObject({
      app_id: "app_a",
      name: "新名字",
      row_count: 9,
      has_html: true,
      has_preview: true,
      description: "现在有描述",
    });
  });

  it("keeps a repeated app at its latest position in the order", () => {
    const apps = collectSessionApps([
      appCardMessage("a1", "app_a"),
      appCardMessage("b1", "app_b"),
      appCardMessage("a2", "app_a"),
    ]);
    expect(apps.map((app) => app.app_id)).toEqual(["app_a", "app_b"]);
  });

  it("ignores malformed envelopes and empty ids", () => {
    const broken: ChatMessage = {
      id: "x",
      role: "assistant",
      content: "",
      timestamp: 0,
      status: "done",
      toolData: { name: "app_open", output: "not json" },
    };
    expect(
      collectSessionApps([
        broken,
        appCardMessage("empty", ""),
        appCardMessage("ok", "app_a"),
      ]).map((app) => app.app_id),
    ).toEqual(["app_a"]);
  });
});

function summary(overrides: Partial<AppSummary>): AppSummary {
  return {
    app_id: "app_a",
    name: "台账",
    description: "",
    agent_id: "agent",
    agent_name: "",
    owner_user_id: 1,
    owner_username: "alice",
    permission: "edit_structure",
    row_count: 3,
    html_bytes: 10,
    data_bytes: 10,
    has_html: true,
    has_preview: true,
    created_at: 0,
    updated_at: 0,
    ...overrides,
  };
}

describe("sessionAppsFromSummaries", () => {
  it("maps server summaries onto dock rows", () => {
    expect(
      sessionAppsFromSummaries([
        summary({ app_id: "app_a", name: "台账", row_count: 12 }),
        summary({ app_id: "app_b", name: "看板", description: "按门店汇总" }),
      ]),
    ).toEqual([
      {
        app_id: "app_a",
        name: "台账",
        row_count: 12,
        has_html: true,
        has_preview: true,
      },
      {
        app_id: "app_b",
        name: "看板",
        description: "按门店汇总",
        row_count: 3,
        has_html: true,
        has_preview: true,
      },
    ]);
  });
});

describe("mergeSessionApps", () => {
  it("keeps the transcript when the server has no answer yet", () => {
    const transcript = [{ app_id: "app_a", name: "台账" }];
    expect(mergeSessionApps(null, transcript)).toEqual(transcript);
  });

  it("prefers fresh server fields and keeps live-only cards", () => {
    const server = [
      { app_id: "app_a", name: "台账", row_count: 9 },
      { app_id: "app_old", name: "旧应用" },
    ];
    const transcript = [
      { app_id: "app_live", name: "刚创建的" },
      { app_id: "app_a", name: "台账", row_count: 1 },
    ];
    expect(
      mergeSessionApps(server, transcript).map((app) => app.app_id),
    ).toEqual(["app_live", "app_a", "app_old"]);
    expect(mergeSessionApps(server, transcript)[1].row_count).toBe(9);
  });
});
