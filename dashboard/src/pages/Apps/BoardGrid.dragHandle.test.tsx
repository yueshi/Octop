import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { AppSummary } from "../../api/modules/apps";
import BoardGrid from "./BoardGrid";

/** Capture the props RGL receives without rendering the real grid. */
const captured = vi.hoisted(() => ({
  props: null as Record<string, unknown> | null,
}));

vi.mock("react-grid-layout/legacy", () => ({
  default: (props: { children: ReactNode }) => {
    captured.props = props as unknown as Record<string, unknown>;
    return <div data-testid="grid-mock">{props.children}</div>;
  },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock("../../components/MiniAppFrame", () => ({
  default: ({ appId }: { appId: string }) => (
    <div data-testid="mini-app-frame" data-app-id={appId} />
  ),
}));

const app: AppSummary = {
  app_id: "app_1",
  name: "Tracker",
  description: "",
  agent_id: "agent_1",
  agent_name: "Bot",
  owner_user_id: 1,
  owner_username: "alice",
  permission: "edit_structure",
  row_count: 0,
  html_bytes: 10,
  data_bytes: 10,
  has_html: true,
  has_preview: false,
  created_at: 0,
  updated_at: 0,
};

describe("BoardGrid drag configuration", () => {
  it("targets the notch and header with class selectors", () => {
    render(
      <BoardGrid
        items={[{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }]}
        apps={[app]}
        editable
      />,
    );

    const handle = captured.props?.draggableHandle as string;
    const selectors = handle.split(", ");
    expect(selectors).toHaveLength(2);
    for (const selector of selectors) {
      // A missing leading dot makes RGL look for a tag name and silently
      // disables dragging altogether.
      expect(selector.startsWith(".")).toBe(true);
      expect(document.querySelector(selector)).not.toBeNull();
    }
  });

  it("marks locked cells static so RGL cannot move them", () => {
    render(
      <BoardGrid
        items={[{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4, locked: true }]}
        apps={[app]}
        editable
      />,
    );

    const layout = captured.props?.layout as Array<{
      i: string;
      static?: boolean;
    }>;
    expect(layout[0]).toMatchObject({ i: "app_1", static: true });
  });

  it("does not bound dragging to the auto-sized canvas", () => {
    render(
      <BoardGrid
        items={[{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }]}
        apps={[app]}
        editable
      />,
    );

    // containerBounds caps the last row at the current canvas height, which
    // makes cells unable to grow taller as the board re-fills.
    expect(captured.props?.isBounded).toBeUndefined();
  });
});
