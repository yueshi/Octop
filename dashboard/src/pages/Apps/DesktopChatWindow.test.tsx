import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ForwardedRef } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AppSummary } from "../../api/modules/apps";
import type { OctopThread } from "../../api/modules/octopThreads";
import type { OctopAgent } from "../../context/AgentContext";
import DesktopChatWindow from "./DesktopChatWindow";

const { mockList, mockCreate, mockUseChat } = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockCreate: vi.fn(),
  mockUseChat: vi.fn(),
}));

vi.mock("../../api/modules/octopThreads", () => ({
  octopThreadsApi: {
    list: mockList,
    create: mockCreate,
  },
}));

vi.mock("../Chat/hooks/useChat", () => ({
  useChat: mockUseChat,
}));

vi.mock("../Chat/components/MessageList", () => ({
  default: ({ messages }: { messages: unknown[] }) => (
    <div data-testid="message-list" data-count={messages.length} />
  ),
}));

vi.mock("../Chat/components/ChatInput", async () => {
  const React = await import("react");
  return {
    default: React.forwardRef(function MockChatInput(
      props: { onNewChat: () => void },
      ref: ForwardedRef<{
        setPrefillText: (text: string) => void;
        setPrefillComposer: (text: string) => void;
        focusComposer: () => void;
      }>,
    ) {
      const [text, setText] = React.useState("");
      React.useImperativeHandle(ref, () => ({
        setPrefillText: (value: string) => setText(value),
        setPrefillComposer: (value: string) => setText(value),
        focusComposer: () => undefined,
      }));
      return (
        <div data-testid="desktop-chat-composer">
          <textarea
            aria-label="composer"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <button
            type="button"
            aria-label="Mock new conversation"
            onClick={props.onNewChat}
          />
        </div>
      );
    }),
  };
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));

const agent: OctopAgent = {
  id: 1,
  agent_id: "agent_1",
  name: "销售助理",
  description: "跟进客户",
  persona_mbti: null,
  default_model: "openai/gpt-5",
  system_prompt: null,
  template_name: null,
  state: "running",
  last_error: null,
  icon: null,
  icon_name: null,
  icon_url: null,
  color: "#6366f1",
  config: {},
  welcome_message: "今天想先处理哪个小程序？",
};

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

const dashboardThread: OctopThread = {
  thread_id: "thr_dash",
  title: "客户跟进",
  channel_type: "dashboard",
  session_key: "session",
  last_active: 1_700_000_000,
  created_at: 1_700_000_000,
  has_messages: true,
};

const imThread: OctopThread = {
  ...dashboardThread,
  thread_id: "thr_im",
  title: "微信里的客户",
  channel_type: "wecom",
};

function mockChat(options?: { messages?: unknown[]; isStreaming?: boolean }) {
  mockUseChat.mockReturnValue({
    messages: options?.messages ?? [],
    isStreaming: options?.isStreaming ?? false,
    thinkingStartedAt: null,
    historyLoading: false,
    historyError: false,
    historyHasMore: false,
    historyLoadingMore: false,
    historyRefreshing: false,
    historyHydrated: true,
    runUsage: null,
    contextUsage: null,
    liveSpeakers: [],
    sendMessage: vi.fn(),
    editAndResend: vi.fn(),
    cancelStream: vi.fn(),
    loadHistory: vi.fn(),
    loadMoreHistory: vi.fn(),
    refreshHistory: vi.fn(),
    retryHistory: vi.fn(),
    resumeHitl: vi.fn(),
  });
}

describe("DesktopChatWindow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mockList.mockResolvedValue([imThread, dashboardThread]);
    mockCreate.mockResolvedValue({
      thread_id: "thr_new",
      session_key: "new-session",
    });
    mockChat();
  });

  it("resumes the latest dashboard conversation and shows the message stream", async () => {
    mockChat({ messages: [{ id: "m1" }, { id: "m2" }] });
    render(
      <DesktopChatWindow
        agentId="agent_1"
        agentName="销售助理"
        agent={agent}
        apps={[app]}
      />,
    );

    await waitFor(() =>
      expect(mockUseChat).toHaveBeenLastCalledWith(
        "thr_dash",
        "agent_1",
        false,
      ),
    );
    expect(screen.getByText("销售助理")).toBeInTheDocument();
    expect(screen.getByText("客户跟进")).toBeInTheDocument();
    expect(screen.getByTestId("message-list")).toHaveAttribute(
      "data-count",
      "2",
    );
  });

  it("prefills an app action into the composer", async () => {
    render(
      <DesktopChatWindow
        agentId="agent_1"
        agentName="销售助理"
        agent={agent}
        apps={[app]}
        initialAppId="app_1"
      />,
    );

    const composer = (await screen.findByLabelText(
      "composer",
    )) as HTMLTextAreaElement;
    fireEvent.click(screen.getByTestId("desktop-chat-action-optimize"));

    expect(composer.value).toContain("客户跟进台账");
    expect(composer.value).toContain("app_1");
  });

  it("opens the selected MiniApp window from the context bar", async () => {
    const onOpenApp = vi.fn();
    render(
      <DesktopChatWindow
        agentId="agent_1"
        agentName="销售助理"
        agent={agent}
        apps={[app]}
        initialAppId="app_1"
        onOpenApp={onOpenApp}
      />,
    );

    fireEvent.click(await screen.findByTestId("desktop-chat-open-app"));

    expect(onOpenApp).toHaveBeenCalledWith(app);
  });

  it("opens a MiniApp window directly from the app picker", async () => {
    const onOpenApp = vi.fn();
    render(
      <DesktopChatWindow
        agentId="agent_1"
        agentName="销售助理"
        agent={agent}
        apps={[app]}
        onOpenApp={onOpenApp}
      />,
    );

    fireEvent.click(await screen.findByTestId("desktop-chat-pick-app"));
    fireEvent.click(await screen.findByTestId("desktop-chat-app-open"));

    expect(onOpenApp).toHaveBeenCalledWith(app);
  });

  it("lists conversations and switches to the selected thread", async () => {
    render(
      <DesktopChatWindow
        agentId="agent_1"
        agentName="销售助理"
        agent={agent}
        apps={[app]}
      />,
    );

    await screen.findByText("客户跟进");
    fireEvent.click(screen.getByTestId("desktop-chat-thread-trigger"));

    const threads = screen.getAllByTestId("desktop-chat-thread");
    expect(threads).toHaveLength(2);
    fireEvent.click(threads[0]);

    await waitFor(() =>
      expect(mockUseChat).toHaveBeenLastCalledWith("thr_im", "agent_1", false),
    );
  });

  it("creates a new conversation from the header", async () => {
    render(
      <DesktopChatWindow
        agentId="agent_1"
        agentName="销售助理"
        agent={agent}
        apps={[app]}
      />,
    );

    await screen.findByTestId("desktop-chat-new");
    fireEvent.click(screen.getByTestId("desktop-chat-new"));

    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith("agent_1"));
  });
});
