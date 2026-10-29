import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { Copy, KeyRound, Play, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { appsApi, type AppSummary } from "../../../api/modules/apps";
import {
  connectorsApi,
  type ConnectorInstance,
} from "../../../api/modules/connectors";
import {
  externalEventsApi,
  type ConnectorTool,
  type ExternalDelivery,
  type ExternalDeliveryStatus,
  type ExternalEvent,
  type ExternalSource,
  type ExternalSourceKind,
  type ExternalSubscription,
  type ExternalTargetMode,
  type ExternalTargetType,
} from "../../../api/modules/externalEvents";
import { teamsApi, type TeamRecord } from "../../../api/modules/teams";
import { useAgent } from "../../../context/AgentContext";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import PageShell from "../../../layouts/PageShell";
import { apiErrorMessage } from "../../../utils/apiError";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import styles from "./index.module.less";

const { Text } = Typography;

const TARGET_MODES: Record<ExternalTargetType, ExternalTargetMode[]> = {
  expert: ["expert.turn", "expert.notify"],
  team: ["team.judge", "team.notify"],
  app: ["app.refresh", "app.import"],
};

const MODE_LABEL: Record<ExternalTargetMode, string> = {
  "expert.turn": "externalEvents.mode.expertTurn",
  "expert.notify": "externalEvents.mode.expertNotify",
  "team.judge": "externalEvents.mode.teamJudge",
  "team.notify": "externalEvents.mode.teamNotify",
  "app.refresh": "externalEvents.mode.appRefresh",
  "app.import": "externalEvents.mode.appImport",
};

const STATUS_COLOR: Record<string, string> = {
  pending: "default",
  delivering: "processing",
  delivered: "success",
  failed: "warning",
  dead: "error",
};

interface SecretInfo {
  sourceId: string;
  secret: string;
  webhookPath: string;
}

interface SourceFormValues {
  name: string;
  kind: ExternalSourceKind;
  instance_id?: string;
  tool?: string;
  interval_seconds?: number;
  event_type?: string;
  args?: string;
  cursor_arg?: string;
  cursor_path?: string;
  items_path?: string;
  id_path?: string;
  summary_path?: string;
  occurred_at_path?: string;
}

interface SubscriptionFormValues {
  target_type: ExternalTargetType;
  target_id: string;
  mode: ExternalTargetMode;
  app_source_id?: string;
  table?: string;
  columns?: string;
  filter?: string;
  template?: string;
  coalesce_seconds?: number;
  max_per_minute?: number;
}

function parseObject(text: string | undefined): Record<string, unknown> | null {
  const trimmed = (text ?? "").trim();
  if (!trimmed) return {};
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }
  return null;
}

