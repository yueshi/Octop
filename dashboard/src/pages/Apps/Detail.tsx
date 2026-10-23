import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  ArrowLeft,
  Edit3,
  MessageSquareText,
  Plus,
  Share2,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";
import { message } from "../../utils/antdMessage";

import { appsApi, type AppDetail } from "../../api/modules/apps";
import PageShell from "../../layouts/PageShell";
import { apiErrorMessage, isNotFoundApiError } from "../../utils/apiError";
import { APP_CSP } from "./appCsp";

const { Text } = Typography;

/**
 * Bridge exposed to app HTML as `window.octopApp`. Every call goes through the
 * parent dashboard over postMessage so the iframe never sees a JWT and never
 * needs `allow-same-origin`.
 */
const APP_SDK = `(function () {
  var pending = new Map();
  var seq = 0;
  function call(method, params) {
    return new Promise(function (resolve, reject) {
      var id = ++seq;
      pending.set(id, { resolve: resolve, reject: reject });
      parent.postMessage(
        { source: "octop-app", type: "request", id: id, method: method, params: params || {} },
        "*"
      );
    });
  }
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.source !== "octop-app-host") return;
    var entry = pending.get(data.id);
    if (!entry) return;
    pending.delete(data.id);
    if (data.ok) entry.resolve(data.data);
    else entry.reject(new Error(data.error || "Request failed"));
  });
  window.octopApp = {
    schema: function () { return call("schema"); },
    listRows: function (options) { return call("rows", options || {}); },
    appendRow: function (row) { return call("append", { row: row }); }
  };
})();`;

function buildSrcDoc(html: string): string {
  const head = `<meta http-equiv="Content-Security-Policy" content="${APP_CSP}"><script>${APP_SDK}</script>`;
  const headMatch = /<head[^>]*>/i.exec(html);
  if (headMatch?.index !== undefined) {
    const at = headMatch.index + headMatch[0].length;
    return `${html.slice(0, at)}${head}${html.slice(at)}`;
  }
  const htmlMatch = /<html[^>]*>/i.exec(html);
  if (htmlMatch?.index !== undefined) {
    const at = htmlMatch.index + htmlMatch[0].length;
    return `${html.slice(0, at)}<head>${head}</head>${html.slice(at)}`;
  }
  const doctypeMatch = /<!doctype[^>]*>/i.exec(html);
  if (doctypeMatch?.index !== undefined) {
    const at = doctypeMatch.index + doctypeMatch[0].length;
    return `${html.slice(0, at)}<html><head>${head}</head><body>${html.slice(
      at,
    )}</body></html>`;
  }
  return `<!doctype html><html><head>${head}</head><body>${html}</body></html>`;
}

function permissionLabel(
  permission: AppDetail["permission"],
  t: (key: string) => string,
): string {
  if (permission === "edit_structure") return t("apps.editStructure");
  if (permission === "edit_data") return t("apps.editData");
  return t("apps.viewOnly");
}

interface ShareDraft {
  username: string;
  permission: "view" | "edit_data";
}

