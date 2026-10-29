import { request } from "../request";

/**
 * Board tasks for any agent, team host or digital employee. Same table and
 * state machine as `/teams/{id}/tasks`; this is the agent-generic surface the
 * board and the goal drawer use.
 */
export interface AgentTask {
  task_id: string;
  team_id: string;
  goal_id?: string | null;
  title: string;
  spec?: string | null;
  assignee?: string | null;
  status: string;
  priority: number;
  result_summary?: string | null;
  blocked_reason?: string | null;
  version: number;
  created_at: number;
  updated_at: number;
  due_at?: number | null;
  window_start?: number | null;
  window_end?: number | null;
  budget_tokens?: number | null;
  concurrency_key?: string | null;
  active_wait_id?: string | null;
}

export interface TaskCreateBody {
  title: string;
  spec?: string;
  goal_id?: string | null;
  assignee?: string | null;
  priority?: number;
  due_at?: number | null;
}

export interface TaskPatchBody {
  status?: string;
  priority?: number;
  due_at?: number | null;
  expected_version: number;
}

const base = (agentId: string) => `/agents/${agentId}/tasks`;

export const tasksApi = {
  list: (agentId: string, status?: string, goalId?: string) => {
    const query = new URLSearchParams();
    if (status) query.set("status", status);
    if (goalId) query.set("goal_id", goalId);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return request<AgentTask[]>(`${base(agentId)}${suffix}`);
  },
  create: (agentId: string, body: TaskCreateBody) =>
    request<AgentTask>(base(agentId), {
      method: "POST",
      body: JSON.stringify(body),
    }),
  patch: (agentId: string, taskId: string, body: TaskPatchBody) =>
    request<AgentTask>(`${base(agentId)}/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
};
