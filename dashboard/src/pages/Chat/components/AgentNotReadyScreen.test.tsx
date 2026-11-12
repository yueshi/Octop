import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentNotReadyScreen from "./AgentNotReadyScreen";

const navigate = vi.fn();
const listResolvedModels = vi.fn();
const listExperts = vi.fn();
const createFromExpert = vi.fn();
let currentUser: { role?: string } | null = { role: "user" };

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: "zh" },
  }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => navigate,
}));

vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => currentUser,
}));

vi.mock("../../../api/modules/provider", () => ({
  providerApi: {
    listResolvedModels: (...args: unknown[]) => listResolvedModels(...args),
  },
}));

vi.mock("../../../api/modules/expertCatalog", () => ({
  expertCatalogApi: {
    list: (...args: unknown[]) => listExperts(...args),
    createFromExpert: (...args: unknown[]) => createFromExpert(...args),
  },
}));

vi.mock("@/utils/antdMessage", () => ({
  message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

const CATALOG = [
  {
    id: "general-assistant",
    label: { zh: "通用助手", en: "General" },
    description: { zh: "适合起点", en: "Start here" },
  },
  {
    id: "office-automation",
    label: { zh: "办公自动化", en: "Office" },
    description: { zh: "文档处理", en: "Documents" },
  },
  {
    id: "business-operations",
    label: { zh: "业务流程", en: "Business" },
    description: { zh: "运营", en: "Ops" },
  },
];

const MODEL = {
  provider_id: 1,
  provider_name: "openai",
  provider_kind: "openai",
  model: "gpt-4o",
  name: "GPT-4o",
};

function renderNoAgents() {
  return render(<AgentNotReadyScreen agent={null} noAgents />);
}

describe("AgentNotReadyScreen onboarding", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentUser = { role: "user" };
    listResolvedModels.mockResolvedValue([MODEL]);
    listExperts.mockResolvedValue(CATALOG);
    createFromExpert.mockResolvedValue({ agent_id: "a1", name: "通用助手" });
  });

  it("renders recommended templates when a model is available", async () => {
    renderNoAgents();
    expect(await screen.findByText("通用助手")).toBeInTheDocument();
    expect(screen.getByText("办公自动化")).toBeInTheDocument();
    expect(screen.queryByText("业务流程")).not.toBeInTheDocument();
  });

  it("renders the admin template set for admins", async () => {
    currentUser = { role: "admin" };
    renderNoAgents();
    expect(await screen.findByText("业务流程")).toBeInTheDocument();
  });

  it("shows the admin model-config link when no model exists", async () => {
    currentUser = { role: "admin" };
    listResolvedModels.mockResolvedValue([]);
    renderNoAgents();
    expect(
      await screen.findByText("chat.noAgentsNoModelAdmin"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("chat.noAgentsGoConfigModel"));
    expect(navigate).toHaveBeenCalledWith("/admin/models");
  });

  it("tells a regular user to contact the admin when no model exists", async () => {
    listResolvedModels.mockResolvedValue([]);
    renderNoAgents();
    expect(
      await screen.findByText("chat.noAgentsNoModelHint"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("chat.noAgentsGoConfigModel"),
    ).not.toBeInTheDocument();
  });

  it("offers a retry when loading models fails", async () => {
    listResolvedModels.mockRejectedValueOnce(new Error("network"));
    renderNoAgents();
    expect(
      await screen.findByText("chat.noAgentsModelFailed"),
    ).toBeInTheDocument();

    listResolvedModels.mockResolvedValue([MODEL]);
    fireEvent.click(screen.getByText("chat.noAgentsRetry"));

    expect(await screen.findByText("通用助手")).toBeInTheDocument();
  });

  it("creates the selected template and navigates to chat", async () => {
    renderNoAgents();
    fireEvent.click(await screen.findByText("通用助手"));

    await waitFor(() => {
      expect(createFromExpert).toHaveBeenCalledWith("general-assistant", {
        name: "通用助手",
        default_model: "openai/gpt-4o",
      });
    });
    expect(navigate).toHaveBeenCalledWith("/chat/a1");
  });

  it("numbers the name when the first choice is taken", async () => {
    createFromExpert
      .mockRejectedValueOnce(
        new Error("Request failed: 409 - AGENT_NAME_TAKEN"),
      )
      .mockResolvedValueOnce({ agent_id: "a2", name: "通用助手 2" });

    renderNoAgents();
    fireEvent.click(await screen.findByText("通用助手"));

    await waitFor(() => {
      expect(createFromExpert).toHaveBeenCalledTimes(2);
    });
    expect(createFromExpert).toHaveBeenLastCalledWith("general-assistant", {
      name: "通用助手 2",
      default_model: "openai/gpt-4o",
    });
  });

  it("falls back to the experts page when no template matches", async () => {
    listExperts.mockResolvedValue([]);
    renderNoAgents();
    fireEvent.click(await screen.findByText("chat.createExpert"));
    expect(navigate).toHaveBeenCalledWith("/experts?tab=library");
    expect(screen.queryByText("chat.noAgentsViewAll")).not.toBeInTheDocument();
  });

  it("routes 'view all' to the built-in expert library tab", async () => {
    renderNoAgents();
    fireEvent.click(await screen.findByText("chat.noAgentsViewAll"));
    expect(navigate).toHaveBeenCalledWith("/experts?tab=library");
  });

  it("does not render onboarding for users who already have agents", () => {
    render(
      <AgentNotReadyScreen
        agent={
          {
            agent_id: "x",
            name: "x",
            state: "stopped",
            last_error: null,
          } as never
        }
      />,
    );
    expect(screen.queryByText("chat.noAgentsTitle")).not.toBeInTheDocument();
  });
});
