import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { buildUrl, getToken } = vi.hoisted(() => ({
  buildUrl: vi.fn(() => "ws://test/notify"),
  getToken: vi.fn(() => "token"),
}));

vi.mock("../api/modules/wsNotifications", () => ({
  buildDashboardNotifyWsUrl: buildUrl,
}));

vi.mock("../api/request", () => ({ getAuthToken: getToken }));

import { useDashboardNotifySocket } from "./useDashboardNotifySocket";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.onclose?.();
  }
}

describe("useDashboardNotifySocket", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    buildUrl.mockClear();
    getToken.mockReturnValue("token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens a single socket for several consumers", () => {
    const first = renderHook(() => useDashboardNotifySocket(() => {}));
    const second = renderHook(() => useDashboardNotifySocket(() => {}));

    expect(FakeWebSocket.instances).toHaveLength(1);

    first.unmount();
    second.unmount();
  });

  it("fans one frame out to every subscriber", () => {
    const seenA: unknown[] = [];
    const seenB: unknown[] = [];
    const a = renderHook(() =>
      useDashboardNotifySocket((raw) => seenA.push(raw)),
    );
    const b = renderHook(() =>
      useDashboardNotifySocket((raw) => seenB.push(raw)),
    );

    FakeWebSocket.instances[0].onmessage?.({
      data: JSON.stringify({ type: "team.task.updated" }),
    });

    expect(seenA).toEqual([{ type: "team.task.updated" }]);
    expect(seenB).toEqual([{ type: "team.task.updated" }]);

    a.unmount();
    b.unmount();
  });

  it("closes the socket only when the last consumer unmounts", () => {
    const a = renderHook(() => useDashboardNotifySocket(() => {}));
    const b = renderHook(() => useDashboardNotifySocket(() => {}));
    const socket = FakeWebSocket.instances[0];
    const close = vi.spyOn(socket, "close");

    a.unmount();
    expect(close).not.toHaveBeenCalled();

    b.unmount();
    expect(close).toHaveBeenCalled();
  });
});
