import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DesktopGrid from "./DesktopGrid";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: "en" },
  }),
}));

vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));

vi.mock("../../context/AgentContext", () => ({
  useAgent: () => ({
    agents: [
      {
        agent_id: "agent_empty",
        name: "空桌面专家",
        state: "running",
        color: "#22c55e",
        icon_name: "sparkles",
        icon_url: null,
      },
    ],
    activeAgentId: null,
    loading: false,
  }),
  selectEnabledExperts: (agents: unknown[]) => agents,
}));

vi.mock("../../components/MiniAppFrame", () => ({
  default: () => <div data-testid="mini-app-frame" />,
}));

vi.mock("./DesktopWebView", () => ({
  default: () => <div data-testid="desktop-webview" />,
}));

vi.mock("./DesktopChatWindow", () => ({
  default: ({ agentId }: { agentId: string }) => (
    <div data-testid="desktop-chat" data-agent-id={agentId} />
  ),
}));

describe("DesktopGrid expert workspaces", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("gives an enabled expert an empty desktop that opens chat", () => {
    render(<DesktopGrid apps={[]} />);

    expect(screen.getByTestId("desktop-empty-space")).toBeInTheDocument();
    expect(screen.getAllByText("空桌面专家").length).toBeGreaterThan(0);

    fireEvent.click(
      within(screen.getByTestId("desktop-empty-space")).getByRole("button"),
    );
    expect(screen.getByTestId("desktop-chat")).toHaveAttribute(
      "data-agent-id",
      "agent_empty",
    );
    expect(screen.getByTestId("desktop-pet")).toHaveAttribute(
      "data-pet-state",
      "working",
    );

    fireEvent.click(screen.getByTestId("desktop-space-trigger"));
    expect(screen.getAllByTestId("desktop-space-option")).toHaveLength(2);
    expect(screen.getByText("All Apps")).toBeInTheDocument();
  });
});
