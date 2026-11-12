import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import ChatDockSessionApps from "./ChatDockSessionApps";

vi.mock("../../../components/MiniAppFrame", () => ({
  default: ({ appId }: { appId: string }) => (
    <div data-testid="mini-app-frame" data-app-id={appId} />
  ),
}));

vi.mock("../../../hooks/usePreviewImage", () => ({
  usePreviewImage: () => null,
}));

const apps = [
  {
    app_id: "app_a",
    name: "客户台账",
    row_count: 12,
    has_preview: true,
  },
  {
    app_id: "app_b",
    name: "业绩看板",
    description: "按门店汇总",
  },
];

function renderDock(dockApps: typeof apps) {
  return render(
    <MemoryRouter>
      <ChatDockSessionApps apps={dockApps} />
    </MemoryRouter>,
  );
}

describe("ChatDockSessionApps", () => {
  it("explains when the conversation has no MiniApp", () => {
    renderDock([]);
    expect(
      screen.getByText("No MiniApp in this conversation yet."),
    ).toBeInTheDocument();
  });

  it("lists the session MiniApps", () => {
    renderDock(apps);
    expect(screen.getByText("客户台账")).toBeInTheDocument();
    expect(screen.getByText("业绩看板")).toBeInTheDocument();
    expect(screen.queryByTestId("mini-app-frame")).not.toBeInTheDocument();
  });

  it("runs the selected MiniApp inside the panel and goes back", () => {
    renderDock(apps);
    fireEvent.click(screen.getByText("客户台账"));

    expect(screen.getByTestId("mini-app-frame")).toHaveAttribute(
      "data-app-id",
      "app_a",
    );

    fireEvent.click(screen.getByRole("button", { name: "Back to list" }));
    expect(screen.queryByTestId("mini-app-frame")).not.toBeInTheDocument();
    expect(screen.getByText("业绩看板")).toBeInTheDocument();
  });
});
