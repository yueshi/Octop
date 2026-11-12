import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import {
  Button,
  Drawer,
  Empty,
  Form,
  Modal,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import type { UploadFile } from "antd";
import {
  AppWindow,
  Bot,
  Clock,
  LayoutGrid,
  Maximize2,
  MessageSquareText,
  Minimize2,
  Table2,
  User,
} from "lucide-react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { message } from "../../utils/antdMessage";

import {
  appsApi,
  appPreviewPath,
  type AppSummary,
} from "../../api/modules/apps";
import PageShell from "../../layouts/PageShell";
import CreateMiniAppModal from "./CreateMiniAppModal";
import AppWidget from "./AppWidget";
import {
  addAppToBoard,
  APP_BOARD_SPANS,
  DEFAULT_APP_BOARD_SIZE,
  isAppOnBoard,
  loadAppBoard,
  moveAppInBoard,
  reconcileAppBoard,
  removeAppFromBoard,
  saveAppBoard,
  sizeOfApp,
  snapAppBoardSize,
  withAppSize,
  type AppBoard,
  type AppBoardSize,
} from "./appBoard";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { usePreviewImage } from "../../hooks/usePreviewImage";
import { useAgent } from "../../context/AgentContext";
import styles from "./index.module.less";

const { Text } = Typography;

/** A drag of the cell resize handle, in board grid cells. */
interface ResizeSession {
  appId: string;
  pointerId: number;
  startX: number;
  startY: number;
  startCols: number;
  startRows: number;
  colWidth: number;
  rowHeight: number;
  gap: number;
  /** Columns the board actually has at this width (responsive layouts). */
  maxCols: number;
}

function permissionLabel(
  permission: AppSummary["permission"],
  t: TFunction<"translation", undefined>,
): string {
  if (permission === "edit_structure") return t("apps.editStructure", "Owner");
  if (permission === "edit_data") return t("apps.editData", "Can append rows");
  return t("apps.viewOnly", "View only");
}

function MetaItem({
  icon,
  children,
  title,
}: {
  icon: ReactNode;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 12,
        color: "var(--fn-text-tertiary)",
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "center" }}>
        {icon}
      </span>
      {title ? <Tooltip title={title}>{children}</Tooltip> : children}
    </span>
  );
}

function AppThumb({ app }: { app: AppSummary }) {
  const url = usePreviewImage(
    app.has_preview ? appPreviewPath(app.app_id) : null,
  );
  if (!url) {
    return (
      <div className={styles.thumbEmpty}>
        <AppWindow size={18} strokeWidth={1.4} />
      </div>
    );
  }
  return (
    <img className={styles.thumb} src={url} alt={app.name} draggable={false} />
  );
}

