import { describe, expect, it, vi } from "vitest";

import { CAP } from "./capabilities";
import { checkMethodAllowed, isAppRequest } from "./bridge";

describe("bridge request checks", () => {
  it("accepts only well-formed requests from the app", () => {
    const request = {
      source: "octop-app",
      type: "request",
      id: 1,
      method: "rows",
    };
    expect(isAppRequest(request)).toBe(true);
    expect(isAppRequest({ ...request, source: "other" })).toBe(false);
    expect(isAppRequest({ ...request, type: "reply" })).toBe(false);
    expect(isAppRequest({ ...request, id: "1" })).toBe(false);
    expect(isAppRequest(null)).toBe(false);
  });

  it("denies a method the app did not declare", () => {
    expect(checkMethodAllowed("rows", [CAP.DATA_READ])).toBe(true);
    expect(checkMethodAllowed("append", [CAP.DATA_READ])).toBe(false);
    expect(checkMethodAllowed("generate", [CAP.DATA_READ])).toBe(false);
  });

  it("denies methods the bridge does not implement", () => {
    // An unknown method has no capability requirement, so allowing it would
    // open every method added later by default.
    expect(checkMethodAllowed("unknownMethod", undefined)).toBe(false);
    expect(checkMethodAllowed("unknownMethod", [CAP.DATA_READ])).toBe(false);
  });

  it("keeps legacy apps working", () => {
    // An app created before the manifest existed keeps every capability.
    expect(checkMethodAllowed("generate", undefined)).toBe(true);
    expect(checkMethodAllowed("launchGame", undefined)).toBe(true);
  });

  it("lets the page ask about itself", () => {
    expect(checkMethodAllowed("me", [])).toBe(true);
  });
});

describe("CSP violation reports", () => {
  it("forwards a violation from the app frame", async () => {
    const { attachBridge } = await import("./bridge");
    const frameWindow = {} as Window;
    const onViolation = vi.fn();

    const detach = attachBridge(
      { capabilities: [CAP.DATA_READ], handle: vi.fn(), onViolation },
      () => frameWindow,
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          source: "octop-app",
          type: "violation",
          directive: "connect-src",
          blockedURI: "https://evil.example/collect?d=1",
          disposition: "enforce",
        },
        source: frameWindow,
      }),
    );

    expect(onViolation).toHaveBeenCalledWith({
      kind: "csp_violation",
      directive: "connect-src",
      blockedURI: "https://evil.example/collect?d=1",
      disposition: "enforce",
    });
    detach();
  });

  it("keeps the sandbox-integrity kind", async () => {
    const { attachBridge } = await import("./bridge");
    const frameWindow = {} as Window;
    const onViolation = vi.fn();

    const detach = attachBridge(
      { capabilities: [CAP.DATA_READ], handle: vi.fn(), onViolation },
      () => frameWindow,
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          source: "octop-app",
          type: "violation",
          kind: "sandbox_integrity",
          directive: "sandbox-integrity",
        },
        source: frameWindow,
      }),
    );

    expect(onViolation).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "sandbox_integrity" }),
    );
    detach();
  });

  it("ignores violations from another window", async () => {
    const { attachBridge } = await import("./bridge");
    const frameWindow = {} as Window;
    const onViolation = vi.fn();

    const detach = attachBridge(
      { capabilities: [CAP.DATA_READ], handle: vi.fn(), onViolation },
      () => frameWindow,
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          source: "octop-app",
          type: "violation",
          directive: "connect-src",
        },
        source: {} as Window,
      }),
    );

    expect(onViolation).not.toHaveBeenCalled();
    detach();
  });

  it("throttles a flood", async () => {
    const { attachBridge } = await import("./bridge");
    const frameWindow = {} as Window;
    const onViolation = vi.fn();

    const detach = attachBridge(
      { capabilities: [CAP.DATA_READ], handle: vi.fn(), onViolation },
      () => frameWindow,
    );

    for (let i = 0; i < 25; i += 1) {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: {
            source: "octop-app",
            type: "violation",
            directive: "connect-src",
            blockedURI: `https://evil.example/${i}`,
          },
          source: frameWindow,
        }),
      );
    }

    // A broken page must not be able to fill the audit log.
    expect(onViolation).toHaveBeenCalledTimes(20);
    detach();
  });
});

describe("attachBridge", () => {
  it("only answers the frame it was given", async () => {
    const { attachBridge } = await import("./bridge");
    const postMessage = vi.fn();
    const frameWindow = { postMessage } as unknown as Window;
    const handle = vi.fn(() => ({ rows: [] }));

    const detach = attachBridge(
      { capabilities: [CAP.DATA_READ], handle, onDenied: vi.fn() },
      () => frameWindow,
    );

    // A message from any other window must not reach the handler.
    const other = { postMessage: vi.fn() } as unknown as Window;
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { source: "octop-app", type: "request", id: 1, method: "rows" },
        source: other,
      }),
    );
    expect(handle).not.toHaveBeenCalled();

    window.dispatchEvent(
      new MessageEvent("message", {
        data: { source: "octop-app", type: "request", id: 2, method: "rows" },
        source: frameWindow,
      }),
    );
    await Promise.resolve();
    expect(handle).toHaveBeenCalledWith("rows", {});
    expect(postMessage).toHaveBeenCalledWith(
      { source: "octop-app-host", id: 2, ok: true, data: { rows: [] } },
      "*",
    );

    detach();
  });

  it("reads a capability getter on every request", async () => {
    const { attachBridge } = await import("./bridge");
    const postMessage = vi.fn();
    const frameWindow = { postMessage } as unknown as Window;
    let capabilities: readonly string[] = [CAP.DATA_READ];
    const handle = vi.fn(() => ({ rows: [] }));

    const detach = attachBridge(
      { capabilities: () => capabilities, handle, onDenied: vi.fn() },
      () => frameWindow,
    );

    const send = (id: number) =>
      window.dispatchEvent(
        new MessageEvent("message", {
          data: {
            source: "octop-app",
            type: "request",
            id,
            method: "generate",
          },
          source: frameWindow,
        }),
      );

    send(4);
    await Promise.resolve();
    expect(handle).not.toHaveBeenCalled();
    expect(postMessage).toHaveBeenLastCalledWith(
      expect.objectContaining({ ok: false }),
      "*",
    );

    // The owner grants the capability without remounting the frame.
    capabilities = [CAP.DATA_READ, CAP.AI_GENERATE];
    send(5);
    await Promise.resolve();
    expect(handle).toHaveBeenCalledWith("generate", {});

    detach();
  });

  it("refuses a method the app did not declare", async () => {
    const { attachBridge } = await import("./bridge");
    const postMessage = vi.fn();
    const frameWindow = { postMessage } as unknown as Window;
    const handle = vi.fn();

    const detach = attachBridge(
      { capabilities: [CAP.DATA_READ], handle, onDenied: vi.fn() },
      () => frameWindow,
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          source: "octop-app",
          type: "request",
          id: 3,
          method: "generate",
        },
        source: frameWindow,
      }),
    );
    await Promise.resolve();
    expect(handle).not.toHaveBeenCalled();
    expect(postMessage).toHaveBeenCalledWith(
      {
        source: "octop-app-host",
        id: 3,
        ok: false,
        error: "capability not declared",
      },
      "*",
    );

    detach();
  });
});
