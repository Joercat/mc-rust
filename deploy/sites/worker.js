// Static host for the Horizon web build on a Freebuff Site.
//
// The page needs three things a plain static host does not give it: COOP/COEP
// (the threaded engine's shared memory), a 33 MB wasm module and a 126 MB
// resource pack. The last two do not fit a site's uploaded assets (10 MiB a
// file), so they live in the site's bucket (`env.FILES`) in parts of 8 MiB, each
// of which is a body of its own — `assets.json` names them, and the page fetches
// and joins them.
//
// That shape is not a preference. Handing the browser one 126 MB response built
// here (relaying the parts into a single stream, with or without a declared
// length) ends early: the runtime stops the body part way through and closes it
// cleanly, so the page receives a truncated pack with no error to report. One
// object per request has no such trouble, and it costs no JavaScript time per
// byte.
//
// Every response carries the isolation headers. A page is only cross-origin
// isolated when the document has them and every subresource it loads is
// same-origin or marked `Cross-Origin-Resource-Policy`, so they are set on the
// assets and on the stored files alike.

const ISOLATION = {
  "cross-origin-opener-policy": "same-origin",
  "cross-origin-embedder-policy": "require-corp",
  "cross-origin-resource-policy": "cross-origin",
};

// The files too big to publish as assets. The key is the object prefix in the
// site's bucket (`<key>.part0001`, …); `scripts/sites_seed.py` is what puts them
// there, and `scripts/sites_bundle.py` writes their part list into assets.json.
const STORED = {
  "/horizon-vfs.bin": { key: "horizon-vfs.bin", type: "application/octet-stream" },
  "/horizon-client-threaded_bg.wasm": {
    key: "horizon-client-threaded_bg.wasm",
    type: "application/wasm",
  },
  "/horizon-client_bg.wasm": { key: "horizon-client_bg.wasm", type: "application/wasm" },
};

// `app.js` stamps every build output with the build it was made for (`?v=…`) and
// the page's own cache is keyed by the SHA-256 in `assets.json`, so a stored copy
// may be kept for as long as the browser likes: a rebuild changes the URL.
// `index.html` is the one file with no stamp, so it is revalidated instead.
const IMMUTABLE = "public, max-age=31536000, immutable";
const REVALIDATE = "public, max-age=0, must-revalidate";

const PART_PREFIX = (key) => `${key}.part`;
const PART_PATH = /^\/__parts\/([^/]+)\/(\d+)$/;
const partKey = (key, number) => `${PART_PREFIX(key)}${String(number).padStart(4, "0")}`;

// A stored file is its parts, in the order of their numbers.
async function parts(env, key) {
  const prefix = PART_PREFIX(key);
  const listed = await env.FILES.list({ prefix });
  const objects = listed.objects
    .filter((object) => /^\d+$/.test(object.key.slice(prefix.length)))
    .sort((a, b) => Number(a.key.slice(prefix.length)) - Number(b.key.slice(prefix.length)));
  let total = 0;
  for (const object of objects) total += object.size;
  return { objects, total };
}

