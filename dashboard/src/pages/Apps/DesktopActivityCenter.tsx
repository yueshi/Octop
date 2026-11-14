import { useEffect, useRef, useState, type ReactNode } from "react";
import { Activity, Bell, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { fillTemplate } from "./appActions";
import {
  markDesktopNotificationRead,
  useDesktopNotifications,
} from "./desktopNotifications";
import { WINDOW_REGISTRY, type DesktopWindowKind } from "./desktopWindows";
import styles from "./index.module.less";

export interface DesktopActivityExpert {
  id: string;
  name: string;
  state?: string;
  accent: string;
  icon: ReactNode;
  openWindows: number;
}

export interface DesktopActivityWindow {
  id: string;
  title: string;
  kind: DesktopWindowKind;
  minimized: boolean;
  icon: ReactNode;
  spaceId: string;
  spaceName: string;
}

interface DesktopActivityCenterProps {
  experts: DesktopActivityExpert[];
  windows: DesktopActivityWindow[];
  onOpenExpert: (agentId: string) => void;
  onOpenWindow: (windowId: string) => void;
}

/**
 * Compact system tray for agent activity: expert run states, every open
 * window, and approvals that still need a human. It deliberately lives in the
 * desktop tree rather than an antd portal so it survives fullscreen.
 */
export default function DesktopActivityCenter({
  experts,
  windows,
  onOpenExpert,
  onOpenWindow,
}: DesktopActivityCenterProps) {
  const { t } = useTranslation();
  const notifications = useDesktopNotifications();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const attention = notifications.filter(
    (item) =>
      !item.read &&
      (item.kind === "approval" ||
        item.kind === "security" ||
        item.kind === "task" ||
        item.kind === "cron"),
  );
  const running = experts.filter((expert) => expert.state === "running").length;
  const activeWindows = windows.length;

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

  return (
    <div className={styles.desktopActivity} ref={rootRef}>
      <button
        type="button"
        className={styles.desktopActivityButton}
        data-testid="desktop-activity-button"
        aria-label={t("apps.desktop.activity.label", "Agent activity")}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Activity size={14} />
        {(activeWindows > 0 || attention.length > 0) && (
          <span
            className={styles.desktopActivityBadge}
            data-testid="desktop-activity-badge"
          >
            {activeWindows + attention.length > 9
              ? "9+"
              : activeWindows + attention.length}
          </span>
        )}
      </button>

      {open && (
        <div
          className={styles.desktopActivityPanel}
          role="dialog"
          aria-label={t("apps.desktop.activity.title", "Agent Activity")}
          data-testid="desktop-activity-panel"
        >
          <div className={styles.desktopActivityHeader}>
            <div>
              <span className={styles.desktopActivityTitle}>
                {t("apps.desktop.activity.title", "Agent Activity")}
              </span>
              <span className={styles.desktopActivitySubtitle}>
                {fillTemplate(
                  t(
                    "apps.desktop.activity.subtitle",
                    "{{running}} running · {{windows}} windows",
                  ),
                  { running, windows: activeWindows },
                )}
              </span>
            </div>
          </div>

          {experts.length === 0 && windows.length === 0 ? (
            <div className={styles.desktopActivityEmpty}>
              <Activity size={22} strokeWidth={1.4} />
              <span>
                {t(
                  "apps.desktop.activity.empty",
                  "No active experts or windows",
                )}
              </span>
            </div>
          ) : (
            <div className={styles.desktopActivityList}>
              {experts.length > 0 && (
                <div className={styles.desktopActivitySection}>
                  <div className={styles.desktopActivitySectionTitle}>
                    {t("apps.desktop.activity.experts", "Experts")}
                  </div>
                  {experts.map((expert) => (
                    <button
                      key={expert.id}
                      type="button"
                      className={styles.desktopActivityRow}
                      data-testid="desktop-activity-expert"
                      data-agent-id={expert.id}
                      onClick={() => {
                        onOpenExpert(expert.id);
                        setOpen(false);
                      }}
                    >
                      <span
                        className={styles.desktopActivityExpertIcon}
                        style={{ borderColor: expert.accent }}
                      >
                        {expert.icon}
                      </span>
                      <span className={styles.desktopActivityRowText}>
                        <span className={styles.desktopActivityRowTitle}>
                          {expert.name}
                        </span>
                        <span className={styles.desktopActivityRowMeta}>
                          <span
                            className={styles.desktopActivityState}
                            data-state={expert.state ?? "unknown"}
                            aria-hidden
                          />
                          {expert.state === "running"
                            ? t("apps.desktop.online", "Online")
                            : t("apps.desktop.offline", "Offline")}
                          {expert.openWindows > 0
                            ? ` · ${expert.openWindows} ${t(
                                "apps.desktop.activity.windowsShort",
                                "windows",
                              )}`
                            : ""}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              )}

              {windows.length > 0 && (
                <div className={styles.desktopActivitySection}>
                  <div className={styles.desktopActivitySectionTitle}>
                    {t("apps.desktop.activity.windows", "Open windows")}
                  </div>
                  {windows.map((win) => (
                    <button
                      key={win.id}
                      type="button"
                      className={styles.desktopActivityRow}
                      data-testid="desktop-activity-window"
                      data-window-id={win.id}
                      data-kind={win.kind}
                      onClick={() => {
                        onOpenWindow(win.id);
                        setOpen(false);
                      }}
                    >
                      <span className={styles.desktopActivityWindowIcon}>
                        {win.icon}
                      </span>
                      <span className={styles.desktopActivityRowText}>
                        <span className={styles.desktopActivityRowTitle}>
                          {win.title}
                        </span>
                        <span className={styles.desktopActivityRowMeta}>
                          {win.spaceName}
                          {win.minimized
                            ? ` · ${t(
                                "apps.desktop.mission.minimized",
                                "Minimized",
                              )}`
                            : ` · ${t(
                                WINDOW_REGISTRY[win.kind].labelKey,
                                WINDOW_REGISTRY[win.kind].labelFallback,
                              )}`}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              )}

              {attention.length > 0 && (
                <div className={styles.desktopActivitySection}>
                  <div className={styles.desktopActivitySectionTitle}>
                    <ShieldAlert size={11} />
                    {t("apps.desktop.activity.attention", "Needs attention")}
                  </div>
                  {attention.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={[
                        styles.desktopActivityRow,
                        styles.desktopActivityAttention,
                      ].join(" ")}
                      data-testid="desktop-activity-attention"
                      onClick={() => {
                        markDesktopNotificationRead(item.id);
                        if (item.windowId) onOpenWindow(item.windowId);
                        setOpen(false);
                      }}
                    >
                      <span className={styles.desktopActivityWindowIcon}>
                        <Bell size={13} />
                      </span>
                      <span className={styles.desktopActivityRowText}>
                        <span className={styles.desktopActivityRowTitle}>
                          {item.title}
                        </span>
                        {item.body && (
                          <span className={styles.desktopActivityRowMeta}>
                            {item.body}
                          </span>
                        )}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
