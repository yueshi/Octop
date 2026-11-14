import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Bell,
  CheckCheck,
  CheckCircle2,
  Clock3,
  ListTodo,
  ShieldAlert,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatMessageTime } from "../../utils/formatMessageTime";
import {
  clearDesktopNotifications,
  markAllDesktopNotificationsRead,
  markDesktopNotificationRead,
  useDesktopNotifications,
  type DesktopNotification,
  type DesktopNotificationKind,
} from "./desktopNotifications";
import styles from "./index.module.less";

interface DesktopNotificationCenterProps {
  onOpenWindow?: (windowId: string) => void;
}

const KIND_ICON = {
  approval: ShieldAlert,
  success: CheckCircle2,
  info: Bell,
  error: AlertCircle,
  task: ListTodo,
  security: ShieldCheck,
  cron: Clock3,
} satisfies Record<DesktopNotificationKind, typeof Bell>;

function NotificationItem({
  notification,
  timeZone,
  onOpen,
}: {
  notification: DesktopNotification;
  timeZone: string;
  onOpen: (notification: DesktopNotification) => void;
}) {
  const Icon = KIND_ICON[notification.kind];
  return (
    <button
      type="button"
      className={[
        styles.desktopNotificationsItem,
        notification.read ? "" : styles.desktopNotificationsItemUnread,
      ]
        .filter(Boolean)
        .join(" ")}
      data-testid="desktop-notification-item"
      data-kind={notification.kind}
      onClick={() => onOpen(notification)}
    >
      <span
        className={styles.desktopNotificationsIcon}
        data-kind={notification.kind}
      >
        <Icon size={15} />
      </span>
      <span className={styles.desktopNotificationsBody}>
        <span className={styles.desktopNotificationsItemTitle}>
          {notification.title}
        </span>
        {notification.body && (
          <span className={styles.desktopNotificationsItemText}>
            {notification.body}
          </span>
        )}
        <span className={styles.desktopNotificationsMeta}>
          {formatMessageTime(notification.createdAt, timeZone)}
        </span>
      </span>
      {!notification.read && (
        <span className={styles.desktopNotificationsUnreadDot} aria-hidden />
      )}
    </button>
  );
}

/**
 * Desktop notification bell + panel. It lives in the desktop menu bar and
 * focuses the originating chat/app window when an item is clicked.
 */
export default function DesktopNotificationCenter({
  onOpenWindow,
}: DesktopNotificationCenterProps) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const notifications = useDesktopNotifications();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const unread = notifications.filter((item) => !item.read).length;

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const root = rootRef.current;
      if (root && !root.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open]);

  const openNotification = (notification: DesktopNotification) => {
    markDesktopNotificationRead(notification.id);
    if (notification.windowId) onOpenWindow?.(notification.windowId);
    setOpen(false);
  };

  return (
    <div className={styles.desktopNotifications} ref={rootRef}>
      <button
        type="button"
        className={styles.desktopNotificationsButton}
        data-testid="desktop-notifications-button"
        aria-label={t("apps.desktop.notifications.label", "Notifications")}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Bell size={14} />
        {unread > 0 && (
          <span className={styles.desktopNotificationsBadge}>
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          className={styles.desktopNotificationsPanel}
          role="dialog"
          aria-label={t(
            "apps.desktop.notifications.title",
            "Notification Center",
          )}
          data-testid="desktop-notifications-panel"
        >
          <div className={styles.desktopNotificationsHeader}>
            <span className={styles.desktopNotificationsTitle}>
              {t("apps.desktop.notifications.title", "Notification Center")}
            </span>
            {notifications.length > 0 && (
              <span className={styles.desktopNotificationsActions}>
                <button
                  type="button"
                  className={styles.desktopNotificationsAction}
                  onClick={() => markAllDesktopNotificationsRead()}
                >
                  <CheckCheck size={12} />
                  {t("apps.desktop.notifications.markAllRead", "Mark all read")}
                </button>
                <button
                  type="button"
                  className={styles.desktopNotificationsAction}
                  aria-label={t(
                    "apps.desktop.notifications.clear",
                    "Clear notifications",
                  )}
                  title={t(
                    "apps.desktop.notifications.clear",
                    "Clear notifications",
                  )}
                  onClick={() => clearDesktopNotifications()}
                >
                  <Trash2 size={12} />
                </button>
              </span>
            )}
          </div>

          {notifications.length === 0 ? (
            <div className={styles.desktopNotificationsEmpty}>
              <Bell size={22} strokeWidth={1.4} />
              <span>
                {t("apps.desktop.notifications.empty", "No notifications yet")}
              </span>
            </div>
          ) : (
            <div className={styles.desktopNotificationsList}>
              {notifications.map((notification) => (
                <NotificationItem
                  key={notification.id}
                  notification={notification}
                  timeZone={timeZone}
                  onOpen={openNotification}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
