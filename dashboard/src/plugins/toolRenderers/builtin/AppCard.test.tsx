import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import zh from "../../../locales/zh.json";
import { builtinPluginHost } from "../host";
import { AppCard } from "./AppCard";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      const parts = key.split(".");
      let value: unknown = zh;
      for (const part of parts) {
        value = (value as Record<string, unknown> | undefined)?.[part];
      }
      if (typeof value !== "string") return key;
      return value.replace(/\{\{(\w+)\}\}/g, (_match, name: string) =>
        String(options?.[name] ?? ""),
      );
    },
  }),
}));

vi.mock("../../../hooks/usePreviewImage", () => ({
  usePreviewImage: () => null,
}));

vi.mock("../../../components/MiniAppFrame", () => ({
  default: () => <div data-testid="mini-app-frame" />,
}));

const openSidePanel = vi.fn();
const base = {
  pluginId: "builtin",
  toolName: "app_write_html",
  callId: "call_1",
  status: "done" as const,
  args: {},
  host: { ...builtinPluginHost, openSidePanel },
  isStreaming: false,
};

const app = {
  app_id: "app_1",
  name: "客户跟进台账",
  description: "按阶段管理客户",
  row_count: 12,
  has_html: true,
  has_preview: false,
};

describe("AppCard", () => {
  it("only offers opening the app in the side panel", () => {
    render(<AppCard {...base} data={app} />);
    expect(screen.getByText("客户跟进台账")).toBeInTheDocument();
    expect(screen.getByText("12 条记录")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /在对话中打开/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /详情页/ }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /侧栏打开/ }));
    expect(openSidePanel).toHaveBeenCalledWith({
      callId: "call_1",
      title: "客户跟进台账",
      toolName: "app_open",
    });
  });

  it("renders only the app frame inside the dock", () => {
    render(<AppCard {...base} data={app} docked />);
    expect(screen.getByTestId("mini-app-frame")).toBeInTheDocument();
    expect(screen.queryByText("客户跟进台账")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /侧栏打开/ }),
    ).not.toBeInTheDocument();
  });

  it("does not offer the sidebar before the UI exists", () => {
    render(<AppCard {...base} data={{ ...app, has_html: false }} />);
    expect(screen.getByText("界面还没有生成")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /侧栏打开/ })).toBeDisabled();
  });
});
