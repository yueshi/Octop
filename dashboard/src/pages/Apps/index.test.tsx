import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { appsApi, type AppSummary } from "../../api/modules/apps";
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
  // The translator must be stable across renders: the page loads the
  // MiniApp list in an effect keyed on `t`, so a fresh function every render
  // would refetch the list forever.
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

/**
 * jsdom has no PointerEvent, so build a plain event and attach the pointer
 * fields the resize handler reads.
 */
function pointerEvent(type: string, props: Record<string, unknown>): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, props);
  return event;
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(appsApi.list).mockResolvedValue({ apps: [app1, app2] });
});

/** The full MiniApp catalog lives behind the title-row button. */
async function openCatalog(): Promise<HTMLElement[]> {
  fireEvent.click(screen.getByRole("button", { name: "全部小程序" }));
  return screen.findAllByRole("button", { name: "加入看板" });
}

describe("workbench board", () => {
  it("starts with an empty board and lists every MiniApp", async () => {
    render(
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("工作台")).toBeInTheDocument();
    expect(screen.getByText(/看板上还没有小程序/)).toBeInTheDocument();
    // The catalog stays closed until the top button opens it.
    expect(screen.queryByText("客户跟进台账")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "全部小程序" }),
    ).toBeInTheDocument();

    await openCatalog();
    expect(screen.getByText("客户跟进台账")).toBeInTheDocument();
    expect(screen.getByText("本月业绩看板")).toBeInTheDocument();
    expect(screen.queryByTestId("mini-app-frame")).not.toBeInTheDocument();
  });

  it("pins a MiniApp and runs it live on the board", async () => {
    render(
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>,
    );
    const pins = await openCatalog();
    fireEvent.click(pins[0]);

    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );
    expect(screen.getByTestId("mini-app-frame")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );
    // The list entry switches to the pinned state.
    expect(
      screen.getByRole("button", { name: "移出看板" }),
    ).toBeInTheDocument();
  });

  it("removes a pinned MiniApp from the board", async () => {
    render(
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>,
    );
    const pins = await openCatalog();
    fireEvent.click(pins[0]);
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    fireEvent.click(screen.getByRole("button", { name: "移出看板" }));
    await waitFor(() =>
      expect(screen.queryByTestId("mini-app-frame")).not.toBeInTheDocument(),
    );
  });

  it("resizes a cell by dragging the corner handle", async () => {
    render(
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>,
    );
    const pins = await openCatalog();
    fireEvent.click(pins[0]);
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    const widget = screen
      .getByTestId("mini-app-frame")
      .closest("[data-size]") as HTMLElement;
    expect(widget.dataset.size).toBe("2x4");

    const handle = screen.getByLabelText("拖动调整大小，或在顶部菜单选择尺寸");
    fireEvent(
      handle,
      pointerEvent("pointerdown", { pointerId: 1, clientX: 0, clientY: 0 }),
    );
    fireEvent(
      window,
      pointerEvent("pointermove", {
        pointerId: 1,
        clientX: 100,
        clientY: 100,
      }),
    );
    fireEvent(
      window,
      pointerEvent("pointerup", { pointerId: 1, clientX: 100, clientY: 100 }),
    );

    // Dragging two rows taller than the 2×4 default snaps to 2×6.
    await waitFor(() => expect(widget.dataset.size).toBe("2x6"));
  });

  it("picks an exact size from the header menu", async () => {
    render(
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>,
    );
    const pins = await openCatalog();
    fireEvent.click(pins[0]);
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    const widget = screen
      .getByTestId("mini-app-frame")
      .closest("[data-size]") as HTMLElement;
    fireEvent.click(screen.getByRole("button", { name: "尺寸" }));

    // Every supported rectangle is offered, including the full-canvas one.
    expect(
      await screen.findByRole("button", { name: "6×6" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "3×3" }));

    await waitFor(() => expect(widget.dataset.size).toBe("3x3"));
  });

  it("moves a pinned MiniApp without dragging", async () => {
    render(
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>,
    );
    const pins = await openCatalog();
    fireEvent.click(pins[0]);
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );
    fireEvent.click(screen.getAllByRole("button", { name: "加入看板" })[0]);
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(2),
    );

    expect(
      screen.getAllByTestId("mini-app-frame").map((node) => node.dataset.appId),
    ).toEqual(["app_1", "app_2"]);

    // The second cell moves up through its own button.
    fireEvent.click(screen.getAllByLabelText("上移")[1]);
    await waitFor(() =>
      expect(
        screen
          .getAllByTestId("mini-app-frame")
          .map((node) => node.dataset.appId),
      ).toEqual(["app_2", "app_1"]),
    );
  });

  it("takes the board fullscreen and restores it", async () => {
    render(
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>,
    );
    // Nothing to expand until the board has a cell.
    expect(screen.getByRole("button", { name: "全屏" })).toBeDisabled();

    const pins = await openCatalog();
    fireEvent.click(pins[0]);
    await waitFor(() =>
      expect(screen.getAllByTestId("mini-app-frame")).toHaveLength(1),
    );

    const board = () =>
      document.querySelector("[data-fullscreen]") as HTMLElement | null;

    fireEvent.click(screen.getByRole("button", { name: "全屏" }));
    await waitFor(() => expect(board()).not.toBeNull());
    expect(board()?.dataset.fullscreen).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "恢复" }));
    await waitFor(() => expect(board()).toBeNull());

    // Escape leaves fullscreen too.
    fireEvent.click(screen.getByRole("button", { name: "全屏" }));
    await waitFor(() => expect(board()).not.toBeNull());
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(board()).toBeNull());
  });
});
