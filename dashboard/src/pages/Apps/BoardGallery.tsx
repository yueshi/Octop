import type { CSSProperties } from "react";
import { ArrowUpRight, LayoutGrid, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { AppSummary } from "../../api/modules/apps";
import type {
  BoardDetail,
  BoardItem,
  BoardSummary,
} from "../../api/modules/boards";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import styles from "./index.module.less";

interface BoardGalleryProps {
  boards: BoardSummary[];
  apps: AppSummary[];
  previews: Record<string, BoardDetail>;
  loading: boolean;
  timeZone: string;
  onOpen: (boardId: string) => void;
  onCreate: () => void;
}

function hashText(text: string): number {
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function cellTint(appId: string): string {
  const hue = hashText(appId) % 360;
  return `linear-gradient(145deg, hsl(${hue} 76% 60%), hsl(${
    (hue + 42) % 360
  } 70% 46%))`;
}

function previewRows(items: BoardItem[]): number {
  return Math.max(6, ...items.map((item) => item.y + item.h));
}

function BoardThumbnail({
  board,
  detail,
  apps,
  timeZone,
}: {
  board: BoardSummary;
  detail?: BoardDetail;
  apps: AppSummary[];
  timeZone: string;
}) {
  const { t } = useTranslation();
  const items = detail?.layout.items ?? [];
  const appById = new Map(apps.map((app) => [app.app_id, app]));
  const rows = previewRows(items);

  if (!items.length) {
    if (!detail && board.item_count > 0) {
      return (
        <span
          className={styles.boardGalleryThumbPending}
          data-testid="board-gallery-preview-loading"
          aria-hidden
        />
      );
    }
    return (
      <span className={styles.boardGalleryThumbEmpty}>
        <LayoutGrid size={22} strokeWidth={1.4} />
        <span>{t("apps.gallery.emptyBoard", "Empty board")}</span>
        <span className={styles.boardGalleryThumbEmptyMeta}>
          {t("apps.gallery.updated", "Updated {{time}}", {
            time: formatServerDateTime(board.updated_at, timeZone),
          })}
        </span>
      </span>
    );
  }

  return (
    <span className={styles.boardGalleryThumb}>
      {items.map((item) => {
        const app = appById.get(item.app_id);
        if (!app) return null;
        const cellStyle = {
          left: `${(item.x / 12) * 100}%`,
          top: `${(item.y / rows) * 100}%`,
          width: `${(item.w / 12) * 100}%`,
          height: `${(item.h / rows) * 100}%`,
          "--cell-tint": cellTint(app.app_id),
        } as CSSProperties;
        return (
          <span
            key={item.app_id}
            className={styles.boardGalleryThumbCell}
            style={cellStyle}
            title={app.name}
          >
            <span className={styles.boardGalleryThumbCellInner}>
              <span className={styles.boardGalleryThumbCellMark}>
                {Array.from(app.name.trim())[0]?.toUpperCase() ?? "?"}
              </span>
            </span>
          </span>
        );
      })}
    </span>
  );
}

/**
 * Workbench landing page: every board as a card with a live mini-thumbnail of
 * its 12-column layout. Cards only summarize; the board itself opens on click.
 */
export default function BoardGallery({
  boards,
  apps,
  previews,
  loading,
  timeZone,
  onOpen,
  onCreate,
}: BoardGalleryProps) {
  const { t } = useTranslation();

  if (loading) {
    return (
      <div className={styles.boardGallery} data-testid="board-gallery">
        <div className={styles.boardGalleryHeader}>
          <div>
            <h2>{t("apps.gallery.title", "Boards")}</h2>
            <p>
              {t(
                "apps.gallery.subtitle",
                "Open a board to arrange MiniApps on its canvas.",
              )}
            </p>
          </div>
        </div>
        <div className={styles.boardGalleryGrid}>
          {[0, 1, 2].map((index) => (
            <div
              key={index}
              className={styles.boardGallerySkeleton}
              aria-hidden
            >
              <span className={styles.boardGallerySkeletonCover} />
              <span className={styles.boardGallerySkeletonLine} />
              <span className={styles.boardGallerySkeletonLineShort} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.boardGallery} data-testid="board-gallery">
      <div className={styles.boardGalleryHeader}>
        <div>
          <h2>{t("apps.gallery.title", "Boards")}</h2>
          <p>
            {t(
              "apps.gallery.subtitle",
              "Open a board to arrange MiniApps on its canvas.",
            )}
          </p>
        </div>
        <span className={styles.boardGalleryCount}>
          {t("apps.gallery.count", "{{count}} boards", {
            count: boards.length,
          })}
        </span>
      </div>

      {boards.length === 0 ? (
        <div className={styles.boardGalleryEmpty}>
          <LayoutGrid size={30} strokeWidth={1.3} />
          <p>{t("apps.gallery.empty", "No boards yet.")}</p>
          <span>
            {t(
              "apps.gallery.emptyHint",
              "Create a board to arrange your MiniApps.",
            )}
          </span>
          <button
            type="button"
            className={styles.boardGalleryEmptyAction}
            onClick={onCreate}
          >
            <Plus size={14} />
            {t("apps.board.new", "New board")}
          </button>
        </div>
      ) : (
        <div className={styles.boardGalleryGrid}>
          {boards.map((board) => (
            <a
              key={board.board_id}
              href={`/apps/boards/${encodeURIComponent(board.board_id)}`}
              className={styles.boardGalleryCard}
              data-testid="board-gallery-card"
              data-board-id={board.board_id}
              onClick={(event) => {
                if (
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                ) {
                  return;
                }
                event.preventDefault();
                onOpen(board.board_id);
              }}
            >
              <span className={styles.boardGalleryCover}>
                <BoardThumbnail
                  board={board}
                  detail={previews[board.board_id]}
                  apps={apps}
                  timeZone={timeZone}
                />
                <span className={styles.boardGalleryOpen}>
                  <ArrowUpRight size={15} />
                </span>
              </span>
              <span className={styles.boardGalleryBody}>
                <span className={styles.boardGalleryTitleRow}>
                  <span className={styles.boardGalleryTitle}>{board.name}</span>
                  <span className={styles.boardGalleryItemCount}>
                    {t("apps.gallery.apps", "{{count}} MiniApps", {
                      count: board.item_count,
                    })}
                  </span>
                </span>
                {board.description && (
                  <span className={styles.boardGalleryDescription}>
                    {board.description}
                  </span>
                )}
                <span className={styles.boardGalleryMeta}>
                  {t("apps.gallery.updated", "Updated {{time}}", {
                    time: formatServerDateTime(board.updated_at, timeZone),
                  })}
                </span>
              </span>
            </a>
          ))}
          <button
            type="button"
            className={styles.boardGalleryNew}
            data-testid="board-gallery-new"
            onClick={onCreate}
          >
            <span className={styles.boardGalleryNewIcon}>
              <Plus size={20} />
            </span>
            <span className={styles.boardGalleryNewTitle}>
              {t("apps.board.new", "New board")}
            </span>
            <span className={styles.boardGalleryNewHint}>
              {t(
                "apps.gallery.newHint",
                "Start with an empty canvas and pin MiniApps.",
              )}
            </span>
          </button>
        </div>
      )}
    </div>
  );
}
