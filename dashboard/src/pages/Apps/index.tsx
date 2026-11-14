import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Button,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Input,
  Modal,
  Segmented,
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
  ChevronLeft,
  Clock,
  Grid2x2,
  LayoutGrid,
  Maximize2,
  MessageSquareText,
  MoreHorizontal,
  Plus,
  Table2,
  Undo2,
  User,
} from "lucide-react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { message } from "../../utils/antdMessage";
import { fullscreenContainer } from "../../utils/overlayContainer";

import {
  appsApi,
  appPreviewPath,
  type AppSummary,
} from "../../api/modules/apps";
import {
  boardsApi,
  type BoardDetail,
  type BoardItem,
  type BoardSummary,
} from "../../api/modules/boards";
import PageShell from "../../layouts/PageShell";
import CreateMiniAppModal from "./CreateMiniAppModal";
import BoardGallery from "./BoardGallery";
import BoardGrid from "./BoardGrid";
import DesktopGrid from "./DesktopGrid";
import {
  addBoardItem,
  autoArrangeBoardLayout,
  placeBoardItem,
} from "./boardLayout";
import {
  APP_BOARD_SPANS,
  DEFAULT_APP_BOARD_SIZE,
  clearAppBoard,
  loadAppBoard,
  type AppBoardSize,
} from "./appBoard";
import { apiErrorMessage, parseApiError } from "../../utils/apiError";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { usePreviewImage } from "../../hooks/usePreviewImage";
import { useAgent } from "../../context/AgentContext";
import styles from "./index.module.less";

const { Text } = Typography;
type ViewMode = "board" | "desktop";

type BoardDialog = { mode: "create" } | { mode: "rename"; board: BoardSummary };

function legacyBoardItems(
  order: string[],
  sizes: Record<string, AppBoardSize>,
): BoardItem[] {
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  return order.map((appId) => {
    const span = APP_BOARD_SPANS[sizes[appId] ?? DEFAULT_APP_BOARD_SIZE];
    const w = Math.min(12, span.cols * 2);
    const h = Math.min(12, span.rows);
    if (x + w > 12) {
      x = 0;
      y += rowHeight;
      rowHeight = 0;
    }
    const item = { app_id: appId, x, y, w, h };
    x += w;
    rowHeight = Math.max(rowHeight, h);
    return item;
  });
}

function sameLayout(left: BoardItem[], right: BoardItem[]): boolean {
  if (left.length !== right.length) return false;
  const signature = (item: BoardItem) =>
    `${item.app_id}:${item.x}:${item.y}:${item.w}:${item.h}:${
      item.locked ? 1 : 0
    }`;
  return (
    left.map(signature).sort().join("|") ===
    right.map(signature).sort().join("|")
  );
}

function permissionLabel(
  permission: AppSummary["permission"],
  t: TFunction<"translation", undefined>,
): string {
  if (permission === "edit_structure") return t("apps.editStructure", "Owner");
  if (permission === "edit_data") return t("apps.editData", "Can append rows");
  return t("apps.viewOnly", "View only");
}

