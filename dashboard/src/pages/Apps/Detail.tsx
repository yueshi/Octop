import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import type { MenuProps, UploadFile } from "antd";
import {
  AlignCenterHorizontal,
  ArrowLeft,
  ChevronDown,
  ChevronsLeft,
  ChevronsRight,
  Download,
  Edit3,
  FileCode,
  History,
  Info,
  Link2,
  MessageSquareText,
  MoreHorizontal,
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
  type AppAssistantChart,
  type AppAssistantMessage,
  type AppColumn,
  type AppDetail,
  type AppFile,
  type AppGameSession,
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
import { fullscreenContainer } from "../../utils/overlayContainer";
import { apiErrorMessage, isNotFoundApiError } from "../../utils/apiError";
import { formatBytes } from "../../utils/formatBytes";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { useIsMobile } from "../../hooks/useIsMobile";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import GameFrame, {
  MINIAPP_GAME_FINISHED_EVENT,
  type MiniAppGameFinishedDetail,
} from "../../components/MiniAppFrame/GameFrame";
import {
  buildSrcDoc,
  hasEmbeddedFrame,
} from "../../components/MiniAppFrame/sdk";
import { buildWrapperDoc } from "../../components/MiniAppFrame/wrapper";
import {
  attachBridge,
  type AppViolation,
  type BridgeParams,
} from "../../components/MiniAppFrame/bridge";
import {
  CAP,
  effectiveCapabilities,
} from "../../components/MiniAppFrame/capabilities";

const { Text } = Typography;

const AssistantChart = lazy(() => import("./AssistantChart"));

/** Assistant drawer width presets (px) shared by the drag handle and the
 *  min/mid/max quick buttons; min and max follow the viewport. */
const assistantMinWidth = () => Math.floor(window.innerWidth * 0.35);
const assistantMaxWidth = () =>
  Math.max(assistantMinWidth(), Math.floor(window.innerWidth * 0.9));

function permissionLabel(
  permission: AppDetail["permission"],
  t: (key: string) => string,
  isOwner?: boolean,
): string {
  if (permission === "edit_structure") {
    return isOwner === false
      ? t("apps.permissionAdmin")
      : t("apps.editStructure");
  }
  if (permission === "edit_data") return t("apps.editData");
  return t("apps.viewOnly");
}

/** One titled block in the details drawer; keeps section rhythm consistent. */
function InfoSection({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section style={{ marginTop: 24 }}>
      <Text strong>{title}</Text>
      {hint ? (
        <div style={{ marginTop: 4 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {hint}
          </Text>
        </div>
      ) : null}
      <div style={{ marginTop: 10 }}>{children}</div>
    </section>
  );
}

const SOURCE_COLUMN_WIDTH = 90;
const ACTIONS_COLUMN_WIDTH = 150;

const CAPABILITY_OPTIONS: {
  value: string;
  labelKey: string;
  fallback: string;
}[] = [
  {
    value: CAP.DATA_READ,
    labelKey: "apps.capabilities.dataRead",
    fallback: "Read tables and rows",
  },
  {
    value: CAP.DATA_WRITE,
    labelKey: "apps.capabilities.dataWrite",
    fallback: "Append rows",
  },
  {
    value: CAP.ASSET_READ,
    labelKey: "apps.capabilities.assetRead",
    fallback: "Load app images",
  },
  {
    value: CAP.EXTERNAL_OPEN,
    labelKey: "apps.capabilities.externalOpen",
    fallback: "Open external links",
  },
  {
    value: CAP.CHAT_COMPOSE,
    labelKey: "apps.capabilities.chatCompose",
    fallback: "Prepare a chat message",
  },
  {
    value: CAP.EVENTS_READ,
    labelKey: "apps.capabilities.eventsRead",
    fallback: "Read app events",
  },
  {
    value: CAP.AI_GENERATE,
    labelKey: "apps.capabilities.aiGenerate",
    fallback: "Call the AI model",
  },
  {
    value: CAP.GAME_LAUNCH,
    labelKey: "apps.capabilities.gameLaunch",
    fallback: "Launch external games",
  },
  {
    value: CAP.EXTERNAL_FETCH,
    labelKey: "apps.capabilities.externalFetch",
    fallback: "Call external services",
  },
  {
    value: CAP.EVENTS_EMIT,
    labelKey: "apps.capabilities.eventsEmit",
    fallback: "Send events and alerts",
  },
  {
    value: CAP.SCHEDULES_MANAGE,
    labelKey: "apps.capabilities.schedulesManage",
    fallback: "Manage schedules",
  },
];

/**
 * Fixed width for a data column, by its schema type.
 *
 * The data table must not use auto layout: a long free-text value (ex. the
 * 内容 column of a reading tracker) would otherwise stretch its column to the
 * full text width and push the whole table into a huge horizontal scroll.
 * Every column gets an explicit width, and cells truncate with an ellipsis
 * (the full value stays available as the cell tooltip and in the row editor).
 */
function staleReasonLabel(reason: string, t: TFunction): string {
  switch (reason) {
    case "html":
      return t("apps.publishStaleHtml", "HTML changed");
    case "schema":
    case "visibility":
      return t("apps.publishStaleSchema", "Table structure changed");
    case "not_published":
      return t(
        "apps.publishNotFixed",
        "This link has not fixed a version yet; publish to freeze the current one.",
      );
    case "workspace":
      return t("apps.publishStaleWorkspace", "Workspace unavailable");
    default:
      return reason;
  }
}

function dataColumnWidth(type: string): number {
  switch (type) {
    case "integer":
    case "number":
      return 110;
    case "boolean":
      return 100;
    case "date":
      return 130;
    case "datetime":
      return 170;
    case "enum":
      return 140;
    case "text":
      return 260;
    default:
      return 180;
  }
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
  const { modal } = App.useApp();
  const isMobile = useIsMobile();
  const currentUser = useCurrentUser();
  const timeZone = useServerTimezone();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const capabilitiesRef = useRef<readonly string[] | undefined>(undefined);

  const [detail, setDetail] = useState<AppDetail | null>(null);
  const [capabilityDraft, setCapabilityDraft] = useState<string[]>([]);
  const [savingCapabilities, setSavingCapabilities] = useState(false);
  useEffect(() => {
    capabilitiesRef.current = detail?.capabilities;
    setCapabilityDraft(detail?.capabilities ?? []);
  }, [detail?.capabilities]);

  const saveCapabilities = useCallback(async () => {
    if (!detail) return;
    setSavingCapabilities(true);
    try {
      const updated = await appsApi.setCapabilities(
        detail.app_id,
        capabilityDraft,
      );
      setDetail(updated);
      message.success(t("apps.capabilities.saved", "Capabilities saved"));
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("apps.capabilities.saveFailed", "Could not save capabilities"),
          t,
        ),
      );
    } finally {
      setSavingCapabilities(false);
    }
  }, [capabilityDraft, detail, t]);

  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [htmlLoading, setHtmlLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [gameSession, setGameSession] = useState<AppGameSession | null>(null);
  const [htmlReloadKey, setHtmlReloadKey] = useState(0);
  const [appEscaped, setAppEscaped] = useState(false);
  const [htmlBlocked, setHtmlBlocked] = useState(false);

  // A remount starts a fresh document; drop the previous escape marker.
  useEffect(() => {
    setAppEscaped(false);
  }, [htmlReloadKey, appId]);

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameForm] = Form.useForm<{ name: string; description: string }>();
  const [shareOpen, setShareOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [publication, setPublication] = useState<AppPublication | null>(null);
  const [publicationBusy, setPublicationBusy] = useState(false);
  // The API returns the public origin (scheme/host/port) the server is reached
  // at; fall back to the browser origin when the server has no public_base_url.
  const publishUrl =
    publication?.url ??
    (publication?.token
      ? `${window.location.origin}/p/${publication.token}`
      : "");
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
    setHtmlBlocked(false);
    appsApi
      .getHtml(appId)
      .then((text) => {
        if (!alive) return;
        if (hasEmbeddedFrame(text)) {
          setHtml("");
          setHtmlBlocked(true);
          return;
        }
        setHtml(text);
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

  // A game wrote rows in its host: remount the app so it re-reads them.
  useEffect(() => {
    const onGameEvent = (event: Event) => {
      const detail = (event as CustomEvent<MiniAppGameFinishedDetail>).detail;
      if (!detail || detail.appId !== appId) return;
      setHtmlReloadKey((value) => value + 1);
    };
    window.addEventListener(MINIAPP_GAME_FINISHED_EVENT, onGameEvent);
    return () =>
      window.removeEventListener(MINIAPP_GAME_FINISHED_EVENT, onGameEvent);
  }, [appId]);

  const reportViolation = useCallback(
    (violation: AppViolation) => {
      // Telemetry only: a failed report must not disturb the page.
      void appsApi.reportViolation(appId, violation).catch(() => {});
    },
    [appId],
  );

  // The wrapper reports that the app frame navigated away. Stop there: the
  // escaping page would otherwise inherit the bridge and could read every row
  // through the host.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = iframeRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const data = event.data as { source?: string; type?: string } | null;
      if (!data || data.source !== "octop-app-host") return;
      if (data.type === "escaped") setAppEscaped(true);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => {
    const detach = attachBridge(
      {
        // Read the ref on every request: the owner can edit the manifest from
        // this page without remounting the frame.
        capabilities: () => capabilitiesRef.current,
        onViolation: reportViolation,
        handle: async (method: string, params: BridgeParams) => {
          if (method === "me") {
            const current = await appsApi.get(appId);
            return {
              permission: current.permission,
              capabilities: effectiveCapabilities(
                current.capabilities ?? capabilitiesRef.current,
              ),
            };
          }
          if (method === "schema") {
            const current = await appsApi.get(appId);
            const tableKey = params.table || "main";
            if (tableKey === "main") {
              return { table: tableKey, columns: current.columns };
            }
            const table = current.tables.find(
              (item) => item.table_key === tableKey,
            );
            if (!table) throw new Error(`unknown table: ${tableKey}`);
            return { table: tableKey, columns: table.columns };
          }
          if (method === "tables") {
            const current = await appsApi.get(appId);
            return {
              tables: current.tables.map((item) => ({
                table_key: item.table_key,
                label: item.label,
                row_count: item.row_count,
                is_main: item.is_main,
              })),
            };
          }
          if (method === "rows") {
            return await appsApi.rows(
              appId,
              params.limit,
              params.offset,
              params.table || "main",
            );
          }
          if (method === "append") {
            const tableKey = params.table || "main";
            const result = await appsApi.appendRow(
              appId,
              params.row ?? {},
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
            return result;
          }
          if (method === "generate") {
            return await appsApi.generate(appId, {
              prompt: String(params.prompt ?? ""),
              system: String(params.system ?? ""),
            });
          }
          if (method === "composeMessage") {
            const text = String(params.text ?? "").trim();
            if (!text) {
              throw new Error(t("apps.composeEmpty", "Message is empty"));
            }
            if (text.length > 4000) {
              throw new Error(t("apps.composeTooLong", "Message is too long"));
            }
            const current = await appsApi.get(appId);
            navigate(`/chat/${encodeURIComponent(current.agent_id)}`, {
              state: { prefillInput: text },
            });
            return { filled: true };
          }
          if (method === "launchGame") {
            const gameId = String(params.gameId ?? "").trim();
            if (!gameId) throw new Error("gameId is required");
            const session = await appsApi.createGameSession(appId, gameId);
            setGameSession(session);
            return { sessionId: session.session_id, status: "opened" };
          }
          if (method === "openExternal") {
            const url = String(params.url ?? "").trim();
            if (!/^https?:\/\//i.test(url)) {
              throw new Error("openExternal requires an http(s) URL");
            }
            // The app page runs sandboxed without popups, so the parent has
            // to open the link — after the user confirms the target domain.
            Modal.confirm({
              getContainer: fullscreenContainer,
              title: t("apps.openExternalTitle"),
              content: url,
              okText: t("apps.openExternalConfirm"),
              cancelText: t("common.cancel"),
              onOk: () => {
                window.open(url, "_blank", "noopener,noreferrer");
              },
            });
            return { opened: true };
          }
          if (method === "asset") {
            const name = String(params.name ?? "").trim();
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
            return { name, mime, data_url: dataUrl };
          }
          throw new Error("unsupported method");
        },
      },
      () => iframeRef.current?.contentWindow ?? null,
    );
    return detach;
  }, [appId, navigate, reportViolation, t]);

  const srcDoc = useMemo(
    () => (html ? buildWrapperDoc(buildSrcDoc(html)) : ""),
    [html],
  );

  const dataColumns: AppColumn[] =
    detail?.tables.find((item) => item.table_key === dataTable)?.columns ??
    detail?.columns ??
    [];
  const canEditRows = detail?.permission !== "view";
  const dataTableWidth =
    dataColumns.reduce((sum, column) => sum + dataColumnWidth(column.type), 0) +
    SOURCE_COLUMN_WIDTH +
    (canEditRows ? ACTIONS_COLUMN_WIDTH : 0);
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

  const setTableVisibility = async (
    tableKey: string,
    publicVisible: boolean,
  ) => {
    try {
      const updated = await appsApi.setTableVisibility(
        appId,
        tableKey,
        publicVisible,
      );
      setDetail((prev) =>
        prev
          ? {
              ...prev,
              tables: prev.tables.map((item) =>
                item.table_key === tableKey ? updated : item,
              ),
            }
          : prev,
      );
      message.success(t("apps.saveSuccess"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.saveFailed"), t));
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

  const publishNow = async () => {
    setPublicationBusy(true);
    try {
      setPublication(await appsApi.publish(appId));
      message.success(t("apps.publishUpdated", "Public version updated"));
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

  const restoreHtml = async () => {
    try {
      const updated = await appsApi.restoreHtml(appId);
      setDetail(updated);
      setHtml(await appsApi.getHtml(appId));
      setHtmlReloadKey((value) => value + 1);
      message.success(t("apps.restoreHtmlDone", "Previous version restored"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
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
        } else if (event.type === "chart") {
          setAssistantMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              next[next.length - 1] = {
                ...last,
                charts: [...(last.charts ?? []), event.chart],
              };
            }
            return next;
          });
        } else if (event.type === "done") {
          const citations = event.citations ?? [];
          const charts = event.charts;
          setAssistantMsgs((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              next[next.length - 1] = {
                ...last,
                citations,
                // `done` carries the authoritative list; chart events are for
                // rendering while the answer is still streaming.
                ...(charts ? { charts } : {}),
              };
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

  const confirmRestoreHtml = () => {
    modal.confirm({
      title: t("apps.restoreHtmlTitle", "Restore the previous version?"),
      content: t(
        "apps.restoreHtmlConfirm",
        "The current HTML will become the previous version.",
      ),
      okText: t("apps.restoreHtml", "Restore previous"),
      cancelText: t("common.cancel"),
      onOk: () => restoreHtml(),
    });
  };

  const confirmDeleteApp = () => {
    modal.confirm({
      title: t("apps.deleteTitle"),
      content: t("apps.deleteConfirm"),
      okText: t("apps.delete"),
      cancelText: t("common.cancel"),
      okButtonProps: { danger: true },
      onOk: () => removeApp(),
    });
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
  // `edit_structure` covers both the owner and admins; only the owner should
  // see the permissions tag, while admins get an explicit label.
  const isOwner = currentUser
    ? detail.owner_username === currentUser.username
    : undefined;
  const permissionText = permissionLabel(detail.permission, t, isOwner);
  const showPermissionTag =
    detail.permission !== "edit_structure" || isOwner === false;

  const sharePublishMenu: MenuProps = {
    items: [
      { key: "share", icon: <Share2 size={14} />, label: t("apps.share") },
      {
        key: "publish",
        icon: <Link2 size={14} />,
        label: t("apps.publishLink"),
      },
    ],
    onClick: ({ key }) => {
      if (key === "share") void openShare();
      else if (key === "publish") void openPublish();
    },
  };

  const moreMenu: MenuProps = {
    items: [
      { key: "rename", icon: <Edit3 size={14} />, label: t("apps.rename") },
      ...(detail.previous_html_at
        ? [
            {
              key: "restore",
              icon: <History size={14} />,
              label: t("apps.restoreHtml"),
            },
          ]
        : []),
      { type: "divider" as const },
      {
        key: "delete",
        icon: <Trash2 size={14} />,
        label: t("apps.delete"),
        danger: true,
      },
    ],
    onClick: ({ key }) => {
      if (key === "rename") openRename();
      else if (key === "restore") confirmRestoreHtml();
      else if (key === "delete") confirmDeleteApp();
    },
  };

  return (
    <PageShell
      title={detail.name}
      subtitle={detail.description || undefined}
      leading={
        <Tooltip title={t("apps.back")}>
          <Button
            type="text"
            icon={<ArrowLeft size={18} />}
            aria-label={t("apps.back")}
            onClick={() => navigate("/apps")}
          />
        </Tooltip>
      }
      titleSuffix={
        showPermissionTag ? (
          <Tag style={{ marginInlineEnd: 0 }}>{permissionText}</Tag>
        ) : undefined
      }
      fill
      actions={
        <Space size={isMobile ? 4 : 8}>
          <Tooltip title={t("apps.assistantTitle")}>
            <Button
              type="text"
              icon={<Sparkles size={16} />}
              aria-label={t("apps.assistant")}
              onClick={() => void openAssistant()}
            >
              {isMobile ? null : t("apps.assistant")}
            </Button>
          </Tooltip>
          {canEditStructure && (
            <Dropdown menu={sharePublishMenu} trigger={["click"]}>
              <Button
                icon={<Share2 size={16} />}
                aria-label={t("apps.shareAndPublish")}
              >
                {isMobile ? null : (
                  <>
                    {t("apps.shareAndPublish")}
                    <ChevronDown size={14} />
                  </>
                )}
              </Button>
            </Dropdown>
          )}
          <Tooltip title={t("apps.infoTitle")}>
            <Button
              type="text"
              icon={<Info size={16} />}
              aria-label={t("apps.info")}
              onClick={() => void openInfo()}
            >
              {isMobile ? null : t("apps.info")}
            </Button>
          </Tooltip>
          {canEditStructure && (
            <Dropdown menu={moreMenu} trigger={["click"]}>
              <Button
                type="text"
                icon={<MoreHorizontal size={16} />}
                aria-label={t("common.more")}
              />
            </Dropdown>
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
                  ) : appEscaped ? (
                    <div style={{ padding: 24 }}>
                      <Text type="warning">
                        {t(
                          "apps.sandboxEscaped",
                          "This MiniApp tried to leave its sandbox, so it was stopped.",
                        )}
                      </Text>
                    </div>
                  ) : htmlBlocked ? (
                    <div style={{ padding: 24 }}>
                      <Text type="warning">
                        {t(
                          "apps.sandboxEmbeddedFrame",
                          "This MiniApp embeds a frame, which is not allowed. Regenerate it.",
                        )}
                      </Text>
                    </div>
                  ) : detail.has_html && html ? (
                    <iframe
                      key={htmlReloadKey}
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
                    scroll={{ x: dataTableWidth }}
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
                        width: dataColumnWidth(column.type),
                        ellipsis: true,
                      })),
                      {
                        title: t("apps.source"),
                        key: "__source",
                        width: SOURCE_COLUMN_WIDTH,
                        render: (_: unknown, __: unknown, index: number) =>
                          visibleRows[index]?.meta?.source || "manual",
                      },
                      ...(canEditRows
                        ? [
                            {
                              title: t("apps.actions"),
                              key: "__actions",
                              width: ACTIONS_COLUMN_WIDTH,
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
            {publication?.active && (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                {publication.published_at ? (
                  <Text type="secondary">
                    {t("apps.publishedAt", "Published {{time}}", {
                      time: formatServerDateTime(
                        publication.published_at,
                        timeZone,
                      ),
                    })}
                  </Text>
                ) : (
                  <Alert
                    type="info"
                    showIcon
                    message={t(
                      "apps.publishNotFixed",
                      "This link has not fixed a version yet; publish to freeze the current one.",
                    )}
                  />
                )}
                {publication.snapshot_stale && (
                  <Alert
                    type="warning"
                    showIcon
                    message={t(
                      "apps.publishStale",
                      "The MiniApp changed since the last publish",
                    )}
                    description={(publication.stale_reasons ?? [])
                      .map((reason) => staleReasonLabel(reason, t))
                      .join(" · ")}
                  />
                )}
              </Space>
            )}
            {publishUrl && (
              <Input
                readOnly
                value={publishUrl}
                addonAfter={
                  <Button
                    type="link"
                    size="small"
                    onClick={() => {
                      void navigator.clipboard?.writeText(publishUrl);
                      message.success(t("apps.publishCopy"));
                    }}
                  >
                    {t("apps.publishCopy")}
                  </Button>
                }
              />
            )}
            <Space>
              {publication?.active && (
                <Button
                  type={publication.snapshot_stale ? "primary" : "default"}
                  onClick={() => void publishNow()}
                  loading={publicationBusy}
                >
                  {t("apps.publishNow", "Publish updates")}
                </Button>
              )}
              <Button
                type={publication?.active ? "default" : "primary"}
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
        width={isMobile ? "100%" : 560}
        styles={{ body: { padding: "20px 24px 28px" } }}
      >
        {detail && (
          <Descriptions
            column={1}
            size="small"
            bordered
            labelStyle={{ width: 120, whiteSpace: "nowrap" }}
          >
            <Descriptions.Item label={t("apps.createdByLabel")}>
              {detail.owner_username || "—"}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.agentLabel")}>
              {detail.agent_name || detail.agent_id}
            </Descriptions.Item>
            <Descriptions.Item label={t("apps.permissionLabel")}>
              {permissionText}
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

        {detail && (
          <InfoSection
            title={t("apps.capabilities.title", "Page capabilities")}
            hint={t(
              "apps.capabilities.hint",
              "Controls what the MiniApp page may ask the host to do; agent tools and this dashboard are not affected.",
            )}
          >
            <Checkbox.Group
              value={capabilityDraft}
              onChange={(values) => setCapabilityDraft(values as string[])}
              disabled={
                detail.permission !== "edit_structure" || savingCapabilities
              }
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(210px, 1fr))",
                columnGap: 12,
                rowGap: 8,
              }}
            >
              {CAPABILITY_OPTIONS.map((option) => (
                <Checkbox key={option.value} value={option.value}>
                  {t(option.labelKey, option.fallback)}
                </Checkbox>
              ))}
            </Checkbox.Group>
            {detail.permission === "edit_structure" && (
              <Button
                type="primary"
                size="small"
                style={{ marginTop: 12 }}
                loading={savingCapabilities}
                onClick={() => void saveCapabilities()}
              >
                {t("apps.capabilities.save", "Save")}
              </Button>
            )}
          </InfoSection>
        )}

        <InfoSection title={t("apps.tablesTitle")} hint={t("apps.tablesHint")}>
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            {detail.tables.map((table) => {
              const source = sources.find(
                (item) => item.table_key === table.table_key,
              );
              return (
                <div
                  key={table.table_key}
                  style={{
                    padding: "10px 12px",
                    border: "1px solid rgba(5, 5, 5, 0.06)",
                    borderRadius: 8,
                    background: "#fafafa",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      minWidth: 0,
                    }}
                  >
                    <Table2 size={14} style={{ flexShrink: 0 }} />
                    <Text
                      strong
                      ellipsis
                      style={{ fontSize: 13, minWidth: 0, flex: "1 1 auto" }}
                    >
                      {table.label || table.table_key}
                    </Text>
                    {table.is_main && (
                      <Tag style={{ marginInlineEnd: 0, flexShrink: 0 }}>
                        {t("apps.mainTable")}
                      </Tag>
                    )}
                    <Text
                      type="secondary"
                      style={{
                        fontSize: 12,
                        whiteSpace: "nowrap",
                        flexShrink: 0,
                      }}
                    >
                      {t("apps.tableRows", { count: table.row_count })}
                    </Text>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      flexWrap: "wrap",
                      gap: 8,
                      marginTop: 8,
                    }}
                  >
                    {canEditStructure && (
                      <Tooltip title={t("apps.tableVisibilityHint")}>
                        <Space size={4}>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {t("apps.publicTable")}
                          </Text>
                          <Switch
                            size="small"
                            checked={table.public_visible}
                            onChange={(checked) =>
                              void setTableVisibility(table.table_key, checked)
                            }
                          />
                        </Space>
                      </Tooltip>
                    )}
                    <span style={{ flex: 1 }} />
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
                  </div>
                  {source && (
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        flexWrap: "wrap",
                        gap: 8,
                        marginTop: 8,
                        paddingTop: 8,
                        borderTop: "1px dashed rgba(5, 5, 5, 0.08)",
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
                        style={{ fontSize: 12, flex: "1 1 200px", minWidth: 0 }}
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
          </Space>
        </InfoSection>

        <InfoSection title={t("apps.filesTitle")} hint={t("apps.filesHint")}>
          {filesLoading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spin />
            </div>
          ) : (
            <Space direction="vertical" size={10} style={{ width: "100%" }}>
              {files.map((file) => (
                <div
                  key={file.name}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 12,
                    padding: "10px 12px",
                    border: "1px solid rgba(5, 5, 5, 0.06)",
                    borderRadius: 8,
                    background: "#fafafa",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      minWidth: 0,
                      flex: 1,
                    }}
                  >
                    <FileCode size={14} style={{ flexShrink: 0 }} />
                    <Text ellipsis style={{ fontSize: 13, minWidth: 0 }}>
                      {file.name}
                    </Text>
                  </div>
                  <Text
                    type="secondary"
                    style={{ fontSize: 12, whiteSpace: "nowrap" }}
                  >
                    {formatBytes(file.size)}
                  </Text>
                </div>
              ))}
            </Space>
          )}
        </InfoSection>
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
                const hasCharts = Boolean(item.charts?.length);
                const streaming =
                  assistantBusy && index === assistantMsgs.length - 1;
                return (
                  <div
                    key={index}
                    style={{
                      // Charts need a concrete parent width for
                      // ResponsiveContainer; give the bubble the full drawer
                      // width (capped on wide screens) instead of letting it
                      // shrink to the chart's min-content width.
                      alignSelf: hasCharts
                        ? "flex-start"
                        : isUser
                        ? "flex-end"
                        : "flex-start",
                      width: hasCharts ? "100%" : undefined,
                      maxWidth: hasCharts ? "min(100%, 720px)" : "85%",
                      minWidth: 0,
                      boxSizing: "border-box",
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
                    ) : hasCharts ? null : (
                      "…"
                    )}
                    {!isUser && hasCharts ? (
                      <div
                        style={{
                          marginTop: 8,
                          display: "grid",
                          gap: 12,
                          minWidth: 0,
                        }}
                      >
                        {item.charts?.map(
                          (chart: AppAssistantChart, chartIndex) => (
                            <Suspense
                              key={`${chart.title}-${chartIndex}`}
                              fallback={<Spin size="small" />}
                            >
                              <AssistantChart chart={chart} />
                            </Suspense>
                          ),
                        )}
                      </div>
                    ) : null}
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

      <Modal
        title={gameSession?.name}
        open={!!gameSession}
        onCancel={() => setGameSession(null)}
        footer={null}
        width="92vw"
        destroyOnHidden
        styles={{ body: { height: "80vh", padding: 0 } }}
      >
        {gameSession && (
          <GameFrame
            appId={appId}
            session={gameSession}
            onFinished={() => {
              setGameSession(null);
              void loadDetail();
            }}
          />
        )}
      </Modal>
    </PageShell>
  );
}
