import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Tooltip, Typography } from "antd";
import { AppWindow, ExternalLink, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import ReactGridLayout, { type Layout } from "react-grid-layout/legacy";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import MiniAppFrame from "../../components/MiniAppFrame";
import type { AppSummary } from "../../api/modules/apps";
import type { BoardItem } from "../../api/modules/boards";
import {
  BOARD_FALLBACK_ROW_HEIGHT,
  BOARD_GRID_COLS,
  BOARD_MARGIN_Y,
  fitBoardRowHeight,
} from "./boardLayout";
import styles from "./index.module.less";
import "./boardGrid.css";

const { Text } = Typography;

const MARGIN: readonly [number, number] = [12, BOARD_MARGIN_Y];
const MIN_W = 2;
const MIN_H = 2;
/** Below this width the 12-column grid is too dense; stack cells instead. */
const NARROW_PX = 640;

interface Props {
  items: BoardItem[];
  apps: AppSummary[];
  editable?: boolean;
  /** Fired as the grid moves or resizes, before anything is persisted. */
  onLayoutChange?: (items: BoardItem[]) => void;
  /** Fired when a drag or resize gesture finishes. */
  onCommit?: (items: BoardItem[]) => void;
  onOpenApp?: (appId: string) => void;
  onRemoveApp?: (appId: string) => void;
  onComposeMessage?: (agentId: string, text: string) => void;
  /** Extra class for the outer container (presentation mode). */
  className?: string;
}

function toLayout(items: BoardItem[]): Layout {
  return items.map((item) => ({
    i: item.app_id,
    x: item.x,
    y: item.y,
    w: item.w,
    h: item.h,
    minW: MIN_W,
    minH: MIN_H,
  }));
}

function fromLayout(layout: Layout): BoardItem[] {
  return layout.map((item) => ({
    app_id: item.i,
    x: item.x,
    y: item.y,
    w: item.w,
    h: item.h,
  }));
}

function BoardCell({
  app,
  editable,
  onOpenApp,
  onRemoveApp,
  onComposeMessage,
}: {
  app: AppSummary;
  editable: boolean;
  onOpenApp?: (appId: string) => void;
  onRemoveApp?: (appId: string) => void;
  onComposeMessage?: (agentId: string, text: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className={styles.cell} data-app-id={app.app_id}>
      <span
        className={[styles.notchZone, editable ? "" : styles.notchZoneStatic]
          .filter(Boolean)
          .join(" ")}
        aria-hidden
        title={t("apps.board.moveHint", "Drag to move")}
        data-testid="board-notch"
      />
      <div
        className={[styles.cellHead, editable ? "" : styles.cellHeadStatic]
          .filter(Boolean)
          .join(" ")}
        title={app.name}
        data-testid="board-head"
      >
        <AppWindow size={13} className={styles.cellIcon} />
        <Text ellipsis className={styles.cellTitle}>
          {app.name}
        </Text>
        {onOpenApp && (
          <Tooltip title={t("apps.board.open", "Open detail")}>
            <Button
              type="text"
              size="small"
              aria-label={t("apps.board.open", "Open detail")}
              icon={<ExternalLink size={13} />}
              onClick={() => onOpenApp(app.app_id)}
            />
          </Tooltip>
        )}
        {editable && onRemoveApp && (
          <Tooltip title={t("apps.board.remove", "Remove from board")}>
            <Button
              type="text"
              size="small"
              aria-label={t("apps.board.remove", "Remove from board")}
              icon={<X size={13} />}
              onClick={() => onRemoveApp(app.app_id)}
            />
          </Tooltip>
        )}
      </div>
      <div className={styles.cellBody}>
        <MiniAppFrame
          appId={app.app_id}
          title={app.name}
          fill
          capabilities={app.capabilities}
          onComposeMessage={
            onComposeMessage
              ? (text) => onComposeMessage(app.agent_id, text)
              : undefined
          }
        />
      </div>
    </div>
  );
}

/** A board canvas: react-grid-layout on desktop, a simple stack on phones. */
export default function BoardGrid({
  items,
  apps,
  editable = false,
  onLayoutChange,
  onCommit,
  onOpenApp,
  onRemoveApp,
  onComposeMessage,
  className,
}: Props) {
  const { t } = useTranslation();
  const byId = new Map(apps.map((app) => [app.app_id, app]));
  const cells = items
    .map((item) => ({ item, app: byId.get(item.app_id) }))
    .filter(
      (entry): entry is { item: BoardItem; app: AppSummary } =>
        entry.app !== undefined,
    );

  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [fill, setFill] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    const measure = (rect?: { width: number; height: number }) => {
      const next = rect ?? element.getBoundingClientRect();
      setFill({ width: next.width, height: next.height });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) measure(rect);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const handleLayoutChange = useCallback(
    (layout: Layout) => onLayoutChange?.(fromLayout(layout)),
    [onLayoutChange],
  );

  const narrow = fill.width > 0 && fill.width < NARROW_PX;
  const gridWidth = fill.width > 0 ? fill.width : 1280;
  // Stretch the row height so the occupied rows fill the page exactly; a
  // dense board keeps a readable minimum and scrolls instead.
  const rowCount = cells.reduce(
    (max, cell) => Math.max(max, cell.item.y + cell.item.h),
    1,
  );
  const rowHeight =
    fill.height > 0
      ? fitBoardRowHeight(fill.height, rowCount)
      : BOARD_FALLBACK_ROW_HEIGHT;

  return (
    <div
      className={[styles.boardRoot, className].filter(Boolean).join(" ")}
      data-board-root
    >
      <div ref={canvasRef} className={styles.boardFill}>
        {narrow ? (
          <div className={styles.boardStack}>
            {cells.map(({ app }) => (
              <div key={app.app_id} className={styles.stackCell}>
                <BoardCell
                  app={app}
                  editable={editable}
                  onOpenApp={onOpenApp}
                  onRemoveApp={onRemoveApp}
                  onComposeMessage={onComposeMessage}
                />
              </div>
            ))}
          </div>
        ) : (
          <ReactGridLayout
            width={gridWidth}
            cols={BOARD_GRID_COLS}
            rowHeight={rowHeight}
            margin={MARGIN}
            containerPadding={[0, 0]}
            layout={toLayout(cells.map((cell) => cell.item))}
            compactType="vertical"
            isDraggable={editable}
            isResizable={editable}
            // Not bounded to the auto-sized grid: vertical growth must be
            // possible, otherwise a cell in the last row cannot get taller.
            resizeHandles={["se"]}
            draggableHandle={`.${styles.notchZone}, .${styles.cellHead}`}
            draggableCancel="button,a,input,textarea,select"
            onLayoutChange={handleLayoutChange}
            onDragStop={(layout) => onCommit?.(fromLayout(layout))}
            onResizeStop={(layout) => onCommit?.(fromLayout(layout))}
          >
            {cells.map(({ app }) => (
              <div key={app.app_id}>
                <BoardCell
                  app={app}
                  editable={editable}
                  onOpenApp={onOpenApp}
                  onRemoveApp={onRemoveApp}
                  onComposeMessage={onComposeMessage}
                />
              </div>
            ))}
          </ReactGridLayout>
        )}
      </div>
      {editable && !narrow && cells.length > 0 && (
        <div className={styles.boardHint}>
          {t(
            "apps.board.dragHint",
            "Drag a cell header to move it; drag its corner to resize.",
          )}
        </div>
      )}
    </div>
  );
}
