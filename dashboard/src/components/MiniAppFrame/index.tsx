import { useEffect, useMemo, useRef, useState } from "react";
import { Modal, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { appsApi } from "../../api/modules/apps";
import {
  MINIAPP_GAME_FINISHED_EVENT,
  type MiniAppGameFinishedDetail,
} from "./GameFrame";
import { buildSrcDoc } from "./sdk";
import { effectiveCapabilities, isMethodAllowed } from "./capabilities";

const { Text } = Typography;

interface Props {
  appId: string;
  height?: number | string;
  title?: string;
  /** Fill the parent container instead of using a fixed height. */
  fill?: boolean;
  /** Launch an external game and return its session id (chat dock only). */
  onLaunchGame?: (gameId: string) => Promise<string>;
  /** Page capabilities; omit for legacy payloads (all page actions allowed). */
  capabilities?: readonly string[];
  /** Fill the chat composer (never sends automatically). */
  onComposeMessage?: (text: string) => void | Promise<void>;
}

interface BridgeParams {
  limit?: number;
  offset?: number;
  table?: string | null;
  row?: Record<string, unknown>;
  url?: string;
  name?: string;
  prompt?: string;
  system?: string;
  gameId?: string;
  text?: string;
}

/**
 * Render a MiniApp inside a sandboxed iframe and answer its `octopApp` SDK
 * calls over postMessage. Used by the chat app card; the app detail page keeps
 * its own richer bridge because it also refreshes its data panel.
 */
export default function MiniAppFrame({
  appId,
  height = 420,
  title,
  fill = false,
  onLaunchGame,
  capabilities,
  onComposeMessage,
}: Props) {
  const { t } = useTranslation();
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    appsApi
      .getHtml(appId)
      .then((text) => {
        if (!alive) return;
        if (!text.trim()) {
          setError(t("apps.create.cardNoHtml"));
          return;
        }
        setHtml(text);
      })
      .catch((err) => {
        if (alive) {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [appId, t]);

  // A game ran in the side dock: remount the app so it re-reads its rows.
  useEffect(() => {
    const onGameFinished = (event: Event) => {
      const detail = (event as CustomEvent<MiniAppGameFinishedDetail>).detail;
      if (!detail || detail.appId !== appId) return;
      setReloadKey((value) => value + 1);
    };
    window.addEventListener(MINIAPP_GAME_FINISHED_EVENT, onGameFinished);
    return () =>
      window.removeEventListener(MINIAPP_GAME_FINISHED_EVENT, onGameFinished);
  }, [appId]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = frameRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const data = event.data as {
        source?: string;
        type?: string;
        id?: number;
        method?: string;
        params?: BridgeParams;
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
          if (!isMethodAllowed(data.method, capabilities)) {
            reply({ ok: false, error: "capability not declared" });
            return;
          }
          if (data.method === "me") {
            const current = await appsApi.get(appId);
            reply({
              ok: true,
              data: {
                permission: current.permission,
                capabilities: effectiveCapabilities(
                  current.capabilities ?? capabilities,
                ),
              },
            });
          } else if (data.method === "schema") {
            const current = await appsApi.get(appId);
            const tableKey = data.params?.table || "main";
            if (tableKey === "main") {
              reply({
                ok: true,
                data: { table: tableKey, columns: current.columns },
              });
            } else {
              const table = current.tables.find(
                (item) => item.table_key === tableKey,
              );
              if (!table) throw new Error(`unknown table: ${tableKey}`);
              reply({
                ok: true,
                data: { table: tableKey, columns: table.columns },
              });
            }
          } else if (data.method === "tables") {
            const current = await appsApi.get(appId);
            reply({
              ok: true,
              data: {
                tables: current.tables.map((item) => ({
                  table_key: item.table_key,
                  label: item.label,
                  row_count: item.row_count,
                  is_main: item.is_main,
                })),
              },
            });
          } else if (data.method === "rows") {
            const rows = await appsApi.rows(
              appId,
              data.params?.limit,
              data.params?.offset,
              data.params?.table || "main",
            );
            reply({ ok: true, data: rows });
          } else if (data.method === "append") {
            const tableKey = data.params?.table || "main";
            const result = await appsApi.appendRow(
              appId,
              data.params?.row ?? {},
              tableKey,
            );
            reply({ ok: true, data: result });
          } else if (data.method === "generate") {
            const result = await appsApi.generate(appId, {
              prompt: String(data.params?.prompt ?? ""),
              system: String(data.params?.system ?? ""),
            });
            reply({ ok: true, data: result });
          } else if (data.method === "composeMessage") {
            const text = String(data.params?.text ?? "").trim();
            if (!text) {
              throw new Error(t("apps.composeEmpty", "Message is empty"));
            }
            if (text.length > 4000) {
              throw new Error(t("apps.composeTooLong", "Message is too long"));
            }
            if (!onComposeMessage) {
              throw new Error(
                t(
                  "apps.composeUnavailable",
                  "Message input is not available here",
                ),
              );
            }
            await onComposeMessage(text);
            reply({ ok: true, data: { filled: true } });
          } else if (data.method === "launchGame") {
            const gameId = String(data.params?.gameId ?? "").trim();
            if (!gameId) throw new Error("gameId is required");
            if (!onLaunchGame) {
              throw new Error(
                t("apps.gameUnavailable", "Games are not available here"),
              );
            }
            const sessionId = await onLaunchGame(gameId);
            reply({ ok: true, data: { sessionId, status: "opened" } });
          } else if (data.method === "openExternal") {
            const url = String(data.params?.url ?? "").trim();
            if (!/^https?:\/\//i.test(url)) {
              reply({
                ok: false,
                error: "openExternal requires an http(s) URL",
              });
            } else {
              Modal.confirm({
                title: t("apps.openExternalTitle"),
                content: url,
                okText: t("apps.openExternalConfirm"),
                cancelText: t("common.cancel"),
                onOk: () => {
                  window.open(url, "_blank", "noopener,noreferrer");
                },
              });
              reply({ ok: true, data: { opened: true } });
            }
          } else if (data.method === "asset") {
            const name = String(data.params?.name ?? "").trim();
            try {
              const blob = await appsApi.asset(appId, name);
              const mime = blob.type || "image/png";
              const reader = new FileReader();
              const dataUrl = await new Promise<string>((resolve, reject) => {
                reader.onload = () => resolve(String(reader.result));
                reader.onerror = () => reject(reader.error);
                reader.readAsDataURL(blob);
              });
              reply({ ok: true, data: { name, mime, data_url: dataUrl } });
            } catch (err) {
              reply({
                ok: false,
                error: err instanceof Error ? err.message : String(err),
              });
            }
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
  }, [appId, capabilities, onComposeMessage, onLaunchGame, t]);

  const srcDoc = useMemo(() => (html ? buildSrcDoc(html) : ""), [html]);

  if (loading) {
    return (
      <div style={{ padding: 32, textAlign: "center" }}>
        <Spin />
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: 20 }}>
        <Text type="secondary">{error}</Text>
      </div>
    );
  }
  return (
    <iframe
      key={reloadKey}
      ref={frameRef}
      title={title || "MiniApp"}
      srcDoc={srcDoc}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      style={{
        width: "100%",
        height: fill ? "100%" : height,
        minHeight: fill ? 420 : undefined,
        border: fill ? "none" : "1px solid var(--fn-color-brand-border)",
        borderRadius: fill ? 0 : 8,
        background: "#fff",
        display: "block",
        ...(fill
          ? { position: "absolute" as const, inset: 0, minHeight: 0 }
          : {}),
      }}
    />
  );
}
