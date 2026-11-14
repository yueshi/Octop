import { beforeEach, describe, expect, it } from "vitest";

import {
  addDesktopNotification,
  clearDesktopNotifications,
  getDesktopNotifications,
  markAllDesktopNotificationsRead,
  markDesktopNotificationRead,
} from "./desktopNotifications";

describe("desktopNotifications", () => {
  beforeEach(() => {
    clearDesktopNotifications();
  });

  it("adds unread notifications newest first", () => {
    addDesktopNotification({ kind: "info", title: "One" });
    addDesktopNotification({ kind: "success", title: "Two" });

    expect(getDesktopNotifications().map((item) => item.title)).toEqual([
      "Two",
      "One",
    ]);
    expect(getDesktopNotifications()[0].read).toBe(false);
  });

  it("suppresses duplicate unread notifications with one dedupe key", () => {
    addDesktopNotification({
      kind: "approval",
      title: "Approval",
      dedupeKey: "chat-1",
    });
    addDesktopNotification({
      kind: "approval",
      title: "Approval",
      dedupeKey: "chat-1",
    });

    expect(getDesktopNotifications()).toHaveLength(1);
  });

  it("marks one or all notifications as read", () => {
    const first = addDesktopNotification({ kind: "info", title: "One" })!;
    addDesktopNotification({ kind: "info", title: "Two" });

    markDesktopNotificationRead(first);
    expect(
      getDesktopNotifications().find((item) => item.id === first)?.read,
    ).toBe(true);

    markAllDesktopNotificationsRead();
    expect(getDesktopNotifications().every((item) => item.read)).toBe(true);
  });

  it("clears the list", () => {
    addDesktopNotification({ kind: "info", title: "One" });
    clearDesktopNotifications();

    expect(getDesktopNotifications()).toEqual([]);
  });
});
