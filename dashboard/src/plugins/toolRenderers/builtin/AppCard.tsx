import { Button, Card, Space, Tag, Typography } from "antd";
import { AppWindow, PanelRightOpen } from "lucide-react";
import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import { appsApi, appPreviewPath } from "../../../api/modules/apps";
import MiniAppFrame from "../../../components/MiniAppFrame";
import { usePreviewImage } from "../../../hooks/usePreviewImage";
import type { ToolRenderProps } from "../types";

const { Text, Paragraph } = Typography;

interface AppCardData {
  app_id: string;
  name: string;
  description?: string;
  row_count?: number;
  has_html?: boolean;
  has_preview?: boolean;
  capabilities?: string[];
}

function isAppCardData(data: unknown): data is AppCardData {
  return (
    !!data &&
    typeof data === "object" &&
    typeof (data as { app_id?: unknown }).app_id === "string"
  );
}

/** Chat card for `app_open` / `app_write_html` / template-created apps. */
export function AppCard({
  data,
  status,
  host,
  callId,
  docked,
}: ToolRenderProps) {
  const { t } = useTranslation();
  const app = isAppCardData(data) ? data : null;
  const appId = app?.app_id ?? "";
  const preview = usePreviewImage(
    app?.has_preview ? appPreviewPath(app.app_id) : null,
  );
  const launchGame = useCallback(
    async (gameId: string) => {
      if (!appId) throw new Error("app is required");
      const session = await appsApi.createGameSession(appId, gameId);
      host.openGamePanel({ appId, session });
      return session.session_id;
    },
    [appId, host],
  );

  if (!app) {
    return (
      <Text type="secondary">
        {t("apps.create.cardInvalid", "No app data")}
      </Text>
    );
  }

  if (docked) {
    return (
      <div
        style={{
          position: "relative",
          height: "100%",
          minHeight: 420,
          display: "flex",
          flexDirection: "column",
        }}
      >
        <MiniAppFrame
          appId={app.app_id}
          title={app.name}
          capabilities={app.capabilities}
          fill
          onLaunchGame={launchGame}
        />
      </div>
    );
  }

  const canOpen = Boolean(app.has_html) && status === "done" && !!callId;
  const openSidePanel = () => {
    if (!canOpen || !callId) return;
    host.openSidePanel({
      callId,
      title: app.name,
      toolName: "app_open",
    });
  };

  return (
    <Card
      size="small"
      style={{
        width: 280,
        maxWidth: "100%",
        borderColor: "var(--fn-color-brand-border)",
      }}
      styles={{ body: { padding: 10 } }}
    >
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        {preview ? (
          <img
            src={preview}
            alt={app.name}
            style={{
              display: "block",
              width: "100%",
              aspectRatio: "1200 / 630",
              objectFit: "cover",
              borderRadius: 6,
              border: "1px solid var(--fn-color-brand-border)",
              background: "var(--fn-color-brand-bg)",
            }}
          />
        ) : (
          <div
            style={{
              width: "100%",
              aspectRatio: "1200 / 630",
              display: "grid",
              placeItems: "center",
              borderRadius: 6,
              background: "var(--fn-color-brand-bg)",
              color: "var(--fn-color-brand)",
            }}
          >
            <AppWindow size={30} />
          </div>
        )}

        <Space size={6} wrap>
          <Text strong>{app.name}</Text>
          <Tag>{t("apps.rows", { count: app.row_count ?? 0 })}</Tag>
        </Space>
        {app.description ? (
          <Paragraph
            type="secondary"
            ellipsis={{ rows: 2 }}
            style={{ margin: 0 }}
          >
            {app.description}
          </Paragraph>
        ) : null}
        <Button
          type="primary"
          size="small"
          block
          icon={<PanelRightOpen size={14} />}
          disabled={!canOpen}
          onClick={openSidePanel}
        >
          {t("apps.create.cardOpenSidePanel", "Open in sidebar")}
        </Button>
        {!app.has_html ? (
          <Text type="secondary">{t("apps.create.cardNoHtml")}</Text>
        ) : null}
      </Space>
    </Card>
  );
}
