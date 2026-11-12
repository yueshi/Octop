import { useTranslation } from "react-i18next";
import { useElapsedSince } from "../../../hooks/useElapsedSeconds";
import styles from "../index.module.less";

interface TurnInProgressNoticeProps {
  /** Turn start timestamp; omitted for member-only tail phases. */
  startedAt?: number | null;
  /** Host turn settled but team members are still speaking. */
  membersOnly?: boolean;
  queuedCount?: number;
  onStop?: () => void;
}

/**
 * Sticky reminder above the composer while the current turn is still running.
 * Prevents users from resubmitting the same request because the folded
 * thinking/tool panel looks idle.
 */
export default function TurnInProgressNotice({
  startedAt = null,
  membersOnly = false,
  queuedCount = 0,
  onStop,
}: TurnInProgressNoticeProps) {
  const { t } = useTranslation();
  const hasStart = Boolean(startedAt && startedAt > 0);
  // Keep the hook's start value stable when there is no timestamp yet;
  // Date.now() here would restart the interval on every render.
  const elapsed = useElapsedSince(startedAt ?? 0);

  const title = membersOnly
    ? t("chat.turnInProgress.membersTitle", {
        defaultValue: "Team members are still replying",
      })
    : t("chat.turnInProgress.title", {
        defaultValue: "This turn is still running",
      });
  const body =
    hasStart && !membersOnly
      ? t("chat.turnInProgress.bodyElapsed", {
          seconds: elapsed,
          defaultValue:
            "Waiting {{seconds}}s; new messages will queue, please don't resubmit.",
        })
      : t("chat.turnInProgress.body", {
          defaultValue:
            "New messages will queue until it finishes; please don't resubmit.",
        });

  return (
    <div
      className={styles.turnInProgressNotice}
      role="status"
      aria-live="polite"
    >
      <span className={styles.turnInProgressDots} aria-hidden>
        <span className={styles.thinkingDot} />
        <span className={styles.thinkingDot} />
        <span className={styles.thinkingDot} />
      </span>
      <div className={styles.turnInProgressText}>
        <span className={styles.turnInProgressTitle}>{title}</span>
        <span className={styles.turnInProgressBody}>
          {body}
          {queuedCount > 0
            ? ` · ${t("chat.turnInProgress.queued", {
                count: queuedCount,
                defaultValue: "{{count}} queued",
              })}`
            : ""}
        </span>
      </div>
      {onStop ? (
        <button
          type="button"
          className={styles.turnInProgressStop}
          onClick={onStop}
        >
          {t("chat.turnInProgress.stop", { defaultValue: "Stop this turn" })}
        </button>
      ) : null}
    </div>
  );
}