function MiniAppThumb({ app }: { app: AppSummary }) {
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

function MetaItem({
  icon,
  children,
}: {
  icon: ReactNode;
  children: ReactNode;
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
      {children}
    </span>
  );
}

export default function AppsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { boardId: routeBoardId } = useParams();
  const timeZone = useServerTimezone();
  const { agents } = useAgent();

  const [apps, setApps] = useState<AppSummary[]>([]);
  const [appsLoading, setAppsLoading] = useState(true);
  const [boards, setBoards] = useState<BoardSummary[]>([]);
  const [boardsLoading, setBoardsLoading] = useState(true);
  const [activeId, setActiveId] = useState<string | null>(routeBoardId ?? null);
  const [board, setBoard] = useState<BoardDetail | null>(null);
  const [items, setItems] = useState<BoardItem[]>([]);
  const [detailLoading, setDetailLoading] = useState<boolean>(
    Boolean(routeBoardId),
  );
  const [bootReady, setBootReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const [undoDepth, setUndoDepth] = useState(0);
  const [boardPreviews, setBoardPreviews] = useState<
    Record<string, BoardDetail>
  >({});
  const [viewMode, setViewMode] = useState<ViewMode>("board");
  const [autoFullscreen, setAutoFullscreen] = useState(false);
  const changeViewMode = useCallback((next: ViewMode) => {
    if (next === "desktop") setAutoFullscreen(true);
    setViewMode(next);
  }, []);

  // The Workbench nav always lands on the board gallery (never the desktop or
  // the last board). A new location key also covers clicking the active nav
  // item while already on /apps; a board URL is always a board view.
  useEffect(() => {
    if (location.pathname !== "/apps" && !routeBoardId) return;
    setViewMode("board");
    setAutoFullscreen(false);
  }, [location.key, location.pathname, routeBoardId]);

  const boardRef = useRef<BoardDetail | null>(null);
  const itemsRef = useRef<BoardItem[]>([]);
  /** Snapshots for the last layout edits, newest last (max 20). */
  const historyRef = useRef<BoardItem[][]>([]);
  /** Apps the caller can currently open; stale board cells are dropped. */
  const availableAppsRef = useRef<Set<string>>(new Set());
  const pendingRef = useRef<BoardItem[] | null>(null);
  const savingRef = useRef(false);
  const bootedRef = useRef(false);

  const [catalogOpen, setCatalogOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [restoreFiles, setRestoreFiles] = useState<UploadFile[]>([]);
  const [restoreSaving, setRestoreSaving] = useState(false);
  const [restoreForm] = Form.useForm<{ agent_id: string }>();
  const [dialog, setDialog] = useState<BoardDialog | null>(null);
  const [dialogSaving, setDialogSaving] = useState(false);
  const [boardForm] = Form.useForm<{ name: string; description?: string }>();

  const applyBoard = useCallback((detail: BoardDetail) => {
    const items = detail.layout.items.filter((item) =>
      availableAppsRef.current.has(item.app_id),
    );
    const clean: BoardDetail = {
      ...detail,
      layout: { ...detail.layout, items },
    };
    boardRef.current = clean;
    setBoard(clean);
    itemsRef.current = items;
    setItems(items);
    setBoardPreviews((prev) => ({ ...prev, [detail.board_id]: clean }));
    setBoards((prev) =>
      prev.map((item) =>
        item.board_id === detail.board_id ? { ...detail } : item,
      ),
    );
  }, []);

  const flush = useCallback(async () => {
    const current = boardRef.current;
    if (!current || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      for (;;) {
        // Re-read on every pass: the user may switch boards while a save is
        // in flight, and the queued items then belong to the new board.
        const boardNow = boardRef.current;
        if (!boardNow) break;
        const next = pendingRef.current;
        if (!next) break;
        pendingRef.current = null;
        if (sameLayout(next, boardNow.layout.items)) continue;
        try {
          const saved = await boardsApi.setLayout(boardNow.board_id, {
            version: boardNow.version,
            items: next,
          });
          // The user may have switched boards while the save was in flight.
          if (boardRef.current?.board_id === boardNow.board_id) {
            applyBoard(saved);
          }
        } catch (err) {
          pendingRef.current = null;
          const parsed = parseApiError(err);
          if (parsed?.code === "BOARD_CONFLICT") {
            message.warning(
              t(
                "apps.board.conflict",
                "This board changed elsewhere; reloaded the latest layout.",
              ),
            );
            try {
              const latest = await boardsApi.get(boardNow.board_id);
              if (boardRef.current?.board_id === boardNow.board_id) {
                applyBoard(latest);
              }
            } catch {
              /* the next reload will retry */
            }
          } else {
            message.error(
              apiErrorMessage(
                err,
                t("apps.board.saveFailed", "Could not save the board"),
                t,
              ),
            );
          }
          break;
        }
      }
    } finally {
      savingRef.current = false;
      setSaving(false);
      // An edit queued between the last loop check and this point still
      // needs a save.
      if (pendingRef.current) void flush();
    }
  }, [applyBoard, t]);

  const queueSave = useCallback(
    (next: BoardItem[]) => {
      // A deleted app (or one whose access was revoked) must not block the
      // rest of the layout from saving — the API rejects unknown app ids.
      pendingRef.current = next.filter((item) =>
        availableAppsRef.current.has(item.app_id),
      );
      void flush();
    },
    [flush],
  );

  // Boot: load the catalog and boards, create the first board, and move any
  // browser-local pins from the pre-server workbench onto it.
  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;
    let alive = true;
    (async () => {
      try {
        const [appList, boardList] = await Promise.all([
          appsApi.list(),
          boardsApi.list(),
        ]);
        if (!alive) return;
        availableAppsRef.current = new Set(
          appList.apps.map((app) => app.app_id),
        );
        setApps(appList.apps);
        let list = boardList;
        if (list.length === 0) {
          const created = await boardsApi.create({
            name: t("apps.board.defaultName", "My board"),
          });
          if (!alive) return;
          list = [created];
          const legacy = loadAppBoard();
          const available = new Set(appList.apps.map((app) => app.app_id));
          const pinned = legacy.order.filter((appId) => available.has(appId));
          if (pinned.length > 0) {
            try {
              const saved = await boardsApi.setLayout(created.board_id, {
                version: created.version,
                items: legacyBoardItems(pinned, legacy.sizes),
              });
              clearAppBoard();
              list = [saved];
            } catch {
              /* an empty board is still usable; pins can be re-added */
            }
          }
        }
        if (!alive) return;
        setBoards(list);
        // Landing on /apps shows the gallery; only an explicit board route
        // (or a card click) selects a board.
      } catch (err) {
        if (alive) {
          message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
        }
      } finally {
        if (alive) {
          setAppsLoading(false);
          setBoardsLoading(false);
          setBootReady(true);
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [t]);

  // Each board has a stable URL so multiple tabs (or a shared link) can open
  // different boards side by side. /apps itself is the gallery.
  useEffect(() => {
    if (routeBoardId) {
      if (routeBoardId !== activeId) setActiveId(routeBoardId);
      return;
    }
    if (activeId !== null) {
      setActiveId(null);
      boardRef.current = null;
      setBoard(null);
      setItems([]);
      setDetailLoading(false);
    }
  }, [routeBoardId, activeId]);

  const selectBoard = useCallback(
    (value: string) => {
      setActiveId(value);
      navigate(`/apps/boards/${encodeURIComponent(value)}`);
    },
    [navigate],
  );

  const openBoardGallery = useCallback(() => {
    navigate("/apps");
  }, [navigate]);

  const galleryVisible = viewMode === "board" && !routeBoardId;
  const boardDetailVisible = viewMode === "board" && Boolean(routeBoardId);

  // Gallery thumbnails need each board's real layout. Fetch them lazily once
  // the gallery is visible; failures leave a plain empty canvas on the card.
  useEffect(() => {
    if (!bootReady || !galleryVisible) return;
    const pending = boards.filter((item) => !boardPreviews[item.board_id]);
    if (pending.length === 0) return;
    let alive = true;
    void Promise.all(
      pending.map(async (item) => {
        try {
          return [item.board_id, await boardsApi.get(item.board_id)] as const;
        } catch {
          return null;
        }
      }),
    ).then((entries) => {
      if (!alive) return;
      const loaded: Record<string, BoardDetail> = {};
      for (const entry of entries) {
        if (entry) loaded[entry[0]] = entry[1];
      }
      if (Object.keys(loaded).length > 0) {
        setBoardPreviews((prev) => ({ ...prev, ...loaded }));
      }
    });
    return () => {
      alive = false;
    };
  }, [boardPreviews, boards, bootReady, galleryVisible]);

  // Load the selected board. A board just created is already in `boardRef`,
  // so skip the redundant fetch.
  useEffect(() => {
    // Wait for the catalog first: applyBoard drops cells whose app is not
    // loaded yet, and a board URL can resolve before the app list arrives.
    if (!bootReady) return;
    pendingRef.current = null;
    historyRef.current = [];
    setUndoDepth(0);
    if (!activeId) {
      boardRef.current = null;
      setBoard(null);
      setItems([]);
      setDetailLoading(false);
      return;
    }
    if (boardRef.current?.board_id === activeId) {
      setDetailLoading(false);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    boardsApi
      .get(activeId)
      .then((detail) => {
        if (alive) applyBoard(detail);
      })
      .catch((err) => {
        if (!alive) return;
        message.error(
          apiErrorMessage(
            err,
            t("apps.board.loadFailed", "Could not load this board"),
            t,
          ),
        );
        boardRef.current = null;
        setBoard(null);
        setItems([]);
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [activeId, applyBoard, bootReady, t]);

  const openApp = useCallback(
    (appId: string) => navigate(`/apps/${appId}`),
    [navigate],
  );

  const composeMessage = useCallback(
    (agentId: string, text: string) => {
      if (!text.trim()) return;
      navigate(`/chat/${encodeURIComponent(agentId)}`, {
        state: { prefillInput: text },
      });
    },
    [navigate],
  );

  const snapshot = useCallback(() => {
    historyRef.current = [
      ...historyRef.current.slice(-19),
      itemsRef.current.map((item) => ({ ...item })),
    ];
    setUndoDepth(historyRef.current.length);
  }, []);

  const undoLayout = useCallback(() => {
    const previous = historyRef.current.pop();
    setUndoDepth(historyRef.current.length);
    if (!previous) return;
    itemsRef.current = previous;
    setItems(previous);
    queueSave(previous);
    message.success(t("apps.board.undoDone", "Undid the last layout change"));
  }, [queueSave, t]);

  const pinApp = useCallback(
    (appId: string) => {
      const current = boardRef.current;
      if (!current) return;
      if (itemsRef.current.some((item) => item.app_id === appId)) return;
      snapshot();
      const next = addBoardItem(itemsRef.current, appId);
      itemsRef.current = next;
      setItems(next);
      queueSave(next);
    },
    [queueSave, snapshot],
  );

  const unpinApp = useCallback(
    (appId: string) => {
      if (!boardRef.current) return;
      snapshot();
      const next = itemsRef.current.filter((item) => item.app_id !== appId);
      itemsRef.current = next;
      setItems(next);
      queueSave(next);
    },
    [queueSave, snapshot],
  );

  const onGridLayoutChange = useCallback((next: BoardItem[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  const onGridCommit = useCallback(
    (next: BoardItem[]) => {
      itemsRef.current = next;
      setItems(next);
      queueSave(next);
    },
    [queueSave],
  );

  const applySizePreset = useCallback(
    (appId: string, w: number, h: number) => {
      if (!boardRef.current) return;
      const target = itemsRef.current.find((item) => item.app_id === appId);
      if (!target || target.locked) return;
      snapshot();
      const next = placeBoardItem(itemsRef.current, appId, w, h);
      itemsRef.current = next;
      setItems(next);
      queueSave(next);
    },
    [queueSave, snapshot],
  );

  const toggleAppLock = useCallback(
    (appId: string) => {
      if (!boardRef.current) return;
      snapshot();
      const next = itemsRef.current.map((item) =>
        item.app_id === appId
          ? { ...item, locked: item.locked ? undefined : true }
          : item,
      );
      itemsRef.current = next;
      setItems(next);
      queueSave(next);
    },
    [queueSave, snapshot],
  );

  const arrangeBoard = useCallback(() => {
    if (!boardRef.current || itemsRef.current.length === 0) return;
    snapshot();
    const next = autoArrangeBoardLayout(itemsRef.current);
    itemsRef.current = next;
    setItems(next);
    queueSave(next);
    message.success(
      t("apps.board.arrangeDone", "Board arranged; undo it from the toolbar"),
    );
  }, [queueSave, snapshot, t]);

  const openCreateBoard = () => {
    boardForm.resetFields();
    setDialog({ mode: "create" });
  };

  const openRenameBoard = () => {
    const current = boardRef.current;
    if (!current) return;
    boardForm.setFieldsValue({
      name: current.name,
      description: current.description,
    });
    setDialog({ mode: "rename", board: current });
  };

  const submitBoardDialog = async () => {
    if (!dialog) return;
    const values = await boardForm.validateFields();
    setDialogSaving(true);
    try {
      if (dialog.mode === "create") {
        const created = await boardsApi.create({
          name: values.name.trim(),
          description: (values.description ?? "").trim(),
        });
        setBoards((prev) => [created, ...prev]);
        setBoardPreviews((prev) => ({
          ...prev,
          [created.board_id]: created,
        }));
        setActiveId(created.board_id);
        applyBoard(created);
        navigate(`/apps/boards/${encodeURIComponent(created.board_id)}`);
      } else {
        const updated = await boardsApi.update(dialog.board.board_id, {
          name: values.name.trim(),
        });
        applyBoard(updated);
      }
      setDialog(null);
      boardForm.resetFields();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("apps.board.saveFailed", "Could not save the board"),
          t,
        ),
      );
    } finally {
      setDialogSaving(false);
    }
  };

  const confirmDeleteBoard = () => {
    const current = boardRef.current;
    if (!current) return;
    Modal.confirm({
      getContainer: fullscreenContainer,
      title: t("apps.board.delete", "Delete board"),
      content: t(
        "apps.board.deleteConfirm",
        "Delete “{{name}}”? The MiniApps on it are kept.",
        { name: current.name },
      ),
      okText: t("common.delete", "Delete"),
      okButtonProps: { danger: true },
      cancelText: t("common.cancel", "Cancel"),
      onOk: async () => {
        try {
          await boardsApi.remove(current.board_id);
          const rest = boards.filter(
            (item) => item.board_id !== current.board_id,
          );
          setBoards(rest);
          setBoardPreviews((prev) => {
            const next = { ...prev };
            delete next[current.board_id];
            return next;
          });
          const nextId =
            activeId === current.board_id
              ? rest[0]?.board_id ?? null
              : activeId;
          setActiveId(nextId);
          navigate(
            nextId ? `/apps/boards/${encodeURIComponent(nextId)}` : "/apps",
            { replace: true },
          );
        } catch (err) {
          message.error(
            apiErrorMessage(
              err,
              t("apps.board.deleteFailed", "Could not delete the board"),
              t,
            ),
          );
          throw err;
        }
      },
    });
  };

  const openPresent = () => {
    const current = boardRef.current;
    if (!current || itemsRef.current.length === 0) return;
    window.open(
      `/apps/boards/${encodeURIComponent(current.board_id)}/view`,
      "_blank",
      "noopener,noreferrer",
    );
  };

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

  const boardMenu = {
    items: [
      {
        key: "rename",
        label: t("apps.board.rename", "Rename board"),
        disabled: !board,
      },
      {
        key: "restore",
        label: t("apps.restoreBackup", "Restore backup"),
      },
      { type: "divider" as const },
      {
        key: "delete",
        label: t("apps.board.delete", "Delete board"),
        danger: true,
        disabled: !board,
      },
    ],
    onClick: ({ key }: { key: string }) => {
      if (key === "rename") openRenameBoard();
      else if (key === "delete") confirmDeleteBoard();
      else if (key === "restore") openRestore();
    },
  };

  const loading =
    appsLoading || (viewMode === "board" && boardsLoading && !galleryVisible);
  const pinned = new Set(items.map((item) => item.app_id));

  return (
    <PageShell
      flush
      fill
      title={t("apps.title", "Workbench")}
      subtitle={t("apps.subtitle", "MiniApps built by your agents")}
      actions={
        <Space wrap>
          <Segmented
            value={viewMode}
            onChange={(value) => changeViewMode(value as ViewMode)}
            options={[
              {
                label: t("apps.viewMode.board", "Board"),
                value: "board",
              },
              {
                label: t("apps.viewMode.desktop", "Desktop"),
                value: "desktop",
              },
            ]}
          />
          {boardDetailVisible && (
            <Tooltip title={t("apps.board.allBoards", "All boards")}>
              <Button
                aria-label={t("apps.board.allBoards", "All boards")}
                icon={<ChevronLeft size={16} />}
                onClick={openBoardGallery}
              />
            </Tooltip>
          )}
          {viewMode === "board" && boardDetailVisible && boards.length > 0 && (
            <Select
              aria-label={t("apps.board.select", "Select board")}
              value={activeId ?? undefined}
              style={{ minWidth: 160, maxWidth: 240 }}
              options={boards.map((item) => ({
                value: item.board_id,
                label: item.name,
              }))}
              onChange={selectBoard}
            />
          )}
          {viewMode === "board" && (
            <Tooltip title={t("apps.board.new", "New board")}>
              <Button
                aria-label={t("apps.board.new", "New board")}
                icon={<Plus size={16} />}
                onClick={openCreateBoard}
              />
            </Tooltip>
          )}
          {viewMode === "board" && boardDetailVisible && (
            <Dropdown menu={boardMenu} trigger={["click"]}>
              <Button
                aria-label={t("apps.board.menu", "Board actions")}
                icon={<MoreHorizontal size={16} />}
              />
            </Dropdown>
          )}
          {viewMode === "board" && boardDetailVisible && (
            <Button
              icon={<LayoutGrid size={16} />}
              onClick={() => setCatalogOpen(true)}
            >
              {t("apps.all.title", "All MiniApps")}
            </Button>
          )}
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            {t("apps.create.button", "Create")}
          </Button>
          {viewMode === "board" && boardDetailVisible && (
            <>
              <Tooltip
                title={t(
                  "apps.board.arrangeHint",
                  "Pack cells at their current sizes; undo from the toolbar",
                )}
              >
                <Button
                  icon={<Grid2x2 size={16} />}
                  disabled={items.length === 0}
                  onClick={arrangeBoard}
                >
                  {t("apps.board.arrange", "Auto-arrange")}
                </Button>
              </Tooltip>
              <Tooltip
                title={t("apps.board.undo", "Undo the last layout change")}
              >
                <Button
                  aria-label={t(
                    "apps.board.undo",
                    "Undo the last layout change",
                  )}
                  icon={<Undo2 size={16} />}
                  disabled={undoDepth === 0}
                  onClick={undoLayout}
                />
              </Tooltip>
              {saving && (
                <Text className={styles.savingHint}>
                  {t("apps.board.saving", "Saving…")}
                </Text>
              )}
              <Tooltip title={t("apps.board.presentHint", "Open in a new tab")}>
                <Button
                  icon={<Maximize2 size={16} />}
                  disabled={items.length === 0}
                  onClick={openPresent}
                >
                  {t("apps.board.present", "Present")}
                </Button>
              </Tooltip>
            </>
          )}
        </Space>
      }
    >
      {loading ? (
        <div className={styles.boardState}>
          <Spin />
        </div>
      ) : viewMode === "desktop" ? (
        <DesktopGrid apps={apps} autoFullscreen={autoFullscreen} />
      ) : galleryVisible ? (
        <BoardGallery
          boards={boards}
          apps={apps}
          previews={boardPreviews}
          loading={boardsLoading}
          timeZone={timeZone}
          onOpen={selectBoard}
          onCreate={openCreateBoard}
        />
      ) : apps.length === 0 ? (
        <div className={styles.boardState}>
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
        </div>
      ) : !board ? (
        detailLoading ? (
          <div className={styles.boardState}>
            <Spin />
          </div>
        ) : (
          <div className={styles.boardEmpty}>
            <Space direction="vertical" size={12}>
              <Text>{t("apps.board.none", "No board yet.")}</Text>
              <Button type="primary" onClick={openCreateBoard}>
                {t("apps.board.new", "New board")}
              </Button>
            </Space>
          </div>
        )
      ) : items.length === 0 ? (
        <div className={styles.boardEmpty}>
          {t(
            "apps.board.empty",
            "No MiniApp on the board yet. Open “All MiniApps” at the top right to pin some.",
          )}
        </div>
      ) : (
        <BoardGrid
          items={items}
          apps={apps}
          editable
          onLayoutChange={onGridLayoutChange}
          onCommit={onGridCommit}
          onEditStart={snapshot}
          onResizeApp={applySizePreset}
          onToggleLock={toggleAppLock}
          onOpenApp={openApp}
          onRemoveApp={unpinApp}
          onComposeMessage={composeMessage}
        />
      )}

      <Drawer
        title={t("apps.all.title", "All MiniApps")}
        open={catalogOpen}
        onClose={() => setCatalogOpen(false)}
        width="min(560px, 100vw)"
        styles={{ body: { padding: 12 } }}
      >
        <div className={styles.list}>
          {apps.map((app) => {
            const onBoard = pinned.has(app.app_id);
            return (
              <div key={app.app_id} className={styles.listRow}>
                <MiniAppThumb app={app} />
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
                  {onBoard ? (
                    <Button onClick={() => unpinApp(app.app_id)}>
                      {t("apps.board.remove", "Remove from board")}
                    </Button>
                  ) : (
                    <Button
                      type="primary"
                      disabled={!board}
                      onClick={() => pinApp(app.app_id)}
                    >
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

      <Modal
        title={
          dialog?.mode === "create"
            ? t("apps.board.createTitle", "New board")
            : t("apps.board.renameTitle", "Rename board")
        }
        open={dialog !== null}
        onCancel={() => {
          setDialog(null);
          boardForm.resetFields();
        }}
        onOk={() => void submitBoardDialog()}
        okText={
          dialog?.mode === "create"
            ? t("apps.board.new", "New board")
            : t("common.save", "Save")
        }
        cancelText={t("common.cancel", "Cancel")}
        confirmLoading={dialogSaving}
        destroyOnHidden
      >
        <Form form={boardForm} layout="vertical">
          <Form.Item
            name="name"
            label={t("apps.board.nameLabel", "Board name")}
            rules={[
              {
                required: true,
                whitespace: true,
                message: t("apps.board.nameRequired", "Enter a board name"),
              },
            ]}
          >
            <Input maxLength={100} />
          </Form.Item>
          {dialog?.mode === "create" && (
            <Form.Item
              name="description"
              label={t("apps.board.descriptionLabel", "Description")}
            >
              <Input.TextArea maxLength={500} rows={2} />
            </Form.Item>
          )}
        </Form>
      </Modal>

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