export default function AppsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const timeZone = useServerTimezone();
  const { agents } = useAgent();
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [board, setBoard] = useState<AppBoard>(loadAppBoard);
  const [draftSize, setDraftSize] = useState<{
    appId: string;
    size: AppBoardSize;
  } | null>(null);
  const [resize, setResize] = useState<ResizeSession | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [listOpen, setListOpen] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [restoreForm] = Form.useForm<{ agent_id: string }>();
  const [restoreFiles, setRestoreFiles] = useState<UploadFile[]>([]);
  const [restoreSaving, setRestoreSaving] = useState(false);

  useEffect(() => {
    saveAppBoard(board);
  }, [board]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    appsApi
      .list()
      .then((data) => {
        if (alive) setApps(data.apps);
      })
      .catch((err) => {
        message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [t]);

  // A pinned app that was deleted (or whose access was revoked) leaves the
  // board instead of leaving a broken cell behind.
  useEffect(() => {
    if (loading) return;
    setBoard((prev) =>
      reconcileAppBoard(
        prev,
        apps.map((app) => app.app_id),
      ),
    );
  }, [apps, loading]);

  const boardApps = useMemo(() => {
    const byId = new Map(apps.map((app) => [app.app_id, app]));
    return board.order
      .map((appId) => byId.get(appId))
      .filter((app): app is AppSummary => Boolean(app));
  }, [apps, board.order]);

  // The board can take over the whole window. Escape restores the page, and
  // the last cell leaving the board also ends the mode (nothing left to show).
  useEffect(() => {
    if (!fullscreen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [fullscreen]);

  useEffect(() => {
    if (fullscreen && boardApps.length === 0) setFullscreen(false);
  }, [boardApps.length, fullscreen]);

  const openApp = useCallback(
    (appId: string) => navigate(`/apps/${appId}`),
    [navigate],
  );

  const pinApp = useCallback((appId: string) => {
    setBoard((prev) => addAppToBoard(prev, appId));
  }, []);

  const unpinApp = useCallback((appId: string) => {
    setBoard((prev) => removeAppFromBoard(prev, appId));
  }, []);

  const sizeOf = useCallback(
    (appId: string): AppBoardSize =>
      draftSize?.appId === appId ? draftSize.size : sizeOfApp(board, appId),
    [board, draftSize],
  );

  const startResize = useCallback(
    (
      event: ReactPointerEvent<HTMLButtonElement>,
      appId: string,
      size: AppBoardSize,
    ) => {
      event.preventDefault();
      event.stopPropagation();
      const grid = gridRef.current;
      if (!grid) return;
      // Capture the pointer so the drag still ends when the mouse is released
      // outside the browser window (otherwise the resize never completes).
      try {
        event.currentTarget.setPointerCapture(event.pointerId);
      } catch {
        /* capture is best-effort */
      }
      const computed = window.getComputedStyle(grid);
      const columns = computed.gridTemplateColumns
        .split(/\s+/)
        .map((value) => Number.parseFloat(value))
        .filter((value) => Number.isFinite(value) && value > 0);
      const colWidth = columns[0] ?? 200;
      const rowHeight = Number.parseFloat(computed.gridAutoRows) || 132;
      const gap = Number.parseFloat(computed.columnGap) || 12;
      const span = APP_BOARD_SPANS[size];
      setDraftSize({ appId, size });
      setResize({
        appId,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startCols: span.cols,
        startRows: span.rows,
        colWidth,
        rowHeight,
        gap,
        maxCols: Math.max(1, Math.min(6, columns.length || 6)),
      });
    },
    [],
  );

  useEffect(() => {
    if (!resize) return;
    let moved = false;
    let next: AppBoardSize = DEFAULT_APP_BOARD_SIZE;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = "none";

    const onMove = (event: PointerEvent) => {
      if (event.pointerId !== resize.pointerId) return;
      const dx = event.clientX - resize.startX;
      const dy = event.clientY - resize.startY;
      if (!moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      moved = true;
      const width = Math.max(
        resize.colWidth,
        resize.startCols * resize.colWidth +
          (resize.startCols - 1) * resize.gap +
          dx,
      );
      const height = Math.max(
        resize.rowHeight,
        resize.startRows * resize.rowHeight +
          (resize.startRows - 1) * resize.gap +
          dy,
      );
      // The board is up to six columns wide; narrower viewports cap it lower.
      const cols = Math.min(
        resize.maxCols,
        Math.max(
          1,
          Math.round((width + resize.gap) / (resize.colWidth + resize.gap)),
        ),
      );
      const rows = Math.min(
        6,
        Math.max(
          1,
          Math.round((height + resize.gap) / (resize.rowHeight + resize.gap)),
        ),
      );
      next = snapAppBoardSize(cols, rows);
      setDraftSize({ appId: resize.appId, size: next });
    };

    const onUp = () => {
      if (moved) {
        setBoard((prev) => withAppSize(prev, resize.appId, next));
      }
      setDraftSize(null);
      setResize(null);
      document.body.style.userSelect = previousUserSelect;
    };

    const onCancel = () => {
      setDraftSize(null);
      setResize(null);
      document.body.style.userSelect = previousUserSelect;
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    window.addEventListener("blur", onCancel);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
      window.removeEventListener("blur", onCancel);
      document.body.style.userSelect = previousUserSelect;
    };
  }, [resize]);

  const openRestore = () => {
    setRestoreOpen(true);
    setRestoreFiles([]);
    restoreForm.resetFields();
    const first = agents[0]?.agent_id;
    if (first) restoreForm.setFieldsValue({ agent_id: first });
  };

  const submitRestore = async () => {
    const values = await restoreForm.validateFields();
    const file = restoreFiles[0]?.originFileObj;
    if (!file) {
      message.warning(t("apps.restoreFileRequired"));
      return;
    }
    setRestoreSaving(true);
    try {
      const detail = await appsApi.restore(values.agent_id, file);
      setRestoreOpen(false);
      navigate(`/apps/${detail.app_id}`);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setRestoreSaving(false);
    }
  };

  return (
    <PageShell
      flush
      title={t("apps.title", "Workbench")}
      subtitle={t("apps.subtitle", "MiniApps built by your agents")}
      actions={
        <Space>
          <Button
            icon={<LayoutGrid size={16} />}
            onClick={() => setListOpen(true)}
          >
            {t("apps.all.title", "All MiniApps")}
          </Button>
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            {t("apps.create.button", "Create")}
          </Button>
          <Button onClick={openRestore}>
            {t("apps.restoreBackup", "Restore backup")}
          </Button>
          <Tooltip title={t("apps.board.fullscreen", "Fullscreen")}>
            <Button
              aria-label={t("apps.board.fullscreen", "Fullscreen")}
              icon={<Maximize2 size={16} />}
              disabled={boardApps.length === 0}
              onClick={() => setFullscreen(true)}
            />
          </Tooltip>
        </Space>
      }
    >
      {loading ? (
        <div style={{ padding: 48, textAlign: "center" }}>
          <Spin />
        </div>
      ) : apps.length === 0 ? (
        <Empty
          image={<AppWindow size={48} strokeWidth={1.2} opacity={0.45} />}
          description={
            <Space direction="vertical" size={4}>
              <Text>{t("apps.empty", "No MiniApps yet.")}</Text>
              <Text type="secondary">
                {t(
                  "apps.emptyHint",
                  'Try: "make a reading list with title, author and status"',
                )}
              </Text>
            </Space>
          }
          style={{ padding: "64px 0" }}
        >
          <Space>
            <Button type="primary" onClick={() => setCreateOpen(true)}>
              {t("apps.create.button", "Create")}
            </Button>
            <Button
              icon={<MessageSquareText size={16} />}
              onClick={() => navigate("/chat")}
            >
              {t("apps.openChat", "Open chat")}
            </Button>
          </Space>
        </Empty>
      ) : boardApps.length === 0 ? (
        <div className={styles.boardEmpty}>
          {t(
            "apps.board.empty",
            "No MiniApp on the board yet. Open “All MiniApps” at the top right to pin some.",
          )}
        </div>
      ) : (
        <>
          <div
            ref={gridRef}
            className={[styles.board, fullscreen ? styles.boardFullscreen : ""]
              .filter(Boolean)
              .join(" ")}
            data-fullscreen={fullscreen ? "true" : undefined}
          >
            {boardApps.map((app, index) => (
              <AppWidget
                key={app.app_id}
                app={app}
                size={sizeOf(app.app_id)}
                canMoveUp={index > 0}
                canMoveDown={index < boardApps.length - 1}
                onMoveUp={() =>
                  setBoard((prev) => moveAppInBoard(prev, app.app_id, -1))
                }
                onMoveDown={() =>
                  setBoard((prev) => moveAppInBoard(prev, app.app_id, 1))
                }
                onOpen={() => openApp(app.app_id)}
                onRemove={() => unpinApp(app.app_id)}
                onResizeStart={(event) =>
                  startResize(event, app.app_id, sizeOf(app.app_id))
                }
                onSetSize={(next) =>
                  setBoard((prev) => withAppSize(prev, app.app_id, next))
                }
              />
            ))}
          </div>
          {fullscreen && (
            <Button
              type="text"
              className={styles.fullscreenExit}
              aria-label={t("apps.board.restore", "Restore")}
              icon={<Minimize2 size={16} />}
              style={{ color: "var(--fn-text-tertiary)" }}
              onClick={() => setFullscreen(false)}
            >
              {t("apps.board.restore", "Restore")}
            </Button>
          )}
        </>
      )}

      <Drawer
        title={t("apps.all.title", "All MiniApps")}
        open={listOpen}
        onClose={() => setListOpen(false)}
        width="min(560px, 100vw)"
        styles={{ body: { padding: 12 } }}
      >
        <div className={styles.list}>
          {apps.map((app) => {
            const pinned = isAppOnBoard(board, app.app_id);
            return (
              <div key={app.app_id} className={styles.listRow}>
                <AppThumb app={app} />
                <div className={styles.listMain}>
                  <Space size={8} align="start">
                    <Text strong ellipsis={{ tooltip: app.name }}>
                      {app.name}
                    </Text>
                    {app.permission !== "edit_structure" && (
                      <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                        {permissionLabel(app.permission, t)}
                      </Tag>
                    )}
                  </Space>
                  <Text
                    type="secondary"
                    ellipsis={{ tooltip: app.description }}
                    style={{ fontSize: 12 }}
                  >
                    {app.description || "—"}
                  </Text>
                  <Space size={12} wrap className={styles.listMeta}>
                    <MetaItem
                      icon={<User size={12} color="var(--fn-color-brand)" />}
                    >
                      {t("apps.createdBy", "Created by {{name}}", {
                        name: app.owner_username || "—",
                      })}
                    </MetaItem>
                    <MetaItem
                      icon={<Bot size={12} color="var(--fn-color-info)" />}
                    >
                      {app.agent_name || app.agent_id}
                    </MetaItem>
                    <MetaItem
                      icon={<Table2 size={12} color="var(--fn-color-info)" />}
                    >
                      {t("apps.rows", "{{count}} rows", {
                        count: app.row_count,
                      })}
                    </MetaItem>
                    <MetaItem icon={<Clock size={12} />}>
                      {formatServerDateTime(app.updated_at, timeZone)}
                    </MetaItem>
                  </Space>
                </div>
                <Space>
                  {pinned ? (
                    <Button onClick={() => unpinApp(app.app_id)}>
                      {t("apps.board.remove", "Remove from board")}
                    </Button>
                  ) : (
                    <Button type="primary" onClick={() => pinApp(app.app_id)}>
                      {t("apps.board.add", "Pin to board")}
                    </Button>
                  )}
                  <Button onClick={() => openApp(app.app_id)}>
                    {t("apps.board.open", "Open detail")}
                  </Button>
                </Space>
              </div>
            );
          })}
        </div>
      </Drawer>

      <CreateMiniAppModal
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onCreated={(appId) => {
          setCreateOpen(false);
          navigate(`/apps/${appId}`);
        }}
      />

      <Modal
        title={t("apps.restoreBackup", "Restore backup")}
        open={restoreOpen}
        onCancel={() => setRestoreOpen(false)}
        onOk={() => void submitRestore()}
        okText={t("apps.restoreBackup", "Restore backup")}
        cancelText={t("common.cancel", "Cancel")}
        confirmLoading={restoreSaving}
        destroyOnHidden
      >
        <Form form={restoreForm} layout="vertical">
          <Form.Item
            name="agent_id"
            label={t("apps.agentLabel", "Owning agent")}
            rules={[{ required: true }]}
          >
            <Select
              options={agents.map((agent) => ({
                value: agent.agent_id,
                label: agent.name,
              }))}
            />
          </Form.Item>
          <Form.Item label={t("apps.restoreFile", "Backup file")}>
            <Upload
              accept=".zip"
              maxCount={1}
              fileList={restoreFiles}
              beforeUpload={() => false}
              onChange={({ fileList }) => setRestoreFiles(fileList)}
            >
              <Button>{t("apps.chooseFile", "Choose file")}</Button>
            </Upload>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("apps.restoreHint", "Restores the MiniApp as a new app.")}
            </Text>
          </Form.Item>
        </Form>
      </Modal>
    </PageShell>
  );
}
