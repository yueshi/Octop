import { request, requestBlob } from "../request";

export type AppPermission = "view" | "edit_data" | "edit_structure";

export interface AppColumn {
  name: string;
  label: string;
  type: string;
  required: boolean;
  values: string[];
}

export interface AppSummary {
  app_id: string;
  name: string;
  description: string;
  agent_id: string;
  agent_name: string;
  owner_user_id: number;
  owner_username: string;
  permission: AppPermission;
  row_count: number;
  html_bytes: number;
  data_bytes: number;
  has_html: boolean;
  created_at: number;
  updated_at: number;
}

export interface AppDetail extends AppSummary {
  columns: AppColumn[];
}

export interface AppFile {
  name: string;
  path: string;
  size: number | null;
}

export interface AppRowsResponse {
  columns: AppColumn[];
  rows: Record<string, string>[];
  total: number;
  limit: number;
  offset: number;
}

export interface AppShare {
  user_id: number;
  username: string;
  display_name: string;
  permission: "view" | "edit_data";
}

export interface AppShareInput {
  username: string;
  permission: "view" | "edit_data";
}

function encodeId(appId: string): string {
  return encodeURIComponent(appId);
}

export const appsApi = {
  list: () => request<{ apps: AppSummary[] }>("/apps"),

  get: (appId: string) => request<AppDetail>(`/apps/${encodeId(appId)}`),

  getHtml: async (appId: string): Promise<string> => {
    const blob = await requestBlob(`/apps/${encodeId(appId)}/html`);
    return blob.text();
  },

  files: (appId: string) =>
    request<{ files: AppFile[] }>(`/apps/${encodeId(appId)}/files`),

  rows: (appId: string, limit = 200, offset = 0) =>
    request<AppRowsResponse>(
      `/apps/${encodeId(appId)}/rows?limit=${limit}&offset=${offset}`,
    ),

  appendRow: (appId: string, row: Record<string, unknown>) =>
    request<{ row_index: number; row: Record<string, string> }>(
      `/apps/${encodeId(appId)}/rows`,
      { method: "POST", body: JSON.stringify({ row }) },
    ),

  update: (appId: string, body: { name?: string; description?: string }) =>
    request<AppDetail>(`/apps/${encodeId(appId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  remove: (appId: string) =>
    request<{ deleted: string }>(`/apps/${encodeId(appId)}`, {
      method: "DELETE",
    }),

  shares: (appId: string) =>
    request<{ shares: AppShare[] }>(`/apps/${encodeId(appId)}/shares`),

  setShares: (appId: string, shares: AppShareInput[]) =>
    request<{ shares: AppShare[] }>(`/apps/${encodeId(appId)}/shares`, {
      method: "PUT",
      body: JSON.stringify({ shares }),
    }),
};
