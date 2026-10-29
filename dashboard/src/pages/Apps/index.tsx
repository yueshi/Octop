import { useEffect, useState, type ReactNode } from "react";
import {
  Button,
  Card,
  Col,
  Empty,
  Row,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  AppWindow,
  Bot,
  Clock,
  MessageSquareText,
  Table2,
  User,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { message } from "../../utils/antdMessage";

import { appsApi, type AppSummary } from "../../api/modules/apps";
import PageShell from "../../layouts/PageShell";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";

const { Text, Paragraph } = Typography;

function permissionLabel(
  permission: AppSummary["permission"],
  t: (key: string) => string,
): string {
  if (permission === "edit_structure") return t("apps.editStructure");
  if (permission === "edit_data") return t("apps.editData");
  return t("apps.viewOnly");
}

function MetaItem({
  icon,
  children,
  title,
}: {
  icon: ReactNode;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 12,
        color: "var(--fn-text-tertiary)",
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "center" }}>
        {icon}
      </span>
      {title ? <Tooltip title={title}>{children}</Tooltip> : children}
    </span>
  );
}

export default function AppsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const timeZone = useServerTimezone();
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    appsApi
      .list()
      .then((data) => {
        if (alive) setApps(data.apps);
      })
      .catch((err) => {
        message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [t]);

  return (
    <PageShell title={t("apps.title")} subtitle={t("apps.subtitle")}>
      {loading ? (
        <div style={{ padding: 48, textAlign: "center" }}>
          <Spin />
        </div>
      ) : apps.length === 0 ? (
        <Empty
          image={<AppWindow size={48} strokeWidth={1.2} opacity={0.45} />}
          description={
            <Space direction="vertical" size={4}>
              <Text>{t("apps.empty")}</Text>
              <Text type="secondary">{t("apps.emptyHint")}</Text>
            </Space>
          }
          style={{ padding: "64px 0" }}
        >
          <Button
            type="primary"
            icon={<MessageSquareText size={16} />}
            onClick={() => navigate("/chat")}
          >
            {t("apps.openChat")}
          </Button>
        </Empty>
      ) : (
        <Row gutter={[16, 16]}>
          {apps.map((app) => (
            <Col key={app.app_id} xs={24} sm={12} lg={8} xl={6}>
              <Card
                hoverable
                onClick={() => navigate(`/apps/${app.app_id}`)}
                styles={{ body: { padding: 0 } }}
                style={{
                  overflow: "hidden",
                  height: "100%",
                  borderColor: "var(--fn-color-brand-border)",
                }}
              >
                <div
                  style={{
                    padding: "14px 16px 10px",
                    background:
                      "linear-gradient(180deg, var(--fn-color-brand-bg) 0%, transparent 100%)",
                    boxShadow: "inset 0 3px 0 var(--fn-color-brand)",
                  }}
                >
                  <Space
                    align="start"
                    style={{ width: "100%", justifyContent: "space-between" }}
                  >
                    <Text strong style={{ fontSize: 15 }}>
                      {app.name}
                    </Text>
                    <Space size={4} style={{ marginInlineEnd: 0 }}>
                      {!app.has_html && (
                        <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                          {t("apps.noInterface")}
                        </Tag>
                      )}
                      {app.permission !== "edit_structure" && (
                        <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                          {permissionLabel(app.permission, t)}
                        </Tag>
                      )}
                    </Space>
                  </Space>
                  <Paragraph
                    type="secondary"
                    ellipsis={{ rows: 2 }}
                    style={{ margin: "6px 0 0", minHeight: 44 }}
                  >
                    {app.description || "—"}
                  </Paragraph>
                </div>
                <div
                  style={{
                    borderTop: "1px solid var(--fn-color-brand-border)",
                    background: "var(--fn-color-brand-bg)",
                    padding: "8px 16px 10px",
                  }}
                >
                  <Space size={12} wrap style={{ marginBottom: 4 }}>
                    <MetaItem
                      icon={<User size={12} color="var(--fn-color-brand)" />}
                    >
                      {t("apps.createdBy", { name: app.owner_username || "—" })}
                    </MetaItem>
                    <MetaItem
                      icon={<Bot size={12} color="var(--fn-color-info)" />}
                    >
                      {app.agent_name || app.agent_id}
                    </MetaItem>
                  </Space>
                  <Space size={12} wrap>
                    <MetaItem
                      icon={<Table2 size={12} color="var(--fn-color-info)" />}
                    >
                      {t("apps.rows", { count: app.row_count })}
                    </MetaItem>
                    <MetaItem icon={<Clock size={12} />}>
                      {formatServerDateTime(app.updated_at, timeZone)}
                    </MetaItem>
                  </Space>
                </div>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </PageShell>
  );
}
