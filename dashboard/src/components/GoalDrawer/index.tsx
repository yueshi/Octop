import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Spin,
  Switch,
  Tag,
  Typography,
  message,
} from "antd";
import { Link2, Paperclip, Target } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  goalsApi,
  type AgentGoal,
  type ArtifactType,
  type GoalDeliverable,
} from "../../api/modules/goals";
import { apiErrorMessage } from "../../utils/apiError";

const { Paragraph, Text } = Typography;

export interface GoalDrawerProps {
  open: boolean;
  agentId: string | null;
  agentKind: "expert" | "team";
  memberOptions?: Array<{ id: string; name: string }>;
  onClose: () => void;
  onChanged?: () => void;
}

interface GoalFormValues {
  title: string;
  description?: string;
  success_criteria?: string;
  deliverables?: Array<{
    title: string;
    description?: string;
    format?: string;
    required?: boolean;
  }>;
}

function artifactUrl(
  deliverable: GoalDeliverable,
  agentId: string,
): string | null {
  if (deliverable.artifact_type === "file") {
    const owner = deliverable.artifact_agent_id || agentId;
    return `/api/agents/${owner}/workspace/download?path=${encodeURIComponent(
      deliverable.artifact_ref,
    )}&from_workspace=true`;
  }
  if (deliverable.artifact_type === "link") return deliverable.artifact_ref;
  return null;
}

