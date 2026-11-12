import { request, requestBlob, requestStream, requestUpload } from "../request";

export type AppPermission = "view" | "edit_data" | "edit_structure";

export interface AppColumn {
  name: string;
  label: string;
  type: string;
  required: boolean;
  values: string[];
  pii?: string | null;
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
  has_preview: boolean;
  created_at: number;
  updated_at: number;
}

export interface AppTable {
  table_key: string;
  label: string;
  row_count: number;
  is_main: boolean;
  public_visible: boolean;
  columns: AppColumn[];
}

export interface AppDetail extends AppSummary {
  columns: AppColumn[];
  tables: AppTable[];
}

export interface AppPublication {
  active: boolean;
  url?: string | null;
  token?: string | null;
  expires_at?: number | null;
}

export type AppTemplateSource = "system" | "agent";

export interface AppTemplate {
  template: string;
  template_id: string;
  label: string;
  name: string;
  description: string;
  category: string;
  version: number;
  has_html: boolean;
  has_preview: boolean;
  source: AppTemplateSource;
  agent_id?: string | null;
  scope: "global" | "agent";
}

export interface AppInspectColumn {
  source: string;
  name_suggestion: string;
  type: string;
  non_empty_ratio: number;
  distinct: number;
  samples: string[];
  max_length: number;
  pii?: string | null;
}

export interface AppInspectSheet {
  name: string;
  rows: number;
  rows_scanned: number;
  header_row: number;
  header: string[];
  sample_rows: string[][];
  columns: AppInspectColumn[];
  warnings: string[];
}

export interface AppInspectReport {
  file: string;
  size_bytes: number;
  sheets: AppInspectSheet[];
  pii_columns: { sheet: string; column: string; kind: string }[];
}

export type QuickView = "dashboard" | "ledger" | "board";

export interface AppViewSpec {
  view: QuickView;
  title: string;
  kpis: { column: string; agg: string; label: string }[];
  charts: { type: string; x: string; y: string; agg: string; label: string }[];
  filters: string[];
  group_by: string;
  table: { columns: string[]; page_size: number };
}

export interface QuickSheetInput {
  sheet: string;
  columns: AppColumn[];
  header_row?: number;
  mapping?: Record<string, string>;
  metric_column?: string | null;
  group_column?: string | null;
  view?: QuickView;
}

export interface QuickInspectResponse {
  upload_path: string;
  sheet: string;
  report: AppInspectReport;
  suggested_view: AppViewSpec;
}

export interface QuickCreateResponse {
  app: AppDetail;
  imported: number;
  updated: number;
  skipped: number;
  errors: string[];
  warnings: string[];
  pii_columns: string[];
  blocked_pii: string[];
  failed_rows: { row: number; values: Record<string, string>; error: string }[];
}

export interface AppFile {
  name: string;
  path: string;
  size: number | null;
}

export interface AppRowMeta {
  row_id: string;
  version: number;
  source?: string;
}

export interface AppRowsResponse {
  columns: AppColumn[];
  rows: Record<string, string>[];
  row_meta: AppRowMeta[];
  total: number;
  limit: number;
  offset: number;
  table: string;
  pii_columns: string[];
}

