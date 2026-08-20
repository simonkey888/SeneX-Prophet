const ORIGIN = "https://h011-web--senecio-h011--wbjggn89fnf8.code.run";
const SAFE = new Set(["GET", "HEAD"]);
const API_PATHS = new Set([
  "/api/health", "/healthz", "/readyz", "/openapi.json",
  "/api/oracle/score", "/api/oracle/state", "/api/portfolio/live_gate",
  "/api/authority/snapshot", "/api/runtime/provenance", "/api/market-context"
]);
function decision(body, status, decision) {
  return new Response(body, {status, headers: {
    "content-type": "text/plain; charset=utf-8",
    "x-senex-edge-decision": decision,
    "cache-control": "no-store"
  }});
}
export default {
  async fetch(request) {
    if (!SAFE.has(request.method)) return decision("method denied\n", 405, "DENY_METHOD");
    const incoming = new URL(request.url);
    const staticPath = incoming.pathname === "/" || incoming.pathname.startsWith("/static/");
    if (!staticPath && !API_PATHS.has(incoming.pathname)) return decision("path denied\n", 404, "DENY_PATH");
    const target = new URL(incoming.pathname + incoming.search, ORIGIN);
    const headers = new Headers(request.headers);
    headers.delete("authorization");
    headers.delete("cookie");
    const upstream = await fetch(new Request(target, {method: request.method, headers, redirect: "manual"}));
    const out = new Headers(upstream.headers);
    out.set("x-senex-edge-decision", "ALLOW_GET_PROXY");
    out.set("cache-control", "no-store");
    return new Response(upstream.body, {status: upstream.status, statusText: upstream.statusText, headers: out});
  }
};
