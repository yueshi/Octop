import { request } from "../request";

export type DeliverableStatus = "pending" | "produced";
export type ArtifactType = "none" | "file" | "link" | "text" | "app";

export interface GoalDeliverable {
  deliverable_id: string;
  goal_id: string;
  title: string;
  description: string;
  format: string;
  required: boolean;
  status: DeliverableStatus;
  task_id?: string | null;
  artifact_type: ArtifactType;
  artifact_ref: string;
  artifact_name: string;
  artifact_agent_id?: string | null;
  created_at: number;
  updated_at: number;
}

export interface AgentGoal {
  goal_id: string;
  agent_id: string;
  team_id: string;
  title: string;
  description?: string | null;
  status: string;
  success_criteria?: string | null;
  created_at: number;
  updated_at: number;
  deliverables: GoalDeliverable[];
}

export interface DeliverableCreateBody {
  title: string;
  description?: string;
  format?: string;
  required?: boolean;
}

export interface GoalCreateBody {
  title: string;
  description?: string;
  success_criteria?: string;
  deliverables?: DeliverableCreateBody[];
}

export interface GoalPatchBody {
  title?: string;
  description?: string;
  success_criteria?: string;
}

export interface DeliverablePatchBody {
  title?: string;
  description?: string;
  format?: string;
  required?: boolean;
  status?: DeliverableStatus;
  task_id?: string | null;
  artifact_type?: ArtifactType;
  artifact_ref?: string;
  artifact_name?: string;
  artifact_agent_id?: string | null;
}

const base = (agentId: string) => `/agents/${agentId}/goals`;

export const goalsApi = {
  list: (agentId: string, status?: string) =>
    request<AgentGoal[]>(
      `${base(agentId)}${
        status ? `?status=${encodeURIComponent(status)}` : ""
      }`,
    ),
  create: (agentId: string, body: GoalCreateBody) =>
    request<AgentGoal>(base(agentId), {
      method: "POST",
      body: JSON.stringify(body),
    }),
  update: (agentId: string, goalId: string, body: GoalPatchBody) =>
    request<AgentGoal>(`${base(agentId)}/${goalId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  addDeliverable: (
    agentId: string,
    goalId: string,
    body: DeliverableCreateBody,
  ) =>
    request<GoalDeliverable>(`${base(agentId)}/${goalId}/deliverables`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateDeliverable: (
    agentId: string,
    goalId: string,
    deliverableId: string,
    body: DeliverablePatchBody,
  ) =>
    request<GoalDeliverable>(
      `${base(agentId)}/${goalId}/deliverables/${deliverableId}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    ),
  deleteDeliverable: (agentId: string, goalId: string, deliverableId: string) =>
    request<void>(`${base(agentId)}/${goalId}/deliverables/${deliverableId}`, {
      method: "DELETE",
    }),
};