export interface AppRowUpdate {
  row_id: string;
  row: Record<string, string>;
  version: number;
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

export interface AppAssistantCitation {
  table: string;
  row_id: string;
}

export interface AppAssistantChartSeries {
  key: string;
  label: string;
}

export interface AppAssistantChart {
  type: "bar" | "line" | "pie" | "area" | "scatter" | "stacked_bar";
  title: string;
  x: string;
  x_label?: string;
  series: AppAssistantChartSeries[];
  data: Record<string, string | number>[];
  sampled?: boolean;
  sample_size?: number;
  matched_rows?: number;
  truncated?: boolean;
}

export interface AppAssistantMessage {
  role: "user" | "assistant";
  content: string;
  created_at: number;
  citations?: AppAssistantCitation[];
  charts?: AppAssistantChart[];
}

export interface AppGenerateResult {
  text: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
}

/** One launch of an external game hosted on its own origin. */
export interface AppGameSession {
  session_id: string;
  game_id: string;
  name: string;
  url: string;
  token: string;
  expires_at: number;
  sandbox: string;
}

export type AppAssistantEvent =
  | { type: "token"; text: string }
  | { type: "tool"; name: string }
  | { type: "chart"; chart: AppAssistantChart }
  | {
      type: "done";
      rounds: number;
      input_tokens: number;
      output_tokens: number;
      citations?: AppAssistantCitation[];
      charts?: AppAssistantChart[];
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
  updated: number;
  skipped: number;
  errors: string[];
  warnings: string[];
  would_import: number;
  dry_run: boolean;
  pii_columns: string[];
  blocked_pii: string[];
  failed_rows: { row: number; values: Record<string, string>; error: string }[];
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

/** Path of the app's preview image (the SVG cover drawn by the agent). */
export function appPreviewPath(appId: string): string {
  return `/apps/${encodeId(appId)}/preview`;
}

/** Path of a template's preview image. */
export function templatePreviewPath(
  template: Pick<AppTemplate, "template_id" | "source">,
  agentId: string,
): string {
  return `/apps/templates/${encodeURIComponent(
    template.template_id,
  )}/preview?source=${encodeURIComponent(
    template.source,
  )}&agent_id=${encodeURIComponent(agentId)}`;
}

/** Fetch the preview image of an app as a blob (the API needs the JWT). */
export function fetchAppPreview(appId: string): Promise<Blob> {
  return requestBlob(appPreviewPath(appId));
}

/** Fetch the preview image of a template as a blob. */
export function fetchTemplatePreview(
  template: Pick<AppTemplate, "template_id" | "source">,
  agentId: string,
): Promise<Blob> {
  return requestBlob(templatePreviewPath(template, agentId));
}

export const appsApi = {
  list: () => request<{ apps: AppSummary[] }>("/apps"),

  /** MiniApps referenced anywhere in a thread, most recently mentioned first. */
  inThread: (agentId: string, threadId: string) =>
    request<{ apps: AppSummary[] }>(
      `/apps/in-thread?agent_id=${encodeURIComponent(
        agentId,
      )}&thread_id=${encodeURIComponent(threadId)}`,
    ),

  get: (appId: string) => request<AppDetail>(`/apps/${encodeId(appId)}`),

  publication: (appId: string) =>
    request<AppPublication>(`/apps/${encodeId(appId)}/publication`),

  createPublication: (appId: string, ttlDays = 0) =>
    request<AppPublication>(`/apps/${encodeId(appId)}/publication`, {
      method: "POST",
      body: JSON.stringify({ ttl_days: ttlDays }),
    }),

  revokePublication: (appId: string) =>
    request<AppPublication>(`/apps/${encodeId(appId)}/publication`, {
      method: "DELETE",
    }),

  listTemplates: (
    agentId: string,
    source: "all" | "system" | "agent" = "all",
  ) =>
    request<{ templates: AppTemplate[] }>(
      `/apps/templates?agent_id=${encodeURIComponent(
        agentId,
      )}&source=${encodeURIComponent(source)}`,
    ),

  createFromTemplate: (body: {
    agent_id: string;
    template: string;
    source?: AppTemplateSource;
    name?: string;
    description?: string;
  }) =>
    request<AppDetail>("/apps/from-template", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  quickInspect: (agentId: string, file: File, sheet?: string) => {
    const form = new FormData();
    form.append("agent_id", agentId);
    form.append("file", file);
    if (sheet) form.append("sheet", sheet);
    return requestUpload<QuickInspectResponse>("/apps/quick/inspect", form);
  },

  quickCreate: (body: {
    agent_id: string;
    path: string;
    columns?: AppColumn[];
    sheets?: QuickSheetInput[];
    view?: QuickView;
    name: string;
    description?: string;
    sheet?: string;
    header_row?: number;
    mapping?: Record<string, string>;
    metric_column?: string | null;
    group_column?: string | null;
    include_pii?: boolean;
    upsert_key?: string[];
  }) =>
    request<QuickCreateResponse>("/apps/quick/create", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getHtml: async (appId: string): Promise<string> => {
    const blob = await requestBlob(`/apps/${encodeId(appId)}/html`);
    return blob.text();
  },

  asset: (appId: string, name: string) =>
    requestBlob(`/apps/${encodeId(appId)}/assets/${encodeURIComponent(name)}`),

  assets: (appId: string) =>
    request<{ assets: { name: string; size: number }[] }>(
      `/apps/${encodeId(appId)}/assets`,
    ),

  files: (appId: string) =>
    request<{ files: AppFile[] }>(`/apps/${encodeId(appId)}/files`),

  tables: (appId: string) =>
    request<{ tables: AppTable[] }>(`/apps/${encodeId(appId)}/tables`),

  setTableVisibility: (
    appId: string,
    tableKey: string,
    publicVisible: boolean,
  ) =>
    request<AppTable>(
      `/apps/${encodeId(appId)}/tables/${encodeURIComponent(tableKey)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ public_visible: publicVisible }),
      },
    ),

  rows: (appId: string, limit = 200, offset = 0, table = "main") =>
    request<AppRowsResponse>(
      `/apps/${encodeId(
        appId,
      )}/rows?limit=${limit}&offset=${offset}&table=${encodeURIComponent(
        table,
      )}`,
    ),

  appendRow: (
    appId: string,
    row: Record<string, unknown>,
    table = "main",
    clientRequestId?: string,
  ) =>
    request<{
      row_index: number;
      row: Record<string, string>;
      table: string;
      row_id: string;
      version: number;
    }>(`/apps/${encodeId(appId)}/rows`, {
      method: "POST",
      body: JSON.stringify({
        row,
        table,
        client_request_id: clientRequestId,
      }),
    }),

  updateRow: (
    appId: string,
    rowId: string,
    row: Record<string, unknown>,
    expectedVersion?: number,
  ) =>
    request<AppRowUpdate>(
      `/apps/${encodeId(appId)}/rows/${encodeURIComponent(rowId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ row, expected_version: expectedVersion }),
      },
    ),

  deleteRow: (appId: string, rowId: string, expectedVersion?: number) =>
    request<{ deleted: string }>(
      `/apps/${encodeId(appId)}/rows/${encodeURIComponent(rowId)}` +
        (expectedVersion === undefined
          ? ""
          : `?expected_version=${expectedVersion}`),
      { method: "DELETE" },
    ),

  exportTable: (appId: string, table = "main") =>
    requestBlob(
      `/apps/${encodeId(appId)}/export?table=${encodeURIComponent(table)}`,
    ),

  backup: (appId: string) => requestBlob(`/apps/${encodeId(appId)}/backup`),

  restore: (agentId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return requestUpload<AppDetail>(
      `/apps/restore?agent_id=${encodeURIComponent(agentId)}`,
      form,
    );
  },

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

  /** One-shot text generation through the app's owning agent model. */
  generate: (appId: string, body: { prompt: string; system?: string }) =>
    request<AppGenerateResult>(`/apps/${encodeId(appId)}/generate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Issue a short-lived, app/user-scoped session for an external game. */
  createGameSession: (appId: string, gameId: string) =>
    request<AppGameSession>(`/apps/${encodeId(appId)}/game-sessions`, {
      method: "POST",
      body: JSON.stringify({ game_id: gameId }),
    }),

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
      path?: string;
      paths?: string[];
      mode: AppImportMode;
      mapping?: Record<string, string> | null;
      sheet?: string | null;
      header_row?: number | null;
      dry_run?: boolean;
      upsert_key?: string[] | null;
      include_pii?: boolean;
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
