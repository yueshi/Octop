import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { appsApi, type AppSummary } from "../../api/modules/apps";
import {
  boardsApi,
  type BoardDetail,
  type BoardItem,
} from "../../api/modules/boards";
import AppsPage from "./index";

vi.mock("react-i18next", async () => {
  const zh = (await import("../../locales/zh.json")).default;
  const lookup = (key: string): unknown => {
    let value: unknown = zh;
    for (const part of key.split(".")) {
      value = (value as Record<string, unknown> | undefined)?.[part];
    }
    return value;
  };
  // The translator must be stable across renders: the page loads the board
  // and catalog in effects keyed on `t`, so a fresh function every render
  // would refetch forever.
  const t = (
    key: string,
    fallback?: unknown,
    options?: Record<string, unknown>,
  ): string => {
    const value = lookup(key);
    const template =
      typeof value === "string"
        ? value
        : typeof fallback === "string"
        ? fallback
        : key;
    const opts =
      options ??
      (fallback && typeof fallback === "object"
        ? (fallback as Record<string, unknown>)
        : undefined);
    return template.replace(/\{\{(\w+)\}\}/g, (_match, name: string) =>
      String(opts?.[name] ?? ""),
    );
  };
  return {
    useTranslation: () => ({
      t,
      i18n: { language: "zh", changeLanguage: () => Promise.resolve() },
    }),
  };
});

vi.mock("../../api/modules/apps", () => {
  const list = vi.fn();
  return {
    appsApi: new Proxy(
      { list },
      {
        get: (target, prop) =>
          prop in target ? target[prop as keyof typeof target] : vi.fn(),
      },
    ),
    appPreviewPath: (appId: string) => `/apps/${appId}/preview`,
  };
});

vi.mock("../../api/modules/boards", () => ({
  boardsApi: {
    list: vi.fn(),
    create: vi.fn(),
    get: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    setLayout: vi.fn(),
  },
}));

vi.mock("../../components/MiniAppFrame", () => ({
  default: ({ appId }: { appId: string }) => (
    <div data-testid="mini-app-frame" data-app-id={appId} />
  ),
}));

vi.mock("./CreateMiniAppModal", () => ({ default: () => null }));

vi.mock("../../hooks/usePreviewImage", () => ({ usePreviewImage: () => null }));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "Asia/Shanghai",
}));
vi.mock("../../context/AgentContext", () => ({
  useAgent: () => ({ agents: [], activeAgentId: null, loading: false }),
  selectEnabledExperts: () => [],
}));

