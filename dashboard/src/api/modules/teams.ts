import { request } from "../request";

export interface TeamMemberSummary {
  agent_id: string;
  name: string;
  color?: string | null;
  icon_name?: string | null;
  icon_url?: string | null;
  state?: string;
  is_shared?: boolean;
  user_id?: number | null;
}

export interface TeamRecord {
  team_id: string;
  agent_id: string;
  name: string;
  description?: string | null;
  default_model?: string | null;
  color?: string | null;
  icon_name?: string | null;
  icon_url?: string | null;
  welcome_message?: string | null;
  state?: string;
  kind: "team";
  member_ids: string[];
  members: TeamMemberSummary[];
}

export interface TeamWriteBody {
  name?: string;
  description?: string | null;
  default_model?: string | null;
  color?: string | null;
  icon_name?: string | null;
  icon_url?: string | null;
  welcome_message?: string | null;
  member_ids?: string[];
}

export interface TeamTemplateFile {
  name: string;
  content: string;
}

export interface TeamGoal {
  goal_id: string;
  team_id: string;
  title: string;
  description?: string | null;
  status: string;
  success_criteria?: string | null;
  created_at: number;
  updated_at: number;
}

export interface TeamTask {
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

export interface TeamGoalCreateBody {
  title: string;
  description?: string;
  success_criteria?: string;
}

export interface TeamTaskPatchBody {
  status?: string;
  priority?: number;
  due_at?: number | null;
  expected_version: number;
}

export interface TeamRuntimeWait {
  wait_id: string;
  kind: string;
  status: string;
  task_id?: string | null;
  due_at?: number | null;
  timeout_at?: number | null;
  external_ref?: string | null;
  reason?: string;
}

export interface TeamRuntimeState {
  team_id: string;
  status: string;
  mode: string;
  goal_id?: string | null;
  active_decision_id?: string | null;
  last_decision_at?: number | null;
  next_heartbeat_at?: number | null;
  heartbeat_interval_sec: number;
  budget_day?: string;
  budget_used: number;
  budget_reserved: number;
  paused_reason?: string | null;
  version: number;
  open_waits: TeamRuntimeWait[];
}

export interface TeamDecisionRecord {
  decision_id: string;
  team_id: string;
  status: string;
  mode: string;
  reason: string;
  snapshot_hash: string;
  cost_tokens: number;
  budget_reserved: number;
  error?: string | null;
  plan?: Record<string, unknown> | null;
  created_at: number;
  started_at?: number | null;
  finished_at?: number | null;
}

export interface TeamWaitRecord {
  wait_id: string;
  team_id: string;
  task_id?: string | null;
  kind: string;
  status: string;
  due_at?: number | null;
  timeout_at?: number | null;
  deadline_at?: number | null;
  external_ref?: string | null;
  reason?: string;
  attempts: number;
  created_at: number;
  settled_at?: number | null;
}

export interface DriveStopResult {
  disabled_jobs: number;
  drained_jobs: number;
  cancelled_tasks: number;
  cancelled_waits: number;
}

export const teamsApi = {
  list: () => request<TeamRecord[]>("/teams"),
  templateFiles: () => request<TeamTemplateFile[]>("/teams/template"),
  get: (teamId: string) => request<TeamRecord>(`/teams/${teamId}`),
  create: (body: TeamWriteBody & { name: string; member_ids: string[] }) =>
    request<TeamRecord>("/teams", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  update: (teamId: string, body: TeamWriteBody) =>
    request<TeamRecord>(`/teams/${teamId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  remove: (teamId: string) =>
    request<void>(`/teams/${teamId}`, { method: "DELETE" }),
  listGoals: (teamId: string) => request<TeamGoal[]>(`/teams/${teamId}/goals`),
  createGoal: (teamId: string, body: TeamGoalCreateBody) =>
    request<TeamGoal>(`/teams/${teamId}/goals`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listTasks: (teamId: string) => request<TeamTask[]>(`/teams/${teamId}/tasks`),
  patchTask: (teamId: string, taskId: string, body: TeamTaskPatchBody) =>
    request<TeamTask>(`/teams/${teamId}/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  stopDrive: (teamId: string) =>
    request<DriveStopResult>(`/teams/${teamId}/drive/stop`, {
      method: "POST",
    }),
  getRuntime: (teamId: string) =>
    request<TeamRuntimeState>(`/teams/${teamId}/runtime`),
  startDrive: (
    teamId: string,
    body: { heartbeat_interval_sec?: number; trigger?: string } = {},
  ) =>
    request<TeamRuntimeState>(`/teams/${teamId}/drive/start`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  pauseDrive: (teamId: string, reason?: string) =>
    request<TeamRuntimeState>(`/teams/${teamId}/drive/pause`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  resumeDrive: (teamId: string) =>
    request<TeamRuntimeState>(`/teams/${teamId}/drive/resume`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  finishDrive: (teamId: string) =>
    request<TeamRuntimeState>(`/teams/${teamId}/drive/finish`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  listDecisions: (teamId: string, limit = 50) =>
    request<TeamDecisionRecord[]>(`/teams/${teamId}/decisions?limit=${limit}`),
  listWaits: (teamId: string, limit = 100) =>
    request<TeamWaitRecord[]>(`/teams/${teamId}/waits?limit=${limit}`),
  resolveWait: (
    teamId: string,
    waitId: string,
    body: { status: "approved" | "rejected"; note?: string },
  ) =>
    request<{ wait: TeamWaitRecord; task: TeamTask | null }>(
      `/teams/${teamId}/waits/${waitId}/resolve`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
};