function missing(info) {
  return new Response(
    `${info.key} is not in the site's storage.\n` +
      "Upload the build's large files with scripts/sites_seed.py (docs/WEB.md).\n",
    {
      status: 503,
      headers: { ...ISOLATION, "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
    },
  );
}

function partHeaders(env, info) {
  const headers = new Headers(ISOLATION);
  headers.set("content-type", info.type);
  headers.set("cache-control", IMMUTABLE);
  return headers;
}

// One part, one body: the runtime hands a single R2 object over as it is, with
// its length, and the page joins the parts in order.
async function servePart(request, env, key, number) {
  const info = Object.values(STORED).find((entry) => entry.key === key);
  if (!info || !env.FILES) return missing({ key });
  const name = partKey(key, number);
  const headers = partHeaders(env, info);
  if (request.method === "HEAD") {
    const head = await env.FILES.head(name);
    if (!head) return missing(info);
    headers.set("content-length", String(head.size));
    return new Response(null, { headers });
  }
  const object = await env.FILES.get(name);
  if (!object) return missing(info);
  headers.set("content-length", String(object.size));
  headers.set("etag", object.httpEtag);
  return new Response(object.body, { headers });
}

// The whole file is not served here: this is what answers a probe, and what the
// page would have fetched if the build published it as one file.
async function describe(request, env, info) {
  if (!env.FILES) return missing(info);
  const shape = await parts(env, info.key);
  if (shape.objects.length === 0) return missing(info);
  const headers = new Headers(ISOLATION);
  headers.set("content-type", "text/plain; charset=utf-8");
  headers.set("cache-control", "no-store");
  headers.set("x-horizon-parts", String(shape.objects.length));
  headers.set("x-horizon-size", String(shape.total));
  if (request.method === "HEAD") return new Response(null, { headers });
  return new Response(
    `${info.key} is published in ${shape.objects.length} part(s), ${shape.total} bytes.\n` +
      `The page fetches /__parts/${info.key}/1 … ${shape.objects.length}, as assets.json lists them.\n`,
    { status: 409, headers },
  );
}

// A host has to answer the app's own HEAD on `/` for its isolation check, and
// `env.ASSETS` does that; only the headers are rewritten here.
async function asset(request, env) {
  const response = await env.ASSETS.fetch(request);
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(ISOLATION)) headers.set(name, value);
  const path = new URL(request.url).pathname;
  if (!headers.has("cache-control")) {
    headers.set("cache-control", path === "/" || path.endsWith(".html") ? REVALIDATE : IMMUTABLE);
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

// ---------------------------------------------------------------------------
// Seeding: the large files arrive one part at a time and are stored as one
// object per part. A request body is capped well below the pack's size, which is
// why they are not one upload.
//
// The routes only exist when the deployment carries a SEED_TOKEN secret, and
// they only write the keys in STORED. Deploy, set the secret, upload once: a
// deployment without the secret answers 501 and is a plain static host again.
// ---------------------------------------------------------------------------
const SEED_PREFIX = "/__seed/";

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

async function seeded(request, env, url) {
  if (!env.SEED_TOKEN) return json({ error: "seeding is not enabled on this deployment" }, 501);
  const token = request.headers.get("x-seed-token") || "";
  if (token !== env.SEED_TOKEN) return json({ error: "bad seed token" }, 403);
  if (!env.FILES) return json({ error: "no object storage bound as FILES" }, 501);

  const action = url.pathname.slice(SEED_PREFIX.length);
  const key = url.searchParams.get("key") || "";
  const info = Object.values(STORED).find((entry) => entry.key === key);
  if (!info) return json({ error: `unknown or unpublished file: ${key}` }, 400);

  if (action === "status" && request.method === "GET") {
    const shape = await parts(env, key);
    return json({ key, parts: shape.objects.length, size: shape.total });
  }

  // Drop every part of this key, so a shorter upload than last time cannot leave
  // a stale tail behind.
  if (action === "reset" && request.method === "POST") {
    const shape = await parts(env, key);
    for (const object of shape.objects) await env.FILES.delete(object.key);
    return json({ key, deleted: shape.objects.length });
  }

  if (action === "part" && request.method === "PUT") {
    const partNumber = Number(url.searchParams.get("partNumber"));
    if (!Number.isInteger(partNumber) || partNumber < 1 || partNumber > 9999) {
      return json({ error: "part needs a partNumber of 1..9999" }, 400);
    }
    const body = await request.arrayBuffer();
    if (body.byteLength === 0) return json({ error: "empty part" }, 400);
    const object = await env.FILES.put(partKey(key, partNumber), body, {
      httpMetadata: { contentType: "application/octet-stream" },
    });
    return json({ partNumber, size: object.size });
  }

  if (action === "finish" && request.method === "POST") {
    const shape = await parts(env, key);
    return json({ key, parts: shape.objects.length, size: shape.total });
  }

  return json({ error: `unknown seed action: ${action}` }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith(SEED_PREFIX)) return seeded(request, env, url);
    const part = PART_PATH.exec(url.pathname);
    if (part) {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return new Response("method not allowed", { status: 405, headers: ISOLATION });
      }
      return servePart(request, env, decodeURIComponent(part[1]), Number(part[2]));
    }
    const info = STORED[url.pathname];
    if (info) {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return new Response("method not allowed", { status: 405, headers: ISOLATION });
      }
      return describe(request, env, info);
    }
    // The browser's own favicon request is the one 404 every session logs;
    // answering it with "nothing" keeps the console about the game.
    if (url.pathname === "/favicon.ico") {
      return new Response(null, { status: 204, headers: ISOLATION });
    }
    return asset(request, env);
  },
};
