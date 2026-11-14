import type { CSSProperties, ReactNode } from "react";
import { LayoutGrid, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { WINDOW_REGISTRY, type DesktopWindowKind } from "./desktopWindows";
import styles from "./index.module.less";

export interface DesktopMissionWindow {
  id: string;
  title: string;
  kind: DesktopWindowKind;
  minimized: boolean;
  icon: ReactNode;
}

export interface DesktopMissionSpace {
  id: string;
  name: string;
  accent: string;
  icon: ReactNode;
  windows: DesktopMissionWindow[];
  active: boolean;
}

interface DesktopMissionControlProps {
  open: boolean;
  spaces: DesktopMissionSpace[];
  onClose: () => void;
  onSelectSpace: (spaceId: string) => void;
  onFocusWindow: (windowId: string, spaceId: string) => void;
}

/**
 * Mission Control shows every expert desktop and every open window at once.
 * Clicking a window focuses it and switches to the desktop that owns it.
 */
export default function DesktopMissionControl({
  open,
  spaces,
  onClose,
  onSelectSpace,
  onFocusWindow,
}: DesktopMissionControlProps) {
  const { t } = useTranslation();
  if (!open) return null;

  return (
    <div
      className={styles.desktopMission}
      role="dialog"
      aria-modal="true"
      aria-label={t("apps.desktop.mission.title", "Mission Control")}
      data-testid="desktop-mission-control"
    >
      <button
        type="button"
        className={styles.desktopMissionBackdrop}
        aria-label={t("apps.desktop.mission.close", "Close Mission Control")}
        onClick={onClose}
      />
      <div className={styles.desktopMissionPanel}>
        <header className={styles.desktopMissionHeader}>
          <span className={styles.desktopMissionEyebrow}>
            <LayoutGrid size={14} />
            {t("apps.desktop.mission.eyebrow", "AGENT OS · WORKSPACES")}
          </span>
          <div className={styles.desktopMissionTitleRow}>
            <div>
              <h2 className={styles.desktopMissionTitle}>
                {t("apps.desktop.mission.title", "Mission Control")}
              </h2>
              <p className={styles.desktopMissionSubtitle}>
                {t(
                  "apps.desktop.mission.subtitle",
                  "Every expert desktop and open window in one place.",
                )}
              </p>
            </div>
            <button
              type="button"
              className={styles.desktopMissionClose}
              aria-label={t(
                "apps.desktop.mission.close",
                "Close Mission Control",
              )}
              onClick={onClose}
            >
              <X size={16} />
            </button>
          </div>
        </header>

        <div className={styles.desktopMissionGrid}>
          {spaces.map((space) => (
            <section
              key={space.id}
              className={[
                styles.desktopMissionSpace,
                space.active ? styles.desktopMissionSpaceActive : "",
              ]
                .filter(Boolean)
                .join(" ")}
              style={{ "--space-accent": space.accent } as CSSProperties}
              data-testid="desktop-mission-space"
              data-space-id={space.id}
            >
              <button
                type="button"
                className={styles.desktopMissionSpaceHeader}
                data-testid="desktop-mission-space-select"
                onClick={() => onSelectSpace(space.id)}
              >
                <span className={styles.desktopMissionSpaceIcon}>
                  {space.icon}
                </span>
                <span className={styles.desktopMissionSpaceName}>
                  {space.name}
                </span>
                <span className={styles.desktopMissionSpaceCount}>
                  {space.windows.length}
                </span>
                {space.active && (
                  <span className={styles.desktopMissionSpaceNow}>
                    {t("apps.desktop.mission.current", "Current")}
                  </span>
                )}
              </button>

              {space.windows.length === 0 ? (
                <div className={styles.desktopMissionEmpty}>
                  {t(
                    "apps.desktop.mission.emptySpace",
                    "No open windows on this desktop",
                  )}
                </div>
              ) : (
                <div className={styles.desktopMissionWindows}>
                  {space.windows.map((win) => (
                    <button
                      key={win.id}
                      type="button"
                      className={styles.desktopMissionWindow}
                      data-testid="desktop-mission-window"
                      data-window-id={win.id}
                      data-kind={win.kind}
                      data-minimized={win.minimized ? "true" : "false"}
                      onClick={() => onFocusWindow(win.id, space.id)}
                    >
                      <span className={styles.desktopMissionWindowIcon}>
                        {win.icon}
                      </span>
                      <span className={styles.desktopMissionWindowText}>
                        <span className={styles.desktopMissionWindowTitle}>
                          {win.title}
                        </span>
                        <span className={styles.desktopMissionWindowKind}>
                          {t(
                            WINDOW_REGISTRY[win.kind].labelKey,
                            WINDOW_REGISTRY[win.kind].labelFallback,
                          )}
                          {win.minimized
                            ? ` · ${t(
                                "apps.desktop.mission.minimized",
                                "Minimized",
                              )}`
                            : ""}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </section>
          ))}
        </div>

        <footer className={styles.desktopMissionFooter}>
          <span>
            {t(
              "apps.desktop.mission.footer",
              "Click a window to jump back · F3 toggles Mission Control",
            )}
          </span>
        </footer>
      </div>
    </div>
  );
}
