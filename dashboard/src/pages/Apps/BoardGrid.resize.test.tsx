import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AppSummary } from "../../api/modules/apps";
import type { BoardItem } from "../../api/modules/boards";
import BoardGrid from "./BoardGrid";

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

const items: BoardItem[] = [{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }];

function renderGrid(editable: boolean, onCommit?: (next: BoardItem[]) => void) {
  return render(
    <BoardGrid
      items={items}
      apps={[app]}
      editable={editable}
      onCommit={onCommit}
    />,
  );
}

describe("BoardGrid resize handles", () => {
  it("renders a corner resize handle when editable", () => {
    const { container } = renderGrid(true);
    expect(screen.getByTestId("mini-app-frame")).toBeInTheDocument();
    expect(
      container.querySelector(".react-resizable-handle-se"),
    ).not.toBeNull();
  });

  it("keeps the handle in the DOM but hidden in presentation mode", () => {
    const { container } = renderGrid(false);
    expect(
      container.querySelector(
        ".react-resizable-hide .react-resizable-handle-se",
      ),
    ).not.toBeNull();
  });

  it("commits a larger rectangle after dragging the corner handle", () => {
    const onCommit = vi.fn();
    const { container } = renderGrid(true, onCommit);
    const handle = container.querySelector(
      ".react-resizable-handle-se",
    ) as HTMLElement;

    fireEvent.mouseDown(handle, { clientX: 100, clientY: 100, button: 0 });
    fireEvent.mouseMove(document, { clientX: 240, clientY: 220, button: 0 });
    fireEvent.mouseUp(document, { clientX: 240, clientY: 220, button: 0 });

    expect(onCommit).toHaveBeenCalled();
    const next = onCommit.mock.calls.at(-1)?.[0] as BoardItem[];
    expect(next[0].w).toBeGreaterThan(4);
    expect(next[0].h).toBeGreaterThan(4);
  });
});
