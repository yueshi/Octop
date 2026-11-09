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
  Card,
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
  closestCenter,
  DndContext,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  AppWindow,
  Bot,
  Clock,
  GripVertical,
  MessageSquareText,
  MoveDiagonal2,
  Table2,
  User,
} from "lucide-react";
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
import styles from "./index.module.less";
import {
  APP_CARD_SPANS,
  loadAppCardLayout,
  moveAppInOrder,
  nextAppCardSize,
  reconcileAppOrder,
  saveAppCardLayout,
  snapAppCardSize,
  withAppCardSize,
  type AppCardLayout,
  type AppCardSize,
} from "./appCardLayout";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { usePreviewImage } from "../../hooks/usePreviewImage";
import { useAgent } from "../../context/AgentContext";

const { Text, Paragraph } = Typography;

const CARD_SIZE_CLASS: Record<AppCardSize, string> = {
  standard: styles.sizeStandard,
  tall: styles.sizeTall,
  large: styles.sizeLarge,
};

const CARD_SIZE_LABEL_KEY: Record<AppCardSize, string> = {
  standard: "apps.layout.sizeStandard",
  tall: "apps.layout.sizeTall",
  large: "apps.layout.sizeLarge",
};

const CARD_BODY_STYLE = {
  padding: 0,
  display: "flex",
  flexDirection: "column" as const,
  flex: 1,
  minHeight: 0,
};

interface ResizeSession {
  appId: string;
  pointerId: number;
  startX: number;
  startY: number;
  startWidth: number;
  startHeight: number;
  startSize: AppCardSize;
  colWidth: number;
  rowHeight: number;
  gap: number;
}

function permissionLabel(
  permission: AppSummary["permission"],
  t: (key: string) => string,
): string {
  if (permission === "edit_structure") return t("apps.editStructure");
  if (permission === "edit_data") return t("apps.editData");
  return t("apps.viewOnly");
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

function AppPreview({
  app,
  className,
}: {
  app: AppSummary;
  className?: string;
}) {
  const url = usePreviewImage(
    app.has_preview ? appPreviewPath(app.app_id) : null,
  );
  return (
    <div className={className}>
      {url ? (
        <img
          className={styles.previewImage}
          src={url}
          alt={app.name}
          draggable={false}
        />
      ) : (
        <div className={styles.previewEmpty}>
          <AppWindow size={30} strokeWidth={1.2} />
        </div>
      )}
    </div>
  );
}

function AppCardTile({
  app,
  size,
  timeZone,
  onOpen,
  onResizeStart,
  onCycleSize,
}: {
  app: AppSummary;
  size: AppCardSize;
  timeZone: string;
  onOpen: (appId: string) => void;
  onResizeStart: (
    event: ReactPointerEvent<HTMLButtonElement>,
    appId: string,
    size: AppCardSize,
  ) => void;
  onCycleSize: (appId: string, size: AppCardSize) => void;
}) {
  const { t } = useTranslation();
  const {
    attributes,
    listeners,
    setNodeRef: setDragNodeRef,
    transform,
    isDragging,
  } = useDraggable({ id: app.app_id });
  const { setNodeRef: setDropNodeRef, isOver } = useDroppable({
    id: app.app_id,
  });
  const setNodeRef = useCallback(
    (node: HTMLElement | null) => {
      setDragNodeRef(node);
      setDropNodeRef(node);
    },
    [setDragNodeRef, setDropNodeRef],
  );
  const sizeLabel = t(CARD_SIZE_LABEL_KEY[size]);

  return (
    <div
      ref={setNodeRef}
      {...attributes}
      {...listeners}
      role="group"
      tabIndex={0}
      aria-label={`${app.name} · ${t("apps.layout.dragHint")}`}
      className={[
        styles.tile,
        CARD_SIZE_CLASS[size],
        isDragging ? styles.tileDragging : "",
        isOver ? styles.tileOver : "",
      ]
        .filter(Boolean)
        .join(" ")}
      style={
        transform
          ? {
              transform: `translate3d(${transform.x}px, ${transform.y}px, 0)`,
              zIndex: 5,
            }
          : undefined
      }
      onClick={() => onOpen(app.app_id)}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(app.app_id);
        }
      }}
    >
      <span className={styles.dragHandle} aria-hidden>
        <GripVertical size={14} />
      </span>
      <button
        type="button"
        className={styles.resizeHandle}
        title={`${t("apps.layout.resizeHint")} · ${sizeLabel}`}
        aria-label={`${t("apps.layout.resizeHint")} · ${sizeLabel}`}
        onPointerDown={(event) => onResizeStart(event, app.app_id, size)}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          event.stopPropagation();
          onCycleSize(app.app_id, size);
        }}
      >
        <MoveDiagonal2 size={13} />
      </button>
      <Card
        hoverable
        className={styles.card}
        styles={{ body: CARD_BODY_STYLE }}
        style={{
          overflow: "hidden",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          borderColor: "var(--fn-color-brand-border)",
        }}
      >
        <AppPreview app={app} className={styles.preview} />
        <div className={styles.body}>
          <Space
            align="start"
            style={{ width: "100%", justifyContent: "space-between" }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <Text
                strong
                ellipsis={{ tooltip: app.name }}
                style={{ fontSize: 15 }}
              >
                {app.name}
              </Text>
            </div>
            <Space size={4} style={{ marginInlineEnd: 0, flex: "none" }}>
              {!app.has_html && (
                <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                  {t("apps.noInterface")}
                </Tag>
              )}
              {app.permission !== "edit_structure" && (
                <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                  {permissionLabel(app.permission, t)}
                </Tag>
              )}
            </Space>
          </Space>
          <Paragraph
            type="secondary"
            ellipsis={{ rows: 2 }}
            style={{ margin: "6px 0 0", minHeight: 44 }}
          >
            {app.description || "—"}
          </Paragraph>
        </div>
        <div className={styles.metaWrap}>
          <div className={styles.meta}>
            <Space size={12} wrap style={{ marginBottom: 4 }}>
              <MetaItem icon={<User size={12} color="var(--fn-color-brand)" />}>
                {t("apps.createdBy", {
                  name: app.owner_username || "—",
                })}
              </MetaItem>
              <MetaItem icon={<Bot size={12} color="var(--fn-color-info)" />}>
                {app.agent_name || app.agent_id}
              </MetaItem>
            </Space>
            <Space size={12} wrap>
              <MetaItem
                icon={<Table2 size={12} color="var(--fn-color-info)" />}
              >
                {t("apps.rows", { count: app.row_count })}
              </MetaItem>
              <MetaItem icon={<Clock size={12} />}>
                {formatServerDateTime(app.updated_at, timeZone)}
              </MetaItem>
            </Space>
          </div>
        </div>
      </Card>
    </div>
  );
}

