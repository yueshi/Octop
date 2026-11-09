import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  Modal,
  Radio,
  Segmented,
  Select,
  Space,
  Spin,
  Steps,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { Inbox, LayoutDashboard, List, Store, Trello } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  appsApi,
  templatePreviewPath,
  type AppColumn,
  type AppInspectColumn,
  type AppInspectSheet,
  type AppTemplate,
  type AppViewSpec,
  type QuickInspectResponse,
  type QuickView,
} from "../../api/modules/apps";
import { useAgent } from "../../context/AgentContext";
import { usePreviewImage } from "../../hooks/usePreviewImage";
import { message } from "../../utils/antdMessage";
import { apiErrorMessage } from "../../utils/apiError";

const { Text, Paragraph } = Typography;

type Mode = "quick" | "template" | "market";
type TemplateFilter = "all" | "system" | "agent";

interface EditableField {
  name: string;
  source: string;
  label: string;
  type: string;
  required: boolean;
  include: boolean;
  pii?: string | null;
}

interface SheetDraft {
  sheet: string;
  selected: boolean;
  fields: EditableField[];
  view: QuickView;
  metric: string;
  group: string;
}

interface Props {
  open: boolean;
  onCancel: () => void;
  onCreated: (appId: string) => void;
}

const TYPE_OPTIONS = [
  { value: "string", labelKey: "apps.create.typeString" },
  { value: "text", labelKey: "apps.create.typeText" },
  { value: "integer", labelKey: "apps.create.typeInteger" },
  { value: "number", labelKey: "apps.create.typeNumber" },
  { value: "boolean", labelKey: "apps.create.typeBoolean" },
  { value: "date", labelKey: "apps.create.typeDate" },
  { value: "datetime", labelKey: "apps.create.typeDatetime" },
];

const VIEW_OPTIONS: {
  value: QuickView;
  icon: ReactNode;
  labelKey: string;
  hintKey: string;
}[] = [
  {
    value: "dashboard",
    icon: <LayoutDashboard size={18} />,
    labelKey: "apps.create.viewDashboard",
    hintKey: "apps.create.viewDashboardHint",
  },
  {
    value: "ledger",
    icon: <List size={18} />,
    labelKey: "apps.create.viewLedger",
    hintKey: "apps.create.viewLedgerHint",
  },
  {
    value: "board",
    icon: <Trello size={18} />,
    labelKey: "apps.create.viewBoard",
    hintKey: "apps.create.viewBoardHint",
  },
];

function normalizeType(type: string): string {
  return TYPE_OPTIONS.some((option) => option.value === type) ? type : "string";
}

function sheetToFields(sheet: AppInspectSheet | undefined): EditableField[] {
  if (!sheet) return [];
  return sheet.columns.map((column) => ({
    name: column.name_suggestion,
    source: column.source,
    label: column.source || column.name_suggestion,
    type: normalizeType(column.type),
    required: false,
    include: true,
    pii: column.pii ?? null,
  }));
}

function isCategorical(column: AppInspectColumn): boolean {
  return (
    column.type === "enum" ||
    column.type === "boolean" ||
    (column.type === "string" && column.distinct > 0 && column.distinct <= 12)
  );
}

function numericNames(sheet: AppInspectSheet | undefined): string[] {
  if (!sheet) return [];
  return sheet.columns
    .filter(
      (column) =>
        !column.pii && (column.type === "number" || column.type === "integer"),
    )
    .map((column) => column.name_suggestion);
}

function categoricalNames(sheet: AppInspectSheet | undefined): string[] {
  if (!sheet) return [];
  return sheet.columns
    .filter((column) => !column.pii && isCategorical(column))
    .map((column) => column.name_suggestion);
}

function defaultMetric(
  sheet: AppInspectSheet | undefined,
  spec: AppViewSpec | undefined,
): string {
  const fromSpec = spec?.kpis.find(
    (item) => item.agg !== "count" && item.column,
  )?.column;
  return fromSpec || numericNames(sheet)[0] || "";
}

