import { request } from "../request";

export type ExternalSourceKind = "webhook" | "connector_poll";
export type ExternalTargetType = "expert" | "team" | "app";
export type ExternalTargetMode =
  | "expert.turn"
  | "expert.notify"
  | "team.judge"
  | "team.notify"
  | "app.refresh"
  | "app.import";

export interface ExternalSource {
  source_id: string;
  name: string;
  kind: ExternalSourceKind;
  enabled: boolean;
  webhook_path: string;
  config: Record<string, unknown>;
  cursor: Record<string, unknown>;
  next_run_at: number | null;
  last_poll_at: number | null;
  fail_count: number;
  last_error: string;
  created_at: number;
  updated_at: number;
}

export interface ExternalSourceCreated extends ExternalSource {
  secret: string;
}

export interface ExternalSubscription {
  subscription_id: string;
  source_id: string;
  target_type: ExternalTargetType;
  target_id: string;
  mode: ExternalTargetMode;
  filter: Record<string, unknown>;
  template: string;
  mapping: Record<string, unknown>;
  coalesce_seconds: number;
  max_per_minute: number;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

export interface ExternalEvent {
  event_id: string;
  source_id: string;
  external_id: string;
  event_type: string;
  occurred_at: number;
  received_at: number;
  summary: string;
  payload_bytes: number;
  status: string;
}

export type ExternalDeliveryStatus =
  | "pending"
  | "delivering"
  | "delivered"
  | "failed"
  | "dead";

export interface ExternalDelivery {
  delivery_id: string;
  event_id: string;
  subscription_id: string;
  target_type: ExternalTargetType;
  target_id: string;
  status: ExternalDeliveryStatus;
  attempt: number;
  next_retry_at: number | null;
  lease_until: number | null;
  last_error: string;
  delivered_at: number | null;
  created_at: number;
}

export interface ConnectorTool {
  name: string;
  description: string;
  read_only: boolean;
  input_schema: Record<string, unknown>;
}

export interface SourceCreateBody {
  name: string;
  kind: ExternalSourceKind;
  config?: Record<string, unknown>;
}

export interface SourcePatchBody {
  name?: string;
  enabled?: boolean;
  config?: Record<string, unknown>;
}

export interface SubscriptionCreateBody {
  source_id: string;
  target_type: ExternalTargetType;
  target_id: string;
  mode: ExternalTargetMode;
  filter?: Record<string, unknown>;
  template?: string;
  mapping?: Record<string, unknown>;
  coalesce_seconds?: number;
  max_per_minute?: number;
}

export interface SubscriptionPatchBody {
  target_id?: string;
  mode?: ExternalTargetMode;
  filter?: Record<string, unknown>;
  template?: string;
  mapping?: Record<string, unknown>;
  coalesce_seconds?: number;
  max_per_minute?: number;
  enabled?: boolean;
}

const q = (params: Record<string, string | number | undefined>) => {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  const text = search.toString();
  return text ? `?${text}` : "";
};

export const externalEventsApi = {
  listSources: () => request<ExternalSource[]>("/external/sources"),

  createSource: (body: SourceCreateBody) =>
    request<ExternalSourceCreated>("/external/sources", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateSource: (sourceId: string, body: SourcePatchBody) =>
    request<ExternalSource>(
      `/external/sources/${encodeURIComponent(sourceId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
      },
    ),

  deleteSource: (sourceId: string) =>
    request<void>(`/external/sources/${encodeURIComponent(sourceId)}`, {
      method: "DELETE",
    }),

  rotateSecret: (sourceId: string) =>
    request<{ source_id: string; secret: string }>(
      `/external/sources/${encodeURIComponent(sourceId)}/rotate-secret`,
      { method: "POST" },
    ),

  pollNow: (sourceId: string) =>
    request<ExternalSource>(
      `/external/sources/${encodeURIComponent(sourceId)}/poll`,
      { method: "POST" },
    ),

  connectorTools: (instanceId: string) =>
    request<ConnectorTool[]>(
      `/external/connector-tools${q({ instance_id: instanceId })}`,
    ),

  listSubscriptions: (sourceId?: string) =>
    request<ExternalSubscription[]>(
      `/external/subscriptions${q({ source_id: sourceId })}`,
    ),

  createSubscription: (body: SubscriptionCreateBody) =>
    request<ExternalSubscription>("/external/subscriptions", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateSubscription: (subscriptionId: string, body: SubscriptionPatchBody) =>
    request<ExternalSubscription>(
      `/external/subscriptions/${encodeURIComponent(subscriptionId)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    ),

  deleteSubscription: (subscriptionId: string) =>
    request<void>(
      `/external/subscriptions/${encodeURIComponent(subscriptionId)}`,
      {
        method: "DELETE",
      },
    ),

  listEvents: (sourceId?: string, limit = 50, offset = 0) =>
    request<ExternalEvent[]>(
      `/external/events${q({ source_id: sourceId, limit, offset })}`,
    ),

  listDeliveries: (
    sourceId?: string,
    status?: ExternalDeliveryStatus,
    limit = 100,
  ) =>
    request<ExternalDelivery[]>(
      `/external/deliveries${q({ source_id: sourceId, status, limit })}`,
    ),

  retryDelivery: (deliveryId: string) =>
    request<{ delivery_id: string; status: string }>(
      `/external/deliveries/${encodeURIComponent(deliveryId)}/retry`,
      { method: "POST" },
    ),
};
