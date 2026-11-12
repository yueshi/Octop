import { useState } from "react";
import { Button, Empty, Typography } from "antd";
import { AppWindow, ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import { appPreviewPath } from "../../../api/modules/apps";
import MiniAppFrame from "../../../components/MiniAppFrame";
import { usePreviewImage } from "../../../hooks/usePreviewImage";
import type { SessionApp } from "../utils/sessionApps";
import styles from "../index.module.less";

const { Text } = Typography;

function SessionAppThumb({ app }: { app: SessionApp }) {
  const url = usePreviewImage(
    app.has_preview ? appPreviewPath(app.app_id) : null,
  );
  if (!url) {
    return (
      <span className={styles.sessionAppThumbEmpty}>
        <AppWindow size={18} strokeWidth={1.4} />
      </span>
    );
  }
  return (
    <img
      className={styles.sessionAppThumb}
      src={url}
      alt={app.name}
      draggable={false}
    />
  );
}

/**
 * MiniApps created in the open conversation, shown in the chat dock.
 *
 * The session has no server-side app list, so the list is derived from the
 * `app_card` tool results in the transcript (see `collectSessionApps`).
 * Selecting one runs it inside the panel; the back button returns to the list.
 */
export default function ChatDockSessionApps({
  apps,
}: {
  apps: readonly SessionApp[];
}) {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = apps.find((app) => app.app_id === selectedId) ?? null;

  if (apps.length === 0) {
    return (
      <div className={styles.sessionAppsEmpty}>
        <Empty
          image={<AppWindow size={40} strokeWidth={1.2} opacity={0.45} />}
          description={
            <Text type="secondary">
              {t(
                "chat.sessionAppsEmpty",
                "No MiniApp in this conversation yet.",
              )}
            </Text>
          }
        />
      </div>
    );
  }

  if (selected) {
    return (
      <div className={styles.sessionAppsOpen}>
        <div className={styles.sessionAppsBar}>
          <Button
            type="text"
            size="small"
            icon={<ChevronLeft size={16} />}
            onClick={() => setSelectedId(null)}
          >
            {t("chat.sessionAppsBack", "Back to list")}
          </Button>
          <Text strong ellipsis={{ tooltip: selected.name }}>
            {selected.name}
          </Text>
        </div>
        <div className={styles.sessionAppsFrame}>
          <MiniAppFrame appId={selected.app_id} title={selected.name} fill />
        </div>
      </div>
    );
  }

  return (
    <div className={styles.sessionAppsList}>
      {apps.map((app) => (
        <button
          key={app.app_id}
          type="button"
          className={styles.sessionAppsRow}
          onClick={() => setSelectedId(app.app_id)}
        >
          <SessionAppThumb app={app} />
          <span className={styles.sessionAppsRowMain}>
            <Text strong ellipsis={{ tooltip: app.name }}>
              {app.name}
            </Text>
            <Text type="secondary" className={styles.sessionAppsRowMeta}>
              {typeof app.row_count === "number"
                ? t("apps.rows", "{{count}} rows", {
                    count: app.row_count,
                  })
                : app.description || ""}
            </Text>
          </span>
          <ChevronRight
            size={16}
            className={styles.sessionAppsRowChevron}
            aria-hidden
          />
        </button>
      ))}
    </div>
  );
}
