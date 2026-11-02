// Does the page's download cache do what it says?
//
// It loads `public/asset-cache.js` — the module the page itself runs — with a
// stubbed `fetch` over a fake host and an in-memory IndexedDB, then walks the
// sequence a player's browser walks:
//
//   1. first load of a build: the manifest and every asset, once each;
//   2. second load of the same build, cache warm: no request at all;
//   3. a rebuild that changed one asset: the manifest and that asset only;
//   4. a browser that refuses IndexedDB: the network, and the page still starts;
//   5. a host that serves bytes the manifest does not name: used, not stored
//      under the wrong hash, so the next load fetches them again.
//
// Corpus counters are absolute per load, so "0" is visible rather than implied.

import { createHash } from "node:crypto";
import { FakeIndexedDB } from "./fake_idb.mjs";
import { createAssetCache } from "../public/asset-cache.js";

const ORIGIN = "https://horizon.example/app.js";
const ASSETS = [
  "horizon-client-threaded.js",
  "horizon-client-threaded_bg.wasm",
  "horizon-vfs.bin",
];

const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");

/// The build's files, as the host would serve them, plus the manifest it
/// derives from them.
function makeHost(sizes = {}) {
  const files = new Map();
  for (const name of ASSETS) {
    const size = sizes[name] ?? (name.endsWith(".wasm") ? 4096 : 2048);
    const bytes = new Uint8Array(size);
    // Content, not zeros: a corrupted copy must be detectable.
    for (let i = 0; i < size; i += 1) bytes[i] = (i * 31 + name.length * 7) & 0xff;
    files.set(name, bytes);
  }
  return { files, build: "2026-11-01.14", manifestFetches: 0 };
}

function hostManifest(host) {
  const files = {};
  for (const [name, bytes] of host.files) files[name] = sha(bytes);
  return { build: host.build, files };
}

