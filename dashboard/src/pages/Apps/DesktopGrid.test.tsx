import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AppSummary } from "../../api/modules/apps";
import DesktopGrid, { iconSpecForApp } from "./DesktopGrid";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: "en" },
  }),
}));

vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));

vi.mock("../../components/MiniAppFrame", () => ({
  default: ({
    appId,
    onOpenExternal,
  }: {
    appId: string;
    onOpenExternal?: (url: string) => void;
  }) => (
    <div>
      <div data-testid="mini-app-frame" data-app-id={appId} />
      <button
        type="button"
        data-testid="mini-app-open-external"
        onClick={() => onOpenExternal?.("https://example.com/path")}
      />
    </div>
  ),
}));

vi.mock("./DesktopWebView", () => ({
  default: ({ url }: { url: string }) => (
    <div data-testid="desktop-webview" data-url={url} />
  ),
}));

vi.mock("./DesktopChatWindow", () => ({
  default: ({
    agentId,
    initialAppId,
    seed,
  }: {
    agentId: string;
    initialAppId?: string | null;
    seed?: { id: number; text: string } | null;
  }) => (
    <div
      data-testid="desktop-chat"
      data-agent-id={agentId}
      data-app-id={initialAppId ?? ""}
      data-seed={seed?.text ?? ""}
    />
  ),
}));

function openAppWindow(appId: string) {
  const icon = screen
    .getAllByTestId("desktop-icon")
    .find((node) => node.getAttribute("data-app-id") === appId)!;
  fireEvent.doubleClick(icon);
}

function switchSpace(spaceId: string) {
  if (!screen.queryByTestId("desktop-spaces-menu")) {
    fireEvent.click(screen.getByTestId("desktop-space-trigger"));
  }
  const option = screen
    .getAllByTestId("desktop-space-option")
    .find((node) => node.getAttribute("data-space-id") === spaceId)!;
  fireEvent.click(option);
}

