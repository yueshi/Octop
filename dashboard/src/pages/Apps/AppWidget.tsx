import { useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  ExternalLink,
  MoreHorizontal,
  MoveDiagonal2,
  RefreshCw,
  X,
} from "lucide-react";
import { Button, Dropdown, Popover, Space, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import MiniAppFrame from "../../components/MiniAppFrame";
import type { AppSummary } from "../../api/modules/apps";
import { APP_BOARD_SPANS, type AppBoardSize } from "./appBoard";
import styles from "./index.module.less";

const { Text } = Typography;

const SIZE_CLASS: Record<AppBoardSize, string> = {
  "1x1": styles.size1x1,
  "2x2": styles.size2x2,
  "2x3": styles.size2x3,
  "2x4": styles.size2x4,
  "2x6": styles.size2x6,
  "3x2": styles.size3x2,
  "3x3": styles.size3x3,
  "3x4": styles.size3x4,
  "3x6": styles.size3x6,
  "6x2": styles.size6x2,
  "6x3": styles.size6x3,
  "6x4": styles.size6x4,
  "6x6": styles.size6x6,
};

function sizeLabel(size: AppBoardSize): string {
  const span = APP_BOARD_SPANS[size];
  return `${span.cols}×${span.rows}`;
}

/** Picker groups: full-width bands, halves, thirds, then the single cell. */
const SIZE_GROUPS: {
  key: string;
  labelKey: string;
  fallback: string;
  sizes: AppBoardSize[];
}[] = [
  {
    key: "full",
    labelKey: "apps.board.groupFull",
    fallback: "Full width (6 cols)",
    sizes: ["6x2", "6x3", "6x4", "6x6"],
  },
  {
    key: "half",
    labelKey: "apps.board.groupHalf",
    fallback: "Half width (3 cols)",
    sizes: ["3x2", "3x3", "3x4", "3x6"],
  },
  {
    key: "third",
    labelKey: "apps.board.groupThird",
    fallback: "One third (2 cols)",
    sizes: ["2x2", "2x3", "2x4", "2x6"],
  },
  {
    key: "single",
    labelKey: "apps.board.groupSingle",
    fallback: "Single cell (1 col)",
    sizes: ["1x1"],
  },
];

/** Board pixels used to draw the proportions of a swatch. */
const SWATCH_SIZE = 34;

interface Props {
  app: AppSummary;
  size: AppBoardSize;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onOpen: () => void;
  onRemove: () => void;
  onResizeStart: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onSetSize: (size: AppBoardSize) => void;
}

/**
 * One cell of the workbench board.
 *
 * The MiniApp runs live inside the cell, so its data and actions are usable
 * without opening the detail page. The cell spans one of the 6x6 grid
 * rectangles: drag the corner handle for a quick resize, or pick an exact
 * size from the header's size menu.
 */
export default function AppWidget({
  app,
  size,
  canMoveUp,
  canMoveDown,
  onMoveUp,
  onMoveDown,
  onOpen,
  onRemove,
  onResizeStart,
  onSetSize,
}: Props) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [reloadKey, setReloadKey] = useState(0);
  const [sizePickerOpen, setSizePickerOpen] = useState(false);
  const currentSizeLabel = sizeLabel(size);

  const sizePicker = (
    <div className={styles.sizePicker}>
      {SIZE_GROUPS.map((group) => (
        <div key={group.key} className={styles.sizePickerGroup}>
          <div className={styles.sizePickerLabel}>
            {t(group.labelKey, group.fallback)}
          </div>
          <div className={styles.sizePickerOptions}>
            {group.sizes.map((option) => {
              const span = APP_BOARD_SPANS[option];
              const active = option === size;
              return (
                <button
                  key={option}
                  type="button"
                  className={`${styles.sizePickerOption} ${
                    active ? styles.sizePickerOptionActive : ""
                  }`}
                  aria-label={sizeLabel(option)}
                  aria-pressed={active}
                  onClick={() => {
                    onSetSize(option);
                    setSizePickerOpen(false);
                  }}
                >
                  <span
                    className={styles.sizeSwatch}
                    style={{
                      width: Math.max(6, (span.cols / 6) * SWATCH_SIZE),
                      height: Math.max(6, (span.rows / 6) * SWATCH_SIZE),
                    }}
                  >
                    {active ? <Check size={12} /> : null}
                  </span>
                  <span className={styles.sizeSwatchLabel}>
                    {sizeLabel(option)}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );

  return (
    <div
      className={[styles.widget, SIZE_CLASS[size]].join(" ")}
      data-size={size}
    >
      <span className={styles.notchZone} aria-hidden />
      <div className={styles.widgetHead}>
        <Text
          strong
          ellipsis={{ tooltip: app.name }}
          className={styles.widgetTitle}
        >
          {app.name}
        </Text>
        <Space size={0}>
          <Tooltip title={t("apps.board.moveUp", "Move up")}>
            <Button
              type="text"
              size="small"
              aria-label={t("apps.board.moveUp", "Move up")}
              icon={<ArrowUp size={14} />}
              disabled={!canMoveUp}
              onClick={onMoveUp}
            />
          </Tooltip>
          <Tooltip title={t("apps.board.moveDown", "Move down")}>
            <Button
              type="text"
              size="small"
              aria-label={t("apps.board.moveDown", "Move down")}
              icon={<ArrowDown size={14} />}
              disabled={!canMoveDown}
              onClick={onMoveDown}
            />
          </Tooltip>
          <Popover
            open={sizePickerOpen}
            onOpenChange={setSizePickerOpen}
            trigger="click"
            placement="bottomRight"
            content={sizePicker}
          >
            <Tooltip title={t("apps.board.size", "Size")}>
              <Button
                type="text"
                size="small"
                aria-label={t("apps.board.size", "Size")}
                icon={<MoveDiagonal2 size={14} />}
              />
            </Tooltip>
          </Popover>
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                {
                  key: "refresh",
                  icon: <RefreshCw size={14} />,
                  label: t("apps.board.refresh", "Refresh"),
                  onClick: () => setReloadKey((value) => value + 1),
                },
                {
                  key: "open",
                  icon: <ExternalLink size={14} />,
                  label: t("apps.board.open", "Open detail"),
                  onClick: onOpen,
                },
                { type: "divider" },
                {
                  key: "remove",
                  icon: <X size={14} />,
                  danger: true,
                  label: t("apps.board.remove", "Remove from board"),
                  onClick: onRemove,
                },
              ],
            }}
          >
            <Button
              type="text"
              size="small"
              aria-label={t("apps.board.menu", "Board actions")}
              icon={<MoreHorizontal size={16} />}
            />
          </Dropdown>
        </Space>
      </div>
      <div className={styles.widgetBody}>
        <MiniAppFrame
          key={reloadKey}
          appId={app.app_id}
          fill
          title={app.name}
          capabilities={app.capabilities}
          onComposeMessage={(text) =>
            navigate(`/chat/${encodeURIComponent(app.agent_id)}`, {
              state: { prefillInput: text },
            })
          }
        />
      </div>
      <Tooltip
        title={`${t(
          "apps.board.resizeHint",
          "Drag to resize, or pick a size from the header",
        )} · ${currentSizeLabel}`}
      >
        <button
          type="button"
          className={styles.resizeHandle}
          aria-label={t(
            "apps.board.resizeHint",
            "Drag to resize, or pick a size from the header",
          )}
          onPointerDown={onResizeStart}
        >
          <MoveDiagonal2 size={13} />
        </button>
      </Tooltip>
    </div>
  );
}
