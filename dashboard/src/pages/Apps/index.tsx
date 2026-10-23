import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Col,
  Empty,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { AppWindow, MessageSquareText } from "lucide-react";
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
                styles={{ body: { padding: 16 } }}
              >
                <Space
                  align="start"
                  style={{ width: "100%", justifyContent: "space-between" }}
                >
                  <Text strong style={{ fontSize: 15 }}>
                    {app.name}
                  </Text>
                  {app.permission !== "edit_structure" && (
                    <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                      {permissionLabel(app.permission, t)}
                    </Tag>
                  )}
                </Space>
                <Paragraph
                  type="secondary"
                  ellipsis={{ rows: 2 }}
                  style={{ margin: "6px 0 12px", minHeight: 44 }}
                >
                  {app.description || "—"}
                </Paragraph>
                <Space size={8} wrap>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {app.agent_name || app.agent_id}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    · {t("apps.rows", { count: app.row_count })}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    · {formatServerDateTime(app.updated_at, timeZone)}
                  </Text>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </PageShell>
  );
}
