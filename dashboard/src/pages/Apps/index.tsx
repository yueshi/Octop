import { useEffect, useState, type ReactNode } from "react";
import {
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
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
  type AppSummary,
  type AppTemplate,
} from "../../api/modules/apps";
import PageShell from "../../layouts/PageShell";
import { apiErrorMessage } from "../../utils/apiError";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useServerTimezone } from "../../hooks/useServerTimezone";
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

export default function AppsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const timeZone = useServerTimezone();
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const { agents } = useAgent();
  const [templateOpen, setTemplateOpen] = useState(false);
  const [templateForm] = Form.useForm<{
    agent_id: string;
    template: string;
    name?: string;
  }>();
  const [templates, setTemplates] = useState<AppTemplate[]>([]);
  const [templateLoading, setTemplateLoading] = useState(false);
  const [templateSaving, setTemplateSaving] = useState(false);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [restoreForm] = Form.useForm<{ agent_id: string }>();
  const [restoreFiles, setRestoreFiles] = useState<UploadFile[]>([]);
  const [restoreSaving, setRestoreSaving] = useState(false);

  const loadTemplates = async (agentId: string) => {
    setTemplateLoading(true);
    try {
      const data = await appsApi.listTemplates(agentId);
      setTemplates(data.templates);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      setTemplates([]);
    } finally {
      setTemplateLoading(false);
    }
  };

  const openTemplates = () => {
    setTemplateOpen(true);
    const first = agents[0]?.agent_id;
    templateForm.resetFields();
    if (first) {
      templateForm.setFieldsValue({ agent_id: first });
      void loadTemplates(first);
    }
  };

  const submitTemplate = async () => {
    const values = await templateForm.validateFields();
    setTemplateSaving(true);
    try {
      const detail = await appsApi.createFromTemplate({
        agent_id: values.agent_id,
        template: values.template,
        name: values.name,
      });
      setTemplateOpen(false);
      navigate(`/apps/${detail.app_id}`);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
    } finally {
      setTemplateSaving(false);
    }
  };

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
          <Button onClick={openTemplates}>{t("apps.fromTemplate")}</Button>
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

      <Modal
        title={t("apps.fromTemplate")}
        open={templateOpen}
        onCancel={() => setTemplateOpen(false)}
        onOk={() => void submitTemplate()}
        okText={t("apps.fromTemplate")}
        cancelText={t("common.cancel")}
        confirmLoading={templateSaving}
        destroyOnHidden
      >
        <Form form={templateForm} layout="vertical">
          <Form.Item
            name="agent_id"
            label={t("apps.agentLabel")}
            rules={[{ required: true }]}
          >
            <Select
              onChange={(value) => {
                templateForm.setFieldValue("template", undefined);
                void loadTemplates(value);
              }}
              options={agents.map((agent) => ({
                value: agent.agent_id,
                label: agent.name,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="template"
            label={t("apps.templateLabel")}
            rules={[{ required: true }]}
          >
            <Select
              loading={templateLoading}
              options={templates.map((item) => ({
                value: item.template,
                label: item.label,
              }))}
            />
          </Form.Item>
          <Form.Item name="name" label={t("apps.name")}>
            <Input maxLength={100} />
          </Form.Item>
        </Form>
      </Modal>

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
