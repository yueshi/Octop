import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { appsApi, type AppSummary } from "../../api/modules/apps";
import { boardsApi, type BoardDetail } from "../../api/modules/boards";
import BoardViewPage from "./BoardView";

vi.mock("react-i18next", async () => {
  const zh = (await import("../../locales/zh.json")).default;
  const lookup = (key: string): unknown => {
    let value: unknown = zh;
    for (const part of key.split(".")) {
      value = (value as Record<string, unknown> | undefined)?.[part];
    }
    return value;
  };
  const t = (key: string, fallback?: unknown): string => {
    const value = lookup(key);
    return typeof value === "string"
      ? value
      : typeof fallback === "string"
      ? fallback
      : key;
  };
  return { useTranslation: () => ({ t, i18n: { language: "zh" } }) };
});

vi.mock("../../api/modules/apps", () => ({
  appsApi: { list: vi.fn() },
}));

vi.mock("../../api/modules/boards", () => ({
  boardsApi: { get: vi.fn() },
}));

vi.mock("../../components/MiniAppFrame", () => ({
  default: ({ appId }: { appId: string }) => (
    <div data-testid="mini-app-frame" data-app-id={appId} />
  ),
}));

const app: AppSummary = {
  app_id: "app_1",
  name: "客户跟进台账",
  description: "按阶段管理客户",
  agent_id: "agent_1",
  agent_name: "销售助理",
  owner_user_id: 1,
  owner_username: "boss",
  permission: "edit_structure",
  row_count: 12,
  html_bytes: 100,
  data_bytes: 100,
  has_html: true,
  has_preview: false,
  created_at: 0,
  updated_at: 0,
};

const board: BoardDetail = {
  board_id: "board_1",
  name: "销售看板",
  description: "",
  version: 1,
  item_count: 1,
  created_at: 0,
  updated_at: 0,
  layout: {
    version: 1,
    items: [{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }],
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(appsApi.list).mockResolvedValue({ apps: [app] });
  vi.mocked(boardsApi.get).mockResolvedValue(board);
});

describe("board presentation view", () => {
  it("renders the board name and its MiniApps read-only", async () => {
    render(
      <MemoryRouter initialEntries={["/apps/boards/board_1/view"]}>
        <Routes>
          <Route
            path="/apps/boards/:boardId/view"
            element={<BoardViewPage />}
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("销售看板")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );
    expect(screen.getByTestId("mini-app-frame")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );
    // Presentation mode never offers to remove a cell.
    expect(
      screen.queryByRole("button", { name: "移出看板" }),
    ).not.toBeInTheDocument();
  });

  it("hides the toolbar in fullscreen and restores it when the browser exits", async () => {
    let fsElement: Element | null = null;
    const requestFullscreen = vi.fn(() => {
      fsElement = document.createElement("div");
      document.dispatchEvent(new Event("fullscreenchange"));
      return Promise.resolve();
    });
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => fsElement,
    });
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
      configurable: true,
      value: requestFullscreen,
    });

    try {
      render(
        <MemoryRouter initialEntries={["/apps/boards/board_1/view"]}>
          <Routes>
            <Route
              path="/apps/boards/:boardId/view"
              element={<BoardViewPage />}
            />
          </Routes>
        </MemoryRouter>,
      );

      fireEvent.click(await screen.findByRole("button", { name: "全屏" }));
      await waitFor(() => expect(requestFullscreen).toHaveBeenCalledTimes(1));
      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "全屏" }),
        ).not.toBeInTheDocument(),
      );

      // Esc is browser-native: fullscreenElement clears and the event fires.
      fsElement = null;
      document.dispatchEvent(new Event("fullscreenchange"));
      expect(
        await screen.findByRole("button", { name: "全屏" }),
      ).toBeInTheDocument();
    } finally {
      delete (document as { fullscreenElement?: unknown }).fullscreenElement;
      delete (HTMLElement.prototype as { requestFullscreen?: unknown })
        .requestFullscreen;
    }
  });
});
