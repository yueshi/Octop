import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Search } from "lucide-react";
import { useTranslation } from "react-i18next";

import styles from "./index.module.less";

export type DesktopCommandGroup = "apps" | "experts" | "windows" | "actions";

export interface DesktopCommandItem {
  id: string;
  group: DesktopCommandGroup;
  label: string;
  hint?: string;
  keywords?: string;
  icon: ReactNode;
  run: () => void;
}

interface DesktopCommandPaletteProps {
  open: boolean;
  onClose: () => void;
  items: DesktopCommandItem[];
}

const GROUP_LABEL: Record<
  DesktopCommandGroup,
  { key: string; fallback: string }
> = {
  apps: { key: "apps.desktop.command.groups.apps", fallback: "MiniApps" },
  experts: {
    key: "apps.desktop.command.groups.experts",
    fallback: "Expert desktops",
  },
  windows: {
    key: "apps.desktop.command.groups.windows",
    fallback: "Open windows",
  },
  actions: {
    key: "apps.desktop.command.groups.actions",
    fallback: "Actions",
  },
};

/**
 * Spotlight-style desktop command palette. It never portals to document.body:
 * the desktop can be fullscreen, where body-mounted overlays are invisible.
 */
export default function DesktopCommandPalette({
  open,
  onClose,
  items,
}: DesktopCommandPaletteProps) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) =>
      `${item.label} ${item.hint ?? ""} ${item.keywords ?? ""}`
        .toLowerCase()
        .includes(needle),
    );
  }, [items, query]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  if (!open) return null;

  const grouped = filtered.reduce<
    Partial<Record<DesktopCommandGroup, DesktopCommandItem[]>>
  >((acc, item) => {
    acc[item.group] = [...(acc[item.group] ?? []), item];
    return acc;
  }, {});

  const run = (item: DesktopCommandItem) => {
    item.run();
    onClose();
  };

  return (
    <div
      className={styles.desktopCommandOverlay}
      data-testid="desktop-command-palette"
      role="dialog"
      aria-modal="true"
      aria-label={t("apps.desktop.command.title", "Command palette")}
    >
      <button
        type="button"
        className={styles.desktopCommandBackdrop}
        aria-label={t("apps.desktop.command.close", "Close search")}
        onClick={onClose}
      />
      <div
        className={styles.desktopCommandPanel}
        onPointerDown={(event) => event.stopPropagation()}
      >
        <div className={styles.desktopCommandInputRow}>
          <Search size={16} />
          <input
            ref={inputRef}
            className={styles.desktopCommandInput}
            data-testid="desktop-command-input"
            value={query}
            placeholder={t(
              "apps.desktop.command.placeholder",
              "Search MiniApps, experts, windows, or actions…",
            )}
            aria-label={t("apps.desktop.command.title", "Command palette")}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                onClose();
                return;
              }
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveIndex((index) =>
                  filtered.length === 0 ? 0 : (index + 1) % filtered.length,
                );
                return;
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((index) =>
                  filtered.length === 0
                    ? 0
                    : (index - 1 + filtered.length) % filtered.length,
                );
                return;
              }
              if (event.key === "Enter") {
                event.preventDefault();
                const item = filtered[activeIndex];
                if (item) run(item);
              }
            }}
          />
          <kbd className={styles.desktopCommandShortcut}>Esc</kbd>
        </div>

        <div
          className={styles.desktopCommandResults}
          role="listbox"
          aria-label={t("apps.desktop.command.results", "Results")}
        >
          {filtered.length === 0 ? (
            <div className={styles.desktopCommandEmpty}>
              {t("apps.desktop.command.empty", "No matching commands")}
            </div>
          ) : (
            (["apps", "experts", "windows", "actions"] as const).map(
              (group) => {
                const groupItems = grouped[group];
                if (!groupItems?.length) return null;
                const meta = GROUP_LABEL[group];
                return (
                  <div key={group} className={styles.desktopCommandGroup}>
                    <div className={styles.desktopCommandGroupTitle}>
                      {t(meta.key, meta.fallback)}
                    </div>
                    {groupItems.map((item) => {
                      const index = filtered.indexOf(item);
                      return (
                        <button
                          key={item.id}
                          type="button"
                          role="option"
                          aria-selected={index === activeIndex}
                          className={[
                            styles.desktopCommandItem,
                            index === activeIndex
                              ? styles.desktopCommandItemActive
                              : "",
                          ]
                            .filter(Boolean)
                            .join(" ")}
                          data-testid="desktop-command-item"
                          data-item-id={item.id}
                          onPointerEnter={() => setActiveIndex(index)}
                          onClick={() => run(item)}
                        >
                          <span className={styles.desktopCommandItemIcon}>
                            {item.icon}
                          </span>
                          <span className={styles.desktopCommandItemText}>
                            <span className={styles.desktopCommandItemLabel}>
                              {item.label}
                            </span>
                            {item.hint && (
                              <span className={styles.desktopCommandItemHint}>
                                {item.hint}
                              </span>
                            )}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                );
              },
            )
          )}
        </div>

        <div className={styles.desktopCommandFooter}>
          <span>↑↓</span>
          <span>Enter</span>
          <span>Esc</span>
          <span className={styles.desktopCommandFooterHint}>
            {t("apps.desktop.command.footer", "Navigate · Open · Close")}
          </span>
        </div>
      </div>
    </div>
  );
}