const app1: AppSummary = {
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

const app2 = { ...app1, app_id: "app_2", name: "本月业绩看板" };

const board: BoardDetail = {
  board_id: "board_1",
  name: "我的看板",
  description: "",
  version: 1,
  item_count: 0,
  created_at: 0,
  updated_at: 0,
  layout: { version: 1, items: [] },
};

function savedBoard(items: BoardItem[], version = 2): BoardDetail {
  return {
    ...board,
    version,
    item_count: items.length,
    layout: { version: 1, items },
  };
}

function renderPage(initialEntry = "/apps/boards/board_1") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/apps" element={<AppsPage />} />
        <Route path="/apps/boards/:boardId" element={<AppsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** The full MiniApp catalog lives behind the title-row button. */
async function openCatalog(): Promise<HTMLElement[]> {
  fireEvent.click(screen.getByRole("button", { name: "添加已有小程序" }));
  return screen.findAllByRole("button", { name: "加入看板" });
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  window.open = vi.fn();
  vi.mocked(appsApi.list).mockResolvedValue({ apps: [app1, app2] });
  vi.mocked(boardsApi.list).mockResolvedValue([board]);
  vi.mocked(boardsApi.get).mockResolvedValue(board);
  vi.mocked(boardsApi.setLayout).mockImplementation(async (_id, input) =>
    savedBoard(input.items),
  );
});

describe("workbench board", () => {
  it("lands on the board gallery and opens a board card", async () => {
    renderPage("/apps");

    expect(await screen.findByTestId("board-gallery")).toBeInTheDocument();
    const cards = await screen.findAllByTestId("board-gallery-card");
    expect(cards).toHaveLength(1);
    expect(screen.getByText("我的看板")).toBeInTheDocument();
    expect(screen.getByText("看板")).toBeInTheDocument();
    expect(screen.getByText("桌面")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建小程序" }),
    ).toBeInTheDocument();

    fireEvent.click(cards[0]);

    expect(await screen.findByText(/看板上还没有小程序/)).toBeInTheDocument();
  });

  it("shows every board as a card thumbnail", async () => {
    vi.mocked(boardsApi.list).mockResolvedValue([
      board,
      { ...board, board_id: "board_2", name: "运营看板" },
    ]);
    renderPage("/apps");

    expect(await screen.findAllByTestId("board-gallery-card")).toHaveLength(2);
    expect(screen.getByText("运营看板")).toBeInTheDocument();
    await waitFor(() => expect(boardsApi.get).toHaveBeenCalledWith("board_2"));
  });

  it("draws the board's real layout inside its card thumbnail", async () => {
    vi.mocked(boardsApi.get).mockResolvedValue(
      savedBoard([{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }], 2),
    );
    renderPage("/apps");

    expect(await screen.findByTitle("客户跟进台账")).toBeInTheDocument();
  });

  it("defaults to board mode even when desktop mode was stored", async () => {
    localStorage.setItem("octop:apps-view-mode:v1", "desktop");
    renderPage("/apps");

    expect(await screen.findByTestId("board-gallery")).toBeInTheDocument();
    expect(screen.queryByTestId("desktop-root")).not.toBeInTheDocument();
  });

  it("returns to the gallery from a board", async () => {
    renderPage();
    await screen.findByText(/看板上还没有小程序/);

    fireEvent.click(screen.getByRole("button", { name: "全部看板" }));

    expect(await screen.findByTestId("board-gallery")).toBeInTheDocument();
  });

  it("loads the selected board and lists every MiniApp", async () => {
    renderPage();
    expect(await screen.findByText("工作台")).toBeInTheDocument();
    await screen.findByText(/看板上还没有小程序/);
    expect(boardsApi.get).toHaveBeenCalledWith("board_1");
    // Board detail has no Board/Desktop mode toggle.
    expect(screen.queryByText("看板")).not.toBeInTheDocument();
    expect(screen.queryByText("桌面")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建小程序" }),
    ).toBeInTheDocument();

    // The catalog stays closed until the top button opens it.
    expect(screen.queryByText("客户跟进台账")).not.toBeInTheDocument();
    await openCatalog();
    expect(screen.getByText("客户跟进台账")).toBeInTheDocument();
    expect(screen.getByText("本月业绩看板")).toBeInTheDocument();
    expect(screen.queryByTestId("mini-app-frame")).not.toBeInTheDocument();
  });

  it("switches to desktop mode and shows every MiniApp", async () => {
    renderPage("/apps");
    expect(await screen.findByTestId("board-gallery")).toBeInTheDocument();
    await waitFor(() => expect(appsApi.list).toHaveBeenCalled());

    fireEvent.click(screen.getByText("桌面"));

    expect(await screen.findByTestId("desktop-root")).toBeInTheDocument();
    expect(screen.getAllByTestId("desktop-icon")).toHaveLength(2);
    expect(screen.getByText("客户跟进台账")).toBeInTheDocument();
    expect(screen.getByText("本月业绩看板")).toBeInTheDocument();
    expect(screen.queryByText(/看板上还没有小程序/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("看板"));
    expect(await screen.findByTestId("board-gallery")).toBeInTheDocument();
  });

  it("loads a board directly from its URL before the catalog resolves", async () => {
    const pinned = savedBoard([{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }], 1);
    vi.mocked(boardsApi.get).mockResolvedValue(pinned);
    let releaseApps!: () => void;
    const appsGate = new Promise<void>((resolve) => {
      releaseApps = resolve;
    });
    vi.mocked(appsApi.list).mockImplementation(async () => {
      await appsGate;
      return { apps: [app1, app2] };
    });

    renderPage();
    // The board detail may resolve before the catalog; its cells must not be
    // filtered away just because the apps are not in memory yet.
    await new Promise((resolve) => setTimeout(resolve, 10));
    releaseApps();

    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );
    expect(screen.getByTestId("mini-app-frame")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );
  });

  it("pins a MiniApp and saves its grid cell", async () => {
    renderPage();
    const pins = await openCatalog();
    fireEvent.click(pins[0]);

    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );
    expect(screen.getByTestId("mini-app-frame")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );
    expect(
      screen.getAllByRole("button", { name: "移出看板" }).length,
    ).toBeGreaterThan(0);

    await waitFor(() =>
      expect(boardsApi.setLayout).toHaveBeenCalledWith("board_1", {
        version: 1,
        items: [{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }],
      }),
    );
  });

  it("removes a pinned MiniApp and persists the empty board", async () => {
    const pinned = savedBoard([{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }], 1);
    vi.mocked(boardsApi.get).mockResolvedValue(pinned);
    renderPage();

    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );
    fireEvent.click(screen.getByRole("button", { name: "移出看板" }));

    await waitFor(() =>
      expect(screen.queryByTestId("mini-app-frame")).not.toBeInTheDocument(),
    );
    expect(boardsApi.setLayout).toHaveBeenCalledWith("board_1", {
      version: 1,
      items: [],
    });
  });

  it("packs cells at their chosen sizes", async () => {
    const pinned = savedBoard(
      [
        { app_id: "app_1", x: 0, y: 0, w: 4, h: 4 },
        { app_id: "app_2", x: 6, y: 0, w: 2, h: 2 },
      ],
      1,
    );
    vi.mocked(boardsApi.get).mockResolvedValue(pinned);
    renderPage();
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(2),
    );

    fireEvent.click(screen.getByRole("button", { name: "看板操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "自动整理" }));

    await waitFor(() =>
      expect(boardsApi.setLayout).toHaveBeenCalledWith("board_1", {
        version: 1,
        items: [
          { app_id: "app_1", x: 0, y: 0, w: 4, h: 4 },
          { app_id: "app_2", x: 4, y: 0, w: 2, h: 2 },
        ],
      }),
    );
  });

  it("resizes a cell through the size presets", async () => {
    const pinned = savedBoard([{ app_id: "app_1", x: 0, y: 0, w: 2, h: 2 }], 1);
    vi.mocked(boardsApi.get).mockResolvedValue(pinned);
    renderPage();
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    fireEvent.click(screen.getByRole("button", { name: "尺寸" }));
    fireEvent.click(await screen.findByRole("button", { name: "大 6×4" }));

    await waitFor(() =>
      expect(boardsApi.setLayout).toHaveBeenCalledWith("board_1", {
        version: 1,
        items: [{ app_id: "app_1", x: 0, y: 0, w: 6, h: 4 }],
      }),
    );
  });

  it("locks a cell and persists the flag", async () => {
    const pinned = savedBoard([{ app_id: "app_1", x: 0, y: 0, w: 2, h: 2 }], 1);
    vi.mocked(boardsApi.get).mockResolvedValue(pinned);
    renderPage();
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    fireEvent.click(screen.getByRole("button", { name: "锁定位置和尺寸" }));

    await waitFor(() =>
      expect(boardsApi.setLayout).toHaveBeenCalledWith("board_1", {
        version: 1,
        items: [{ app_id: "app_1", x: 0, y: 0, w: 2, h: 2, locked: true }],
      }),
    );
    expect(
      await screen.findByRole("button", { name: "解锁" }),
    ).toBeInTheDocument();
  });

  it("undoes the last layout change", async () => {
    const pinned = savedBoard([{ app_id: "app_1", x: 3, y: 0, w: 2, h: 2 }], 1);
    vi.mocked(boardsApi.get).mockResolvedValue(pinned);
    renderPage();
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    fireEvent.click(screen.getByRole("button", { name: "看板操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "自动整理" }));
    await waitFor(() => expect(boardsApi.setLayout).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "看板操作" }));
    fireEvent.click(
      await screen.findByRole("menuitem", { name: "撤销上一次布局" }),
    );

    await waitFor(() =>
      expect(boardsApi.setLayout).toHaveBeenLastCalledWith("board_1", {
        version: 2,
        items: [{ app_id: "app_1", x: 3, y: 0, w: 2, h: 2 }],
      }),
    );
  });

  it("creates the first board and migrates browser-local pins", async () => {
    const created = { ...board, board_id: "board_new", name: "旧看板迁移" };
    const migrated: BoardDetail = {
      ...created,
      version: 2,
      item_count: 1,
      layout: {
        version: 1,
        items: [{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }],
      },
    };
    localStorage.setItem(
      "octop:apps-board:v1",
      JSON.stringify({ order: ["app_1"], sizes: { app_1: "2x4" } }),
    );
    vi.mocked(boardsApi.list).mockResolvedValue([]);
    vi.mocked(boardsApi.create).mockResolvedValue(created);
    vi.mocked(boardsApi.setLayout).mockResolvedValue(migrated);
    vi.mocked(boardsApi.get).mockResolvedValue(migrated);

    renderPage("/apps");

    await waitFor(() =>
      expect(boardsApi.create).toHaveBeenCalledWith({ name: "我的看板" }),
    );
    await waitFor(() =>
      expect(boardsApi.setLayout).toHaveBeenCalledWith("board_new", {
        version: 1,
        items: [{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }],
      }),
    );
    await waitFor(() =>
      expect(localStorage.getItem("octop:apps-board:v1")).toBeNull(),
    );
    expect(await screen.findByText("旧看板迁移")).toBeInTheDocument();
  });

  it("renames the board through the actions menu", async () => {
    vi.mocked(boardsApi.update).mockResolvedValue({
      ...board,
      name: "工作台 2",
    });
    renderPage();
    await screen.findByText(/看板上还没有小程序/);

    fireEvent.click(screen.getByRole("button", { name: "看板操作" }));
    fireEvent.click(await screen.findByText("重命名看板"));
    const input = await screen.findByLabelText("看板名称");
    fireEvent.change(input, { target: { value: "工作台 2" } });
    // Ant Design spaces two-character CJK button labels (“保 存”).
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() =>
      expect(boardsApi.update).toHaveBeenCalledWith("board_1", {
        name: "工作台 2",
      }),
    );
  });

  it("opens the presentation view in a new tab", async () => {
    const pinned = savedBoard([{ app_id: "app_1", x: 0, y: 0, w: 4, h: 4 }], 1);
    vi.mocked(boardsApi.get).mockResolvedValue(pinned);
    renderPage();
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    fireEvent.click(screen.getByRole("button", { name: "演示" }));
    expect(window.open).toHaveBeenCalledWith(
      "/apps/boards/board_1/view",
      "_blank",
      "noopener,noreferrer",
    );
  });
});
