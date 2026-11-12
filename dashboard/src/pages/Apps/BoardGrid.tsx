import { useCallback, type RefObject } from "react";
import { Button, Tooltip, Typography } from "antd";
import { AppWindow, ExternalLink, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import ReactGridLayout, { type Layout } from "react-grid-layout/legacy";
import { useContainerWidth } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import MiniAppFrame from "../../components/MiniAppFrame";
import type { AppSummary } from "../../api/modules/apps";
import type { BoardItem } from "../../api/modules/boards";
import styles from "./index.module.less";
import "./boardGrid.css";

const { Text } = Typography;

const GRID_COLS = 12;
const ROW_HEIGHT = 64;
const MARGIN: readonly [number, number] = [12, 12];
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
      />
      <div
        className={[styles.cellHead, editable ? "" : styles.cellHeadStatic]
          .filter(Boolean)
          .join(" ")}
        title={app.name}
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
  const { width, containerRef, mounted } = useContainerWidth();
  const byId = new Map(apps.map((app) => [app.app_id, app]));
  const cells = items
    .map((item) => ({ item, app: byId.get(item.app_id) }))
    .filter(
      (entry): entry is { item: BoardItem; app: AppSummary } =>
        entry.app !== undefined,
    );

  const handleLayoutChange = useCallback(
    (layout: Layout) => onLayoutChange?.(fromLayout(layout)),
    [onLayoutChange],
  );

  const narrow = mounted && width > 0 && width < NARROW_PX;

  return (
    <div
      // react-grid-layout ships React 19 ref types; React 18 accepts the same
      // object structurally.
      ref={containerRef as unknown as RefObject<HTMLDivElement>}
      className={[className ?? "", narrow ? "" : styles.boardRoot]
        .filter(Boolean)
        .join(" ")}
      data-board-root
    >
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
          className={styles.boardGrid}
          width={width}
          cols={GRID_COLS}
          rowHeight={ROW_HEIGHT}
          margin={MARGIN}
          containerPadding={[0, 0]}
          layout={toLayout(cells.map((cell) => cell.item))}
          compactType="vertical"
          isDraggable={editable}
          isResizable={editable}
          isBounded
          resizeHandles={["se"]}
          draggableHandle={`${styles.notchZone}, ${styles.cellHead}`}
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
