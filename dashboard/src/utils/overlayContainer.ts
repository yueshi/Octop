/**
 * Antd portals default to `document.body`. When the desktop root is in the
 * Fullscreen API, only that root and its descendants are painted, so modals
 * mounted on body become invisible and keyboard focus is trapped off-screen.
 * Mount overlays inside the current fullscreen element instead.
 */
export function fullscreenContainer(): HTMLElement {
  const element = document.fullscreenElement;
  return element instanceof HTMLElement ? element : document.body;
}