/// A `fetch` for the fake host. Every call is counted, including the manifest,
/// which is what makes "no request at all" checkable.
function makeFetch(host, counter) {
  return async (url) => {
    const parsed = typeof url === "string" ? new URL(url) : url;
    const name = parsed.pathname.replace(/^.*\//, "");
    counter.total += 1;
    counter.urls.push(`${name}?${parsed.searchParams.toString()}`);
    if (name === "assets.json") {
      counter.manifest += 1;
      const body = JSON.stringify(hostManifest(host));
      return {
        ok: true,
        status: 200,
        headers: new Headers({ "content-length": String(body.length) }),
        json: async () => JSON.parse(body),
        arrayBuffer: async () => new TextEncoder().encode(body).buffer,
        body: null,
      };
    }
    const bytes = host.files.get(name);
    if (!bytes) return { ok: false, status: 404, headers: new Headers(), body: null };
    counter.assets += 1;
    return {
      ok: true,
      status: 200,
      headers: new Headers({ "content-length": String(bytes.length) }),
      arrayBuffer: async () => bytes.slice().buffer,
      body: null,
    };
  };
}

function emptyCounter() {
  return { total: 0, manifest: 0, assets: 0, urls: [] };
}

/// One page load: the cache the page builds, then the assets it asks for.
async function load(host, indexedDB, counter, { nocache = false, names = ASSETS } = {}) {
  const cache = createAssetCache({
    fetchImpl: makeFetch(host, counter),
    indexedDB,
    origin: ORIGIN,
    build: host.build,
    nocache,
    warn: () => {},
  });
  await cache.open();
  const got = new Map();
  for (const name of names) got.set(name, await cache.bytes(name));
  await cache.prune(names);
  return { cache, got };
}

const problems = [];
function check(what, actual, expected) {
  if (actual !== expected) problems.push(`${what}: expected ${expected}, got ${actual}`);
}

const idb = new FakeIndexedDB();
const host = makeHost();

// 1. First load: manifest plus every asset.
const first = emptyCounter();
const a = await load(host, idb, first);
check("first load fetches", first.total, ASSETS.length + 1);
check("first load manifest fetches", first.manifest, 1);
check("first load asset fetches", first.assets, ASSETS.length);
for (const name of ASSETS) {
  check(`first load ${name} cached flag`, a.got.get(name).cached, false);
  check(`first load ${name} bytes`, sha(a.got.get(name).bytes), sha(host.files.get(name)));
}

// 2. Second load, cache warm: nothing on the network, not even a HEAD.
const second = emptyCounter();
const b = await load(host, idb, second);
check("second load fetches", second.total, 0);
for (const name of ASSETS) {
  check(`second load ${name} cached flag`, b.got.get(name).cached, true);
  check(`second load ${name} bytes`, sha(b.got.get(name).bytes), sha(host.files.get(name)));
}

// 3. A rebuild: the pack changed, the engines did not.
const changed = "horizon-vfs.bin";
const repacked = host.files.get(changed).slice();
repacked[0] ^= 0xff;
host.files.set(changed, repacked);
host.build = "2026-11-01.15";
const rebuilt = emptyCounter();
const c = await load(host, idb, rebuilt);
check("rebuild fetches", rebuilt.total, 2);
check("rebuild fetched the changed asset", rebuilt.urls.includes(`${changed}?v=2026-11-01.15`), true);
for (const name of ASSETS) {
  const expectedCached = name !== changed;
  check(`rebuild ${name} cached flag`, c.got.get(name).cached, expectedCached);
  check(`rebuild ${name} bytes`, sha(c.got.get(name).bytes), sha(host.files.get(name)));
}

// A second load of the rebuilt build is silent again.
const settled = emptyCounter();
await load(host, idb, settled);
check("post-rebuild load fetches", settled.total, 0);

// 4. A browser that refuses IndexedDB: the network, and no exception.
const refused = new FakeIndexedDB();
refused.failOpen = "the browser refused the cache";
const noCache = emptyCounter();
const d = await load(host, refused, noCache);
check("no-cache load fetches", noCache.total, ASSETS.length + 1);
for (const name of ASSETS) {
  check(`no-cache load ${name} bytes`, sha(d.got.get(name).bytes), sha(host.files.get(name)));
}

// 5. A host serving bytes the manifest does not name: used once, never stored
//    under the manifest's hash, so the next load tries again.
const liar = new FakeIndexedDB();
const tamperedHost = makeHost();
const served = tamperedHost.files.get("horizon-client-threaded_bg.wasm").slice();
served[1] ^= 0xff;
const honestFetch = makeFetch(tamperedHost, emptyCounter());
const tamperedFetch = (counter) => async (url, init) => {
  const name = new URL(url).pathname.replace(/^.*\//, "");
  counter.total += 1;
  counter.urls.push(name);
  if (name === "assets.json") counter.manifest += 1;
  else counter.assets += 1;
  const response = await honestFetch(url, init);
  if (name !== "horizon-client-threaded_bg.wasm") return response;
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "content-length": String(served.length) }),
    arrayBuffer: async () => served.slice().buffer,
    body: null,
  };
};
const tamperedCounter = emptyCounter();
const tampered = createAssetCache({
  fetchImpl: tamperedFetch(tamperedCounter),
  indexedDB: liar,
  origin: ORIGIN,
  build: tamperedHost.build,
  warn: () => {},
});
await tampered.open();
const suspect = await tampered.bytes("horizon-client-threaded_bg.wasm");
check("tampered bytes used", suspect.bytes.length, served.length);
check("tampered bytes reported uncached", suspect.cached, false);
check(
  "tampered bytes not stored under the manifest hash",
  `sha256:${hostManifest(tamperedHost).files["horizon-client-threaded_bg.wasm"]}` === suspect.key,
  false,
);
const retry = emptyCounter();
const retried = createAssetCache({
  fetchImpl: tamperedFetch(retry),
  indexedDB: liar,
  origin: ORIGIN,
  build: tamperedHost.build,
  warn: () => {},
});
await retried.open();
await retried.bytes("horizon-client-threaded_bg.wasm");
check("tampered asset is fetched again next load", retry.assets, 1);

// 6. `?nocache=1` ignores what is stored and refreshes it.
const nocache = emptyCounter();
await load(host, idb, nocache, { nocache: true });
check("nocache load fetches", nocache.total, ASSETS.length + 1);

console.log(`first load:   ${first.total} fetch(es) — manifest 1, assets ${first.assets}`);
console.log(`second load:  ${second.total} fetch(es) — cached ${ASSETS.length}/${ASSETS.length}`);
console.log(
  `after rebuild: ${rebuilt.total} fetch(es) — manifest ${rebuilt.manifest}, changed ${rebuilt.urls
    .filter((url) => url.startsWith(changed))
    .length}, unchanged re-downloaded ${rebuilt.assets - 1}`,
);
console.log(`settled load: ${settled.total} fetch(es)`);
console.log(`cache refused: ${noCache.total} fetch(es) — page still starts`);
console.log(`host mismatch: tampered asset fetched again (${retry.assets}) instead of stored`);
console.log(`nocache=1:    ${nocache.total} fetch(es)`);
const records = [...idb.databases.values()].reduce(
  (total, db) => total + [...db.stores.values()].reduce((n, store) => n + store.size, 0),
  0,
);
console.log(`stored:       ${records} record(s) in the warm cache`);

if (problems.length > 0) {
  for (const problem of problems) console.error(`FAIL ${problem}`);
  process.exit(1);
}
console.log("ASSET_CACHE_OK");
