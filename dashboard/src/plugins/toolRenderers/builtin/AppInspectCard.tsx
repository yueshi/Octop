import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { FileSpreadsheet, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import type { ToolRenderProps } from "../types";

const { Text } = Typography;

interface InspectColumn {
  source: string;
  name_suggestion: string;
  type: string;
  pii: string | null;
  samples: string[];
}

interface InspectSheet {
  name: string;
  rows: number;
  header_row: number;
  header: string[];
  columns: InspectColumn[];
  warnings: string[];
}

interface InspectReport {
  file: string;
  size_bytes: number;
  sheets: InspectSheet[];
}

interface DraftColumn {
  key: string;
  source: string;
  label: string;
  name: string;
  type: string;
  required: boolean;
  include: boolean;
  pii: string | null;
}

const TYPE_OPTIONS = [
  "string",
  "text",
  "integer",
  "number",
  "boolean",
  "date",
  "datetime",
  "enum",
].map((value) => ({ value, label: value }));

function isInspectReport(data: unknown): data is InspectReport {
  return (
    !!data &&
    typeof data === "object" &&
    Array.isArray((data as { sheets?: unknown }).sheets)
  );
}

/** Structured confirmation card for `app_inspect_file` results. */
export function AppInspectCard({ data, status, isStreaming }: ToolRenderProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const report = isInspectReport(data) ? data : null;
  const [sheetIndex, setSheetIndex] = useState(0);
  const [appName, setAppName] = useState("");
  const [rows, setRows] = useState<DraftColumn[]>([]);

  const sheet = report?.sheets[sheetIndex] ?? null;

  useEffect(() => {
    if (!sheet) {
      setRows([]);
      return;
    }
    setRows(
      sheet.columns.map((column) => ({
        key: column.source,
        source: column.source,
        label: column.source,
        name: column.name_suggestion,
        type: column.type,
        required: false,
        include: !column.pii,
        pii: column.pii,
      })),
    );
  }, [sheet]);

  useEffect(() => {
    if (report && !appName) {
      const base = report.file.split("/").pop() || "App";
      setAppName(base.replace(/\.[^.]+$/, "") || "App");
    }
  }, [appName, report]);

  const update = (key: string, patch: Partial<DraftColumn>) => {
    setRows((prev) =>
      prev.map((row) => (row.key === key ? { ...row, ...patch } : row)),
    );
  };

  const selected = useMemo(() => rows.filter((row) => row.include), [rows]);
  const includesPii = selected.some((row) => !!row.pii);
  const canConfirm =
    !!report &&
    !!sheet &&
    selected.length > 0 &&
    !!appName.trim() &&
    status === "done";

  const confirm = () => {
    if (!report || !sheet || !canConfirm) return;
    const columns = selected.map((row) => ({
      name: row.name.trim(),
      label: row.label || row.source,
      type: row.type,
      required: row.required,
      ...(row.pii ? { pii: row.pii } : {}),
    }));
    const text = t("apps.inspectConfirmPrompt", {
      file: report.file,
      sheet: sheet.name,
      headerRow: sheet.header_row,
      name: appName.trim(),
      schema: JSON.stringify(columns),
      includePii: includesPii ? "true" : "false",
    });
    navigate(location.pathname, {
      replace: true,
      state: { prefillInput: text },
    });
  };

  if (!report) {
    return (
      <Text type="secondary">
        {t("apps.inspectCardInvalid", "No structured inspection data")}
      </Text>
    );
  }

  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <Space size={6}>
        <FileSpreadsheet size={16} />
        <Text strong>{report.file}</Text>
        <Text type="secondary">
          {t("apps.inspectRows", { count: sheet?.rows ?? 0 })}
        </Text>
      </Space>
      <Space wrap>
        <Text type="secondary">{t("apps.inspectSheet")}</Text>
        <Select
          size="small"
          value={sheetIndex}
          onChange={setSheetIndex}
          options={report.sheets.map((item, index) => ({
            value: index,
            label: item.name,
          }))}
          style={{ minWidth: 140 }}
        />
        <Text type="secondary">
          {t("apps.inspectHeaderRow", { row: (sheet?.header_row ?? 0) + 1 })}
        </Text>
        <Text type="secondary">{t("apps.name")}</Text>
        <Input
          size="small"
          value={appName}
          maxLength={100}
          onChange={(event) => setAppName(event.target.value)}
          style={{ width: 180 }}
        />
      </Space>
      {sheet?.warnings?.length ? (
        <Text type="warning">{sheet.warnings.join(" · ")}</Text>
      ) : null}
      <Table
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={rows}
        scroll={{ x: true }}
        columns={[
          {
            title: t("apps.inspectInclude"),
            width: 70,
            render: (_, row: DraftColumn) => (
              <Checkbox
                checked={row.include}
                onChange={(event) =>
                  update(row.key, { include: event.target.checked })
                }
              />
            ),
          },
          {
            title: t("apps.inspectSource"),
            dataIndex: "source",
            width: 140,
            ellipsis: true,
            render: (value: string, row: DraftColumn) => (
              <Space size={4}>
                {value}
                {row.pii ? <Tag color="red">{row.pii}</Tag> : null}
              </Space>
            ),
          },
          {
            title: t("apps.inspectName"),
            width: 160,
            render: (_, row: DraftColumn) => (
              <Input
                size="small"
                value={row.name}
                onChange={(event) =>
                  update(row.key, { name: event.target.value })
                }
              />
            ),
          },
          {
            title: t("apps.inspectType"),
            width: 120,
            render: (_, row: DraftColumn) => (
              <Select
                size="small"
                value={row.type}
                onChange={(value) => update(row.key, { type: value })}
                options={TYPE_OPTIONS}
                style={{ width: "100%" }}
              />
            ),
          },
          {
            title: t("apps.inspectRequired"),
            width: 70,
            render: (_, row: DraftColumn) => (
              <Checkbox
                checked={row.required}
                onChange={(event) =>
                  update(row.key, { required: event.target.checked })
                }
              />
            ),
          },
        ]}
      />
      {includesPii ? (
        <Text type="danger">
          <ShieldAlert size={14} /> {t("apps.inspectPiiWarning")}
        </Text>
      ) : null}
      <Space>
        <Button type="primary" disabled={!canConfirm} onClick={confirm}>
          {t("apps.inspectConfirm")}
        </Button>
        <Text type="secondary">
          {isStreaming
            ? t("apps.inspectWait", "Waiting for the inspection to finish…")
            : t("apps.inspectConfirmHint")}
        </Text>
      </Space>
    </Space>
  );
}
