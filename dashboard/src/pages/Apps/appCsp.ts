/**
 * CSP for the agent-generated app iframe.
 *
 * Inline CSS/JS is required (apps are single-file HTML). Remote scripts,
 * styles, images, fonts and media are blocked so page code cannot exfiltrate
 * rows through image beacons or CDN scripts; `connect-src 'none'` alone would
 * not stop those. The iframe is also sandboxed without `allow-same-origin`, so
 * it never sees the dashboard JWT.
 */
export const APP_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; " +
  "style-src 'unsafe-inline'; img-src data: blob:; " +
  "font-src data:; media-src data: blob:; connect-src 'none'; " +
  "base-uri 'none'; form-action 'none'";