export default function AppsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const timeZone = useServerTimezone();
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const { agents } = useAgent();
  const [createOpen, setCreateOpen] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [restoreForm] = Form.useForm<{ agent_id: string }>();
  const [restoreFiles, setRestoreFiles] = useState<UploadFile[]>([]);
  const [restoreSaving, setRestoreSaving] = useState(false);

  const [layout, setLayout] = useState<AppCardLayout>(loadAppCardLayout);
  const [draftSize, setDraftSize] = useState<{
    appId: string;
    size: AppCardSize;
  } | null>(null);
  const [resize, setResize] = useState<ResizeSession | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const suppressClickRef = useRef(false);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
  );

  const orderedApps = useMemo(() => {
    const byId = new Map(apps.map((app) => [app.app_id, app]));
    return reconcileAppOrder(
      apps.map((app) => app.app_id),
      layout.order,
    )
      .map((appId) => byId.get(appId))
      .filter((app): app is AppSummary => Boolean(app));
  }, [apps, layout.order]);

  useEffect(() => {
    saveAppCardLayout(layout);
  }, [layout]);

  const sizeOf = useCallback(
    (appId: string): AppCardSize =>
      draftSize?.appId === appId
        ? draftSize.size
        : layout.sizes[appId] ?? "standard",
    [draftSize, layout.sizes],
  );

  const openApp = useCallback(
    (appId: string) => {
      // A drag or resize ends with a click; do not navigate in that case.
      if (suppressClickRef.current) {
        suppressClickRef.current = false;
        return;
      }
      navigate(`/apps/${appId}`);
    },
    [navigate],
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const activeId = String(event.active.id);
      const overId = event.over ? String(event.over.id) : null;
      if (!overId || activeId === overId) return;
      setLayout((prev) => {
        const ids = reconcileAppOrder(
          apps.map((app) => app.app_id),
          prev.order,
        );
        const from = ids.indexOf(activeId);
        const to = ids.indexOf(overId);
        if (from < 0 || to < 0) return prev;
        return { ...prev, order: moveAppInOrder(ids, from, to) };
      });
    },
    [apps],
  );

  const cycleSize = useCallback((appId: string, size: AppCardSize) => {
    setLayout((prev) => withAppCardSize(prev, appId, nextAppCardSize(size)));
  }, []);

  const startResize = useCallback(
    (
      event: ReactPointerEvent<HTMLButtonElement>,
      appId: string,
      size: AppCardSize,
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
      const colWidth = columns[0] ?? 280;
      const rowHeight = Number.parseFloat(computed.gridAutoRows) || 260;
      const gap = Number.parseFloat(computed.columnGap) || 16;
      const span = APP_CARD_SPANS[size];
      suppressClickRef.current = true;
      setDraftSize({ appId, size });
      setResize({
        appId,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startWidth: span.cols * colWidth + (span.cols - 1) * gap,
        startHeight: span.rows * rowHeight + (span.rows - 1) * gap,
        startSize: size,
        colWidth,
        rowHeight,
        gap,
      });
    },
    [],
  );

  useEffect(() => {
    if (!resize) return;
    let moved = false;
    let next = resize.startSize;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = "none";
    const releaseClick = () => {
      window.setTimeout(() => {
        suppressClickRef.current = false;
      }, 0);
    };

    const onMove = (event: PointerEvent) => {
      if (event.pointerId !== resize.pointerId) return;
      const dx = event.clientX - resize.startX;
      const dy = event.clientY - resize.startY;
      if (!moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      moved = true;
      const width = Math.max(resize.colWidth, resize.startWidth + dx);
      const height = Math.max(resize.rowHeight, resize.startHeight + dy);
      const cols = Math.min(
        2,
        Math.max(
          1,
          Math.round((width + resize.gap) / (resize.colWidth + resize.gap)),
        ),
      );
      const rows = Math.min(
        2,
        Math.max(
          1,
          Math.round((height + resize.gap) / (resize.rowHeight + resize.gap)),
        ),
      );
      next = snapAppCardSize(cols, rows);
      setDraftSize({ appId: resize.appId, size: next });
    };

    const onUp = (event: PointerEvent) => {
      if (event.pointerId !== resize.pointerId) return;
      const finalSize = moved ? next : nextAppCardSize(resize.startSize);
      setLayout((prev) => withAppCardSize(prev, resize.appId, finalSize));
      setDraftSize(null);
      setResize(null);
      releaseClick();
    };

    const onCancel = () => {
      setDraftSize(null);
      setResize(null);
      releaseClick();
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
      releaseClick();
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

  return (
    <PageShell
      title={t("apps.title")}
      subtitle={t("apps.subtitle")}
      actions={
        <Space>
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            {t("apps.create.button")}
          </Button>
          <Button onClick={openRestore}>{t("apps.restoreBackup")}</Button>
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
              <Text>{t("apps.empty")}</Text>
              <Text type="secondary">{t("apps.emptyHint")}</Text>
            </Space>
          }
          style={{ padding: "64px 0" }}
        >
          <Space>
            <Button type="primary" onClick={() => setCreateOpen(true)}>
              {t("apps.create.button")}
            </Button>
            <Button
              icon={<MessageSquareText size={16} />}
              onClick={() => navigate("/chat")}
            >
              {t("apps.openChat")}
            </Button>
          </Space>
        </Empty>
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragStart={() => {
            suppressClickRef.current = true;
          }}
          onDragEnd={(event) => {
            handleDragEnd(event);
            window.setTimeout(() => {
              suppressClickRef.current = false;
            }, 0);
          }}
          onDragCancel={() => {
            window.setTimeout(() => {
              suppressClickRef.current = false;
            }, 0);
          }}
        >
          <div ref={gridRef} className={styles.grid}>
            {orderedApps.map((app) => (
              <AppCardTile
                key={app.app_id}
                app={app}
                size={sizeOf(app.app_id)}
                timeZone={timeZone}
                onOpen={openApp}
                onResizeStart={startResize}
                onCycleSize={cycleSize}
              />
            ))}
          </div>
        </DndContext>
      )}

      <CreateMiniAppModal
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onCreated={(appId) => {
          setCreateOpen(false);
          navigate(`/apps/${appId}`);
        }}
      />

      <Modal
        title={t("apps.restoreBackup")}
        open={restoreOpen}
        onCancel={() => setRestoreOpen(false)}
        onOk={() => void submitRestore()}
        okText={t("apps.restoreBackup")}
        cancelText={t("common.cancel")}
        confirmLoading={restoreSaving}
        destroyOnHidden
      >
        <Form form={restoreForm} layout="vertical">
          <Form.Item
            name="agent_id"
            label={t("apps.agentLabel")}
            rules={[{ required: true }]}
          >
            <Select
              options={agents.map((agent) => ({
                value: agent.agent_id,
                label: agent.name,
              }))}
            />
          </Form.Item>
          <Form.Item label={t("apps.restoreFile")}>
            <Upload
              accept=".zip"
              maxCount={1}
              fileList={restoreFiles}
              beforeUpload={() => false}
              onChange={({ fileList }) => setRestoreFiles(fileList)}
            >
              <Button>{t("apps.chooseFile")}</Button>
            </Upload>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("apps.restoreHint")}
            </Text>
          </Form.Item>
        </Form>
      </Modal>
    </PageShell>
  );
}
