// waken.dev/apps/fern/* -> Fern. Everything else on waken.dev (the blog) is untouched.
//
// MODE=live:    forward to the cluster through the Cloudflare Tunnel hostname (ORIGIN).
// MODE=archive: after the 72-hour window, serve the static archive (ARCHIVE_ORIGIN, GitHub Pages).
// MODE=paused:  money-moving requests get 503; reads and the archive links keep working.
const PREFIX = "/apps/fern";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== PREFIX && !url.pathname.startsWith(PREFIX + "/")) return fetch(request);
    if (url.pathname === PREFIX) return Response.redirect(url.origin + PREFIX + "/", 301);

    const rest = url.pathname.slice(PREFIX.length) || "/";
    const mode = env.MODE || "live";

    if (mode === "archive") {
      const target = new URL(env.ARCHIVE_ORIGIN.replace(/\/$/, "") + rest + url.search);
      const r = await fetch(target, { headers: { "User-Agent": "fern-router" } });
      return new Response(r.body, r);
    }

    if (mode === "paused" && request.method !== "GET" && request.method !== "HEAD") {
      return Response.json({ error: "Paused", message: "Fern is paused; reads still work." }, { status: 503 });
    }

    const target = new URL(env.ORIGIN.replace(/\/$/, "") + rest + url.search);
    const headers = new Headers(request.headers);
    headers.set("X-Forwarded-Prefix", PREFIX);
    headers.set("X-Forwarded-Host", url.host);
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "manual",
    });
    const response = new Response(upstream.body, upstream);
    response.headers.set("X-Fern-Mode", mode);
    return response;
  },
};