function defaultGroup(
  sheet: AppInspectSheet | undefined,
  spec: AppViewSpec | undefined,
): string {
  return spec?.group_by || categoricalNames(sheet)[0] || "";
}

function defaultName(filename: string): string {
  const base = filename.split(/[\\/]/).pop() || "小程序";
  return base.replace(/\.[^.]+$/, "").slice(0, 100) || "小程序";
}

function TemplatePreview({
  template,
  agentId,
}: {
  template: AppTemplate;
  agentId: string;
}) {
  const url = usePreviewImage(
    template.has_preview && agentId
      ? templatePreviewPath(template, agentId)
      : null,
  );
  if (!url) return null;
  return (
    <img
      src={url}
      alt={template.name || template.template_id}
      style={{
        display: "block",
        width: "100%",
        height: 96,
        objectFit: "cover",
        borderRadius: 6,
        marginBottom: 8,
        background: "var(--fn-color-brand-bg)",
      }}
    />
  );
}

export default function CreateMiniAppModal({
  open,
  onCancel,
  onCreated,
}: Props) {
  const { t } = useTranslation();
  const { agents, activeAgentId } = useAgent();
  const [mode, setMode] = useState<Mode>("quick");
  const [agentId, setAgentId] = useState<string>("");
  const [busy, setBusy] = useState(false);

  // Quick mode
  const [quickFile, setQuickFile] = useState<File | null>(null);
  const [inspect, setInspect] = useState<QuickInspectResponse | null>(null);
  const [drafts, setDrafts] = useState<SheetDraft[]>([]);
  const [activeSheet, setActiveSheet] = useState<string>("");
  const [includePii, setIncludePii] = useState(false);
  const [quickName, setQuickName] = useState("");

  // Template mode
  const [filter, setFilter] = useState<TemplateFilter>("all");
  const [templates, setTemplates] = useState<AppTemplate[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [templateId, setTemplateId] = useState("");
  const [templateName, setTemplateName] = useState("");

  useEffect(() => {
    if (!open) return;
    const fallback = activeAgentId || agents[0]?.agent_id || "";
    setAgentId((current) => current || fallback);
  }, [open, activeAgentId, agents]);

  useEffect(() => {
    if (!open || mode !== "template" || !agentId) return;
    let alive = true;
    setTemplatesLoading(true);
    appsApi
      .listTemplates(agentId, filter)
      .then((data) => {
        if (!alive) return;
        setTemplates(data.templates);
        setTemplateId((current) =>
          data.templates.some(
            (item) => `${item.source}:${item.template_id}` === current,
          )
            ? current
            : "",
        );
      })
      .catch((err) => {
        if (alive) message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      })
      .finally(() => {
        if (alive) setTemplatesLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [open, mode, agentId, filter, t]);

  const selectedSheet = useMemo(
    () => inspect?.report.sheets.find((sheet) => sheet.name === activeSheet),
    [inspect, activeSheet],
  );

  const activeDraft = useMemo(
    () => drafts.find((draft) => draft.sheet === activeSheet),
    [drafts, activeSheet],
  );
  const fields = useMemo(() => activeDraft?.fields || [], [activeDraft]);
  const view = activeDraft?.view || "dashboard";
  const metric = activeDraft?.metric || "";
  const group = activeDraft?.group || "";

  const updateDraft = (sheet: string, patch: Partial<SheetDraft>) => {
    setDrafts((current) =>
      current.map((draft) =>
        draft.sheet === sheet ? { ...draft, ...patch } : draft,
      ),
    );
  };

  const piiColumns = useMemo(
    () =>
      Array.from(
        new Set(
          drafts
            .filter((draft) => draft.selected)
            .flatMap((draft) =>
              draft.fields
                .filter((field) => Boolean(field.pii))
                .map((field) => field.label),
            ),
        ),
      ),
    [drafts],
  );

  const metricOptions = useMemo(
    () =>
      fields
        .filter(
          (field) =>
            field.include &&
            !field.pii &&
            (field.type === "number" || field.type === "integer"),
        )
        .map((field) => ({
          value: field.name,
          label: field.label || field.name,
        })),
    [fields],
  );

  const groupOptions = useMemo(() => {
    const stats = new Map(
      (selectedSheet?.columns || []).map((column) => [
        column.name_suggestion,
        column,
      ]),
    );
    return fields
      .filter((field) => {
        if (!field.include || field.pii) return false;
        if (field.type === "enum" || field.type === "boolean") return true;
        if (field.type !== "string") return false;
        const distinct = stats.get(field.name)?.distinct || 0;
        return distinct > 0 && distinct <= 12;
      })
      .map((field) => ({
        value: field.name,
        label: field.label || field.name,
      }));
  }, [fields, selectedSheet]);

  useEffect(() => {
    if (!activeSheet) return;
    const patch: Partial<SheetDraft> = {};
    if (metric && !metricOptions.some((option) => option.value === metric)) {
      patch.metric = metricOptions[0]?.value || "";
    }
    if (group && !groupOptions.some((option) => option.value === group)) {
      patch.group = groupOptions[0]?.value || "";
    }
    if (Object.keys(patch).length) {
      setDrafts((current) =>
        current.map((draft) =>
          draft.sheet === activeSheet ? { ...draft, ...patch } : draft,
        ),
      );
    }
  }, [activeSheet, metric, metricOptions, group, groupOptions]);

  const resetQuick = () => {
    setQuickFile(null);
    setInspect(null);
    setDrafts([]);
    setActiveSheet("");
    setIncludePii(false);
    setQuickName("");
  };

  useEffect(() => {
    if (!open) return;
    setMode("quick");
    setQuickFile(null);
    setInspect(null);
    setDrafts([]);
    setActiveSheet("");
    setIncludePii(false);
    setQuickName("");
    setTemplateId("");
    setTemplateName("");
  }, [open]);

  const inspectFile = async (file: File, selectedAgent = agentId) => {
    if (!selectedAgent) {
      message.warning(t("apps.create.agentRequired"));
      return;
    }
    setBusy(true);
    setQuickFile(file);
    setInspect(null);
    try {
      const result = await appsApi.quickInspect(selectedAgent, file);
      setInspect(result);
      const built = result.report.sheets.map((sheet, index) => ({
        sheet: sheet.name,
        selected: index === 0,
        fields: sheetToFields(sheet),
        view:
          index === 0
            ? result.suggested_view.view || ("dashboard" as QuickView)
            : ("dashboard" as QuickView),
        metric: defaultMetric(
          sheet,
          index === 0 ? result.suggested_view : undefined,
        ),
        group: defaultGroup(
          sheet,
          index === 0 ? result.suggested_view : undefined,
        ),
      }));
      setDrafts(built);
      setActiveSheet(result.sheet || result.report.sheets[0]?.name || "");
      setQuickName(defaultName(file.name));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setBusy(false);
    }
  };

  const toggleSheet = (name: string, selected: boolean) => {
    setDrafts((current) =>
      current.map((draft) =>
        draft.sheet === name ? { ...draft, selected } : draft,
      ),
    );
    if (selected) {
      setActiveSheet(name);
    } else if (name === activeSheet) {
      const next = drafts.find(
        (draft) => draft.sheet !== name && draft.selected,
      );
      // Nothing left to edit: clear the editor instead of keeping a sheet
      // that will not be imported.
      setActiveSheet(next?.sheet || "");
    }
  };

  const updateField = (name: string, patch: Partial<EditableField>) => {
    setDrafts((current) =>
      current.map((draft) =>
        draft.sheet === activeSheet
          ? {
              ...draft,
              fields: draft.fields.map((field) =>
                field.name === name ? { ...field, ...patch } : field,
              ),
            }
          : draft,
      ),
    );
  };

  const submitQuick = async () => {
    if (!inspect || !agentId) return;
    const selected = drafts.filter((draft) => draft.selected);
    if (!selected.length) {
      message.warning(t("apps.create.sheetsRequired"));
      return;
    }
    if (
      selected.some((draft) => !draft.fields.some((field) => field.include))
    ) {
      message.warning(t("apps.create.fieldsRequired"));
      return;
    }
    setBusy(true);
    try {
      const sheets = selected.map((draft) => {
        const sheet = inspect.report.sheets.find(
          (item) => item.name === draft.sheet,
        );
        const picked = draft.fields.filter((field) => field.include);
        const columns: AppColumn[] = picked.map((field) => ({
          name: field.name,
          label: field.label || field.name,
          type: field.type,
          required: field.required,
          values: [],
          pii: field.pii ?? null,
        }));
        const mapping: Record<string, string> = {};
        for (const field of picked) mapping[field.source] = field.name;
        return {
          sheet: draft.sheet,
          columns,
          header_row: sheet?.header_row,
          mapping,
          metric_column: draft.metric || null,
          group_column: draft.group || null,
          view: draft.view,
        };
      });
      const result = await appsApi.quickCreate({
        agent_id: agentId,
        path: inspect.upload_path,
        sheets,
        name: quickName.trim() || defaultName(quickFile?.name || "小程序"),
        include_pii: includePii,
      });
      if (result.blocked_pii.length) {
        message.warning(
          t("apps.create.blockedPii", {
            columns: result.blocked_pii.join("、"),
          }),
        );
      } else if (result.warnings.length) {
        message.warning(result.warnings.join("；"));
      }
      message.success(t("apps.create.success"));
      resetQuick();
      onCreated(result.app.app_id);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setBusy(false);
    }
  };

  const submitTemplate = async () => {
    const selected = templates.find(
      (item) => `${item.source}:${item.template_id}` === templateId,
    );
    if (!selected || !agentId) {
      message.warning(t("apps.create.templateRequired"));
      return;
    }
    setBusy(true);
    try {
      const detail = await appsApi.createFromTemplate({
        agent_id: agentId,
        template: selected.template_id,
        source: selected.source,
        name: templateName.trim() || selected.name,
      });
      message.success(t("apps.create.success"));
      setTemplateId("");
      setTemplateName("");
      onCreated(detail.app_id);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setBusy(false);
    }
  };

  const fieldColumns: ColumnsType<EditableField> = [
    {
      title: t("apps.create.fieldLabel"),
      dataIndex: "label",
      render: (_value, field) => (
        <Input
          value={field.label}
          onChange={(event) =>
            updateField(field.name, { label: event.target.value })
          }
        />
      ),
    },
    {
      title: t("apps.create.fieldKey"),
      dataIndex: "name",
      width: 150,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("apps.create.fieldType"),
      dataIndex: "type",
      width: 130,
      render: (value: string, field) => (
        <Select
          value={value}
          options={TYPE_OPTIONS.map((option) => ({
            value: option.value,
            label: t(option.labelKey),
          }))}
          style={{ width: "100%" }}
          onChange={(next) => updateField(field.name, { type: next })}
        />
      ),
    },
    {
      title: t("apps.create.fieldRequired"),
      dataIndex: "required",
      width: 80,
      render: (value: boolean, field) => (
        <Checkbox
          checked={value}
          onChange={(event) =>
            updateField(field.name, { required: event.target.checked })
          }
        />
      ),
    },
    {
      title: t("apps.create.fieldInclude"),
      dataIndex: "include",
      width: 80,
      render: (value: boolean, field) => (
        <Checkbox
          checked={value}
          onChange={(event) =>
            updateField(field.name, { include: event.target.checked })
          }
        />
      ),
    },
  ];

  return (
    <Modal
      title={t("apps.create.title")}
      open={open}
      onCancel={onCancel}
      footer={null}
      width={840}
      destroyOnHidden
      styles={{
        body: {
          maxHeight: "calc(100vh - 180px)",
          overflowY: "auto",
          paddingRight: 6,
        },
      }}
    >
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <Segmented
          block
          value={mode}
          onChange={(value) => setMode(value as Mode)}
          options={[
            { value: "quick", label: t("apps.create.modeQuick") },
            { value: "template", label: t("apps.create.modeTemplate") },
            { value: "market", label: t("apps.create.modeMarket") },
          ]}
        />

        <Space wrap style={{ width: "100%" }}>
          <Text type="secondary">{t("apps.agentLabel")}</Text>
          <Select
            style={{ minWidth: 240 }}
            value={agentId || undefined}
            placeholder={t("apps.create.agentPlaceholder")}
            options={agents.map((agent) => ({
              value: agent.agent_id,
              label: agent.name,
            }))}
            onChange={(value) => {
              setAgentId(value);
              if (mode === "quick" && quickFile) {
                void inspectFile(quickFile, value);
              }
            }}
          />
        </Space>

        {mode === "quick" ? (
          <Space direction="vertical" size={14} style={{ width: "100%" }}>
            <Paragraph type="secondary" style={{ margin: 0 }}>
              {t("apps.create.quickIntro")}
            </Paragraph>
            <Steps
              size="small"
              current={inspect ? 2 : 0}
              items={[
                {
                  title: t("apps.create.stepUpload"),
                  description: t("apps.create.stepUploadHint"),
                },
                {
                  title: t("apps.create.stepFields"),
                  description: t("apps.create.stepFieldsHint"),
                },
                {
                  title: t("apps.create.stepView"),
                  description: t("apps.create.stepViewHint"),
                },
              ]}
            />
            <Upload.Dragger
              accept=".xlsx,.xls,.csv,.tsv,.json"
              showUploadList={false}
              disabled={busy || !agentId}
              beforeUpload={(file) => {
                void inspectFile(file as File);
                return false;
              }}
            >
              <p className="ant-upload-drag-icon">
                <Inbox size={26} />
              </p>
              <p className="ant-upload-text">{t("apps.create.uploadTitle")}</p>
              <p className="ant-upload-hint">{t("apps.create.uploadHint")}</p>
            </Upload.Dragger>

            {busy && !inspect ? (
              <div style={{ textAlign: "center", padding: 24 }}>
                <Spin />
              </div>
            ) : null}

            {inspect ? (
              <>
                <Space wrap>
                  <Text type="secondary">{t("apps.create.sheets")}</Text>
                  {inspect.report.sheets.map((sheet) => {
                    const draft = drafts.find(
                      (item) => item.sheet === sheet.name,
                    );
                    const isActive = activeSheet === sheet.name;
                    return (
                      <span
                        key={sheet.name}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 6,
                          padding: "4px 10px",
                          border: `1px solid ${
                            isActive ? "var(--fn-color-brand)" : "#e5e7eb"
                          }`,
                          borderRadius: 999,
                          background: isActive
                            ? "var(--fn-color-brand-bg)"
                            : undefined,
                        }}
                      >
                        <Checkbox
                          checked={Boolean(draft?.selected)}
                          onChange={(event) =>
                            toggleSheet(sheet.name, event.target.checked)
                          }
                        />
                        <span
                          style={{ cursor: "pointer" }}
                          onClick={() => setActiveSheet(sheet.name)}
                        >
                          {sheet.name} · {t("apps.rows", { count: sheet.rows })}
                        </span>
                      </span>
                    );
                  })}
                  <Text type="secondary">
                    {t("apps.create.selectedSheets", {
                      count: drafts.filter((draft) => draft.selected).length,
                    })}
                  </Text>
                  <Text type="secondary">{t("apps.name")}</Text>
                  <Input
                    style={{ width: 260 }}
                    value={quickName}
                    maxLength={100}
                    onChange={(event) => setQuickName(event.target.value)}
                  />
                  {selectedSheet && selectedSheet.header_row >= 0 ? (
                    <Text type="secondary">
                      {t("apps.inspectHeaderRow", {
                        row: selectedSheet.header_row + 1,
                      })}
                    </Text>
                  ) : null}
                </Space>

                {selectedSheet?.warnings?.length ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={selectedSheet.warnings.join("；")}
                  />
                ) : null}

                {piiColumns.length ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={t("apps.create.piiWarning", {
                      columns: piiColumns.join("、"),
                    })}
                    action={
                      <Checkbox
                        checked={includePii}
                        onChange={(event) =>
                          setIncludePii(event.target.checked)
                        }
                      >
                        {t("apps.create.includePii")}
                      </Checkbox>
                    }
                  />
                ) : null}

                <Table
                  size="small"
                  rowKey="name"
                  pagination={false}
                  columns={fieldColumns}
                  dataSource={fields}
                  scroll={{ y: 240 }}
                />

                {view !== "ledger" &&
                (metricOptions.length > 0 || groupOptions.length > 0) ? (
                  <Space wrap>
                    <Text type="secondary">{t("apps.create.analysis")}</Text>
                    {metricOptions.length ? (
                      <>
                        <Text type="secondary">{t("apps.create.metric")}</Text>
                        <Select
                          allowClear
                          style={{ minWidth: 160 }}
                          value={metric || undefined}
                          placeholder={t("apps.create.metricPlaceholder")}
                          options={metricOptions}
                          onChange={(value) =>
                            updateDraft(activeSheet, { metric: value || "" })
                          }
                        />
                      </>
                    ) : null}
                    {groupOptions.length ? (
                      <>
                        <Text type="secondary">{t("apps.create.group")}</Text>
                        <Select
                          allowClear
                          style={{ minWidth: 160 }}
                          value={group || undefined}
                          placeholder={t("apps.create.groupPlaceholder")}
                          options={groupOptions}
                          onChange={(value) =>
                            updateDraft(activeSheet, { group: value || "" })
                          }
                        />
                      </>
                    ) : null}
                  </Space>
                ) : null}

                {selectedSheet && selectedSheet.sample_rows.length ? (
                  <details
                    style={{
                      background: "rgba(127,127,127,0.06)",
                      border: "1px solid rgba(127,127,127,0.2)",
                      borderRadius: 8,
                      padding: "8px 12px",
                    }}
                  >
                    <summary
                      style={{
                        cursor: "pointer",
                        opacity: 0.65,
                        fontSize: 12,
                      }}
                    >
                      {t("apps.create.dataPreview", {
                        count: selectedSheet.sample_rows.length,
                      })}
                    </summary>
                    <div
                      style={{
                        overflow: "auto",
                        maxHeight: 220,
                        marginTop: 8,
                      }}
                    >
                      <table
                        style={{
                          borderCollapse: "collapse",
                          width: "100%",
                          fontSize: 12,
                        }}
                      >
                        <thead>
                          <tr>
                            {selectedSheet.header.map((header, index) => (
                              <th
                                key={index}
                                style={{
                                  textAlign: "left",
                                  padding: "4px 8px",
                                  borderBottom:
                                    "1px solid rgba(127,127,127,0.2)",
                                  whiteSpace: "nowrap",
                                  opacity: 0.65,
                                  fontWeight: 500,
                                }}
                              >
                                {header}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {selectedSheet.sample_rows.map((row, rowIndex) => (
                            <tr key={rowIndex}>
                              {row.map((cell, cellIndex) => (
                                <td
                                  key={cellIndex}
                                  style={{
                                    padding: "4px 8px",
                                    borderBottom:
                                      rowIndex ===
                                      selectedSheet.sample_rows.length - 1
                                        ? "none"
                                        : "1px solid rgba(127,127,127,0.2)",
                                    whiteSpace: "nowrap",
                                  }}
                                >
                                  {cell || "—"}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </details>
                ) : null}

                <div>
                  <Text type="secondary">{t("apps.create.view")}</Text>
                  <Radio.Group
                    value={view}
                    onChange={(event) =>
                      updateDraft(activeSheet, {
                        view: event.target.value as QuickView,
                      })
                    }
                    style={{ marginTop: 8, width: "100%" }}
                  >
                    <Space wrap>
                      {VIEW_OPTIONS.map((option) => (
                        <Radio.Button key={option.value} value={option.value}>
                          <Space size={6}>
                            {option.icon}
                            <span>{t(option.labelKey)}</span>
                          </Space>
                        </Radio.Button>
                      ))}
                    </Space>
                  </Radio.Group>
                  <Paragraph type="secondary" style={{ margin: "6px 0 0" }}>
                    {t(
                      VIEW_OPTIONS.find((option) => option.value === view)
                        ?.hintKey || "apps.create.viewDashboardHint",
                    )}
                  </Paragraph>
                </div>

                <Space>
                  <Button
                    type="primary"
                    loading={busy}
                    disabled={!fields.some((field) => field.include)}
                    onClick={() => void submitQuick()}
                  >
                    {t("apps.create.submit")}
                  </Button>
                  <Button onClick={resetQuick}>{t("apps.create.reset")}</Button>
                </Space>
              </>
            ) : null}
          </Space>
        ) : mode === "template" ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Segmented
              value={filter}
              onChange={(value) => setFilter(value as TemplateFilter)}
              options={[
                { value: "all", label: t("apps.create.templatesAll") },
                { value: "system", label: t("apps.create.templatesSystem") },
                { value: "agent", label: t("apps.create.templatesAgent") },
              ]}
            />
            {templatesLoading ? (
              <div style={{ textAlign: "center", padding: 24 }}>
                <Spin />
              </div>
            ) : templates.length === 0 ? (
              <Empty description={t("apps.create.noTemplates")} />
            ) : (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))",
                  gap: 10,
                  maxHeight: 320,
                  overflowY: "auto",
                }}
              >
                {templates.map((item) => {
                  const key = `${item.source}:${item.template_id}`;
                  return (
                    <Card
                      key={key}
                      size="small"
                      hoverable
                      onClick={() => {
                        setTemplateId(key);
                        setTemplateName(item.name || item.template_id);
                      }}
                      style={{
                        borderColor:
                          templateId === key
                            ? "var(--fn-color-brand)"
                            : undefined,
                        boxShadow:
                          templateId === key
                            ? "0 0 0 2px var(--fn-color-brand-bg)"
                            : undefined,
                        overflow: "hidden",
                      }}
                    >
                      <TemplatePreview template={item} agentId={agentId} />
                      <Space size={6} wrap>
                        <Text strong>{item.name || item.template_id}</Text>
                        <Tag
                          color={item.source === "system" ? "blue" : "default"}
                        >
                          {item.source === "system"
                            ? t("apps.create.templatesSystem")
                            : t("apps.create.templatesAgent")}
                        </Tag>
                      </Space>
                      <Paragraph
                        type="secondary"
                        ellipsis={{ rows: 2 }}
                        style={{ margin: "6px 0 0", minHeight: 40 }}
                      >
                        {item.description || "—"}
                      </Paragraph>
                    </Card>
                  );
                })}
              </div>
            )}
            <Space>
              <Text type="secondary">{t("apps.name")}</Text>
              <Input
                style={{ width: 260 }}
                value={templateName}
                maxLength={100}
                placeholder={t("apps.create.templateNamePlaceholder")}
                onChange={(event) => setTemplateName(event.target.value)}
              />
              <Button
                type="primary"
                loading={busy}
                disabled={!templateId}
                onClick={() => void submitTemplate()}
              >
                {t("apps.create.submit")}
              </Button>
            </Space>
          </Space>
        ) : (
          <div style={{ textAlign: "center", padding: "44px 16px 36px" }}>
            <Store
              size={44}
              strokeWidth={1.3}
              color="var(--fn-color-brand)"
              style={{ marginBottom: 14 }}
            />
            <Typography.Title level={4} style={{ margin: "0 0 8px" }}>
              {t("apps.create.marketTitle")}
            </Typography.Title>
            <Tag color="gold" style={{ marginBottom: 12 }}>
              {t("apps.create.marketComingSoon")}
            </Tag>
            <Paragraph type="secondary" style={{ margin: 0 }}>
              {t("apps.create.marketHint")}
            </Paragraph>
          </div>
        )}
      </Space>
    </Modal>
  );
}
