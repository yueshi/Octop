import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import type { DesktopWindowKind } from "./desktopWindows";
import styles from "./index.module.less";

export interface DesktopDockItem {
  id: string;
  kind: DesktopWindowKind | "launcher";
  label: string;
  icon: ReactNode;
  appId?: string;
  windowId?: string;
  running?: boolean;
  minimized?: boolean;
  active?: boolean;
  onActivate: () => void;
  onMinimize?: () => void;
  onClose?: () => void;
}

interface DockMenuState {
  item: DesktopDockItem;
  x: number;
  y: number;
}

interface DesktopDockProps {
  items: DesktopDockItem[];
}

const MENU_WIDTH = 176;
const MENU_HEIGHT = 118;

/**
 * Dock 2.0: pinned/current MiniApps plus every running chat and web window,
 * an All Apps launcher, and right-click window actions. It stays inside the
 * desktop root so it remains visible while the Fullscreen API is active.
 */
export default function DesktopDock({ items }: DesktopDockProps) {
  const { t } = useTranslation();
  const [menu, setMenu] = useState<DockMenuState | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menu) return;
    const onPointerDown = (event: PointerEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return;
      setMenu(null);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenu(null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menu]);

  const openMenu = (
    item: DesktopDockItem,
    event: ReactMouseEvent<HTMLElement>,
  ) => {
    event.preventDefault();
    if (item.kind === "launcher") return;
    const width = window.innerWidth || 1280;
    const height = window.innerHeight || 800;
    setMenu({
      item,
      x: Math.max(8, Math.min(event.clientX, width - MENU_WIDTH - 8)),
      y: Math.max(
        8,
        Math.min(event.clientY - MENU_HEIGHT - 8, height - MENU_HEIGHT - 8),
      ),
    });
  };

  return (
    <>
      <div className={styles.desktopDock} data-testid="desktop-dock">
        <div
          className={styles.desktopDockInner}
          data-testid="desktop-dock-surface"
        >
          {items.map((item, index) => {
            const divider =
              item.kind === "launcher" &&
              index > 0 &&
              items[index - 1]?.kind !== "launcher";
            return (
              <span key={item.id} className={styles.desktopDockSlot}>
                {divider && (
                  <span className={styles.desktopDockDivider} aria-hidden />
                )}
                <Tooltip title={item.label} placement="top">
                  <button
                    type="button"
                    className={[
                      styles.desktopDockIcon,
                      item.kind === "launcher"
                        ? styles.desktopDockLauncher
                        : "",
                      item.active ? styles.desktopDockIconActive : "",
                      item.minimized ? styles.desktopDockIconMinimized : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    data-testid={
                      item.kind === "launcher"
                        ? "desktop-dock-launcher"
                        : item.kind === "app"
                        ? "desktop-dock-icon"
                        : `desktop-dock-${item.kind}`
                    }
                    data-kind={item.kind}
                    data-app-id={item.appId ?? ""}
                    data-window-id={item.windowId ?? ""}
                    data-minimized={item.minimized ? "true" : "false"}
                    aria-label={item.label}
                    onClick={() => {
                      setMenu(null);
                      item.onActivate();
                    }}
                    onContextMenu={(event) => openMenu(item, event)}
                  >
                    <span className={styles.desktopDockArt}>{item.icon}</span>
                    {item.running && (
                      <span
                        className={styles.desktopDockRunning}
                        data-testid="dock-running"
                        aria-hidden
                      />
                    )}
                    {item.minimized && (
                      <span
                        className={styles.desktopDockMinimized}
                        data-testid="dock-minimized"
                        aria-hidden
                      />
                    )}
                  </button>
                </Tooltip>
              </span>
            );
          })}
        </div>
      </div>

      {menu && (
        <div
          ref={menuRef}
          className={styles.desktopDockMenu}
          style={{ left: menu.x, top: menu.y }}
          role="menu"
          data-testid="desktop-dock-menu"
          aria-label={menu.item.label}
        >
          <div className={styles.desktopDockMenuTitle}>{menu.item.label}</div>
          <button
            type="button"
            role="menuitem"
            className={styles.desktopDockMenuItem}
            onClick={() => {
              menu.item.onActivate();
              setMenu(null);
            }}
          >
            {menu.item.running
              ? t("apps.desktop.dock.restore", "Show window")
              : t("apps.desktop.dock.open", "Open")}
          </button>
          {menu.item.running && menu.item.onMinimize && (
            <button
              type="button"
              role="menuitem"
              className={styles.desktopDockMenuItem}
              onClick={() => {
                menu.item.onMinimize?.();
                setMenu(null);
              }}
            >
              {t("apps.desktop.minimizeWindow", "Minimize")}
            </button>
          )}
          {menu.item.running && menu.item.onClose && (
            <button
              type="button"
              role="menuitem"
              className={[
                styles.desktopDockMenuItem,
                styles.desktopDockMenuDanger,
              ].join(" ")}
              onClick={() => {
                menu.item.onClose?.();
                setMenu(null);
              }}
            >
              {t("apps.desktop.closeWindow", "Close")}
            </button>
          )}
        </div>
      )}
    </>
  );
}
