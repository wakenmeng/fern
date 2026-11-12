// node --test infra/cloudflare/worker.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { modeOf } from "./worker.js";

const seen = [];
globalThis.fetch = async (input, init) => {
  const url = input instanceof Request ? input.url : String(input);
  seen.push({ url: String(url), method: init?.method ?? input.method ?? "GET" });
  return new Response("ok", { status: 200 });
};
const env = { MODE: "live", ORIGIN: "https://origin.example", ARCHIVE_ORIGIN: "https://archive.example/fern" };

test("blog paths pass through untouched", async () => {
  seen.length = 0;
  await worker.fetch(new Request("https://waken.dev/posts/hello"), env);
  assert.equal(seen[0].url, "https://waken.dev/posts/hello");
});

test("live: strips the prefix and forwards to the tunnel origin", async () => {
  seen.length = 0;
  const r = await worker.fetch(new Request("https://waken.dev/apps/fern/spaces?x=1", { method: "POST", body: "{}" }), env);
  assert.equal(seen[0].url, "https://origin.example/spaces?x=1");
  assert.equal(seen[0].method, "POST");
  assert.equal(r.headers.get("X-Fern-Mode"), "live");
});

test("bare prefix redirects to the trailing slash", async () => {
  const r = await worker.fetch(new Request("https://waken.dev/apps/fern"), env);
  assert.equal(r.status, 301);
});

test("paused blocks writes but not reads", async () => {
  seen.length = 0;
  const w = await worker.fetch(new Request("https://waken.dev/apps/fern/p2p", { method: "POST", body: "{}" }), { ...env, MODE: "paused" });
  assert.equal(w.status, 503);
  await worker.fetch(new Request("https://waken.dev/apps/fern/me"), { ...env, MODE: "paused" });
  assert.equal(seen[0].url, "https://origin.example/me");
});

const files = { "/index.html": "<html>", "/bench.json": "{}", "/archive.json": "{}" };
const ASSETS = {
  async fetch(req) {
    const p = new URL(req.url).pathname;
    return p in files ? new Response(files[p], { status: 200 }) : new Response("nope", { status: 404 });
  },
};

test("archive serves the bundled files and nothing reaches the origin", async () => {
  seen.length = 0;
  const a = { ...env, MODE: "archive", ASSETS };
  assert.equal(await (await worker.fetch(new Request("https://waken.dev/apps/fern/"), a)).text(), "<html>");
  assert.equal((await worker.fetch(new Request("https://waken.dev/apps/fern/archive.json"), a)).status, 200);
  const api = await worker.fetch(new Request("https://waken.dev/apps/fern/p2p", { method: "POST", body: "{}" }), a);
  assert.equal(api.status, 410);
  assert.equal(seen.length, 0);
});

test("LIVE_UNTIL switches to the archive on time", async () => {
  assert.equal(modeOf({ MODE: "live", LIVE_UNTIL: "2026-11-19T19:00:00Z" }, Date.parse("2026-11-19T18:59:59Z")), "live");
  assert.equal(modeOf({ MODE: "live", LIVE_UNTIL: "2026-11-19T19:00:00Z" }, Date.parse("2026-11-19T19:00:00Z")), "archive");
  assert.equal(modeOf({ MODE: "paused" }), "paused");
});
