import { request } from "../request";

/** Minimal expert-catalog shape used by the Chat onboarding cards. */
export interface ExpertCatalogEntry {
  id: string;
  label: { zh?: string; en?: string };
  description: { zh?: string; en?: string };
  icon_name?: string | null;
  icon_url?: string | null;
  color?: string | null;
}

export interface CreatedAgentBody {
  agent_id: string;
  name: string;
}

export const expertCatalogApi = {
  list: () => request<ExpertCatalogEntry[]>("/experts"),

  createFromExpert: (
    expertId: string,
    body: { name?: string; default_model?: string | null },
  ) =>
    request<CreatedAgentBody>(
      `/agents/from-expert/${encodeURIComponent(expertId)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
};