export default function GoalDrawer({
  open,
  agentId,
  agentKind,
  memberOptions = [],
  onClose,
  onChanged,
}: GoalDrawerProps) {
  const { t } = useTranslation();
  const [goal, setGoal] = useState<AgentGoal | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false);
  const [createForm] = Form.useForm<GoalFormValues>();
  const [editForm] = Form.useForm<GoalFormValues>();
  const [deliverableForm] = Form.useForm();
  const [artifactForm] = Form.useForm();
  const [deliverableOpen, setDeliverableOpen] = useState(false);
  const [artifactTarget, setArtifactTarget] = useState<GoalDeliverable | null>(
    null,
  );
  const artifactType = Form.useWatch("artifact_type", artifactForm) as
    | ArtifactType
    | undefined;

  const load = useCallback(async () => {
    if (!agentId) return;
    setLoading(true);
    try {
      const rows = await goalsApi.list(agentId);
      setGoal(rows.find((row) => row.status === "active") ?? null);
    } catch (err) {
      message.error(apiErrorMessage(err, t("goals.loadFailed"), t));
    } finally {
      setLoading(false);
    }
  }, [agentId, t]);

  useEffect(() => {
    if (open) {
      setEditing(false);
      setDeliverableOpen(false);
      setArtifactTarget(null);
      void load();
    }
  }, [open, load]);

  const notifyChanged = useCallback(() => {
    onChanged?.();
  }, [onChanged]);

  const createGoal = useCallback(async () => {
    if (!agentId) return;
    const values = await createForm.validateFields();
    setSaving(true);
    try {
      await goalsApi.create(agentId, {
        title: values.title,
        description: values.description,
        success_criteria: values.success_criteria,
        deliverables: (values.deliverables ?? []).map((item) => ({
          title: item.title,
          description: item.description,
          format: item.format,
          required: item.required ?? true,
        })),
      });
      message.success(t("goals.created"));
      createForm.resetFields();
      await load();
      notifyChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("goals.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  }, [agentId, createForm, load, notifyChanged, t]);

  const saveGoal = useCallback(async () => {
    if (!agentId || !goal) return;
    const values = await editForm.validateFields();
    setSaving(true);
    try {
      await goalsApi.update(agentId, goal.goal_id, {
        title: values.title,
        description: values.description,
        success_criteria: values.success_criteria,
      });
      message.success(t("goals.saved"));
      setEditing(false);
      await load();
      notifyChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("goals.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  }, [agentId, editForm, goal, load, notifyChanged, t]);

  const addDeliverable = useCallback(async () => {
    if (!agentId || !goal) return;
    const values = await deliverableForm.validateFields();
    setSaving(true);
    try {
      await goalsApi.addDeliverable(agentId, goal.goal_id, values);
      message.success(t("goals.deliverableAdded"));
      deliverableForm.resetFields();
      setDeliverableOpen(false);
      await load();
      notifyChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("goals.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  }, [agentId, deliverableForm, goal, load, notifyChanged, t]);

  const attachArtifact = useCallback(async () => {
    if (!agentId || !goal || !artifactTarget) return;
    const values = await artifactForm.validateFields();
    setSaving(true);
    try {
      await goalsApi.updateDeliverable(
        agentId,
        goal.goal_id,
        artifactTarget.deliverable_id,
        values,
      );
      message.success(t("goals.artifactSaved"));
      setArtifactTarget(null);
      await load();
      notifyChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("goals.saveFailed"), t));
    } finally {
      setSaving(false);
    }
  }, [agentId, artifactForm, artifactTarget, goal, load, notifyChanged, t]);

  const removeDeliverable = useCallback(
    async (deliverable: GoalDeliverable) => {
      if (!agentId || !goal) return;
      try {
        await goalsApi.deleteDeliverable(
          agentId,
          goal.goal_id,
          deliverable.deliverable_id,
        );
        message.success(t("goals.deliverableDeleted"));
        await load();
        notifyChanged();
      } catch (err) {
        message.error(apiErrorMessage(err, t("goals.saveFailed"), t));
      }
    },
    [agentId, goal, load, notifyChanged, t],
  );

  const openArtifact = (deliverable: GoalDeliverable) => {
    setArtifactTarget(deliverable);
    artifactForm.setFieldsValue({
      status: deliverable.status,
      artifact_type: deliverable.artifact_type,
      artifact_ref: deliverable.artifact_ref,
      artifact_name: deliverable.artifact_name,
      artifact_agent_id:
        deliverable.artifact_agent_id ||
        (deliverable.artifact_type === "file" ? agentId : undefined),
    });
  };

  const openEdit = () => {
    if (!goal) return;
    editForm.setFieldsValue({
      title: goal.title,
      description: goal.description ?? "",
      success_criteria: goal.success_criteria ?? "",
    });
    setEditing(true);
  };

  const ownerOptions = [
    ...(agentId ? [{ id: agentId, name: t("goals.host") }] : []),
    ...memberOptions.filter((item) => item.id !== agentId),
  ];

  const content = loading ? (
    <div style={{ textAlign: "center", padding: 48 }}>
      <Spin />
    </div>
  ) : !goal ? (
    <Form form={createForm} layout="vertical">
      <Form.Item
        name="title"
        label={t("goals.form.title")}
        rules={[{ required: true }]}
      >
        <Input maxLength={200} />
      </Form.Item>
      <Form.Item name="description" label={t("goals.form.description")}>
        <Input.TextArea rows={2} maxLength={5000} />
      </Form.Item>
      <Form.Item
        name="success_criteria"
        label={t("goals.form.successCriteria")}
      >
        <Input.TextArea rows={2} maxLength={5000} />
      </Form.Item>
      <Divider orientation="left">{t("goals.form.deliverables")}</Divider>
      <Form.List name="deliverables">
        {(fields, { add, remove }) => (
          <>
            {fields.map((field) => (
              <div
                key={field.key}
                style={{
                  border: "1px solid var(--fn-border-primary)",
                  borderRadius: 8,
                  padding: 12,
                  marginBottom: 10,
                }}
              >
                <Form.Item
                  name={[field.name, "title"]}
                  label={t("goals.form.deliverableTitle")}
                  rules={[{ required: true }]}
                >
                  <Input maxLength={200} />
                </Form.Item>
                <Form.Item
                  name={[field.name, "description"]}
                  label={t("goals.form.deliverableDesc")}
                >
                  <Input.TextArea rows={2} maxLength={1000} />
                </Form.Item>
                <div style={{ display: "flex", gap: 12 }}>
                  <Form.Item
                    name={[field.name, "format"]}
                    label={t("goals.form.deliverableFormat")}
                    style={{ flex: 1 }}
                  >
                    <Input maxLength={200} />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, "required"]}
                    label={t("goals.form.required")}
                    valuePropName="checked"
                    initialValue
                    style={{ width: 90 }}
                  >
                    <Switch />
                  </Form.Item>
                </div>
                <Button danger type="link" onClick={() => remove(field.name)}>
                  {t("common.delete")}
                </Button>
              </div>
            ))}
            <Button type="dashed" block onClick={() => add({ required: true })}>
              {t("goals.form.addDeliverable")}
            </Button>
          </>
        )}
      </Form.List>
      <Button
        type="primary"
        loading={saving}
        onClick={() => void createGoal()}
        style={{ marginTop: 16 }}
      >
        {t("goals.form.create")}
      </Button>
    </Form>
  ) : (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <Text strong style={{ fontSize: 16 }}>
            {goal.title}
          </Text>
          <div style={{ marginTop: 4 }}>
            <Tag color="processing">{goal.status}</Tag>
          </div>
        </div>
        <Button onClick={openEdit}>{t("common.edit")}</Button>
      </div>
      {editing ? (
        <Form form={editForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item
            name="title"
            label={t("goals.form.title")}
            rules={[{ required: true }]}
          >
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item name="description" label={t("goals.form.description")}>
            <Input.TextArea rows={2} maxLength={5000} />
          </Form.Item>
          <Form.Item
            name="success_criteria"
            label={t("goals.form.successCriteria")}
          >
            <Input.TextArea rows={2} maxLength={5000} />
          </Form.Item>
          <Button
            type="primary"
            loading={saving}
            onClick={() => void saveGoal()}
          >
            {t("common.save")}
          </Button>
        </Form>
      ) : (
        <div style={{ marginTop: 12 }}>
          {goal.description ? (
            <Paragraph type="secondary" style={{ whiteSpace: "pre-wrap" }}>
              {goal.description}
            </Paragraph>
          ) : null}
          {goal.success_criteria ? (
            <Paragraph style={{ whiteSpace: "pre-wrap" }}>
              <Text strong>{t("goals.form.successCriteria")}: </Text>
              {goal.success_criteria}
            </Paragraph>
          ) : null}
        </div>
      )}

      <Divider orientation="left">
        <Target size={14} /> {t("goals.deliverables")}
      </Divider>
      {goal.deliverables.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("goals.noDeliverables")}
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {goal.deliverables.map((deliverable) => {
            const url = artifactUrl(deliverable, agentId ?? "");
            return (
              <div
                key={deliverable.deliverable_id}
                style={{
                  border: "1px solid var(--fn-border-primary)",
                  borderRadius: 8,
                  padding: 12,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    flexWrap: "wrap",
                  }}
                >
                  <Text strong>{deliverable.title}</Text>
                  {deliverable.required ? <Tag color="red">*</Tag> : null}
                  <Tag
                    color={
                      deliverable.status === "produced" ? "success" : "default"
                    }
                  >
                    {t(`goals.status.${deliverable.status}`)}
                  </Tag>
                  {deliverable.format ? (
                    <Text type="secondary">{deliverable.format}</Text>
                  ) : null}
                </div>
                {deliverable.description ? (
                  <Paragraph
                    type="secondary"
                    style={{ margin: "6px 0", whiteSpace: "pre-wrap" }}
                  >
                    {deliverable.description}
                  </Paragraph>
                ) : null}
                {deliverable.artifact_type === "text" &&
                deliverable.artifact_ref ? (
                  <Paragraph
                    style={{ margin: "6px 0", whiteSpace: "pre-wrap" }}
                  >
                    {deliverable.artifact_ref}
                  </Paragraph>
                ) : null}
                {deliverable.artifact_type === "file" && url ? (
                  <a href={url} target="_blank" rel="noreferrer">
                    <Paperclip size={13} />{" "}
                    {deliverable.artifact_name || deliverable.artifact_ref}
                  </a>
                ) : null}
                {deliverable.artifact_type === "link" && url ? (
                  <a href={url} target="_blank" rel="noreferrer">
                    <Link2 size={13} /> {deliverable.artifact_name || url}
                  </a>
                ) : null}
                <div style={{ marginTop: 8 }}>
                  <Button
                    size="small"
                    onClick={() => openArtifact(deliverable)}
                  >
                    {t("goals.attachArtifact")}
                  </Button>
                  <Popconfirm
                    title={t("goals.deleteConfirm")}
                    onConfirm={() => void removeDeliverable(deliverable)}
                    okText={t("common.delete")}
                    cancelText={t("common.cancel")}
                  >
                    <Button size="small" danger type="link">
                      {t("common.delete")}
                    </Button>
                  </Popconfirm>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <Button
        type="dashed"
        block
        style={{ marginTop: 12 }}
        onClick={() => setDeliverableOpen(true)}
      >
        {t("goals.form.addDeliverable")}
      </Button>
    </>
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={agentKind === "team" ? 560 : 520}
      title={t("goals.title")}
      destroyOnHidden
    >
      {content}

      <Modal
        open={deliverableOpen}
        title={t("goals.form.addDeliverable")}
        onCancel={() => setDeliverableOpen(false)}
        onOk={() => void addDeliverable()}
        confirmLoading={saving}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form form={deliverableForm} layout="vertical">
          <Form.Item
            name="title"
            label={t("goals.form.deliverableTitle")}
            rules={[{ required: true }]}
          >
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item name="description" label={t("goals.form.deliverableDesc")}>
            <Input.TextArea rows={2} maxLength={1000} />
          </Form.Item>
          <Form.Item name="format" label={t("goals.form.deliverableFormat")}>
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item
            name="required"
            label={t("goals.form.required")}
            valuePropName="checked"
            initialValue
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={artifactTarget !== null}
        title={t("goals.attachArtifact")}
        onCancel={() => setArtifactTarget(null)}
        onOk={() => void attachArtifact()}
        confirmLoading={saving}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form form={artifactForm} layout="vertical">
          <Form.Item name="status" label={t("goals.statusLabel")}>
            <Select
              options={[
                { value: "pending", label: t("goals.status.pending") },
                { value: "produced", label: t("goals.status.produced") },
              ]}
            />
          </Form.Item>
          <Form.Item name="artifact_type" label={t("goals.artifactType")}>
            <Select
              options={[
                { value: "none", label: t("goals.artifact.none") },
                { value: "file", label: t("goals.artifact.file") },
                { value: "link", label: t("goals.artifact.link") },
                { value: "text", label: t("goals.artifact.text") },
              ]}
            />
          </Form.Item>
          {artifactType === "file" ? (
            <>
              <Form.Item
                name="artifact_agent_id"
                label={t("goals.artifact.agent")}
              >
                <Select
                  options={ownerOptions.map((item) => ({
                    value: item.id,
                    label: item.name,
                  }))}
                />
              </Form.Item>
              <Form.Item
                name="artifact_ref"
                label={t("goals.artifact.filePath")}
                rules={[{ required: true }]}
              >
                <Input placeholder="reports/plan.docx" />
              </Form.Item>
            </>
          ) : null}
          {artifactType === "link" ? (
            <Form.Item
              name="artifact_ref"
              label={t("goals.artifact.linkUrl")}
              rules={[{ required: true }]}
            >
              <Input placeholder="https://…" />
            </Form.Item>
          ) : null}
          {artifactType === "text" ? (
            <Form.Item
              name="artifact_ref"
              label={t("goals.artifact.textBody")}
              rules={[{ required: true }]}
            >
              <Input.TextArea rows={5} maxLength={4000} />
            </Form.Item>
          ) : null}
          <Form.Item name="artifact_name" label={t("goals.artifact.name")}>
            <Input maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </Drawer>
  );
}
