import { useSyncExternalStore } from "react";

export type DesktopNotificationKind =
  | "approval"
  | "success"
  | "info"
  | "error"
  | "task"
  | "security"
  | "cron";

export interface DesktopNotificationInput {
  kind: DesktopNotificationKind;
  title: string;
  body?: string;
  /** Desktop window to focus when the notification is clicked. */
  windowId?: string;
  /** Suppress duplicate unread notifications with the same key. */
  dedupeKey?: string;
}

export interface DesktopNotification extends DesktopNotificationInput {
  id: string;
  createdAt: number;
  read: boolean;
}

const MAX_NOTIFICATIONS = 50;

let items: DesktopNotification[] = [];
let sequence = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function addDesktopNotification(
  input: DesktopNotificationInput,
): string | null {
  if (
    input.dedupeKey &&
    items.some((item) => !item.read && item.dedupeKey === input.dedupeKey)
  ) {
    return null;
  }
  sequence += 1;
  const item: DesktopNotification = {
    ...input,
    id: `desk-${Date.now()}-${sequence}`,
    createdAt: Date.now(),
    read: false,
  };
  items = [item, ...items].slice(0, MAX_NOTIFICATIONS);
  emit();
  return item.id;
}

export function markDesktopNotificationRead(id: string): void {
  let changed = false;
  items = items.map((item) => {
    if (item.id !== id || item.read) return item;
    changed = true;
    return { ...item, read: true };
  });
  if (changed) emit();
}

export function markAllDesktopNotificationsRead(): void {
  if (items.every((item) => item.read)) return;
  items = items.map((item) => (item.read ? item : { ...item, read: true }));
  emit();
}

export function clearDesktopNotifications(): void {
  if (items.length === 0) return;
  items = [];
  emit();
}

export function subscribeDesktopNotifications(
  listener: () => void,
): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getDesktopNotifications(): DesktopNotification[] {
  return items;
}

export function useDesktopNotifications(): DesktopNotification[] {
  return useSyncExternalStore(
    subscribeDesktopNotifications,
    getDesktopNotifications,
    getDesktopNotifications,
  );
}
