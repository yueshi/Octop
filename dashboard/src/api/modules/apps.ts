import { request, requestBlob, requestStream, requestUpload } from "../request";

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

export interface AppTable {
  table_key: string;
  label: string;
  row_count: number;
  is_main: boolean;
  columns: AppColumn[];
}

export interface AppDetail extends AppSummary {
  columns: AppColumn[];
  tables: AppTable[];
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
  table: string;
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

export interface AppAssistantMessage {
  role: "user" | "assistant";
  content: string;
  created_at: number;
}

export type AppAssistantEvent =
  | { type: "token"; text: string }
  | { type: "tool"; name: string }
  | {
      type: "done";
      rounds: number;
      input_tokens: number;
      output_tokens: number;
    }
  | { type: "error"; message: string };

/**
 * Ask the app's built-in assistant. Resolves when the SSE stream ends;
 * ``onEvent`` receives each parsed frame (token / tool / done / error).
 */
export async function askAppAssistant(
  appId: string,
  question: string,
  onEvent: (event: AppAssistantEvent) => void,
): Promise<void> {
  const res = await requestStream(`/apps/${encodeId(appId)}/assistant`, {
    method: "POST",
    // requestStream does not set Content-Type; without it the browser sends
    // the string body as text/plain and FastAPI rejects it as a non-object.
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const dataLine = block
        .split("\n")
        .find((line) => line.startsWith("data: "));
      if (!dataLine) continue;
      try {
        onEvent(JSON.parse(dataLine.slice(6)) as AppAssistantEvent);
      } catch {
        /* skip malformed frame */
      }
    }
  }
}

export type AppImportMode = "append" | "replace";

export interface AppImportResult {
  table: string;
  imported: number;
  skipped: number;
  errors: string[];
}

export type AppSourceKind = "workspace_file" | "external";

export interface AppSource {
  source_id: string;
  table_key: string;
  kind: AppSourceKind;
  mode: AppImportMode;
  schedule_seconds: number;
  params: Record<string, unknown>;
  mapping: Record<string, string>;
  last_run_at: number | null;
  last_status: "ok" | "error" | null;
  last_error: string | null;
  last_row_count: number | null;
  created_by: number;
  created_at: number;
  updated_at: number;
}

export interface AppSourceTool {
  name: string;
  description: string;
  read_only: boolean;
  input_schema: Record<string, unknown>;
}

export interface AppSourceInput {
  table_key: string;
  kind: AppSourceKind;
  mode: AppImportMode;
  schedule_seconds?: number;
  params: Record<string, unknown>;
  mapping?: Record<string, string>;
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

  tables: (appId: string) =>
    request<{ tables: AppTable[] }>(`/apps/${encodeId(appId)}/tables`),

  rows: (appId: string, limit = 200, offset = 0, table = "main") =>
    request<AppRowsResponse>(
      `/apps/${encodeId(
        appId,
      )}/rows?limit=${limit}&offset=${offset}&table=${encodeURIComponent(
        table,
      )}`,
    ),

  appendRow: (appId: string, row: Record<string, unknown>, table = "main") =>
    request<{ row_index: number; row: Record<string, string>; table: string }>(
      `/apps/${encodeId(appId)}/rows`,
      { method: "POST", body: JSON.stringify({ row, table }) },
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

  assistantHistory: (appId: string) =>
    request<{ messages: AppAssistantMessage[] }>(
      `/apps/${encodeId(appId)}/assistant/history`,
    ),

  uploadImportFile: (
    appId: string,
    file: File,
  ): Promise<{ path: string; filename: string; size: number }> => {
    const form = new FormData();
    form.append("file", file);
    return requestUpload(`/apps/${encodeId(appId)}/upload`, form);
  },

  importTable: (
    appId: string,
    tableKey: string,
    body: {
      kind: "upload" | "workspace_file";
      path: string;
      mode: AppImportMode;
      mapping?: Record<string, string> | null;
    },
  ) =>
    request<AppImportResult>(
      `/apps/${encodeId(appId)}/tables/${encodeURIComponent(tableKey)}/import`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  setShares: (appId: string, shares: AppShareInput[]) =>
    request<{ shares: AppShare[] }>(`/apps/${encodeId(appId)}/shares`, {
      method: "PUT",
      body: JSON.stringify({ shares }),
    }),

  listSources: (appId: string) =>
    request<{ sources: AppSource[] }>(`/apps/${encodeId(appId)}/sources`),

  saveSource: (appId: string, sourceId: string, body: AppSourceInput) =>
    request<AppSource>(
      `/apps/${encodeId(appId)}/sources/${encodeURIComponent(sourceId)}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  deleteSource: (appId: string, sourceId: string) =>
    request<void>(
      `/apps/${encodeId(appId)}/sources/${encodeURIComponent(sourceId)}`,
      { method: "DELETE" },
    ),

  runSource: (appId: string, sourceId: string) =>
    request<AppImportResult>(
      `/apps/${encodeId(appId)}/sources/${encodeURIComponent(sourceId)}/run`,
      { method: "POST" },
    ),

  sourceTools: (appId: string, instanceId: string) =>
    request<{ tools: AppSourceTool[] }>(
      `/apps/${encodeId(appId)}/source-tools?instance_id=${encodeURIComponent(
        instanceId,
      )}`,
    ),
};