export default function AppDetailPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { appId = "" } = useParams<{ appId: string }>();
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const [detail, setDetail] = useState<AppDetail | null>(null);
  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [htmlLoading, setHtmlLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameForm] = Form.useForm<{ name: string; description: string }>();
  const [shareOpen, setShareOpen] = useState(false);
  const [shareLoading, setShareLoading] = useState(false);
  const [savingShare, setSavingShare] = useState(false);
  const [shares, setShares] = useState<ShareDraft[]>([]);

  const loadDetail = useCallback(async () => {
    setLoading(true);
    try {
      const data = await appsApi.get(appId);
      setDetail(data);
      setNotFound(false);
    } catch (err) {
      if (isNotFoundApiError(err)) {
        setNotFound(true);
      } else {
        message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      }
    } finally {
      setLoading(false);
    }
  }, [appId, t]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  useEffect(() => {
    if (!detail?.has_html) {
      setHtml("");
      return;
    }
    let alive = true;
    setHtmlLoading(true);
    appsApi
      .getHtml(appId)
      .then((text) => {
        if (alive) setHtml(text);
      })
      .catch((err) => {
        if (alive)
          message.error(apiErrorMessage(err, t("apps.loadHtmlFailed"), t));
      })
      .finally(() => {
        if (alive) setHtmlLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [appId, detail?.has_html, t]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = iframeRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const data = event.data as {
        source?: string;
        type?: string;
        id?: number;
        method?: string;
        params?: {
          limit?: number;
          offset?: number;
          row?: Record<string, unknown>;
        };
      } | null;
      if (
        !data ||
        data.source !== "octop-app" ||
        data.type !== "request" ||
        typeof data.id !== "number"
      ) {
        return;
      }
      const reply = (payload: Record<string, unknown>) => {
        frame.contentWindow?.postMessage(
          { source: "octop-app-host", id: data.id, ...payload },
          "*",
        );
      };
      void (async () => {
        try {
          if (data.method === "schema") {
            const current = await appsApi.get(appId);
            reply({ ok: true, data: { columns: current.columns } });
          } else if (data.method === "rows") {
            const rows = await appsApi.rows(
              appId,
              data.params?.limit,
              data.params?.offset,
            );
            reply({ ok: true, data: rows });
          } else if (data.method === "append") {
            const result = await appsApi.appendRow(
              appId,
              data.params?.row ?? {},
            );
            setDetail((prev) =>
              prev ? { ...prev, row_count: prev.row_count + 1 } : prev,
            );
            reply({ ok: true, data: result });
          } else {
            reply({ ok: false, error: "unsupported method" });
          }
        } catch (err) {
          reply({
            ok: false,
            error: err instanceof Error ? err.message : String(err),
          });
        }
      })();
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [appId]);

  const srcDoc = useMemo(() => (html ? buildSrcDoc(html) : ""), [html]);

  const openRename = () => {
    if (!detail) return;
    renameForm.setFieldsValue({
      name: detail.name,
      description: detail.description,
    });
    setRenameOpen(true);
  };

  const submitRename = async (values: {
    name: string;
    description: string;
  }) => {
    try {
      const updated = await appsApi.update(appId, {
        name: values.name,
        description: values.description ?? "",
      });
      setDetail(updated);
      setRenameOpen(false);
      message.success(t("apps.saveSuccess"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.saveFailed"), t));
    }
  };

  const openShare = async () => {
    setShareOpen(true);
    setShareLoading(true);
    try {
      const data = await appsApi.shares(appId);
      setShares(
        data.shares.map((item) => ({
          username: item.username,
          permission: item.permission,
        })),
      );
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.loadFailed"), t));
      setShareOpen(false);
    } finally {
      setShareLoading(false);
    }
  };

  const submitShare = async () => {
    const payload = shares
      .filter((item) => item.username.trim())
      .map((item) => ({
        username: item.username.trim(),
        permission: item.permission,
      }));
    setSavingShare(true);
    try {
      await appsApi.setShares(appId, payload);
      message.success(t("apps.saveSuccess"));
      setShareOpen(false);
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.saveFailed"), t));
    } finally {
      setSavingShare(false);
    }
  };

  const removeApp = async () => {
    try {
      await appsApi.remove(appId);
      message.success(t("apps.deleteSuccess"));
      navigate("/apps", { replace: true });
    } catch (err) {
      message.error(apiErrorMessage(err, t("apps.deleteFailed"), t));
    }
  };

  if (loading) {
    return (
      <PageShell title={t("apps.title")}>
        <div style={{ padding: 48, textAlign: "center" }}>
          <Spin />
        </div>
      </PageShell>
    );
  }

  if (notFound || !detail) {
    return (
      <PageShell title={t("apps.title")}>
        <Empty description={t("apps.notFound")} style={{ padding: "64px 0" }}>
          <Button onClick={() => navigate("/apps")}>{t("apps.back")}</Button>
        </Empty>
      </PageShell>
    );
  }

  const canEditStructure = detail.permission === "edit_structure";

  return (
    <PageShell
      title={detail.name}
      subtitle={detail.description || undefined}
      fill
      actions={
        <Space size={8}>
          <Tag style={{ marginInlineEnd: 0 }}>
            {permissionLabel(detail.permission, t)}
          </Tag>
          <Button
            type="text"
            icon={<ArrowLeft size={16} />}
            onClick={() => navigate("/apps")}
          >
            {t("apps.back")}
          </Button>
          {canEditStructure && (
            <>
              <Button
                icon={<Share2 size={16} />}
                onClick={() => void openShare()}
              >
                {t("apps.share")}
              </Button>
              <Button icon={<Edit3 size={16} />} onClick={openRename}>
                {t("apps.rename")}
              </Button>
              <Popconfirm
                title={t("apps.deleteTitle")}
                description={t("apps.deleteConfirm")}
                okButtonProps={{ danger: true }}
                okText={t("apps.delete")}
                cancelText={t("common.cancel")}
                onConfirm={() => void removeApp()}
              >
                <Button danger icon={<Trash2 size={16} />} />
              </Popconfirm>
            </>
          )}
        </Space>
      }
    >
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
        }}
      >
        {htmlLoading ? (
          <div style={{ padding: 48, textAlign: "center" }}>
            <Spin />
          </div>
        ) : detail.has_html && html ? (
          <iframe
            ref={iframeRef}
            title={t("apps.iframeTitle")}
            sandbox="allow-scripts"
            srcDoc={srcDoc}
            style={{
              flex: 1,
              width: "100%",
              minHeight: 0,
              border: 0,
              borderRadius: 8,
              background: "#fff",
            }}
          />
        ) : (
          <Empty
            image={
              <MessageSquareText size={48} strokeWidth={1.2} opacity={0.45} />
            }
            description={t("apps.noHtml")}
            style={{ padding: "64px 0" }}
          >
            <Button
              type="primary"
              onClick={() =>
                navigate(`/chat/${encodeURIComponent(detail.agent_id)}`)
              }
            >
              {t("apps.openChat")}
            </Button>
          </Empty>
        )}
      </div>

      <Modal
        title={t("apps.renameTitle")}
        open={renameOpen}
        onCancel={() => setRenameOpen(false)}
        onOk={() => renameForm.submit()}
        okText={t("apps.save")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form form={renameForm} layout="vertical" onFinish={submitRename}>
          <Form.Item
            name="name"
            label={t("apps.name")}
            rules={[{ required: true, message: t("apps.nameRequired") }]}
          >
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="description" label={t("apps.description")}>
            <Input.TextArea
              maxLength={2000}
              autoSize={{ minRows: 2, maxRows: 6 }}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("apps.shareTitle", { name: detail.name })}
        open={shareOpen}
        onCancel={() => setShareOpen(false)}
        onOk={() => void submitShare()}
        okText={t("apps.shareSave")}
        cancelText={t("common.cancel")}
        confirmLoading={savingShare}
        destroyOnHidden
      >
        <Text type="secondary">{t("apps.shareHint")}</Text>
        <div style={{ marginTop: 12 }}>
          {shareLoading ? (
            <div style={{ padding: 24, textAlign: "center" }}>
              <Spin />
            </div>
          ) : (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {shares.length === 0 && (
                <Text type="secondary">{t("apps.shareEmpty")}</Text>
              )}
              {shares.map((item, index) => (
                <Space.Compact key={index} style={{ width: "100%" }}>
                  <Input
                    placeholder={t("apps.shareUser")}
                    value={item.username}
                    onChange={(event) => {
                      const next = [...shares];
                      next[index] = {
                        ...next[index],
                        username: event.target.value,
                      };
                      setShares(next);
                    }}
                  />
                  <Select
                    style={{ width: 190 }}
                    value={item.permission}
                    options={[
                      { value: "view", label: t("apps.permissionView") },
                      {
                        value: "edit_data",
                        label: t("apps.permissionEditData"),
                      },
                    ]}
                    onChange={(permission) => {
                      const next = [...shares];
                      next[index] = { ...next[index], permission };
                      setShares(next);
                    }}
                  />
                  <Button
                    danger
                    icon={<Trash2 size={14} />}
                    onClick={() =>
                      setShares(shares.filter((_, i) => i !== index))
                    }
                  />
                </Space.Compact>
              ))}
              <Button
                type="dashed"
                icon={<Plus size={14} />}
                onClick={() =>
                  setShares([...shares, { username: "", permission: "view" }])
                }
                block
              >
                {t("apps.shareAdd")}
              </Button>
            </Space>
          )}
        </div>
      </Modal>
    </PageShell>
  );
}
