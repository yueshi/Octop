import { describe, expect, it } from "vitest";

import {
  APP_CARD_SPANS,
  loadAppCardLayout,
  moveAppInOrder,
  nextAppCardSize,
  reconcileAppOrder,
  saveAppCardLayout,
  snapAppCardSize,
  withAppCardSize,
  type AppCardLayoutStorage,
} from "./appCardLayout";

function memoryStorage(): AppCardLayoutStorage & {
  value: () => string | null;
} {
  let value: string | null = null;
  return {
    getItem: () => value,
    setItem: (_key: string, next: string) => {
      value = next;
    },
    value: () => value,
  };
}

describe("appCardLayout", () => {
  it("defines the three specs", () => {
    expect(APP_CARD_SPANS).toEqual({
      standard: { cols: 1, rows: 1 },
      tall: { cols: 1, rows: 2 },
      large: { cols: 2, rows: 2 },
    });
  });

  it("reconciles saved order with the live app list", () => {
    expect(reconcileAppOrder(["b", "c", "a"], ["a", "gone", "b"])).toEqual([
      "a",
      "b",
      "c",
    ]);
    expect(reconcileAppOrder(["a", "b"], ["a", "b", "a"])).toEqual(["a", "b"]);
  });

  it("moves a card to the dropped index", () => {
    expect(moveAppInOrder(["a", "b", "c"], 0, 2)).toEqual(["b", "c", "a"]);
    expect(moveAppInOrder(["a", "b", "c"], 2, 0)).toEqual(["c", "a", "b"]);
    expect(moveAppInOrder(["a"], 0, 3)).toEqual(["a"]);
  });

  it("cycles through the three sizes", () => {
    expect(nextAppCardSize("standard")).toBe("tall");
    expect(nextAppCardSize("tall")).toBe("large");
    expect(nextAppCardSize("large")).toBe("standard");
  });

  it("snaps a drag span to the nearest spec", () => {
    expect(snapAppCardSize(1, 1)).toBe("standard");
    expect(snapAppCardSize(2, 1)).toBe("standard");
    expect(snapAppCardSize(1, 2)).toBe("tall");
    expect(snapAppCardSize(1, 4)).toBe("tall");
    expect(snapAppCardSize(2, 2)).toBe("large");
    expect(snapAppCardSize(2, 4)).toBe("large");
  });

  it("stores sizes but drops the standard default", () => {
    const base = { order: ["a"], sizes: { a: "large" as const } };
    const tall = withAppCardSize(base, "a", "tall");
    expect(tall.sizes).toEqual({ a: "tall" });
    const standard = withAppCardSize(tall, "a", "standard");
    expect(standard.sizes).toEqual({});
    expect(standard.order).toEqual(["a"]);
  });

  it("round-trips the saved layout", () => {
    const storage = memoryStorage();
    saveAppCardLayout(
      { order: ["b", "a"], sizes: { a: "tall", b: "large" } },
      storage,
    );
    expect(loadAppCardLayout(storage)).toEqual({
      order: ["b", "a"],
      sizes: { a: "tall", b: "large" },
    });
  });

  it("ignores malformed saved layouts", () => {
    const storage = memoryStorage();
    storage.setItem("ignored", "{not json");
    expect(loadAppCardLayout(storage)).toEqual({ order: [], sizes: {} });

    storage.setItem(
      "ignored",
      JSON.stringify({
        order: ["a", 3],
        sizes: { a: "huge", b: "tall" },
      }),
    );
    expect(loadAppCardLayout(storage)).toEqual({
      order: ["a"],
      sizes: { b: "tall" },
    });
  });
});
