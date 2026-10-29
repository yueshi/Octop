import { useEffect, useRef } from "react";
import { buildDashboardNotifyWsUrl } from "../api/modules/wsNotifications";
import { getAuthToken } from "../api/request";

const PING_INTERVAL_MS = 25_000;
const MAX_RETRY_MS = 15_000;

type NotifyHandler = (raw: unknown) => void;

/**
 * One WebSocket shared by every consumer of the dashboard notify endpoint,
 * with reconnect backoff and ping keepalive.
 *
 * The socket is a module-level singleton: a per-hook socket meant the page
 * held one connection per consumer (push toasts plus every open team board),
 * each with its own ping timer and reconnect storm after a server restart,
 * all receiving identical frames. Consumers subscribe a callback instead and
 * filter by `type`.
 */
const handlers = new Set<NotifyHandler>();

let socket: WebSocket | null = null;
let retry = 0;
let reconnectTimer: number | null = null;
let pingTimer: number | null = null;
let subscriberCount = 0;

const clearReconnect = (): void => {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
};

const scheduleReconnect = (): void => {
  clearReconnect();
  const delay = Math.min(1000 * 2 ** retry, MAX_RETRY_MS);
  retry += 1;
  reconnectTimer = window.setTimeout(connect, delay);
};

function connect(): void {
  if (subscriberCount === 0) return;
  const token = getAuthToken();
  if (!token) {
    scheduleReconnect();
    return;
  }
  let next: WebSocket;
  try {
    next = new WebSocket(buildDashboardNotifyWsUrl());
  } catch {
    scheduleReconnect();
    return;
  }
  if (socket && socket !== next) {
    const previous = socket;
    socket = null;
    try {
      previous.close();
    } catch {
      // ignore
    }
  }
  socket = next;
  next.onopen = () => {
    retry = 0;
  };
  next.onmessage = (event) => {
    let raw: unknown;
    try {
      raw = JSON.parse(String(event.data));
    } catch {
      return;
    }
    if (
      raw &&
      typeof raw === "object" &&
      (raw as { type?: string }).type === "pong"
    ) {
      return;
    }
    for (const handler of handlers) handler(raw);
  };
  next.onclose = () => {
    if (socket !== next) return;
    socket = null;
    if (subscriberCount > 0) scheduleReconnect();
  };
}

const startPing = (): void => {
  if (pingTimer !== null) return;
  pingTimer = window.setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({ type: "ping" }));
      } catch {
        // ignore
      }
    }
  }, PING_INTERVAL_MS);
};

const stopPing = (): void => {
  if (pingTimer !== null) {
    window.clearInterval(pingTimer);
    pingTimer = null;
  }
};

const teardown = (): void => {
  clearReconnect();
  stopPing();
  if (socket) {
    const current = socket;
    socket = null;
    try {
      current.close();
    } catch {
      // ignore
    }
  }
};

/**
 * Subscribe to the shared notify socket. `onMessage` receives each parsed
 * JSON frame except `pong`; the same socket carries dashboard pushes, HITL
 * approvals and board events, so consumers filter by `type`.
 */
export function useDashboardNotifySocket(onMessage: NotifyHandler): void {
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    // The ref indirection keeps a re-rendered callback from resubscribing,
    // so the socket stays up for the whole page lifetime.
    const handler: NotifyHandler = (raw) => handlerRef.current(raw);
    handlers.add(handler);
    subscriberCount += 1;
    if (subscriberCount === 1) {
      retry = 0;
      connect();
      startPing();
    }

    return () => {
      handlers.delete(handler);
      subscriberCount -= 1;
      if (subscriberCount === 0) teardown();
    };
  }, []);
}
