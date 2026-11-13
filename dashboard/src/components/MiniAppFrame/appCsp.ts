/**
 * CSP for the agent-generated app iframe.
 *
 * Inline CSS/JS is required (apps are single-file HTML). Remote scripts,
 * styles, images, fonts and media are blocked so page code cannot exfiltrate
 * rows through image beacons or CDN scripts; `connect-src 'none'` alone would
 * not stop those. The iframe is also sandboxed without `allow-same-origin`, so
 * it never sees the dashboard JWT.
 *
 * `frame-src 'none'` and `object-src 'none'` close the remaining channels:
 * the app cannot embed a nested frame and cannot instantiate a plugin object.
 */
export const APP_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; " +
  "style-src 'unsafe-inline'; img-src data: blob:; " +
  "font-src data:; media-src data: blob:; connect-src 'none'; " +
  "base-uri 'none'; form-action 'none'; " +
  "frame-src 'none'; object-src 'none'";

/**
 * CSP for the wrapper document that hosts the app frame.
 *
 * A srcdoc child inherits its embedder's policy, so the wrapper must allow
 * everything the app itself needs (data: images, fonts, media) *in addition
 * to* denying navigation. It is therefore the app policy, unchanged — the
 * navigation ban is already part of `APP_CSP`.
 *
 * The wrapper is our own fixed markup; the app never sees it. Its `frame-src`
 * is what actually stops the app from navigating itself away: a nested frame's
 * own navigation is checked against the *embedder's* `frame-src`, so the
 * wrapper denies it while the dashboard page (which embeds the wrapper) stays
 * free to load it.
 */
export const WRAPPER_CSP = APP_CSP;
