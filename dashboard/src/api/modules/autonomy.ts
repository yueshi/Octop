import { request } from "../request";

/**
 * Digital-employee switch and job spec. The switch is separate from the
 * policy: turning it off also stops the loop and cancels in-flight work.
 */
export interface AutonomyState {
  agent_id: string;
  enabled: boolean;
}

export interface AutonomyPolicy {
  agent_id: string;
  active_hours_start: string;
  active_hours_end: string;
  min_interval_hours: number;
  max_interval_hours: number;
  budget_limit_tokens: number;
  budget_window: "day" | "month";
  sla_tasks_per_day: number;
  escalate_to: string;
  responsible_scope: string;
  tool_scope_allow: string;
  tool_scope_deny: string;
}

export type AutonomyPolicyBody = Omit<AutonomyPolicy, "agent_id">;

const base = (agentId: string) => `/agents/${agentId}`;

export const autonomyApi = {
  set: (agentId: string, enabled: boolean) =>
    request<AutonomyState>(`${base(agentId)}/autonomy`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  getPolicy: (agentId: string) =>
    request<AutonomyPolicy>(`${base(agentId)}/autonomy-policy`),
  putPolicy: (agentId: string, body: AutonomyPolicyBody) =>
    request<AutonomyPolicy>(`${base(agentId)}/autonomy-policy`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
