import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import type { UploadFile } from "antd";
import {
  AlignCenterHorizontal,
  ArrowLeft,
  ChevronsLeft,
  ChevronsRight,
  Download,
  Edit3,
  FileCode,
  Info,
  Link2,
  MessageSquareText,
  Plus,
  RefreshCw,
  Share2,
  Sparkles,
  Table2,
  Trash2,
  Upload as UploadIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useNavigate, useParams } from "react-router-dom";
import { message } from "../../utils/antdMessage";

import {
  appsApi,
  askAppAssistant,
  type AppAssistantMessage,
  type AppColumn,
  type AppDetail,
  type AppFile,
  type AppImportMode,
  type AppPublication,
  type AppRowMeta,
  type AppSource,
  type AppSourceKind,
  type AppSourceTool,
} from "../../api/modules/apps";
import {
  connectorsApi,
  type ConnectorInstance,
} from "../../api/modules/connectors";
import PageShell, { pageShellStyles } from "../../layouts/PageShell";
import Markdown from "../../components/Markdown";
import { apiErrorMessage, isNotFoundApiError } from "../../utils/apiError";
import { formatBytes } from "../../utils/formatBytes";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { APP_CSP } from "./appCsp";

const { Text } = Typography;

/** Assistant drawer width presets (px) shared by the drag handle and the
 *  min/mid/max quick buttons; min and max follow the viewport. */
const assistantMinWidth = () => Math.floor(window.innerWidth * 0.35);
const assistantMaxWidth = () =>
  Math.max(assistantMinWidth(), Math.floor(window.innerWidth * 0.9));

/**
 * Bridge exposed to app HTML as `window.octopApp`. Every call goes through the
 * parent dashboard over postMessage so the iframe never sees a JWT and never
 * needs `allow-same-origin`.
 */
const APP_SDK = `(function () {
  var pending = new Map();
  var seq = 0;
  function call(method, params) {
    return new Promise(function (resolve, reject) {
      var id = ++seq;
      pending.set(id, { resolve: resolve, reject: reject });
      parent.postMessage(
        { source: "octop-app", type: "request", id: id, method: method, params: params || {} },
        "*"
      );
    });
  }
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.source !== "octop-app-host") return;
    var entry = pending.get(data.id);
    if (!entry) return;
    pending.delete(data.id);
    if (data.ok) entry.resolve(data.data);
    else entry.reject(new Error(data.error || "Request failed"));
  });
  window.octopApp = {
    schema: function (table) { return call("schema", { table: table || null }); },
    tables: function () { return call("tables"); },
    listRows: function (options) { return call("rows", options || {}); },
    appendRow: function (row, options) {
      return call("append", { row: row, table: (options || {}).table || null });
    },
    openExternal: function (url) { return call("openExternal", { url: url || "" }); },
    asset: function (name) { return call("asset", { name: name || "" }); }
  };
})();`;

function buildSrcDoc(html: string): string {
  const head = `<meta http-equiv="Content-Security-Policy" content="${APP_CSP}"><script>${APP_SDK}</script>`;
  const headMatch = /<head[^>]*>/i.exec(html);
  if (headMatch?.index !== undefined) {
    const at = headMatch.index + headMatch[0].length;
    return `${html.slice(0, at)}${head}${html.slice(at)}`;
  }
  const htmlMatch = /<html[^>]*>/i.exec(html);
  if (htmlMatch?.index !== undefined) {
    const at = htmlMatch.index + htmlMatch[0].length;
    return `${html.slice(0, at)}<head>${head}</head>${html.slice(at)}`;
  }
  const doctypeMatch = /<!doctype[^>]*>/i.exec(html);
  if (doctypeMatch?.index !== undefined) {
    const at = doctypeMatch.index + doctypeMatch[0].length;
    return `${html.slice(0, at)}<html><head>${head}</head><body>${html.slice(
      at,
    )}</body></html>`;
  }
  return `<!doctype html><html><head>${head}</head><body>${html}</body></html>`;
}

function permissionLabel(
  permission: AppDetail["permission"],
  t: (key: string) => string,
): string {
  if (permission === "edit_structure") return t("apps.editStructure");
  if (permission === "edit_data") return t("apps.editData");
  return t("apps.viewOnly");
}

const SOURCE_SCHEDULE_OPTIONS: { value: number; labelKey: string }[] = [
  { value: 0, labelKey: "apps.sourceScheduleManual" },
  { value: 300, labelKey: "apps.sourceSchedule5min" },
  { value: 1800, labelKey: "apps.sourceSchedule30min" },
  { value: 3600, labelKey: "apps.sourceSchedule1h" },
  { value: 21600, labelKey: "apps.sourceSchedule6h" },
  { value: 86400, labelKey: "apps.sourceSchedule1d" },
];

function sourceScheduleLabel(seconds: number, t: TFunction): string {
  if (seconds % 86400 === 0)
    return t("apps.scheduleDays", { count: seconds / 86400 });
  if (seconds % 3600 === 0)
    return t("apps.scheduleHours", { count: seconds / 3600 });
  return t("apps.scheduleMinutes", {
    count: Math.max(1, Math.round(seconds / 60)),
  });
}

interface ShareDraft {
  username: string;
  permission: "view" | "edit_data";
}

