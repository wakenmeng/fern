// waken.dev/apps/fern/* -> Fern. Everything else on waken.dev (the blog) is untouched.
//
// MODE=live:    forward to the cluster through the Cloudflare Tunnel hostname (ORIGIN).
// MODE=archive: serve the static archive bundled with this Worker (./archive: index.html,
//               bench.json, archive.json). Any other path is 410: the live API is gone.
// MODE=paused:  money-moving requests get 503; reads keep working.
// LIVE_UNTIL:   from this moment on the Worker behaves as MODE=archive, whatever MODE says,
//               so the site goes static on time without anyone deploying anything.
const PREFIX = "/apps/fern";

export function modeOf(env, now = Date.now()) {
  if (env.LIVE_UNTIL && now >= Date.parse(env.LIVE_UNTIL)) return "archive";
  return env.MODE || "live";
}

async function archive(env, url, rest) {
  const path = rest === "/" ? "/index.html" : rest;
  const r = await env.ASSETS.fetch(new Request(new URL(path, url.origin)));
  if (r.status === 200) {
    const response = new Response(r.body, r);
    response.headers.set("X-Fern-Mode", "archive");
    response.headers.set("Cache-Control", "no-cache");
    return response;
  }
  return Response.json(
    { error: "Archived", message: "Fern's live run has ended; this is a static archive of it." },
    { status: 410, headers: { "X-Fern-Mode": "archive" } },
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== PREFIX && !url.pathname.startsWith(PREFIX + "/")) return fetch(request);
    if (url.pathname === PREFIX) return Response.redirect(url.origin + PREFIX + "/", 301);

    const rest = url.pathname.slice(PREFIX.length) || "/";
    const mode = modeOf(env);

    if (mode === "archive") return archive(env, url, rest);

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
