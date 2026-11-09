import { afterEach, describe, expect, it } from "vitest";
import {
  cancelStream,
  onStreamEvent,
  removeSession,
  sendTurn,
  type StreamEvent,
} from "./chatStore";

const SESSION = "test-bridge-stream";

type FakeInstance = {
  readyState: number;
  sent: string[];
  onopen: (() => void) | null;
  onmessage: ((event: { data: string }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  send: (data: string) => void;
  close: () => void;
};

describe("Web → IM bridge turn", () => {
  afterEach(() => {
    removeSession(SESSION);
  });

  it("sends reply_via and emits turnQueued for queue frames", async () => {
    const instances: FakeInstance[] = [];
    class FakeWebSocket {
      static instances = instances;
      static OPEN = 1;
      static CONNECTING = 0;
      static CLOSING = 2;
      static CLOSED = 3;
      readyState = 0;
      sent: string[] = [];
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      onerror: (() => void) | null = null;

      constructor() {
        instances.push(this);
        queueMicrotask(() => {
          this.readyState = 1;
          this.onopen?.();
        });
      }

      send(data: string) {
        this.sent.push(data);
      }

      close() {
        this.readyState = 3;
        this.onclose?.();
      }
    }

    const RealWS = globalThis.WebSocket;
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const events: StreamEvent[] = [];
    const unsubscribe = onStreamEvent((event) => events.push(event));
    try {
      const turn = sendTurn(
        SESSION,
        "hello",
        "agent-1",
        "",
        undefined,
        undefined,
        null,
        SESSION,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        "im",
      );
      await Promise.resolve();
      const ws = instances[0];
      expect(ws).toBeDefined();
      const payload = JSON.parse(ws.sent[0]) as Record<string, unknown>;
      expect(payload.reply_via).toBe("im");

      ws.onmessage?.({
        data: JSON.stringify({
          type: "turn.queued",
          thread_id: SESSION,
          turn_id: "bridge_x",
        }),
      });
      expect(events.some((event) => event.kind === "turnQueued")).toBe(true);

      cancelStream(SESSION);
      const frames = ws.sent.map(
        (row) => JSON.parse(row) as Record<string, unknown>,
      );
      const cancel = frames.find((frame) => frame.type === "cancel");
      expect(cancel?.turn_id).toBe("bridge_x");
      await turn;
    } finally {
      unsubscribe();
      globalThis.WebSocket = RealWS;
    }
  });
});