export default function ExternalEventsPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const timezone = useServerTimezone();
  const { agents } = useAgent();

  const [sources, setSources] = useState<ExternalSource[]>([]);
  const [selected, setSelected] = useState<string>();
  const [subscriptions, setSubscriptions] = useState<ExternalSubscription[]>(
    [],
  );
  const [events, setEvents] = useState<ExternalEvent[]>([]);
  const [deliveries, setDeliveries] = useState<ExternalDelivery[]>([]);
  const [teams, setTeams] = useState<TeamRecord[]>([]);
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [connectors, setConnectors] = useState<ConnectorInstance[]>([]);
  const [tools, setTools] = useState<ConnectorTool[]>([]);
  const [appSources, setAppSources] = useState<
    Array<{ source_id: string; kind: string; table_key: string; mode: string }>
  >([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [sourceOpen, setSourceOpen] = useState(false);
  const [subscriptionOpen, setSubscriptionOpen] = useState(false);
  const [secretInfo, setSecretInfo] = useState<SecretInfo | null>(null);
  const [deliveryStatus, setDeliveryStatus] = useState<
    ExternalDeliveryStatus | undefined
  >();
  const [sourceForm] = Form.useForm<SourceFormValues>();
  const [subscriptionForm] = Form.useForm<SubscriptionFormValues>();
  const sourceKind = Form.useWatch("kind", sourceForm) ?? "webhook";
  const instanceId = Form.useWatch("instance_id", sourceForm);
  const targetType = Form.useWatch("target_type", subscriptionForm) ?? "expert";
  const targetId = Form.useWatch("target_id", subscriptionForm);
  const targetMode = Form.useWatch("mode", subscriptionForm);

  const selectedSource = useMemo(
    () => sources.find((item) => item.source_id === selected),
    [sources, selected],
  );

  const copyText = useCallback(
    async (text: string) => {
      try {
        await navigator.clipboard.writeText(text);
        message.success(t("externalEvents.copied"));
      } catch {
        message.info(text);
      }
    },
    [message, t],
  );

  const loadSources = useCallback(async () => {
    try {
      const rows = await externalEventsApi.listSources();
      setSources(rows);
      setSelected((current) => current ?? rows[0]?.source_id);
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.loadFailed"), t));
    }
  }, [message, t]);

  const loadSourceData = useCallback(
    async (sourceId: string) => {
      setLoading(true);
      try {
        const [subs, eventRows, deliveryRows] = await Promise.all([
          externalEventsApi.listSubscriptions(sourceId),
          externalEventsApi.listEvents(sourceId),
          externalEventsApi.listDeliveries(sourceId, deliveryStatus),
        ]);
        setSubscriptions(subs);
        setEvents(eventRows);
        setDeliveries(deliveryRows);
      } catch (err) {
        message.error(apiErrorMessage(err, t("externalEvents.loadFailed"), t));
      } finally {
        setLoading(false);
      }
    },
    [deliveryStatus, message, t],
  );

  useEffect(() => {
    void loadSources();
    void teamsApi
      .list()
      .then(setTeams)
      .catch(() => undefined);
    void appsApi
      .list()
      .then((res) => setApps(res.apps))
      .catch(() => undefined);
    void connectorsApi
      .listInstances()
      .then(setConnectors)
      .catch(() => undefined);
  }, [loadSources]);

  useEffect(() => {
    if (selected) void loadSourceData(selected);
  }, [selected, loadSourceData]);

  useEffect(() => {
    if (sourceKind !== "connector_poll" || !instanceId) {
      setTools([]);
      return;
    }
    void externalEventsApi
      .connectorTools(instanceId)
      .then((rows) => setTools(rows.filter((row) => row.read_only)))
      .catch((err) =>
        message.error(apiErrorMessage(err, t("externalEvents.loadFailed"), t)),
      );
  }, [instanceId, message, sourceKind, t]);

  useEffect(() => {
    if (targetType !== "app" || targetMode !== "app.refresh" || !targetId) {
      setAppSources([]);
      return;
    }
    void appsApi
      .listSources(targetId)
      .then((res) => setAppSources(res.sources))
      .catch(() => setAppSources([]));
  }, [targetId, targetMode, targetType]);

  const createSource = async () => {
    const values = await sourceForm.validateFields();
    let config: Record<string, unknown> | undefined;
    if (values.kind === "connector_poll") {
      const args = parseObject(values.args);
      if (args === null) {
        message.error(t("externalEvents.invalidJson", { field: "args" }));
        return;
      }
      config = {
        instance_id: values.instance_id,
        tool: values.tool,
        interval_seconds: values.interval_seconds ?? 300,
        event_type: values.event_type || "connector.event",
        args,
        cursor_arg: values.cursor_arg || "",
        cursor_path: values.cursor_path || "",
        items_path: values.items_path || "",
        id_path: values.id_path || "id",
        summary_path: values.summary_path || "summary",
        occurred_at_path: values.occurred_at_path || "",
      };
    }
    setSaving(true);
    try {
      const created = await externalEventsApi.createSource({
        name: values.name,
        kind: values.kind,
        config,
      });
      setSourceOpen(false);
      sourceForm.resetFields();
      await loadSources();
      setSelected(created.source_id);
      if (created.kind === "webhook") {
        setSecretInfo({
          sourceId: created.source_id,
          secret: created.secret,
          webhookPath: created.webhook_path,
        });
      } else {
        message.success(t("externalEvents.created"));
      }
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  };

  const createSubscription = async () => {
    if (!selected) return;
    const values = await subscriptionForm.validateFields();
    const filter = parseObject(values.filter);
    if (filter === null) {
      message.error(t("externalEvents.invalidJson", { field: "filter" }));
      return;
    }
    const mapping: Record<string, unknown> = {};
    if (values.target_type === "app") {
      if (values.mode === "app.refresh") {
        if (!values.app_source_id) {
          message.error(t("externalEvents.required"));
          return;
        }
        mapping.source_id = values.app_source_id;
      } else {
        mapping.table = values.table || "main";
        const columns = parseObject(values.columns);
        if (columns === null) {
          message.error(t("externalEvents.invalidJson", { field: "columns" }));
          return;
        }
        if (Object.keys(columns).length) mapping.columns = columns;
      }
    }
    setSaving(true);
    try {
      await externalEventsApi.createSubscription({
        source_id: selected,
        target_type: values.target_type,
        target_id: values.target_id,
        mode: values.mode,
        filter,
        template: values.template || "",
        mapping,
        coalesce_seconds: values.coalesce_seconds ?? 0,
        max_per_minute: values.max_per_minute ?? 0,
      });
      setSubscriptionOpen(false);
      subscriptionForm.resetFields();
      message.success(t("externalEvents.created"));
      await loadSourceData(selected);
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  };

  const patchSource = async (source: ExternalSource, enabled: boolean) => {
    try {
      await externalEventsApi.updateSource(source.source_id, { enabled });
      await loadSources();
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.saveFailed"), t));
    }
  };

  const rotateSecret = async (source: ExternalSource) => {
    try {
      const result = await externalEventsApi.rotateSecret(source.source_id);
      setSecretInfo({
        sourceId: source.source_id,
        secret: result.secret,
        webhookPath: source.webhook_path,
      });
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.saveFailed"), t));
    }
  };

  const pollNow = async (source: ExternalSource) => {
    try {
      await externalEventsApi.pollNow(source.source_id);
      message.success(t("externalEvents.pollQueued"));
      await loadSources();
      await loadSourceData(source.source_id);
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.saveFailed"), t));
    }
  };

  const removeSource = async (source: ExternalSource) => {
    try {
      await externalEventsApi.deleteSource(source.source_id);
      if (selected === source.source_id) setSelected(undefined);
      await loadSources();
      message.success(t("externalEvents.deleted"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.deleteFailed"), t));
    }
  };

  const toggleSubscription = async (
    subscription: ExternalSubscription,
    enabled: boolean,
  ) => {
    try {
      await externalEventsApi.updateSubscription(subscription.subscription_id, {
        enabled,
      });
      if (selected) await loadSourceData(selected);
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.saveFailed"), t));
    }
  };

  const removeSubscription = async (subscription: ExternalSubscription) => {
    try {
      await externalEventsApi.deleteSubscription(subscription.subscription_id);
      if (selected) await loadSourceData(selected);
      message.success(t("externalEvents.deleted"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.deleteFailed"), t));
    }
  };

  const retryDelivery = async (delivery: ExternalDelivery) => {
    try {
      await externalEventsApi.retryDelivery(delivery.delivery_id);
      message.success(t("externalEvents.retried"));
      if (selected) await loadSourceData(selected);
    } catch (err) {
      message.error(apiErrorMessage(err, t("externalEvents.retryFailed"), t));
    }
  };

  const targetName = (type: ExternalTargetType, id: string): string => {
    if (type === "team")
      return teams.find((item) => item.team_id === id)?.name ?? id;
    if (type === "app")
      return apps.find((item) => item.app_id === id)?.name ?? id;
    return agents.find((item) => item.agent_id === id)?.name ?? id;
  };

  const expertOptions = agents
    .filter((agent) => agent.kind !== "team" && agent.is_owner !== false)
    .map((agent) => ({ value: agent.agent_id, label: agent.name }));

  const targetOptions =
    targetType === "team"
      ? teams.map((team) => ({ value: team.team_id, label: team.name }))
      : targetType === "app"
      ? apps.map((app) => ({ value: app.app_id, label: app.name }))
      : expertOptions;

  const sourceColumns: ColumnsType<ExternalSource> = [
    {
      title: t("externalEvents.name"),
      dataIndex: "name",
      render: (_value, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.name}</Text>
          <Text type="secondary" className={styles.mono}>
            {row.source_id}
          </Text>
        </Space>
      ),
    },
    {
      title: t("externalEvents.kindLabel"),
      dataIndex: "kind",
      width: 150,
      render: (value: ExternalSourceKind) => (
        <Tag color={value === "webhook" ? "blue" : "purple"}>
          {t(`externalEvents.kind.${value}`)}
        </Tag>
      ),
    },
    {
      title: t("externalEvents.status"),
      dataIndex: "enabled",
      width: 110,
      render: (value: boolean, row) => (
        <Switch
          checked={value}
          onChange={(checked) => void patchSource(row, checked)}
        />
      ),
    },
    {
      title: t("externalEvents.connection"),
      render: (_value, row) =>
        row.kind === "webhook" ? (
          <Space size={4}>
            <Text code className={styles.mono}>
              {row.webhook_path}
            </Text>
            <Button
              size="small"
              type="text"
              icon={<Copy size={14} />}
              onClick={() =>
                void copyText(`${window.location.origin}${row.webhook_path}`)
              }
            />
          </Space>
        ) : (
          <Space direction="vertical" size={0}>
            <Text type="secondary">
              {t("externalEvents.intervalSeconds")}:{" "}
              {String(row.config.interval_seconds ?? 300)}
            </Text>
            <Text type="secondary">
              {t("externalEvents.nextRun")}:{" "}
              {formatServerDateTime(row.next_run_at ?? 0, timezone)}
            </Text>
          </Space>
        ),
    },
    {
      title: t("externalEvents.lastError"),
      dataIndex: "last_error",
      render: (value: string) =>
        value ? (
          <Tooltip title={value}>
            <Text type="danger" ellipsis className={styles.errorText}>
              {value}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("externalEvents.actions"),
      width: 260,
      render: (_value, row) => (
        <Space wrap size={4}>
          {row.kind === "webhook" ? (
            <Button
              size="small"
              icon={<KeyRound size={14} />}
              onClick={() => void rotateSecret(row)}
            >
              {t("externalEvents.rotateSecret")}
            </Button>
          ) : (
            <Button
              size="small"
              icon={<Play size={14} />}
              onClick={() => void pollNow(row)}
            >
              {t("externalEvents.pollNow")}
            </Button>
          )}
          <Popconfirm
            title={t("externalEvents.deleteSourceConfirm")}
            onConfirm={() => void removeSource(row)}
            okText={t("common.delete")}
            cancelText={t("common.cancel")}
          >
            <Button size="small" danger type="text" icon={<Trash2 size={14} />}>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const subscriptionColumns: ColumnsType<ExternalSubscription> = [
    {
      title: t("externalEvents.target"),
      render: (_value, row) => (
        <Space direction="vertical" size={0}>
          <Tag>{t(`externalEvents.targetType.${row.target_type}`)}</Tag>
          <Text>{targetName(row.target_type, row.target_id)}</Text>
        </Space>
      ),
    },
    {
      title: t("externalEvents.mode.label"),
      dataIndex: "mode",
      render: (value: ExternalTargetMode) => t(MODE_LABEL[value]),
    },
    {
      title: t("externalEvents.filter"),
      dataIndex: "filter",
      render: (value: Record<string, unknown>) => (
        <Text code className={styles.mono}>
          {JSON.stringify(value)}
        </Text>
      ),
    },
    {
      title: t("externalEvents.status"),
      dataIndex: "enabled",
      width: 100,
      render: (value: boolean, row) => (
        <Switch
          checked={value}
          onChange={(checked) => void toggleSubscription(row, checked)}
        />
      ),
    },
    {
      title: t("externalEvents.actions"),
      width: 100,
      render: (_value, row) => (
        <Popconfirm
          title={t("externalEvents.deleteSubscriptionConfirm")}
          onConfirm={() => void removeSubscription(row)}
          okText={t("common.delete")}
          cancelText={t("common.cancel")}
        >
          <Button size="small" danger type="text" icon={<Trash2 size={14} />}>
            {t("common.delete")}
          </Button>
        </Popconfirm>
      ),
    },
  ];

  const eventColumns: ColumnsType<ExternalEvent> = [
    {
      title: t("externalEvents.receivedAt"),
      dataIndex: "received_at",
      width: 190,
      render: (value: number) => formatServerDateTime(value, timezone),
    },
    {
      title: t("externalEvents.eventType"),
      dataIndex: "event_type",
      width: 200,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("externalEvents.summary"),
      dataIndex: "summary",
      render: (value: string) => (
        <Tooltip title={value}>
          <Text ellipsis className={styles.summaryText}>
            {value || "—"}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: t("externalEvents.status"),
      dataIndex: "status",
      width: 140,
      render: (value: string) => <Tag>{value}</Tag>,
    },
  ];

  const deliveryColumns: ColumnsType<ExternalDelivery> = [
    {
      title: t("externalEvents.createdAt"),
      dataIndex: "created_at",
      width: 190,
      render: (value: number) => formatServerDateTime(value, timezone),
    },
    {
      title: t("externalEvents.target"),
      render: (_value, row) => (
        <Space direction="vertical" size={0}>
          <Tag>{t(`externalEvents.targetType.${row.target_type}`)}</Tag>
          <Text>{targetName(row.target_type, row.target_id)}</Text>
        </Space>
      ),
    },
    {
      title: t("externalEvents.status"),
      dataIndex: "status",
      width: 120,
      render: (value: ExternalDeliveryStatus) => (
        <Tag color={STATUS_COLOR[value]}>
          {t(`externalEvents.deliveryStatus.${value}`)}
        </Tag>
      ),
    },
    {
      title: t("externalEvents.attempt"),
      dataIndex: "attempt",
      width: 90,
    },
    {
      title: t("externalEvents.nextRetry"),
      dataIndex: "next_retry_at",
      width: 190,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timezone) : "—",
    },
    {
      title: t("externalEvents.lastError"),
      dataIndex: "last_error",
      render: (value: string) =>
        value ? (
          <Tooltip title={value}>
            <Text type="danger" ellipsis className={styles.errorText}>
              {value}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("externalEvents.actions"),
      width: 110,
      render: (_value, row) =>
        row.status === "failed" || row.status === "dead" ? (
          <Button size="small" onClick={() => void retryDelivery(row)}>
            {t("externalEvents.retry")}
          </Button>
        ) : null,
    },
  ];

  return (
    <PageShell
      title={t("externalEvents.title")}
      subtitle={t("externalEvents.subtitle")}
      actions={
        <Space wrap>
          <Select
            className={styles.sourceSelect}
            value={selected}
            placeholder={t("externalEvents.selectSource")}
            options={sources.map((source) => ({
              value: source.source_id,
              label: source.name,
            }))}
            onChange={setSelected}
          />
          <Button
            icon={<RefreshCw size={15} />}
            onClick={() => {
              void loadSources();
              if (selected) void loadSourceData(selected);
            }}
          >
            {t("externalEvents.refresh")}
          </Button>
          <Button
            type="primary"
            icon={<Plus size={15} />}
            onClick={() => setSourceOpen(true)}
          >
            {t("externalEvents.createSource")}
          </Button>
        </Space>
      }
    >
      {sources.length === 0 ? (
        <Alert
          showIcon
          type="info"
          message={t("externalEvents.noSources")}
          description={t("externalEvents.noSourcesHint")}
        />
      ) : (
        <Tabs
          items={[
            {
              key: "sources",
              label: `${t("externalEvents.sources")} (${sources.length})`,
              children: (
                <Table
                  rowKey="source_id"
                  size="small"
                  loading={loading}
                  dataSource={sources}
                  columns={sourceColumns}
                  pagination={false}
                />
              ),
            },
            {
              key: "subscriptions",
              label: `${t("externalEvents.subscriptions")} (${
                subscriptions.length
              })`,
              children: (
                <Space direction="vertical" size={12} style={{ width: "100%" }}>
                  <Button
                    type="dashed"
                    icon={<Plus size={14} />}
                    disabled={!selectedSource}
                    onClick={() => setSubscriptionOpen(true)}
                  >
                    {t("externalEvents.createSubscription")}
                  </Button>
                  <Table
                    rowKey="subscription_id"
                    size="small"
                    loading={loading}
                    dataSource={subscriptions}
                    columns={subscriptionColumns}
                    pagination={false}
                  />
                </Space>
              ),
            },
            {
              key: "events",
              label: `${t("externalEvents.events")} (${events.length})`,
              children: (
                <Table
                  rowKey="event_id"
                  size="small"
                  loading={loading}
                  dataSource={events}
                  columns={eventColumns}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                />
              ),
            },
            {
              key: "deliveries",
              label: `${t("externalEvents.deliveries")} (${deliveries.length})`,
              children: (
                <Space direction="vertical" size={12} style={{ width: "100%" }}>
                  <Select
                    allowClear
                    style={{ width: 180 }}
                    placeholder={t("externalEvents.statusFilter")}
                    value={deliveryStatus}
                    options={[
                      "pending",
                      "delivering",
                      "delivered",
                      "failed",
                      "dead",
                    ].map((value) => ({
                      value,
                      label: t(`externalEvents.deliveryStatus.${value}`),
                    }))}
                    onChange={(value) => setDeliveryStatus(value)}
                  />
                  <Table
                    rowKey="delivery_id"
                    size="small"
                    loading={loading}
                    dataSource={deliveries}
                    columns={deliveryColumns}
                    pagination={{ pageSize: 20, showSizeChanger: false }}
                  />
                </Space>
              ),
            },
          ]}
        />
      )}

      <Modal
        open={sourceOpen}
        title={t("externalEvents.createSource")}
        onCancel={() => setSourceOpen(false)}
        onOk={() => void createSource()}
        confirmLoading={saving}
        okText={t("common.create")}
        cancelText={t("common.cancel")}
        destroyOnHidden
        width={640}
      >
        <Form
          form={sourceForm}
          layout="vertical"
          initialValues={{ kind: "webhook" }}
        >
          <Form.Item
            name="name"
            label={t("externalEvents.name")}
            rules={[{ required: true }]}
          >
            <Input maxLength={120} />
          </Form.Item>
          <Form.Item name="kind" label={t("externalEvents.kindLabel")}>
            <Select
              options={[
                { value: "webhook", label: t("externalEvents.kind.webhook") },
                {
                  value: "connector_poll",
                  label: t("externalEvents.kind.connector_poll"),
                },
              ]}
            />
          </Form.Item>
          {sourceKind === "connector_poll" ? (
            <>
              <Form.Item
                name="instance_id"
                label={t("externalEvents.connectorInstance")}
                rules={[{ required: true }]}
              >
                <Select
                  showSearch
                  optionFilterProp="label"
                  onChange={() =>
                    sourceForm.setFieldsValue({ tool: undefined })
                  }
                  options={connectors
                    .filter((item) => item.status === "active")
                    .map((item) => ({
                      value: item.instance_id,
                      label: `${item.display_name} (${item.kind})`,
                    }))}
                />
              </Form.Item>
              <Form.Item
                name="tool"
                label={t("externalEvents.tool")}
                rules={[{ required: true }]}
              >
                <Select
                  showSearch
                  optionFilterProp="label"
                  options={tools.map((tool) => ({
                    value: tool.name,
                    label: tool.description
                      ? `${tool.name} — ${tool.description}`
                      : tool.name,
                  }))}
                />
              </Form.Item>
              <Space style={{ display: "flex" }} align="start">
                <Form.Item
                  name="interval_seconds"
                  label={t("externalEvents.intervalSeconds")}
                  initialValue={300}
                >
                  <InputNumber min={30} max={86400} />
                </Form.Item>
                <Form.Item
                  name="event_type"
                  label={t("externalEvents.eventType")}
                  initialValue="connector.event"
                  style={{ flex: 1 }}
                >
                  <Input maxLength={100} />
                </Form.Item>
              </Space>
              <Form.Item name="args" label="args (JSON)">
                <Input.TextArea rows={2} placeholder='{"limit": 20}' />
              </Form.Item>
              <Space style={{ display: "flex" }} align="start">
                <Form.Item
                  name="cursor_arg"
                  label={t("externalEvents.cursorArg")}
                >
                  <Input placeholder="since" />
                </Form.Item>
                <Form.Item
                  name="cursor_path"
                  label={t("externalEvents.cursorPath")}
                >
                  <Input placeholder="updated_at" />
                </Form.Item>
              </Space>
              <Space style={{ display: "flex" }} align="start">
                <Form.Item
                  name="items_path"
                  label={t("externalEvents.itemsPath")}
                >
                  <Input placeholder="data.items" />
                </Form.Item>
                <Form.Item name="id_path" label={t("externalEvents.idPath")}>
                  <Input placeholder="id" />
                </Form.Item>
              </Space>
              <Space style={{ display: "flex" }} align="start">
                <Form.Item
                  name="summary_path"
                  label={t("externalEvents.summaryPath")}
                >
                  <Input placeholder="summary" />
                </Form.Item>
                <Form.Item
                  name="occurred_at_path"
                  label={t("externalEvents.occurredAtPath")}
                >
                  <Input placeholder="created_at" />
                </Form.Item>
              </Space>
            </>
          ) : null}
        </Form>
      </Modal>

      <Modal
        open={subscriptionOpen}
        title={t("externalEvents.createSubscription")}
        onCancel={() => setSubscriptionOpen(false)}
        onOk={() => void createSubscription()}
        confirmLoading={saving}
        okText={t("common.create")}
        cancelText={t("common.cancel")}
        destroyOnHidden
        width={620}
      >
        <Form
          form={subscriptionForm}
          layout="vertical"
          initialValues={{ target_type: "expert", filter: "{}" }}
        >
          <Form.Item
            name="target_type"
            label={t("externalEvents.targetType.title")}
          >
            <Select
              options={(["expert", "team", "app"] as ExternalTargetType[]).map(
                (value) => ({
                  value,
                  label: t(`externalEvents.targetType.${value}`),
                }),
              )}
              onChange={() => {
                subscriptionForm.setFieldsValue({
                  target_id: undefined,
                  mode: undefined,
                });
              }}
            />
          </Form.Item>
          <Form.Item
            name="target_id"
            label={t("externalEvents.targetId")}
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              options={targetOptions}
            />
          </Form.Item>
          <Form.Item
            name="mode"
            label={t("externalEvents.mode.label")}
            rules={[{ required: true }]}
          >
            <Select
              options={(targetType ? TARGET_MODES[targetType] : []).map(
                (value) => ({
                  value,
                  label: t(MODE_LABEL[value]),
                }),
              )}
            />
          </Form.Item>
          {targetType === "app" && targetMode === "app.refresh" ? (
            <Form.Item
              name="app_source_id"
              label={t("externalEvents.appSource")}
              rules={[{ required: true }]}
            >
              <Select
                options={appSources.map((source) => ({
                  value: source.source_id,
                  label: `${source.source_id} (${source.kind}/${source.mode})`,
                }))}
              />
            </Form.Item>
          ) : null}
          {targetType === "app" && targetMode === "app.import" ? (
            <>
              <Alert
                type="warning"
                showIcon
                message={t("externalEvents.importWarning")}
                style={{ marginBottom: 12 }}
              />
              <Form.Item
                name="table"
                label={t("externalEvents.table")}
                initialValue="main"
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="columns"
                label={`${t("externalEvents.columns")} (JSON)`}
              >
                <Input.TextArea
                  rows={2}
                  placeholder='{"order_title": "title"}'
                />
              </Form.Item>
            </>
          ) : null}
          <Form.Item
            name="filter"
            label={`${t("externalEvents.filter")} (JSON)`}
          >
            <Input.TextArea
              rows={2}
              placeholder='{"event_type": "order.created", "equals": {"region": "east"}}'
            />
          </Form.Item>
          <Form.Item name="template" label={t("externalEvents.template")}>
            <Input placeholder="[{source}] {summary}" maxLength={2000} />
          </Form.Item>
          <Space style={{ display: "flex" }} align="start">
            <Form.Item
              name="coalesce_seconds"
              label={t("externalEvents.coalesceSeconds")}
            >
              <InputNumber min={0} max={3600} />
            </Form.Item>
            <Form.Item
              name="max_per_minute"
              label={t("externalEvents.maxPerMinute")}
            >
              <InputNumber min={0} max={600} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        open={secretInfo !== null}
        title={t("externalEvents.secretTitle")}
        onCancel={() => setSecretInfo(null)}
        footer={
          <Button type="primary" onClick={() => setSecretInfo(null)}>
            {t("common.close")}
          </Button>
        }
        width={620}
      >
        {secretInfo ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Alert
              type="warning"
              showIcon
              message={t("externalEvents.secretWarning")}
            />
            <div>
              <Text strong>{t("externalEvents.webhookPath")}</Text>
              <Input
                readOnly
                value={`${window.location.origin}${secretInfo.webhookPath}`}
                addonAfter={
                  <Button
                    size="small"
                    type="text"
                    icon={<Copy size={14} />}
                    onClick={() =>
                      void copyText(
                        `${window.location.origin}${secretInfo.webhookPath}`,
                      )
                    }
                  />
                }
              />
            </div>
            <div>
              <Text strong>{t("externalEvents.secret")}</Text>
              <Input
                readOnly
                value={secretInfo.secret}
                addonAfter={
                  <Button
                    size="small"
                    type="text"
                    icon={<Copy size={14} />}
                    onClick={() => void copyText(secretInfo.secret)}
                  />
                }
              />
            </div>
            <Text type="secondary">{t("externalEvents.signatureHint")}</Text>
          </Space>
        ) : null}
      </Modal>
    </PageShell>
  );
}
