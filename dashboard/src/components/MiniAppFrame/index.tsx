import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Modal, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { appsApi } from "../../api/modules/apps";
import {
  MINIAPP_GAME_FINISHED_EVENT,
  type MiniAppGameFinishedDetail,
} from "./GameFrame";
import { buildSrcDoc, hasEmbeddedFrame } from "./sdk";
import { buildWrapperDoc } from "./wrapper";
import { attachBridge, type AppViolation, type BridgeParams } from "./bridge";
import { effectiveCapabilities } from "./capabilities";

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

/**
 * Render a MiniApp inside a sandboxed iframe and answer its `octopApp` SDK
 * calls over postMessage. Used by the chat app card; the app detail page keeps
 * its own richer bridge because it also refreshes its data panel.
 *
 * The app HTML runs one level deeper than the dashboard: a fixed wrapper
 * document owns the frame, so the app cannot navigate itself away and take the
 * bridge with it. See `wrapper.ts` for why that matters.
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
  const [escaped, setEscaped] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    setEscaped(false);
    appsApi
      .getHtml(appId)
      .then((text) => {
        if (!alive) return;
        if (!text.trim()) {
          setError(t("apps.create.cardNoHtml"));
          return;
        }
        if (hasEmbeddedFrame(text)) {
          setError(t("apps.sandboxEmbeddedFrame"));
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

  const reportViolation = useCallback(
    (violation: AppViolation) => {
      // Telemetry only: a failed report must not disturb the page.
      void appsApi.reportViolation(appId, violation).catch(() => {});
    },
    [appId],
  );

  // The wrapper reports that the app frame navigated away. Stop there: the
  // escaping page would otherwise inherit the bridge and could read every row
  // through the host.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = frameRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const data = event.data as { source?: string; type?: string } | null;
      if (!data || data.source !== "octop-app-host") return;
      if (data.type === "escaped") setEscaped(true);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => {
    const detach = attachBridge(
      {
        capabilities,
        onViolation: reportViolation,
        handle: async (method: string, params: BridgeParams) => {
          if (method === "me") {
            const current = await appsApi.get(appId);
            return {
              permission: current.permission,
              capabilities: effectiveCapabilities(
                current.capabilities ?? capabilities,
              ),
            };
          }
          if (method === "schema") {
            const current = await appsApi.get(appId);
            const tableKey = params.table || "main";
            if (tableKey === "main") {
              return { table: tableKey, columns: current.columns };
            }
            const table = current.tables.find(
              (item) => item.table_key === tableKey,
            );
            if (!table) throw new Error(`unknown table: ${tableKey}`);
            return { table: tableKey, columns: table.columns };
          }
          if (method === "tables") {
            const current = await appsApi.get(appId);
            return {
              tables: current.tables.map((item) => ({
                table_key: item.table_key,
                label: item.label,
                row_count: item.row_count,
                is_main: item.is_main,
              })),
            };
          }
          if (method === "rows") {
            return await appsApi.rows(
              appId,
              params.limit,
              params.offset,
              params.table || "main",
            );
          }
          if (method === "append") {
            return await appsApi.appendRow(
              appId,
              params.row ?? {},
              params.table || "main",
            );
          }
          if (method === "generate") {
            return await appsApi.generate(appId, {
              prompt: String(params.prompt ?? ""),
              system: String(params.system ?? ""),
            });
          }
          if (method === "composeMessage") {
            const text = String(params.text ?? "").trim();
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
            return { filled: true };
          }
          if (method === "launchGame") {
            const gameId = String(params.gameId ?? "").trim();
            if (!gameId) throw new Error("gameId is required");
            if (!onLaunchGame) {
              throw new Error(
                t("apps.gameUnavailable", "Games are not available here"),
              );
            }
            const sessionId = await onLaunchGame(gameId);
            return { sessionId, status: "opened" };
          }
          if (method === "openExternal") {
            const url = String(params.url ?? "").trim();
            if (!/^https?:\/\//i.test(url)) {
              throw new Error("openExternal requires an http(s) URL");
            }
            // The app page runs sandboxed without popups, so the parent has
            // to open the link — after the user confirms the target domain.
            Modal.confirm({
              title: t("apps.openExternalTitle"),
              content: url,
              okText: t("apps.openExternalConfirm"),
              cancelText: t("common.cancel"),
              onOk: () => {
                window.open(url, "_blank", "noopener,noreferrer");
              },
            });
            return { opened: true };
          }
          if (method === "asset") {
            const name = String(params.name ?? "").trim();
            const blob = await appsApi.asset(appId, name);
            const mime = blob.type || "image/png";
            const reader = new FileReader();
            const dataUrl = await new Promise<string>((resolve, reject) => {
              reader.onload = () => resolve(String(reader.result));
              reader.onerror = () => reject(reader.error);
              reader.readAsDataURL(blob);
            });
            return { name, mime, data_url: dataUrl };
          }
          throw new Error("unsupported method");
        },
      },
      () => frameRef.current?.contentWindow ?? null,
    );
    return detach;
  }, [appId, capabilities, onComposeMessage, onLaunchGame, reportViolation, t]);

  const srcDoc = useMemo(() => {
    if (!html) return "";
    return buildWrapperDoc(buildSrcDoc(html));
  }, [html]);

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
  if (escaped) {
    return (
      <div style={{ padding: 20 }}>
        <Text type="warning">
          {t(
            "apps.sandboxEscaped",
            "This MiniApp tried to leave its sandbox, so it was stopped.",
          )}
        </Text>
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
