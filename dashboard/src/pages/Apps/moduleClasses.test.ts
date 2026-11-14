import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * Guard against silently deleted CSS.
 *
 * Vitest's CSS-module mock returns a scoped name for *any* key, so a missing
 * class never fails a component test — but the production build returns
 * `undefined` for it and the style (or the whole feature, e.g. the board's
 * notch/fullscreen overlays) stops working. This test compares the classes
 * referenced from the TSX with the rule definitions in the LESS file.
 */
const dir = path.dirname(fileURLToPath(import.meta.url));
const less = readFileSync(path.join(dir, "index.module.less"), "utf8");

/** Rule definitions only: `.name {` (selectors like `.name:hover` are not definitions). */
const defined = new Set(
  [...less.matchAll(/^\s*\.([A-Za-z0-9_-]+)\s*\{/gm)].map((match) => match[1]),
);

const used = new Set<string>();
for (const file of [
  "index.tsx",
  "BoardGrid.tsx",
  "BoardView.tsx",
  "BoardGallery.tsx",
  "DesktopGrid.tsx",
  "DesktopDock.tsx",
  "DesktopMissionControl.tsx",
  "DesktopActivityCenter.tsx",
  "DesktopChatWindow.tsx",
  "DesktopBootScreen.tsx",
  "DesktopWebView.tsx",
  "DesktopNotificationCenter.tsx",
  "DesktopCommandPalette.tsx",
]) {
  // Drop import lines: `react-grid-layout/css/styles.css` is not a style ref.
  const source = readFileSync(path.join(dir, file), "utf8")
    .split("\n")
    .filter((line) => !line.trim().startsWith("import "))
    .join("\n");
  for (const match of source.matchAll(/styles\.([A-Za-z0-9_]+)/g)) {
    used.add(match[1]);
  }
}

describe("Apps CSS modules", () => {
  it("defines every class referenced by the components", () => {
    expect([...used].filter((name) => !defined.has(name))).toEqual([]);
  });

  it("does not leave unused classes behind", () => {
    expect([...defined].filter((name) => !used.has(name))).toEqual([]);
  });
});