const base: AppSummary = {
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

const apps = [base, { ...base, app_id: "app_2", name: "本月业绩看板" }];

describe("DesktopGrid", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("renders every app as a desktop icon", () => {
    render(<DesktopGrid apps={apps} />);

    expect(screen.getByTestId("desktop-root")).toBeInTheDocument();
    expect(screen.getByTestId("desktop-dock")).toBeInTheDocument();
    expect(screen.getAllByTestId("desktop-icon")).toHaveLength(2);
    expect(screen.getByText("客户跟进台账")).toBeInTheDocument();
    expect(screen.getByText("本月业绩看板")).toBeInTheDocument();
  });

  it("draws generated icons instead of app screenshots", () => {
    const { container } = render(<DesktopGrid apps={apps} />);

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelectorAll("svg").length).toBeGreaterThan(0);
  });

  it("derives a stable icon spec per app", () => {
    const spec = iconSpecForApp(apps[0]);

    expect(iconSpecForApp(apps[0])).toEqual(spec);
    expect(spec.from).toMatch(/^#[0-9a-f]{6}$/i);
    expect(spec.to).toMatch(/^#[0-9a-f]{6}$/i);
    expect(["aurora", "glass", "ring", "mesh", "solid"]).toContain(spec.style);
    expect(spec.Glyph).toBeTruthy();
  });

  it("varies generated icons for generic app names", () => {
    const generic = Array.from({ length: 8 }, (_, index) => ({
      ...base,
      app_id: `app_generic_${index}`,
      name: `事项 ${index}`,
      description: "",
    }));
    const keys = new Set(
      generic.map((app) => {
        const spec = iconSpecForApp(app);
        return `${spec.from}|${spec.to}|${spec.style}|${spec.Glyph}`;
      }),
    );

    expect(keys.size).toBeGreaterThan(4);
  });

  it("requests fullscreen from the menu bar", () => {
    const requestFullscreen = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
      configurable: true,
      value: requestFullscreen,
    });

    render(<DesktopGrid apps={apps} />);
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));

    expect(requestFullscreen).toHaveBeenCalled();
  });

  it("auto-enters fullscreen and plays the 4.5-second 智管家 OS boot", () => {
    const requestFullscreen = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
      configurable: true,
      value: requestFullscreen,
    });
    vi.useFakeTimers();
    try {
      render(<DesktopGrid apps={apps} autoFullscreen />);

      expect(screen.getByTestId("desktop-boot")).toBeInTheDocument();
      expect(screen.getByText("智管家")).toBeInTheDocument();
      expect(requestFullscreen).toHaveBeenCalledTimes(1);

      act(() => {
        vi.advanceTimersByTime(4500);
      });
      expect(screen.queryByTestId("desktop-boot")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("lets the user skip the Agent OS boot sequence", () => {
    Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
    render(<DesktopGrid apps={apps} autoFullscreen />);

    fireEvent.click(screen.getByRole("button", { name: "Skip" }));

    expect(screen.queryByTestId("desktop-boot")).not.toBeInTheDocument();
  });

  it("opens an app in a desktop window on double-click", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );
    expect(screen.getByTestId("mini-app-frame")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );
    expect(screen.getByTestId("dock-running")).toBeInTheDocument();
  });

  it("opens an app window with Enter for keyboard users", () => {
    render(<DesktopGrid apps={apps} />);

    const icon = screen
      .getAllByTestId("desktop-icon")
      .find((node) => node.getAttribute("data-app-id") === "app_2")!;
    fireEvent.keyDown(icon, { key: "Enter" });

    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-app-id",
      "app_2",
    );
  });

  it("moves a window through a compositor transform while dragging", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");
    const root = screen.getByTestId("desktop-root");
    Object.defineProperty(root, "clientWidth", {
      configurable: true,
      value: 1280,
    });
    Object.defineProperty(root, "clientHeight", {
      configurable: true,
      value: 800,
    });

    const win = screen.getByTestId("desktop-window");
    const header = document.querySelector("[data-titlebar]")!;
    // jsdom has no PointerEvent constructor; MouseEvent with the pointerdown
    // type still reaches React's onPointerDown and native pointer listeners.
    fireEvent(
      header,
      new MouseEvent("pointerdown", {
        bubbles: true,
        button: 0,
        clientX: 100,
        clientY: 100,
      }),
    );
    fireEvent(
      window,
      new MouseEvent("pointermove", {
        bubbles: true,
        clientX: 160,
        clientY: 140,
      }),
    );

    expect(win).toHaveAttribute("data-dragging", "true");
    expect(win.style.transform).toContain("translate3d(60px, 40px");
    expect(win.style.left).toBe("64px");

    fireEvent(window, new MouseEvent("pointerup", { bubbles: true }));
    expect(win).toHaveAttribute("data-dragging", "false");
    expect(win.style.transform).toBe("");
    expect(win.style.left).toBe("124px");
    expect(win.style.top).toBe("96px");
  });

  it("scales window content while resizing and commits once", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");
    const root = screen.getByTestId("desktop-root");
    Object.defineProperty(root, "clientWidth", {
      configurable: true,
      value: 1280,
    });
    Object.defineProperty(root, "clientHeight", {
      configurable: true,
      value: 800,
    });

    const win = screen.getByTestId("desktop-window");
    const content = screen.getByTestId("desktop-window-content");
    const handle = screen.getByTestId("desktop-window-resize");
    fireEvent(
      handle,
      new MouseEvent("pointerdown", {
        bubbles: true,
        button: 0,
        clientX: 500,
        clientY: 500,
      }),
    );
    fireEvent(
      window,
      new MouseEvent("pointermove", {
        bubbles: true,
        clientX: 580,
        clientY: 560,
      }),
    );

    expect(win).toHaveAttribute("data-resizing", "true");
    expect(win.style.width).toBe("840px");
    expect(win.style.height).toBe("620px");
    expect(content.style.width).toBe("758px");
    expect(content.style.transform).toContain("scale(");

    fireEvent(window, new MouseEvent("pointerup", { bubbles: true }));
    expect(win).toHaveAttribute("data-resizing", "false");
    expect(win.style.width).toBe("840px");
    expect(content.style.transform).toBe("");
  });

  it("reveals the window title bar only from the top edge", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    const zone = screen.getByTestId("desktop-window-top-zone");
    const header = document.querySelector("[data-titlebar]")!;
    expect(header).toHaveAttribute("data-titlebar", "false");

    fireEvent.pointerEnter(zone);
    expect(header).toHaveAttribute("data-titlebar", "true");

    vi.useFakeTimers();
    try {
      fireEvent.pointerLeave(header);
      act(() => {
        vi.advanceTimersByTime(220);
      });
      expect(header).toHaveAttribute("data-titlebar", "false");
    } finally {
      vi.useRealTimers();
    }
  });

  it("minimizes, maximizes/restores and closes a window", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    const windowNode = () => screen.getByTestId("desktop-window");

    fireEvent.click(screen.getByRole("button", { name: "Minimize" }));
    expect(windowNode()).toHaveAttribute("data-minimized", "true");

    const dockIcon = screen
      .getAllByTestId("desktop-dock-icon")
      .find((node) => node.getAttribute("data-app-id") === "app_1")!;
    fireEvent.click(dockIcon);
    expect(windowNode()).toHaveAttribute("data-minimized", "false");

    fireEvent.click(screen.getByRole("button", { name: "Maximize" }));
    expect(windowNode()).toHaveAttribute("data-maximized", "true");

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(windowNode()).toHaveAttribute("data-maximized", "false");

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("desktop-window")).not.toBeInTheDocument();
  });

  it("groups apps into one desktop per expert and switches desktops", () => {
    const otherExpert = {
      ...base,
      app_id: "app_ops",
      name: "库存预警",
      agent_id: "agent_2",
      agent_name: "运营专家",
    };
    render(<DesktopGrid apps={[...apps, otherExpert]} />);

    fireEvent.click(screen.getByTestId("desktop-space-trigger"));
    // Two expert desktops plus the global All Apps library.
    expect(screen.getAllByTestId("desktop-space-option")).toHaveLength(3);
    expect(screen.getByText("All Apps")).toBeInTheDocument();
    expect(screen.getByTestId("desktop-spaces-menu")).toBeInTheDocument();

    switchSpace("agent_2");

    expect(screen.getAllByTestId("desktop-icon")).toHaveLength(1);
    expect(screen.getByText("库存预警")).toBeInTheDocument();
    expect(screen.queryByText("客户跟进台账")).not.toBeInTheDocument();
  });

  it("keeps windows scoped to their expert desktop", () => {
    const otherExpert = {
      ...base,
      app_id: "app_ops",
      name: "库存预警",
      agent_id: "agent_2",
      agent_name: "运营专家",
    };
    render(<DesktopGrid apps={[...apps, otherExpert]} />);
    switchSpace("agent_1");
    openAppWindow("app_1");
    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );

    switchSpace("agent_2");
    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-current",
      "false",
    );

    switchSpace("agent_1");
    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-current",
      "true",
    );
  });

  it("opens expert chat inside the desktop from the pet", () => {
    render(<DesktopGrid apps={apps} />);

    fireEvent.click(screen.getByTestId("desktop-pet"));
    fireEvent.click(screen.getByTestId("desktop-pet"));

    expect(screen.getAllByTestId("desktop-chat")).toHaveLength(1);
    expect(screen.getByTestId("desktop-chat")).toHaveAttribute(
      "data-agent-id",
      "agent_1",
    );
    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-kind",
      "chat",
    );
  });

  it("switches desktops with Ctrl+Arrow", () => {
    const otherExpert = {
      ...base,
      app_id: "app_ops",
      name: "库存预警",
      agent_id: "agent_2",
      agent_name: "运营专家",
    };
    render(<DesktopGrid apps={[...apps, otherExpert]} />);

    const trigger = screen.getByTestId("desktop-space-trigger");
    const initial = trigger.textContent;
    fireEvent.keyDown(window, { key: "ArrowRight", ctrlKey: true });

    expect(trigger.textContent).not.toBe(initial);
  });

  it("opens the command palette and launches an app", async () => {
    render(<DesktopGrid apps={apps} />);

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(
      await screen.findByTestId("desktop-command-palette"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("desktop-command-input"), {
      target: { value: "本月" },
    });
    fireEvent.click(screen.getByTestId("desktop-command-item"));

    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-app-id",
      "app_2",
    );
    expect(
      screen.queryByTestId("desktop-command-palette"),
    ).not.toBeInTheDocument();
  });

  it("restores open desktop windows after a reload", async () => {
    const first = render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    await waitFor(() =>
      expect(localStorage.getItem("octop:apps-desktop-windows:v1")).toContain(
        "app_1",
      ),
    );
    first.unmount();

    render(<DesktopGrid apps={apps} />);
    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-app-id",
      "app_1",
    );
  });

  it("opens MiniApp external links in an in-desktop web window", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    fireEvent.click(screen.getByTestId("mini-app-open-external"));

    const external = screen
      .getAllByTestId("desktop-window")
      .find((node) => node.getAttribute("data-kind") === "external")!;
    expect(external).toHaveAttribute("data-url", "https://example.com/path");
    expect(external).toHaveAttribute("data-current", "true");
    expect(screen.getByTestId("desktop-webview")).toHaveAttribute(
      "data-url",
      "https://example.com/path",
    );
    expect(screen.getByText("example.com")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("mini-app-open-external"));
    expect(screen.getAllByTestId("desktop-webview")).toHaveLength(1);
  });

  it("opens each expert's own chat from that desktop's pet", () => {
    const otherExpert = {
      ...base,
      app_id: "app_ops",
      name: "库存预警",
      agent_id: "agent_2",
      agent_name: "运营专家",
    };
    render(<DesktopGrid apps={[...apps, otherExpert]} />);

    switchSpace("agent_2");
    fireEvent.click(screen.getByTestId("desktop-pet"));

    expect(screen.getByTestId("desktop-chat")).toHaveAttribute(
      "data-agent-id",
      "agent_2",
    );
  });

  it("seeds app-specific expert actions into the desktop chat window", () => {
    render(<DesktopGrid apps={apps} />);

    const icon = screen
      .getAllByTestId("desktop-icon")
      .find((node) => node.getAttribute("data-app-id") === "app_1")!;
    fireEvent.click(icon);

    expect(screen.getByTestId("desktop-app-actions")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("desktop-action-optimize"));

    const chat = screen.getByTestId("desktop-chat");
    expect(chat).toHaveAttribute("data-agent-id", "agent_1");
    expect(chat).toHaveAttribute("data-app-id", "app_1");
    expect(chat.getAttribute("data-seed")).toContain("客户跟进台账");
    expect(chat.getAttribute("data-seed")).toContain("app_1");
  });

  it("passes the selected app into expert chat from the pet", () => {
    render(<DesktopGrid apps={apps} />);

    const icon = screen
      .getAllByTestId("desktop-icon")
      .find((node) => node.getAttribute("data-app-id") === "app_1")!;
    fireEvent.click(icon);
    fireEvent.click(screen.getByTestId("desktop-pet"));

    const chat = screen.getByTestId("desktop-chat");
    expect(chat).toHaveAttribute("data-app-id", "app_1");
    expect(chat).toHaveAttribute("data-seed", "");
  });

  it("opens Mission Control from the menu bar and F3", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    fireEvent.click(screen.getByTestId("desktop-mission-button"));
    const mission = screen.getByTestId("desktop-mission-control");
    expect(mission).toBeInTheDocument();
    expect(screen.getAllByTestId("desktop-mission-space")).toHaveLength(2);
    expect(screen.getAllByTestId("desktop-mission-window")).toHaveLength(1);

    fireEvent.click(screen.getByTestId("desktop-mission-window"));
    expect(
      screen.queryByTestId("desktop-mission-control"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("desktop-window")).toHaveAttribute(
      "data-minimized",
      "false",
    );

    fireEvent.keyDown(window, { key: "F3" });
    expect(screen.getByTestId("desktop-mission-control")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(
      screen.queryByTestId("desktop-mission-control"),
    ).not.toBeInTheDocument();
  });

  it("shows chat, web and All Apps entries in Dock 2.0", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");
    fireEvent.click(screen.getByTestId("desktop-pet"));
    fireEvent.click(screen.getByTestId("mini-app-open-external"));

    expect(screen.getByTestId("desktop-dock-chat")).toBeInTheDocument();
    expect(screen.getByTestId("desktop-dock-external")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("desktop-dock-launcher"));
    expect(screen.getAllByTestId("desktop-icon")).toHaveLength(2);
  });

  it("limits the dock to ten MiniApp entries", () => {
    const many = Array.from({ length: 12 }, (_, index) => ({
      ...base,
      app_id: `app_${index}`,
      name: `MiniApp ${index}`,
    }));
    render(<DesktopGrid apps={many} />);

    expect(screen.getAllByTestId("desktop-dock-icon")).toHaveLength(10);
    expect(screen.getAllByTestId("desktop-icon")).toHaveLength(12);
  });

  it("closes a running window from the dock context menu", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    const dockIcon = screen
      .getAllByTestId("desktop-dock-icon")
      .find((node) => node.getAttribute("data-app-id") === "app_1")!;
    fireEvent.contextMenu(dockIcon);
    expect(screen.getByTestId("desktop-dock-menu")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("menuitem", { name: "Close" }));
    expect(screen.queryByTestId("desktop-window")).not.toBeInTheDocument();
  });

  it("summarizes experts and windows in the Agent Activity Center", () => {
    render(<DesktopGrid apps={apps} />);
    openAppWindow("app_1");

    expect(screen.getByTestId("desktop-activity-badge")).toHaveTextContent("1");
    fireEvent.click(screen.getByTestId("desktop-activity-button"));

    expect(screen.getByTestId("desktop-activity-panel")).toBeInTheDocument();
    expect(screen.getAllByTestId("desktop-activity-expert")).toHaveLength(1);
    expect(screen.getAllByTestId("desktop-activity-window")).toHaveLength(1);
  });
});
