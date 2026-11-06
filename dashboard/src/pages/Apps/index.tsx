import { useEffect, useState, type ReactNode } from "react";
import {
  Button,
  Card,
  Col,
  Empty,
  Form,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import type { UploadFile } from "antd";
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

import {
  appsApi,
  appPreviewPath,
  type AppSummary,
} from "../../api/modules/apps";
import PageShell from "../../layouts/PageShell";
import CreateMiniAppModal from "./CreateMiniAppModal";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { usePreviewImage } from "../../hooks/usePreviewImage";
import { useAgent } from "../../context/AgentContext";

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

function AppPreview({ app }: { app: AppSummary }) {
  const url = usePreviewImage(
    app.has_preview ? appPreviewPath(app.app_id) : null,
  );
  if (!url) return null;
  return (
    <img
      src={url}
      alt={app.name}
      style={{
        display: "block",
        width: "100%",
        height: 140,
        objectFit: "cover",
        background: "var(--fn-color-brand-bg)",
      }}
    />
  );
}

export default function AppsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const timeZone = useServerTimezone();
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const { agents } = useAgent();
  const [createOpen, setCreateOpen] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [restoreForm] = Form.useForm<{ agent_id: string }>();
  const [restoreFiles, setRestoreFiles] = useState<UploadFile[]>([]);
  const [restoreSaving, setRestoreSaving] = useState(false);

  const openRestore = () => {
    setRestoreOpen(true);
    setRestoreFiles([]);
    restoreForm.resetFields();
    const first = agents[0]?.agent_id;
    if (first) restoreForm.setFieldsValue({ agent_id: first });
  };

  const submitRestore = async () => {
    const values = await restoreForm.validateFields();
    const file = restoreFiles[0]?.originFileObj;
    if (!file) {
      message.warning(t("apps.restoreFileRequired"));
      return;
    }
    setRestoreSaving(true);
    try {
      const detail = await appsApi.restore(values.agent_id, file);
      setRestoreOpen(false);
      navigate(`/apps/${detail.app_id}`);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setRestoreSaving(false);
    }
  };

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
    <PageShell
      title={t("apps.title")}
      subtitle={t("apps.subtitle")}
      actions={
        <Space>
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            {t("apps.create.button")}
          </Button>
          <Button onClick={openRestore}>{t("apps.restoreBackup")}</Button>
        </Space>
      }
    >
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
          <Space>
            <Button type="primary" onClick={() => setCreateOpen(true)}>
              {t("apps.create.button")}
            </Button>
            <Button
              icon={<MessageSquareText size={16} />}
              onClick={() => navigate("/chat")}
            >
              {t("apps.openChat")}
            </Button>
          </Space>
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
                <AppPreview app={app} />
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

      <CreateMiniAppModal
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onCreated={(appId) => {
          setCreateOpen(false);
          navigate(`/apps/${appId}`);
        }}
      />

      <Modal
        title={t("apps.restoreBackup")}
        open={restoreOpen}
        onCancel={() => setRestoreOpen(false)}
        onOk={() => void submitRestore()}
        okText={t("apps.restoreBackup")}
        cancelText={t("common.cancel")}
        confirmLoading={restoreSaving}
        destroyOnHidden
      >
        <Form form={restoreForm} layout="vertical">
          <Form.Item
            name="agent_id"
            label={t("apps.agentLabel")}
            rules={[{ required: true }]}
          >
            <Select
              options={agents.map((agent) => ({
                value: agent.agent_id,
                label: agent.name,
              }))}
            />
          </Form.Item>
          <Form.Item label={t("apps.restoreFile")}>
            <Upload
              accept=".zip"
              maxCount={1}
              fileList={restoreFiles}
              beforeUpload={() => false}
              onChange={({ fileList }) => setRestoreFiles(fileList)}
            >
              <Button>{t("apps.chooseFile")}</Button>
            </Upload>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("apps.restoreHint")}
            </Text>
          </Form.Item>
        </Form>
      </Modal>
    </PageShell>
  );
}