export default function AppDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { appId = "" } = useParams<{ appId: string }>();
  const timeZone = useServerTimezone();
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const [detail, setDetail] = useState<AppDetail | null>(null);
  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [htmlLoading, setHtmlLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameForm] = Form.useForm<{ name: string; description: string }>();
  const [shareOpen, setShareOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [publication, setPublication] = useState<AppPublication | null>(null);
  const [publicationBusy, setPublicationBusy] = useState(false);
  const [shareLoading, setShareLoading] = useState(false);
  const [savingShare, setSavingShare] = useState(false);
  const [shares, setShares] = useState<ShareDraft[]>([]);
  const [infoOpen, setInfoOpen] = useState(false);
  const [files, setFiles] = useState<AppFile[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);

  const [assistantOpen, setAssistantOpen] = useState(false);
  const [assistantMsgs, setAssistantMsgs] = useState<AppAssistantMessage[]>([]);
  const [assistantLoading, setAssistantLoading] = useState(false);
  const [assistantBusy, setAssistantBusy] = useState(false);
  const [assistantTool, setAssistantTool] = useState("");
  const [assistantError, setAssistantError] = useState("");
  const [assistantInput, setAssistantInput] = useState("");
  const [assistantWidth, setAssistantWidth] = useState(assistantMinWidth);
  const [assistantResizing, setAssistantResizing] = useState(false);
  const [assistantResizeHover, setAssistantResizeHover] = useState(false);
  const assistantListRef = useRef<HTMLDivElement>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importTable, setImportTable] = useState("main");
  const [importMode, setImportMode] = useState<AppImportMode>("append");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importIncludePii, setImportIncludePii] = useState(false);

  const [activeView, setActiveView] = useState<"ui" | "data">("ui");
  const [dataTable, setDataTable] = useState("main");
  const [dataSearch, setDataSearch] = useState("");
  const [dataRows, setDataRows] = useState<Record<string, string>[]>([]);
  const [dataRowMeta, setDataRowMeta] = useState<AppRowMeta[]>([]);
  const [dataTotal, setDataTotal] = useState(0);
  const [dataPage, setDataPage] = useState(1);
  const [dataLoading, setDataLoading] = useState(false);
  const [rowModalOpen, setRowModalOpen] = useState(false);
  const [rowEdit, setRowEdit] = useState<{
    row_id: string;
    version: number;
  } | null>(null);
  const [rowSaving, setRowSaving] = useState(false);
  const [rowForm] = Form.useForm<Record<string, unknown>>();
  const dataPageSize = 50;

  const [sources, setSources] = useState<AppSource[]>([]);
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceTable, setSourceTable] = useState("main");
  const [sourceKind, setSourceKind] = useState<AppSourceKind>("workspace_file");
  const [sourceMode, setSourceMode] = useState<AppImportMode>("append");
  const [sourcePath, setSourcePath] = useState("");
  const [sourceInstance, setSourceInstance] = useState("");
  const [sourceTool, setSourceTool] = useState("");
  const [sourceArgs, setSourceArgs] = useState("");
  const [sourceResultPath, setSourceResultPath] = useState("");
  const [sourceCursorArg, setSourceCursorArg] = useState("");
  const [sourceCursorPath, setSourceCursorPath] = useState("");
  const [sourceIncludePii, setSourceIncludePii] = useState(false);
  const [sourceMapping, setSourceMapping] = useState("");
  const [sourceSchedule, setSourceSchedule] = useState(0);
  const [sourceTools, setSourceTools] = useState<AppSourceTool[]>([]);
  const [sourceToolsLoading, setSourceToolsLoading] = useState(false);
  const [sourceInstances, setSourceInstances] = useState<ConnectorInstance[]>(
    [],
  );
  const [sourceEditId, setSourceEditId] = useState<string | null>(null);
  const [sourceSaving, setSourceSaving] = useState(false);

  const loadDetail = useCallback(async () => {
    setLoading(true);
    try {
      const data = await appsApi.get(appId);
      setDetail(data);
      setNotFound(false);
    } catch (err) {
      if (isNotFoundApiError(err)) {
        setNotFound(true);
      } else {
        message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      }
    } finally {
      setLoading(false);
    }
  }, [appId, t]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  useEffect(() => {
    if (!detail?.has_html) {
      setHtml("");
      return;
    }
    let alive = true;
    setHtmlLoading(true);
    appsApi
      .getHtml(appId)
      .then((text) => {
        if (alive) setHtml(text);
      })
      .catch((err) => {
        if (alive)
          message.error(apiErrorMessage(err, t("apps.loadHtmlFailed"), t));
      })
      .finally(() => {
        if (alive) setHtmlLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [appId, detail?.has_html, t]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = iframeRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const data = event.data as {
        source?: string;
        type?: string;
        id?: number;
        method?: string;
        params?: {
          limit?: number;
          offset?: number;
          table?: string | null;
          row?: Record<string, unknown>;
          url?: string;
          name?: string;
        };
      } | null;
      if (
        !data ||
        data.source !== "octop-app" ||
        data.type !== "request" ||
        typeof data.id !== "number"
      ) {
        return;
      }
      const reply = (payload: Record<string, unknown>) => {
        frame.contentWindow?.postMessage(
          { source: "octop-app-host", id: data.id, ...payload },
          "*",
        );
      };
      void (async () => {
        try {
          if (data.method === "schema") {
            const current = await appsApi.get(appId);
            const tableKey = data.params?.table || "main";
            if (tableKey === "main") {
              reply({
                ok: true,
                data: { table: tableKey, columns: current.columns },
              });
            } else {
              const table = current.tables.find(
                (item) => item.table_key === tableKey,
              );
              if (!table) throw new Error(`unknown table: ${tableKey}`);
              reply({
                ok: true,
                data: { table: tableKey, columns: table.columns },
              });
            }
          } else if (data.method === "tables") {
            const current = await appsApi.get(appId);
            reply({
              ok: true,
              data: {
                tables: current.tables.map((item) => ({
                  table_key: item.table_key,
                  label: item.label,
                  row_count: item.row_count,
                  is_main: item.is_main,
                })),
              },
            });
          } else if (data.method === "rows") {
            const rows = await appsApi.rows(
              appId,
              data.params?.limit,
              data.params?.offset,
              data.params?.table || "main",
            );
            reply({ ok: true, data: rows });
          } else if (data.method === "append") {
            const tableKey = data.params?.table || "main";
            const result = await appsApi.appendRow(
              appId,
              data.params?.row ?? {},
              tableKey,
            );
            setDetail((prev) => {
              if (!prev) return prev;
              if (tableKey === "main") {
                return { ...prev, row_count: prev.row_count + 1 };
              }
              return {
                ...prev,
                tables: prev.tables.map((item) =>
                  item.table_key === tableKey
                    ? { ...item, row_count: item.row_count + 1 }
                    : item,
                ),
              };
            });
            reply({ ok: true, data: result });
          } else if (data.method === "openExternal") {
            const url = String(data.params?.url ?? "").trim();
            if (!/^https?:\/\//i.test(url)) {
              reply({
                ok: false,
                error: "openExternal requires an http(s) URL",
              });
            } else {
              // The app page runs sandboxed without popups, so the parent has
              // to open the link — after the user confirms the target domain.
              Modal.confirm({
                title: t("apps.openExternalTitle"),
                content: url,
                okText: t("apps.openExternalConfirm"),
                cancelText: t("common.cancel"),
                onOk: () => {
                  window.open(url, "_blank", "noopener,noreferrer");
                },
              });
              reply({ ok: true, data: { opened: true } });
            }
          } else if (data.method === "asset") {
            const name = String(data.params?.name ?? "").trim();
            try {
              // The parent fetches the asset with the JWT and hands the iframe
              // a data: URL — the sandbox has no same-origin access, so a blob
              // URL from this document would not be readable there.
              const blob = await appsApi.asset(appId, name);
              const mime = blob.type || "image/png";
              const reader = new FileReader();
              const dataUrl = await new Promise<string>((resolve, reject) => {
                reader.onload = () => resolve(String(reader.result));
                reader.onerror = () => reject(reader.error);
                reader.readAsDataURL(blob);
              });
              reply({ ok: true, data: { name, mime, data_url: dataUrl } });
            } catch (err) {
              reply({
                ok: false,
                error: err instanceof Error ? err.message : String(err),
              });
            }
          } else {
            reply({ ok: false, error: "unsupported method" });
          }
        } catch (err) {
          reply({
            ok: false,
            error: err instanceof Error ? err.message : String(err),
          });
        }
      })();
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [appId, t]);

  const srcDoc = useMemo(() => (html ? buildSrcDoc(html) : ""), [html]);

  const dataColumns: AppColumn[] =
    detail?.tables.find((item) => item.table_key === dataTable)?.columns ??
    detail?.columns ??
    [];
  const canEditRows = detail?.permission !== "view";
  const dataQuery = dataSearch.trim().toLowerCase();
  const visibleRows = dataRows
    .map((row, index) => ({ row, meta: dataRowMeta[index] }))
    .filter(
      ({ row }) =>
        !dataQuery ||
        Object.values(row).some((value) =>
          value.toLowerCase().includes(dataQuery),
        ),
    );

  const loadData = useCallback(
    async (page: number) => {
      if (!appId) return;
      setDataLoading(true);
      try {
        const result = await appsApi.rows(
          appId,
          dataPageSize,
          (page - 1) * dataPageSize,
          dataTable,
        );
        setDataRows(result.rows);
        setDataRowMeta(result.row_meta);
        setDataTotal(result.total);
        setDataPage(page);
      } catch (err) {
        message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      } finally {
        setDataLoading(false);
      }
    },
    [appId, dataTable, t],
  );

  useEffect(() => {
    if (activeView === "data") void loadData(1);
  }, [activeView, dataTable, loadData]);

  const openAddRow = () => {
    setRowEdit(null);
    rowForm.resetFields();
    setRowModalOpen(true);
  };

  const openEditRow = (
    meta: AppRowMeta | undefined,
    values: Record<string, string>,
  ) => {
    if (!meta?.row_id) return;
    setRowEdit(meta);
    rowForm.setFieldsValue(values);
    setRowModalOpen(true);
  };

  const submitRow = async () => {
    if (!appId) return;
    const values = await rowForm.validateFields();
    setRowSaving(true);
    try {
      if (rowEdit) {
        await appsApi.updateRow(appId, rowEdit.row_id, values, rowEdit.version);
      } else {
        await appsApi.appendRow(appId, values, dataTable);
      }
      setRowModalOpen(false);
      message.success(t("apps.rowSaved"));
      await loadData(dataPage);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.rowConflict"), t));
    } finally {
      setRowSaving(false);
    }
  };

  const removeRow = async (meta: AppRowMeta | undefined) => {
    if (!appId || !meta?.row_id) return;
    try {
      await appsApi.deleteRow(appId, meta.row_id, meta.version);
      message.success(t("apps.rowDeleted"));
      const nextPage =
        dataRows.length === 1 && dataPage > 1 ? dataPage - 1 : dataPage;
      await loadData(nextPage);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.rowConflict"), t));
    }
  };

  const exportData = async () => {
    if (!appId) return;
    try {
      const blob = await appsApi.exportTable(appId, dataTable);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${dataTable}.csv`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    }
  };

  const backupApp = async () => {
    if (!appId) return;
    try {
      const blob = await appsApi.backup(appId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${detail?.name || appId}-backup.zip`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    }
  };

  const openRename = () => {
    if (!detail) return;
    renameForm.setFieldsValue({
      name: detail.name,
      description: detail.description,
    });
    setRenameOpen(true);
  };

  const submitRename = async (values: {
    name: string;
    description: string;
  }) => {
    try {
      const updated = await appsApi.update(appId, {
        name: values.name,
        description: values.description ?? "",
      });
      setDetail(updated);
      setRenameOpen(false);
      message.success(t("apps.saveSuccess"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.saveFailed"), t));
    }
  };

  const openShare = async () => {
    setShareOpen(true);
    setShareLoading(true);
    try {
      const data = await appsApi.shares(appId);
      setShares(
        data.shares.map((item) => ({
          username: item.username,
          permission: item.permission,
        })),
      );
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      setShareOpen(false);
    } finally {
      setShareLoading(false);
    }
  };

  const openPublish = async () => {
    setPublishOpen(true);
    setPublicationBusy(true);
    try {
      setPublication(await appsApi.publication(appId));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      setPublishOpen(false);
    } finally {
      setPublicationBusy(false);
    }
  };

  const createPublish = async () => {
    setPublicationBusy(true);
    try {
      setPublication(await appsApi.createPublication(appId));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setPublicationBusy(false);
    }
  };

  const revokePublish = async () => {
    setPublicationBusy(true);
    try {
      setPublication(await appsApi.revokePublication(appId));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setPublicationBusy(false);
    }
  };

  const openInfo = async () => {
    setInfoOpen(true);
    setFilesLoading(true);
    try {
      const [filesData] = await Promise.all([
        appsApi.files(appId),
        appsApi
          .listSources(appId)
          .then((data) => setSources(data.sources))
          .catch(() => setSources([])),
      ]);
      setFiles(filesData.files);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      setFiles([]);
    } finally {
      setFilesLoading(false);
    }
  };

  const openAssistant = async () => {
    setAssistantOpen(true);
    setAssistantLoading(true);
    try {
      const data = await appsApi.assistantHistory(appId);
      setAssistantMsgs(data.messages);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.assistantLoadFailed"), t));
    } finally {
      setAssistantLoading(false);
    }
  };

  const sendAssistant = async () => {
    const question = assistantInput.trim();
    if (!question || assistantBusy) return;
    setAssistantInput("");
    setAssistantError("");
    setAssistantBusy(true);
    setAssistantMsgs((prev) => [
      ...prev,
      { role: "user", content: question, created_at: 0 },
      { role: "assistant", content: "", created_at: 0 },
    ]);
    try {
      await askAppAssistant(appId, question, (event) => {
        if (event.type === "token") {
          setAssistantMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              next[next.length - 1] = {
                ...last,
                content: last.content + event.text,
              };
            }
            return next;
          });
        } else if (event.type === "tool") {
          setAssistantTool(event.name);
        } else if (event.type === "done") {
          const citations = event.citations ?? [];
          setAssistantMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              next[next.length - 1] = { ...last, citations };
            }
            return next;
          });
        } else if (event.type === "error") {
          setAssistantError(event.message);
        }
      });
    } catch (err) {
      setAssistantError(
        apiErrorMessage(err, t("apps.assistantRequestFailed"), t),
      );
    } finally {
      setAssistantTool("");
      setAssistantBusy(false);
    }
  };

  // Drag the handle on the drawer's left edge to resize. Pointer capture
  // keeps the drag alive outside the handle; the drawer wrapper's own
  // width transition is disabled while dragging so it follows the cursor.
  const startAssistantResize = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    setAssistantResizing(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const moveAssistantResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!assistantResizing) return;
    setAssistantWidth(
      Math.min(
        Math.max(window.innerWidth - e.clientX, assistantMinWidth()),
        assistantMaxWidth(),
      ),
    );
  };
  const endAssistantResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!assistantResizing) return;
    setAssistantResizing(false);
    e.currentTarget.releasePointerCapture(e.pointerId);
  };
  const setAssistantPreset = (width: number) =>
    setAssistantWidth(
      Math.min(Math.max(width, assistantMinWidth()), assistantMaxWidth()),
    );

  useEffect(() => {
    const el = assistantListRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [assistantMsgs, assistantTool, assistantOpen]);

  const openImport = (tableKey: string) => {
    setImportTable(tableKey);
    setImportMode("append");
    setImportFile(null);
    setImportOpen(true);
  };

  const submitImport = async () => {
    if (!importFile || importBusy) return;
    setImportBusy(true);
    try {
      const stored = await appsApi.uploadImportFile(appId, importFile);
      const result = await appsApi.importTable(appId, importTable, {
        kind: "upload",
        path: stored.path,
        mode: importMode,
        include_pii: importIncludePii,
      });
      message.success(
        t("apps.importSuccess", {
          imported: result.imported,
          skipped: result.skipped,
        }),
      );
      if (result.blocked_pii.length > 0) {
        message.warning(
          t("apps.piiBlocked", { columns: result.blocked_pii.join(", ") }),
        );
      }
      if (result.failed_rows.length > 0) {
        const blob = new Blob(
          [
            JSON.stringify(
              result.failed_rows.map((item) => item.values),
              null,
              2,
            ),
          ],
          { type: "application/json" },
        );
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "failed-rows.json";
        link.click();
        URL.revokeObjectURL(url);
        message.warning(
          t("apps.importFailedRows", { count: result.failed_rows.length }),
        );
      }
      setImportOpen(false);
      setImportFile(null);
      setImportIncludePii(false);
      void loadDetail();
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.importFailed"), t));
    } finally {
      setImportBusy(false);
    }
  };

  const loadSourceTools = useCallback(
    async (instanceId: string) => {
      if (!instanceId) {
        setSourceTools([]);
        return;
      }
      setSourceToolsLoading(true);
      try {
        const data = await appsApi.sourceTools(appId, instanceId);
        setSourceTools(data.tools);
      } catch {
        setSourceTools([]);
      } finally {
        setSourceToolsLoading(false);
      }
    },
    [appId],
  );

  const openSourceModal = (tableKey: string) => {
    const existing =
      sources.find((item) => item.table_key === tableKey) ?? null;
    setSourceTable(tableKey);
    setSourceEditId(existing?.source_id ?? null);
    setSourceKind(existing?.kind ?? "workspace_file");
    setSourceMode(existing?.mode ?? "append");
    setSourcePath(
      existing?.kind === "workspace_file"
        ? String(existing.params.path || "")
        : "",
    );
    setSourceInstance(
      existing?.kind === "external"
        ? String(existing.params.instance_id || "")
        : "",
    );
    setSourceTool(
      existing?.kind === "external" ? String(existing.params.tool || "") : "",
    );
    setSourceArgs(
      existing?.kind === "external" && existing.params.args
        ? JSON.stringify(existing.params.args, null, 2)
        : "",
    );
    setSourceResultPath(
      existing?.kind === "external"
        ? String(existing.params.result_path || "")
        : "",
    );
    setSourceCursorArg(
      existing?.kind === "external"
        ? String(existing.params.cursor_arg || "")
        : "",
    );
    setSourceCursorPath(
      existing?.kind === "external"
        ? String(existing.params.cursor_path || "")
        : "",
    );
    setSourceIncludePii(existing?.params.include_pii === true);
    setSourceMapping(
      existing && Object.keys(existing.mapping).length
        ? JSON.stringify(existing.mapping, null, 2)
        : "",
    );
    setSourceSchedule(existing?.schedule_seconds ?? 0);
    setSourceTools([]);
    setSourceInstances([]);
    setSourceOpen(true);
    void connectorsApi
      .listInstances()
      .then((data) => setSourceInstances(data))
      .catch(() => setSourceInstances([]));
    if (existing?.kind === "external") {
      void loadSourceTools(String(existing.params.instance_id || ""));
    }
  };

  const submitSource = async () => {
    if (sourceSaving) return;
    let args: Record<string, unknown> = {};
    if (sourceKind === "external" && sourceArgs.trim()) {
      try {
        args = JSON.parse(sourceArgs) as Record<string, unknown>;
      } catch {
        message.error(t("apps.sourceArgsInvalid"));
        return;
      }
    }
    let mapping: Record<string, string> = {};
    if (sourceMapping.trim()) {
      try {
        mapping = JSON.parse(sourceMapping) as Record<string, string>;
      } catch {
        message.error(t("apps.sourceArgsInvalid"));
        return;
      }
    }
    const params: Record<string, unknown> =
      sourceKind === "workspace_file"
        ? { path: sourcePath.trim() }
        : {
            instance_id: sourceInstance,
            tool: sourceTool,
            ...(Object.keys(args).length ? { args } : {}),
            ...(sourceResultPath.trim()
              ? { result_path: sourceResultPath.trim() }
              : {}),
            ...(sourceCursorArg.trim()
              ? { cursor_arg: sourceCursorArg.trim() }
              : {}),
            ...(sourceCursorPath.trim()
              ? { cursor_path: sourceCursorPath.trim() }
              : {}),
            ...(sourceIncludePii ? { include_pii: true } : {}),
          };
    const sourceId =
      sourceEditId ?? `src_${Math.random().toString(36).slice(2, 10)}`;
    setSourceSaving(true);
    try {
      await appsApi.saveSource(appId, sourceId, {
        table_key: sourceTable,
        kind: sourceKind,
        mode: sourceMode,
        schedule_seconds: sourceSchedule,
        params,
        mapping,
      });
      message.success(t("apps.sourceSaveSuccess"));
      setSourceOpen(false);
      const data = await appsApi.listSources(appId);
      setSources(data.sources);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.sourceSaveFailed"), t));
    } finally {
      setSourceSaving(false);
    }
  };

  const runSourceNow = async (source: AppSource) => {
    try {
      const result = await appsApi.runSource(appId, source.source_id);
      message.success(
        t("apps.sourceRunSuccess", {
          imported: result.imported,
          skipped: result.skipped,
        }),
      );
      const data = await appsApi.listSources(appId);
      setSources(data.sources);
      void loadDetail();
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.sourceRunFailed"), t));
    }
  };

  const removeSource = async (source: AppSource) => {
    try {
      await appsApi.deleteSource(appId, source.source_id);
      message.success(t("apps.sourceDeleteSuccess"));
      setSources(sources.filter((item) => item.source_id !== source.source_id));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.sourceDeleteFailed"), t));
    }
  };

  const submitShare = async () => {
    const payload = shares
      .filter((item) => item.username.trim())
      .map((item) => ({
        username: item.username.trim(),
        permission: item.permission,
      }));
    setSavingShare(true);
    try {
      await appsApi.setShares(appId, payload);
      message.success(t("apps.saveSuccess"));
      setShareOpen(false);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.saveFailed"), t));
    } finally {
      setSavingShare(false);
    }
  };

  const removeApp = async () => {
    try {
      await appsApi.remove(appId);
      message.success(t("apps.deleteSuccess"));
      navigate("/apps", { replace: true });
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.deleteFailed"), t));
    }
  };

  if (loading) {
    return (
      <PageShell title={t("apps.title")}>
        <div style={{ padding: 48, textAlign: "center" }}>
          <Spin />
        </div>
      </PageShell>
    );
  }

  if (notFound || !detail) {
    return (
      <PageShell title={t("apps.title")}>
        <Empty description={t("apps.notFound")} style={{ padding: "64px 0" }}>
          <Button onClick={() => navigate("/apps")}>{t("apps.back")}</Button>
        </Empty>
      </PageShell>
    );
  }

  const canEditStructure = detail.permission === "edit_structure";
  const canEditData = detail.permission !== "view";

  return (
    <PageShell
      title={detail.name}
      subtitle={detail.description || undefined}
      fill
      actions={
        <Space size={8}>
          <Tag style={{ marginInlineEnd: 0 }}>
            {permissionLabel(detail.permission, t)}
          </Tag>
          <Button
            type="text"
            icon={<Sparkles size={16} />}
            onClick={() => void openAssistant()}
          >
            {t("apps.assistant")}
          </Button>
          <Button
            type="text"
            icon={<Info size={16} />}
            onClick={() => void openInfo()}
          >
            {t("apps.info")}
          </Button>
          <Button
            type="text"
            icon={<ArrowLeft size={16} />}
            onClick={() => navigate("/apps")}
          >
            {t("apps.back")}
          </Button>
          {canEditStructure && (
            <>
              <Button
                icon={<Share2 size={16} />}
                onClick={() => void openShare()}
              >
                {t("apps.share")}
              </Button>
              <Button
                icon={<Link2 size={16} />}
                onClick={() => void openPublish()}
              >
                {t("apps.publishLink")}
              </Button>
              <Button icon={<Edit3 size={16} />} onClick={openRename}>
                {t("apps.rename")}
              </Button>
              <Popconfirm
                title={t("apps.deleteTitle")}
                description={t("apps.deleteConfirm")}
                okButtonProps={{ danger: true }}
                okText={t("apps.delete")}
                cancelText={t("common.cancel")}
                onConfirm={() => void removeApp()}
              >
                <Button danger icon={<Trash2 size={16} />} />
              </Popconfirm>
            </>
          )}
        </Space>
      }
    >
      <div className={pageShellStyles.fillTabs}>
        <Tabs
          activeKey={activeView}
          onChange={(key) => setActiveView(key as "ui" | "data")}
          items={[
            {
              key: "ui",
              label: t("apps.uiTab"),
              children: (
                <div
                  style={{
                    height: "100%",
                    minHeight: 0,
                    display: "flex",
                    flexDirection: "column",
                  }}
                >
                  {htmlLoading ? (
                    <div style={{ padding: 48, textAlign: "center" }}>
                      <Spin />
                    </div>
                  ) : detail.has_html && html ? (
                    <iframe
                      ref={iframeRef}
                      title={t("apps.iframeTitle")}
                      sandbox="allow-scripts"
                      srcDoc={srcDoc}
                      style={{
                        flex: 1,
                        width: "100%",
                        minHeight: 0,
                        border: 0,
                        borderRadius: 8,
                        background: "#fff",
                      }}
                    />
                  ) : (
                    <Empty
                      image={
                        <MessageSquareText
                          size={48}
                          strokeWidth={1.2}
                          opacity={0.45}
                        />
                      }
                      description={t("apps.noHtml")}
                      style={{ padding: "64px 0" }}
                    >
                      <Button
                        type="primary"
                        onClick={() =>
                          navigate(
                            `/chat/${encodeURIComponent(detail.agent_id)}`,
                          )
                        }
                      >
                        {t("apps.openChat")}
                      </Button>
                    </Empty>
                  )}
                </div>
              ),
            },
            {
              key: "data",
              label: t("apps.dataTab"),
              children: (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 12,
                    minHeight: 0,
                  }}
                >
                  <Space wrap>
                    {detail.tables.length > 1 && (
                      <Space size={4}>
                        <Text type="secondary">{t("apps.tableLabel")}</Text>
                        <Select
                          size="small"
                          value={dataTable}
                          onChange={(value) => {
                            setDataTable(value);
                            setDataPage(1);
                          }}
                          options={detail.tables.map((item) => ({
                            value: item.table_key,
                            label: item.label || item.table_key,
                          }))}
                        />
                      </Space>
                    )}
                    {canEditRows && (
                      <Button
                        type="primary"
                        size="small"
                        icon={<Plus size={14} />}
                        onClick={openAddRow}
                      >
                        {t("apps.addRow")}
                      </Button>
                    )}
                    <Button
                      size="small"
                      icon={<UploadIcon size={14} />}
                      onClick={() => void exportData()}
                    >
                      {t("apps.exportCsv")}
                    </Button>
                    <Button
                      size="small"
                      icon={<Download size={14} />}
                      onClick={() => void backupApp()}
                    >
                      {t("apps.backup")}
                    </Button>
                    <Input.Search
                      size="small"
                      allowClear
                      value={dataSearch}
                      placeholder={t("apps.searchRows")}
                      onChange={(event) => setDataSearch(event.target.value)}
                      style={{ width: 200 }}
                    />
                  </Space>
                  {dataColumns.some((column) => column.pii) && (
                    <Alert
                      type="warning"
                      showIcon
                      message={t("apps.piiNotice")}
                      style={{ marginBottom: 8 }}
                    />
                  )}
                  <Table
                    size="small"
                    rowKey={(_, index) => `${dataPage}-${index ?? 0}`}
                    loading={dataLoading}
                    dataSource={visibleRows.map((item) => item.row)}
                    locale={{ emptyText: t("apps.emptyRows") }}
                    scroll={{ x: true }}
                    pagination={{
                      current: dataPage,
                      pageSize: dataPageSize,
                      total: dataTotal,
                      showSizeChanger: false,
                      onChange: (page) => void loadData(page),
                    }}
                    columns={[
                      ...dataColumns.map((column) => ({
                        title: (
                          <Space size={4}>
                            {column.label || column.name}
                            {column.pii ? (
                              <Tag color="red">{t("apps.piiTag")}</Tag>
                            ) : null}
                          </Space>
                        ),
                        dataIndex: column.name,
                        key: column.name,
                        ellipsis: true,
                      })),
                      {
                        title: t("apps.source"),
                        key: "__source",
                        width: 90,
                        render: (_: unknown, __: unknown, index: number) =>
                          visibleRows[index]?.meta?.source || "manual",
                      },
                      ...(canEditRows
                        ? [
                            {
                              title: t("apps.actions"),
                              key: "__actions",
                              width: 150,
                              render: (
                                _: unknown,
                                __: unknown,
                                index: number,
                              ) => (
                                <Space size={4}>
                                  <Button
                                    size="small"
                                    type="link"
                                    disabled={!visibleRows[index]?.meta?.row_id}
                                    onClick={() =>
                                      openEditRow(
                                        visibleRows[index]?.meta,
                                        visibleRows[index]?.row ?? {},
                                      )
                                    }
                                  >
                                    {t("apps.editRow")}
                                  </Button>
                                  <Popconfirm
                                    title={t("apps.rowDeleteConfirm")}
                                    okText={t("apps.delete")}
                                    cancelText={t("common.cancel")}
                                    okButtonProps={{ danger: true }}
                                    onConfirm={() =>
                                      void removeRow(visibleRows[index]?.meta)
                                    }
                                  >
                                    <Button
                                      size="small"
                                      type="link"
                                      danger
                                      disabled={
                                        !visibleRows[index]?.meta?.row_id
                                      }
                                    >
                                      {t("apps.deleteRow")}
                                    </Button>
                                  </Popconfirm>
                                </Space>
                              ),
                            },
                          ]
                        : []),
                    ]}
                  />
                </div>
              ),
            },
          ]}
        />
      </div>

      <Modal
        title={rowEdit ? t("apps.rowEditTitle") : t("apps.rowAddTitle")}
        open={rowModalOpen}
        onCancel={() => setRowModalOpen(false)}
        onOk={() => void submitRow()}
        okText={t("apps.save")}
        cancelText={t("common.cancel")}
        confirmLoading={rowSaving}
        destroyOnHidden
      >
        <Form form={rowForm} layout="vertical">
          {dataColumns.map((column) => (
            <Form.Item
              key={column.name}
              name={column.name}
              label={column.label || column.name}
              rules={
                column.required
                  ? [{ required: true, message: t("apps.requiredField") }]
                  : []
              }
            >
              {column.type === "enum" ? (
                <Select
                  allowClear={!column.required}
                  options={column.values.map((value) => ({
                    value,
                    label: value,
                  }))}
                />
              ) : (
                <Input
                  placeholder={
                    column.type === "date"
                      ? "YYYY-MM-DD"
                      : column.type === "boolean"
                      ? "true / false"
                      : undefined
                  }
                />
              )}
            </Form.Item>
          ))}
        </Form>
      </Modal>

      <Modal
        title={t("apps.publishTitle")}
        open={publishOpen}
        onCancel={() => setPublishOpen(false)}
        footer={null}
        destroyOnHidden
      >
        {publicationBusy ? (
          <div style={{ padding: 24, textAlign: "center" }}>
            <Spin />
          </div>
        ) : (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Text type="secondary">{t("apps.publishHint")}</Text>
            {detail.columns.some((column) => column.pii) && (
              <Alert
                type="warning"
                showIcon
                message={t("apps.piiShareWarning")}
              />
            )}
            <Text>
              {publication?.active
                ? t("apps.publishActive")
                : t("apps.publishInactive")}
            </Text>
            {publication?.token && (
              <Input
                readOnly
                value={`${window.location.origin}/p/${publication.token}`}
                addonAfter={
                  <Button
                    type="link"
                    size="small"
                    onClick={() => {
                      void navigator.clipboard?.writeText(
                        `${window.location.origin}/p/${publication.token}`,
                      );
                      message.success(t("apps.publishCopy"));
                    }}
                  >
                    {t("apps.publishCopy")}
                  </Button>
                }
              />
            )}
            <Space>
              <Button
                type="primary"
                onClick={() => void createPublish()}
                loading={publicationBusy}
              >
                {publication?.active
                  ? t("apps.publishRotate")
                  : t("apps.publishCreate")}
              </Button>
              {publication?.active && (
                <Popconfirm
                  title={t("apps.publishRevokeConfirm")}
                  okText={t("apps.publishRevoke")}
                  cancelText={t("common.cancel")}
                  okButtonProps={{ danger: true }}
                  onConfirm={() => void revokePublish()}
                >
                  <Button danger>{t("apps.publishRevoke")}</Button>
                </Popconfirm>
              )}
            </Space>
          </Space>
        )}
      </Modal>

      <Modal
        title={t("apps.renameTitle")}
        open={renameOpen}
        onCancel={() => setRenameOpen(false)}
        onOk={() => renameForm.submit()}
        okText={t("apps.save")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form form={renameForm} layout="vertical" onFinish={submitRename}>
          <Form.Item
            name="name"
            label={t("apps.name")}
            rules={[{ required: true, message: t("apps.nameRequired") }]}
          >
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="description" label={t("apps.description")}>
            <Input.TextArea
              maxLength={2000}
              autoSize={{ minRows: 2, maxRows: 6 }}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("apps.importTitle")}
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        onOk={() => void submitImport()}
        okText={t("apps.importSubmit")}
        cancelText={t("common.cancel")}
        okButtonProps={{ disabled: !importFile, loading: importBusy }}
        destroyOnHidden
      >
        <Form layout="vertical">
          <Form.Item label={t("apps.importTableLabel")}>
            <Select
              value={importTable}
              onChange={(value) => setImportTable(value)}
              options={detail.tables.map((item) => ({
                value: item.table_key,
                label: item.label || item.table_key,
              }))}
            />
          </Form.Item>
          <Form.Item
            label={t("apps.importFileLabel")}
            extra={t("apps.importFileHint")}
          >
            <Upload
              maxCount={1}
              beforeUpload={(file) => {
                setImportFile(file);
                return false;
              }}
              onRemove={() => setImportFile(null)}
              fileList={
                importFile
                  ? [
                      {
                        uid: "-1",
                        name: importFile.name,
                        status: "done",
                      } as UploadFile,
                    ]
                  : []
              }
            >
              <Button icon={<UploadIcon size={14} />}>
                {t("apps.importChooseFile")}
              </Button>
            </Upload>
          </Form.Item>
          <Form.Item label={t("apps.importMode")}>
            <Select
              value={importMode}
              onChange={(value) => setImportMode(value)}
              options={[
                { value: "append", label: t("apps.importModeAppend") },
                {
                  value: "replace",
                  label: t("apps.importModeReplace"),
                  disabled: !canEditStructure,
                },
              ]}
            />
          </Form.Item>
          <Form.Item>
            <Checkbox
              checked={importIncludePii}
              onChange={(event) => setImportIncludePii(event.target.checked)}
            >
              {t("apps.importIncludePii")}
            </Checkbox>
            <Text type="secondary" style={{ display: "block", fontSize: 12 }}>
              {t("apps.importIncludePiiHint")}
            </Text>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("apps.sourceTitle")}
        open={sourceOpen}
        onCancel={() => setSourceOpen(false)}
        onOk={() => void submitSource()}
        okText={t("apps.sourceSave")}
        cancelText={t("common.cancel")}
        confirmLoading={sourceSaving}
        destroyOnHidden
        width={520}
      >
        <Form layout="vertical">
          <Form.Item label={t("apps.importTableLabel")}>
            <Select
              value={sourceTable}
              onChange={(value) => setSourceTable(value)}
              options={detail.tables.map((item) => ({
                value: item.table_key,
                label: item.label || item.table_key,
              }))}
            />
          </Form.Item>
          <Form.Item label={t("apps.sourceKind")}>
            <Select
              value={sourceKind}
              onChange={(value) => setSourceKind(value)}
              options={[
                {
                  value: "workspace_file",
                  label: t("apps.sourceKindWorkspaceFile"),
                },
                { value: "external", label: t("apps.sourceKindExternal") },
              ]}
            />
          </Form.Item>
          {sourceKind === "workspace_file" ? (
            <Form.Item
              label={t("apps.sourcePathLabel")}
              extra={t("apps.sourcePathHint")}
            >
              <Input
                value={sourcePath}
                onChange={(event) => setSourcePath(event.target.value)}
                placeholder="inbound/exports/data.csv"
                maxLength={500}
              />
            </Form.Item>
          ) : (
            <>
              <Form.Item label={t("apps.sourceInstanceLabel")}>
                <Select
                  value={sourceInstance || undefined}
                  onChange={(value) => {
                    setSourceInstance(value);
                    setSourceTool("");
                    void loadSourceTools(value);
                  }}
                  options={sourceInstances.map((item) => ({
                    value: item.instance_id,
                    label: item.display_name,
                  }))}
                  showSearch
                  optionFilterProp="label"
                  allowClear
                />
              </Form.Item>
              <Form.Item
                label={t("apps.sourceToolLabel")}
                extra={t("apps.sourceToolHint")}
              >
                <Select
                  value={sourceTool || undefined}
                  onChange={(value) => setSourceTool(value)}
                  options={sourceTools
                    .filter((tool) => tool.read_only)
                    .map((tool) => ({
                      value: tool.name,
                      label: tool.description || tool.name,
                    }))}
                  loading={sourceToolsLoading}
                  disabled={!sourceInstance}
                  notFoundContent={
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {t("apps.sourceToolsEmpty")}
                    </Text>
                  }
                />
              </Form.Item>
              <Form.Item label={t("apps.sourceArgsLabel")}>
                <Input.TextArea
                  value={sourceArgs}
                  onChange={(event) => setSourceArgs(event.target.value)}
                  placeholder='{"q": "lake"}'
                  autoSize={{ minRows: 1, maxRows: 4 }}
                />
              </Form.Item>
              <Form.Item
                label={t("apps.sourceResultPathLabel")}
                extra={t("apps.sourceResultPathHint")}
              >
                <Input
                  value={sourceResultPath}
                  onChange={(event) => setSourceResultPath(event.target.value)}
                  placeholder="data.items"
                  maxLength={200}
                />
              </Form.Item>
              <Form.Item
                label={t("apps.sourceCursorArgLabel")}
                extra={t("apps.sourceCursorArgHint")}
              >
                <Input
                  value={sourceCursorArg}
                  onChange={(event) => setSourceCursorArg(event.target.value)}
                  placeholder="since"
                  maxLength={100}
                />
              </Form.Item>
              <Form.Item
                label={t("apps.sourceCursorPathLabel")}
                extra={t("apps.sourceCursorPathHint")}
              >
                <Input
                  value={sourceCursorPath}
                  onChange={(event) => setSourceCursorPath(event.target.value)}
                  placeholder="data.next_cursor"
                  maxLength={100}
                />
              </Form.Item>
            </>
          )}
          <Form.Item>
            <Checkbox
              checked={sourceIncludePii}
              onChange={(event) => setSourceIncludePii(event.target.checked)}
            >
              {t("apps.importIncludePii")}
            </Checkbox>
          </Form.Item>
          <Form.Item
            label={t("apps.sourceMappingLabel")}
            extra={t("apps.sourceMappingHint")}
          >
            <Input.TextArea
              value={sourceMapping}
              onChange={(event) => setSourceMapping(event.target.value)}
              placeholder='{"name": "title"}'
              autoSize={{ minRows: 1, maxRows: 4 }}
            />
          </Form.Item>
          <Form.Item label={t("apps.importMode")}>
            <Select
              value={sourceMode}
              onChange={(value) => setSourceMode(value)}
              options={[
                { value: "append", label: t("apps.importModeAppend") },
                {
                  value: "replace",
                  label: t("apps.importModeReplace"),
                  disabled: !canEditStructure,
                },
              ]}
            />
          </Form.Item>
          <Form.Item label={t("apps.sourceScheduleLabel")}>
            <Select
              value={sourceSchedule}
              onChange={(value) => setSourceSchedule(value)}
              options={SOURCE_SCHEDULE_OPTIONS.map((option) => ({
                value: option.value,
                label: t(option.labelKey),
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("apps.shareTitle", { name: detail.name })}
        open={shareOpen}
        onCancel={() => setShareOpen(false)}
        onOk={() => void submitShare()}
        okText={t("apps.shareSave")}
        cancelText={t("common.cancel")}
        confirmLoading={savingShare}
        destroyOnHidden
      >
        <Text type="secondary">{t("apps.shareHint")}</Text>
        {detail.columns.some((column) => column.pii) && (
          <Alert
            type="warning"
            showIcon
            message={t("apps.piiShareWarning")}
            style={{ marginTop: 12 }}
          />
        )}
        <div style={{ marginTop: 12 }}>
          {shareLoading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spin />
            </div>
          ) : (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {shares.length === 0 && (
                <Text type="secondary">{t("apps.shareEmpty")}</Text>
              )}
              {shares.map((item, index) => (
                <Space.Compact key={index} style={{ width: "100%" }}>
                  <Input
                    placeholder={t("apps.shareUser")}
                    value={item.username}
                    onChange={(event) => {
                      const next = [...shares];
                      next[index] = {
                        ...next[index],
                        username: event.target.value,
                      };
                      setShares(next);
                    }}
                  />
                  <Select
                    style={{ width: 190 }}
                    value={item.permission}
                    options={[
                      { value: "view", label: t("apps.permissionView") },
                      {
                        value: "edit_data",
                        label: t("apps.permissionEditData"),
                      },
                    ]}
                    onChange={(permission) => {
                      const next = [...shares];
                      next[index] = { ...next[index], permission };
                      setShares(next);
                    }}
                  />
                  <Button
                    danger
                    icon={<Trash2 size={14} />}
                    onClick={() =>
                      setShares(shares.filter((_, i) => i !== index))
                    }
                  />
                </Space.Compact>
              ))}
              <Button
                type="dashed"
                icon={<Plus size={14} />}
                onClick={() =>
                  setShares([...shares, { username: "", permission: "view" }])
                }
                block
              >
                {t("apps.shareAdd")}
              </Button>
            </Space>
          )}
        </div>
      </Modal>

      <Drawer
        title={t("apps.infoTitle")}
        open={infoOpen}
        onClose={() => setInfoOpen(false)}
        width={420}
      >
        {detail && (
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label={t("apps.createdByLabel")}>
              {detail.owner_username || "—"}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.agentLabel")}>
              {detail.agent_name || detail.agent_id}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.permissionLabel")}>
              {permissionLabel(detail.permission, t)}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.rowsLabel")}>
              {t("apps.rows", { count: detail.row_count })}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.createdAt")}>
              {formatServerDateTime(detail.created_at, timeZone)}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.updatedAt")}>
              {formatServerDateTime(detail.updated_at, timeZone)}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.appId")}>
              <Text code copyable={{ text: detail.app_id }}>
                {detail.app_id}
              </Text>
            </Descriptions.Item>
          </Descriptions>
        )}

        <div style={{ marginTop: 20 }}>
          <Text strong>{t("apps.tablesTitle")}</Text>
          <Space
            direction="vertical"
            size={8}
            style={{ width: "100%", marginTop: 8 }}
          >
            {detail.tables.map((table) => {
              const source = sources.find(
                (item) => item.table_key === table.table_key,
              );
              return (
                <div
                  key={table.table_key}
                  style={{
                    padding: "8px 10px",
                    border: "1px solid rgba(5, 5, 5, 0.06)",
                    borderRadius: 6,
                    background: "#fafafa",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 12,
                    }}
                  >
                    <Space size={8} style={{ minWidth: 0 }}>
                      <Table2 size={14} />
                      <Text style={{ fontSize: 13 }}>
                        {table.label || table.table_key}
                      </Text>
                      {table.is_main && (
                        <Tag style={{ marginInlineEnd: 0 }}>
                          {t("apps.mainTable")}
                        </Tag>
                      )}
                    </Space>
                    <Space size={4}>
                      {canEditData && (
                        <Button
                          type="text"
                          size="small"
                          icon={<UploadIcon size={13} />}
                          onClick={() => openImport(table.table_key)}
                        >
                          {t("apps.importButton")}
                        </Button>
                      )}
                      {canEditStructure && (
                        <Button
                          type="text"
                          size="small"
                          icon={<Link2 size={13} />}
                          onClick={() => openSourceModal(table.table_key)}
                        >
                          {t("apps.sourceButton")}
                        </Button>
                      )}
                      <Text
                        type="secondary"
                        style={{ fontSize: 12, whiteSpace: "nowrap" }}
                      >
                        {t("apps.tableRows", { count: table.row_count })}
                      </Text>
                    </Space>
                  </div>
                  {source && (
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        marginTop: 6,
                        paddingLeft: 22,
                      }}
                    >
                      <Tag
                        color={
                          source.last_status === "error"
                            ? "red"
                            : source.last_status === "ok"
                            ? "green"
                            : "default"
                        }
                        style={{
                          marginInlineEnd: 0,
                          fontSize: 11,
                          lineHeight: "18px",
                        }}
                      >
                        {source.last_status === "error"
                          ? t("apps.sourceStatusError")
                          : source.last_status === "ok"
                          ? t("apps.sourceStatusOk")
                          : t("apps.sourceStatusPending")}
                      </Tag>
                      <Text
                        type="secondary"
                        style={{ fontSize: 12, flex: 1, minWidth: 0 }}
                        ellipsis
                      >
                        {source.kind === "workspace_file"
                          ? String(source.params.path || "")
                          : String(source.params.tool || "")}
                        {source.schedule_seconds > 0 &&
                          ` · ${t("apps.sourceEvery", {
                            text: sourceScheduleLabel(
                              source.schedule_seconds,
                              t,
                            ),
                          })}`}
                        {source.last_run_at &&
                          ` · ${t(
                            "apps.sourceLastSync",
                          )} ${formatServerDateTime(
                            source.last_run_at,
                            timeZone,
                          )}`}
                        {source.last_status === "ok" &&
                          source.last_row_count !== null &&
                          ` · ${t("apps.tableRows", {
                            count: source.last_row_count,
                          })}`}
                      </Text>
                      {canEditData && (
                        <Button
                          type="text"
                          size="small"
                          icon={<RefreshCw size={13} />}
                          onClick={() => void runSourceNow(source)}
                        >
                          {t("apps.sourceRun")}
                        </Button>
                      )}
                      {canEditStructure && (
                        <Popconfirm
                          title={t("apps.sourceDeleteTitle")}
                          description={t("apps.sourceDeleteConfirm")}
                          okButtonProps={{ danger: true }}
                          okText={t("apps.delete")}
                          cancelText={t("common.cancel")}
                          onConfirm={() => void removeSource(source)}
                        >
                          <Button
                            type="text"
                            size="small"
                            danger
                            icon={<Trash2 size={13} />}
                          />
                        </Popconfirm>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("apps.tablesHint")}
            </Text>
          </Space>
        </div>

        <div style={{ marginTop: 20 }}>
          <Text strong>{t("apps.filesTitle")}</Text>
          {filesLoading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spin />
            </div>
          ) : (
            <Space
              direction="vertical"
              size={8}
              style={{ width: "100%", marginTop: 8 }}
            >
              {files.map((file) => (
                <div
                  key={file.name}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 12,
                    padding: "8px 10px",
                    border: "1px solid rgba(5, 5, 5, 0.06)",
                    borderRadius: 6,
                    background: "#fafafa",
                  }}
                >
                  <Space size={8} style={{ minWidth: 0 }}>
                    <FileCode size={14} />
                    <Text style={{ fontSize: 13 }}>{file.name}</Text>
                  </Space>
                  <Text
                    type="secondary"
                    style={{ fontSize: 12, whiteSpace: "nowrap" }}
                  >
                    {formatBytes(file.size)}
                  </Text>
                </div>
              ))}
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t("apps.filesHint")}
              </Text>
            </Space>
          )}
        </div>
      </Drawer>

      <Drawer
        title={
          <Space size={8}>
            <Sparkles size={16} />
            {t("apps.assistantTitle")}
          </Space>
        }
        open={assistantOpen}
        onClose={() => setAssistantOpen(false)}
        width={assistantWidth}
        extra={
          // The drawer hugs the right edge: dragging its left border outward
          // (leftwards) widens it, inwards shrinks it — the chevrons mirror
          // exactly that, so left = widen, right = narrow.
          <Space size={0}>
            <Tooltip title={t("apps.assistantWidthMax")}>
              <Button
                type="text"
                size="small"
                aria-label={t("apps.assistantWidthMax")}
                icon={<ChevronsLeft size={14} />}
                onClick={() => setAssistantPreset(assistantMaxWidth())}
              />
            </Tooltip>
            <Tooltip title={t("apps.assistantWidthMid")}>
              <Button
                type="text"
                size="small"
                aria-label={t("apps.assistantWidthMid")}
                icon={<AlignCenterHorizontal size={14} />}
                onClick={() =>
                  setAssistantPreset(Math.floor(window.innerWidth * 0.5))
                }
              />
            </Tooltip>
            <Tooltip title={t("apps.assistantWidthMin")}>
              <Button
                type="text"
                size="small"
                aria-label={t("apps.assistantWidthMin")}
                icon={<ChevronsRight size={14} />}
                onClick={() => setAssistantPreset(assistantMinWidth())}
              />
            </Tooltip>
          </Space>
        }
        styles={{
          body: { display: "flex", flexDirection: "column" },
          // antd animates wrapper width changes; freeze it mid-drag.
          wrapper: { transition: assistantResizing ? "none" : undefined },
        }}
        footer={
          <div>
            <Space.Compact style={{ width: "100%" }}>
              <Input
                value={assistantInput}
                onChange={(event) => setAssistantInput(event.target.value)}
                placeholder={t("apps.assistantPlaceholder")}
                maxLength={4000}
                disabled={assistantBusy}
                onPressEnter={() => void sendAssistant()}
              />
              <Button
                type="primary"
                loading={assistantBusy}
                disabled={!assistantInput.trim()}
                onClick={() => void sendAssistant()}
              >
                {t("apps.assistantSend")}
              </Button>
            </Space.Compact>
            <Text
              type="secondary"
              style={{ fontSize: 12, display: "block", marginTop: 6 }}
            >
              {t("apps.assistantHint")}
            </Text>
          </div>
        }
      >
        {/* Resize grip: absolute against the drawer content-wrapper (the
            nearest positioned ancestor), so it spans the full panel height
            and escapes the scrolling body's clipping. */}
        <div
          onPointerDown={startAssistantResize}
          onPointerMove={moveAssistantResize}
          onPointerUp={endAssistantResize}
          onPointerCancel={endAssistantResize}
          onMouseEnter={() => setAssistantResizeHover(true)}
          onMouseLeave={() => setAssistantResizeHover(false)}
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            bottom: 0,
            width: 8,
            cursor: "col-resize",
            zIndex: 10,
            touchAction: "none",
            background:
              assistantResizeHover || assistantResizing
                ? "rgba(22, 119, 255, 0.18)"
                : "transparent",
          }}
        />
        <div
          ref={assistantListRef}
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {assistantLoading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spin />
            </div>
          ) : (
            <>
              {assistantMsgs.length === 0 && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t("apps.assistantEmpty")}
                </Text>
              )}
              {assistantMsgs.map((item, index) => {
                const isUser = item.role === "user";
                const streaming =
                  assistantBusy && index === assistantMsgs.length - 1;
                return (
                  <div
                    key={index}
                    style={{
                      alignSelf: isUser ? "flex-end" : "flex-start",
                      maxWidth: "85%",
                      background: isUser
                        ? "rgba(22, 119, 255, 0.08)"
                        : "rgba(0, 0, 0, 0.03)",
                      padding: "8px 12px",
                      borderRadius: 10,
                      whiteSpace: isUser ? "pre-wrap" : "normal",
                      wordBreak: "break-word",
                      fontSize: 13,
                    }}
                  >
                    {item.content ? (
                      isUser ? (
                        item.content
                      ) : (
                        <Markdown
                          content={item.content}
                          isStreaming={streaming}
                        />
                      )
                    ) : streaming ? (
                      <Spin size="small" />
                    ) : (
                      "…"
                    )}
                    {!isUser && item.citations?.length ? (
                      <div
                        style={{
                          marginTop: 6,
                          display: "flex",
                          gap: 8,
                          flexWrap: "wrap",
                          fontSize: 12,
                        }}
                      >
                        <Text type="secondary">
                          {t("apps.assistantCitations")}:
                        </Text>
                        {item.citations.slice(0, 5).map((citation) => (
                          <a
                            key={`${citation.table}-${citation.row_id}`}
                            onClick={() => {
                              setDataTable(citation.table);
                              setActiveView("data");
                              setAssistantOpen(false);
                            }}
                          >
                            {citation.row_id}
                          </a>
                        ))}
                      </div>
                    ) : null}
                  </div>
                );
              })}
              {assistantTool && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  <Spin size="small" style={{ marginRight: 6 }} />
                  {t("apps.assistantToolRunning", { name: assistantTool })}
                </Text>
              )}
              {assistantError && (
                <Text type="danger" style={{ fontSize: 12 }}>
                  {assistantError}
                </Text>
              )}
            </>
          )}
        </div>
      </Drawer>
    </PageShell>
  );
}
