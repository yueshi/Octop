import { useCallback, useEffect, useMemo, useRef } from "react";

import { getApiUrl } from "../../api/config";
import type { AppGameSession } from "../../api/modules/apps";

/** Fired when a game session ends so mounted MiniApps can reload their rows. */
export const MINIAPP_GAME_FINISHED_EVENT = "octop:miniapp-game-finished";

export interface MiniAppGameFinishedDetail {
  appId: string;
  sessionId: string;
}

/**
 * Deep link for the external game.
 *
 * The long-lived dashboard JWT never leaves the parent page: the game gets a
 * scoped session token in the URL fragment (not sent to the game's server) plus
 * the API base it should write results to.
 */
export function buildGameUrl(appId: string, session: AppGameSession): string {
  const url = new URL(session.url);
  const apiBase = new URL(getApiUrl(""), window.location.origin)
    .toString()
    .replace(/\/$/, "");
  url.hash = new URLSearchParams({
    app_id: appId,
    game_id: session.game_id,
    session_id: session.session_id,
    token: session.token,
    api: apiBase,
  }).toString();
  return url.toString();
}

interface Props {
  appId: string;
  session: AppGameSession;
  onFinished?: () => void;
}

/**
 * Trusted host for an external game opened from a MiniApp.
 *
 * This is deliberately not ``MiniAppFrame``: the game runs on its own origin
 * with its own CSP/resources, and writes results through its session token.
 * It tells the parent it is done with
 * ``postMessage({source: "octop-game", type: "session-finished"})``; closing
 * the tab counts as finished too.
 */
export default function GameFrame({ appId, session, onFinished }: Props) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const gameOrigin = useMemo(() => {
    try {
      return new URL(session.url).origin;
    } catch {
      return null;
    }
  }, [session.url]);
  const src = useMemo(() => {
    try {
      return buildGameUrl(appId, session);
    } catch {
      // A malformed URL in config.json must not take the chat page down.
      return null;
    }
  }, [appId, session]);
  const sandbox = useMemo(() => {
    // A game on the dashboard's own origin would share its storage (the
    // dashboard keeps its access token there), so it runs without
    // `allow-same-origin` and stays a separate origin.
    if (!gameOrigin || gameOrigin !== window.location.origin) {
      return session.sandbox;
    }
    return session.sandbox
      .replace(/allow-same-origin/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }, [gameOrigin, session.sandbox]);
  const onFinishedRef = useRef(onFinished);
  useEffect(() => {
    onFinishedRef.current = onFinished;
  }, [onFinished]);
  const latestRef = useRef({ appId, sessionId: session.session_id });
  useEffect(() => {
    latestRef.current = { appId, sessionId: session.session_id };
  }, [appId, session.session_id]);
  const notifyRefresh = useCallback(() => {
    const current = latestRef.current;
    window.dispatchEvent(
      new CustomEvent<MiniAppGameFinishedDetail>(MINIAPP_GAME_FINISHED_EVENT, {
        detail: { appId: current.appId, sessionId: current.sessionId },
      }),
    );
  }, []);
  const notifyFinished = useCallback(() => {
    notifyRefresh();
    onFinishedRef.current?.();
  }, [notifyRefresh]);

  // Unmount only: closing the game tab ends the session. This must not fire
  // when the session is replaced (relaunching the same game), or the app
  // would reload before the new game even started.
  useEffect(() => () => notifyFinished(), [notifyFinished]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = frameRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const data = event.data as {
        source?: string;
        type?: string;
        session_id?: string;
      } | null;
      if (!data || data.source !== "octop-game") return;
      if (data.session_id && data.session_id !== session.session_id) return;
      if (data.type === "result-written") {
        // Rows changed but play continues: refresh, do not end the session.
        notifyRefresh();
      } else if (data.type === "session-finished") {
        notifyFinished();
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [notifyFinished, notifyRefresh, session.session_id]);

  if (!src) {
    return (
      <div style={{ padding: 20, color: "var(--fn-text-secondary)" }}>
        {session.name}: {session.url}
      </div>
    );
  }

  return (
    <iframe
      ref={frameRef}
      title={session.name}
      src={src}
      sandbox={sandbox}
      allow="fullscreen; gamepad; autoplay; clipboard-write"
      allowFullScreen
      referrerPolicy="no-referrer"
      style={{
        width: "100%",
        height: "100%",
        flex: 1,
        minHeight: 0,
        border: "none",
        background: "#000",
        display: "block",
      }}
    />
  );
}
