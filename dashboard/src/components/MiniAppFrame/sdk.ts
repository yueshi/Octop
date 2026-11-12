import { APP_CSP } from "./appCsp";

/**
 * Bridge exposed to app HTML as `window.octopApp`. Every call goes through the
 * parent dashboard over postMessage so the iframe never sees a JWT and never
 * needs `allow-same-origin`.
 */
export const APP_SDK = `(function () {
  var pending = new Map();
  var seq = 0;
  var TIMEOUT_MS = 30000;
  function call(method, params) {
    return new Promise(function (resolve, reject) {
      var id = ++seq;
      var entry = { resolve: resolve, reject: reject, timer: null };
      pending.set(id, entry);
      // The host may go away mid-request (the frame was remounted); reject
      // instead of leaving the caller's promise pending forever.
      entry.timer = setTimeout(function () {
        pending.delete(id);
        reject(new Error("Request timed out"));
      }, TIMEOUT_MS);
      parent.postMessage(
        { source: "octop-app", type: "request", id: id, method: method, params: params || {} },
        "*"
      );
    });
  }
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.source !== "octop-app-host") return;
    var entry = pending.get(data.id);
    if (!entry) return;
    pending.delete(data.id);
    if (entry.timer) clearTimeout(entry.timer);
    if (data.ok) entry.resolve(data.data);
    else entry.reject(new Error(data.error || "Request failed"));
  });
  window.octopApp = {
    me: function () { return call("me"); },
    schema: function (table) { return call("schema", { table: table || null }); },
    tables: function () { return call("tables"); },
    listRows: function (options) { return call("rows", options || {}); },
    appendRow: function (row, options) {
      return call("append", { row: row, table: (options || {}).table || null });
    },
    generate: function (prompt, options) {
      return call("generate", {
        prompt: prompt || "",
        system: (options || {}).system || ""
      });
    },
    launchGame: function (gameId) {
      return call("launchGame", { gameId: gameId || "" });
    },
    composeMessage: function (text) {
      return call("composeMessage", { text: text || "" });
    },
    openExternal: function (url) { return call("openExternal", { url: url || "" }); },
    asset: function (name) { return call("asset", { name: name || "" }); }
  };
})();`;

export function buildSrcDoc(html: string): string {
  const head = `<meta http-equiv="Content-Security-Policy" content="${APP_CSP}"><script>${APP_SDK}</script>`;
  const headMatch = /<head[^>]*>/i.exec(html);
  if (headMatch?.index !== undefined) {
    const at = headMatch.index + headMatch[0].length;
    return `${html.slice(0, at)}${head}${html.slice(at)}`;
  }
  const htmlMatch = /<html[^>]*>/i.exec(html);
  if (htmlMatch?.index !== undefined) {
    const at = htmlMatch.index + htmlMatch[0].length;
    return `${html.slice(0, at)}<head>${head}</head>${html.slice(at)}`;
  }
  const doctypeMatch = /<!doctype[^>]*>/i.exec(html);
  if (doctypeMatch?.index !== undefined) {
    const at = doctypeMatch.index + doctypeMatch[0].length;
    return `${html.slice(0, at)}<html><head>${head}</head><body>${html.slice(
      at,
    )}</body></html>`;
  }
  return `<!doctype html><html><head>${head}</head><body>${html}</body></html>`;
}
