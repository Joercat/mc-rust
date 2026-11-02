// Content-addressed cache for the page's build outputs.
//
// The engine's wasm module (~33 MB), its glue and the resource pack (~33 MB)
// change when `scripts/build_web.sh` publishes a build. Their URLs carry the
// build stamp (`?v=BUILD`), so a stamp-keyed cache would throw all of them away
// on every deploy even when one file changed, and the revalidation it did was a
// request per file per load. This cache is keyed by the SHA-256 that the build
// recorded in `assets.json`:
//
//   * a second load with the cache populated makes no request at all for an
//     asset it already has — no GET, not even a HEAD;
//   * a rebuild that changes one file fetches the manifest and that file;
//     every other asset is still stored under the hash it kept;
//   * a stored copy can never be served for a different hash: the hash is the
//     key, so a changed file is a miss by construction.
//
// Everything here fails safe. A refused IndexedDB, a cache read that throws or
// a manifest that will not download all fall back to a plain fetch: the page
// cannot start without these bytes, so a cache problem is a slow start, never
// a broken one.

const DB_NAME = "horizon-web-assets";
const STORE = "assets";
// The old stamp-keyed cache: two engines and a pack, revalidated every load.
// It is dead weight now, so opening the new one drops it.
const OLD_DB_NAME = "horizon-web-cache";
export const MANIFEST = "assets.json";

/// The lowercase hex SHA-256 of `bytes`, or null where SubtleCrypto is absent
/// (an insecure context): the cache then stores by name, and a rebuild is a
/// full download again rather than a wrong one.
export function sha256Hex(bytes) {
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  if (!subtle || typeof subtle.digest !== "function") return Promise.resolve(null);
  return subtle.digest("SHA-256", bytes).then((digest) => {
    let hex = "";
    for (const byte of new Uint8Array(digest)) hex += byte.toString(16).padStart(2, "0");
    return hex;
  });
}

export async function readBody(response, onProgress) {
  const total = Number(response.headers.get("content-length") || 0);
  if (!response.body || total === 0) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    onProgress(1);
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onProgress(received / total);
  }
  const out = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

function openDb(factory) {
  return new Promise((resolve, reject) => {
    if (!factory) {
      reject(new Error("no IndexedDB"));
      return;
    }
    const request = factory.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: "key" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("another tab holds the cache open"));
  });
}

function get(db, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const request = tx.objectStore(STORE).get(key);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
  });
}

function put(db, record) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(record);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

function allKeys(db) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const request = tx.objectStore(STORE).getAllKeys();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

function forget(db, keys) {
  return new Promise((resolve, reject) => {
    if (keys.length === 0) {
      resolve();
      return;
    }
    const tx = db.transaction(STORE, "readwrite");
    for (const key of keys) tx.objectStore(STORE).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/// A cache for one page load. `origin` is the URL the asset paths are relative
/// to (the module's own `import.meta.url` in `app.js`); `build` is the stamp
/// the URLs carry; `nocache` ignores what is stored and refreshes it.
export function createAssetCache({
  fetchImpl,
  indexedDB: factory = globalThis.indexedDB,
  origin,
  build,
  nocache = false,
  warn = (message, error) => console.warn(message, error),
}) {
  const urlFor = (name) => {
    const url = new URL(name, origin);
    url.searchParams.set("v", build);
    return url;
  };
  let db = null;
  let opened = null;
  let manifestPromise = null;

  async function open() {
    if (opened) return opened;
    opened = (async () => {
      try {
        db = await openDb(factory);
      } catch (error) {
        warn("[horizon] no download cache", error);
        db = null;
        return null;
      }
      try {
        factory.deleteDatabase(OLD_DB_NAME);
      } catch {
        /* an old cache that will not go is only disk space */
      }
      return db;
    })();
    return opened;
  }

  async function storedManifest() {
    if (!db || nocache) return null;
    try {
      const record = await get(db, "manifest");
      // This page's own code carries the build it was made for. A record for
      // any other build describes bytes this page must not run, so it is not
      // used even as an offline fallback: the hash of a file from another
      // build is a different hash, and that is the whole point of the cache.
      if (!record || record.build !== build) return null;
      return record;
    } catch (error) {
      warn("[horizon] could not read the build manifest", error);
      return null;
    }
  }

  /// `{ build, files: { name: sha256 } }`. Stored, so a second load does not
  /// even ask whether the build changed: the stamp in this page's own code is
  /// what it was built against.
  function manifest() {
    if (manifestPromise) return manifestPromise;
    manifestPromise = (async () => {
      const stored = await storedManifest();
      if (stored && stored.files) return stored;
      try {
        const response = await fetchImpl(urlFor(MANIFEST), {
          cache: "no-cache",
          headers: { accept: "application/json" },
        });
        if (!response.ok) throw new Error(`${MANIFEST}: HTTP ${response.status}`);
        const body = await response.json();
        const record = {
          key: "manifest",
          build: body.build || build,
          files: body.files || {},
          at: Date.now(),
        };
        if (db) {
          await put(db, record).catch((error) =>
            warn("[horizon] could not store the build manifest", error),
          );
        }
        return record;
      } catch (error) {
        // Offline, or the host has no manifest: a stored one still names this
        // build's assets.
        if (stored && stored.files) {
          warn("[horizon] using the stored build manifest", error);
          return stored;
        }
        throw error;
      }
    })();
    return manifestPromise;
  }

  /// Whether the build has `name`, without asking the network: `null` when
  /// there is no manifest to ask (the caller then probes the host).
  async function has(name) {
    let files = null;
    try {
      files = (await manifest()).files;
    } catch {
      return null;
    }
    return files != null && Object.prototype.hasOwnProperty.call(files, name);
  }

  /// The bytes of `name`, from IndexedDB when this browser already has that
  /// hash. `{ bytes, cached, key }`.
  async function bytes(name, onProgress = () => {}) {
    let files = null;
    try {
      files = (await manifest()).files;
    } catch (error) {
      warn(`[horizon] no build manifest for ${name}; downloading it by URL`, error);
    }
    const hash = files && typeof files[name] === "string" ? files[name] : null;
    const key = hash ? `sha256:${hash}` : null;
    if (key && db && !nocache) {
      try {
        const record = await get(db, key);
        if (record && record.bytes) {
          onProgress(1);
          return { bytes: new Uint8Array(await record.bytes.arrayBuffer()), cached: true, key };
        }
      } catch (error) {
        warn(`[horizon] could not read ${name} from the cache`, error);
      }
    }
    const response = await fetchImpl(urlFor(name), { cache: "no-cache" });
    if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
    const fetched = await readBody(response, onProgress);
    if (hash) {
      const actual = await sha256Hex(fetched).catch(() => null);
      if (actual && actual !== hash) {
        // The host served bytes the build did not record. Use them — the page
        // cannot start without an engine — but do not store them under the
        // manifest's hash, or the next load would run them as if they were it.
        warn(`[horizon] ${name} does not match the build manifest (${actual} != ${hash})`);
        return { bytes: fetched, cached: false, key: `sha256:${actual}` };
      }
    }
    if (db && !nocache) {
      const record = {
        key: key || `raw:${name}`,
        // A Blob is stored on disk rather than in the IndexedDB heap.
        bytes: new Blob([fetched]),
        size: fetched.length,
        at: Date.now(),
      };
      await put(db, record).catch((error) =>
        warn("[horizon] could not store a download", error),
      );
    }
    return { bytes: fetched, cached: false, key };
  }

  /// Drops every stored asset this boot did not use: a file from another build
  /// (or another hash of the same name) would otherwise hold its megabytes.
  async function prune(names) {
    if (!db) return;
    try {
      const recorded = await manifest().catch(() => null);
      const keep = new Set(["manifest"]);
      for (const name of names) {
        const hash = recorded && recorded.files ? recorded.files[name] : null;
        keep.add(hash ? `sha256:${hash}` : `raw:${name}`);
      }
      const stored = await allKeys(db);
      await forget(db, stored.filter((key) => !keep.has(key)));
    } catch (error) {
      warn("[horizon] could not prune the download cache", error);
    }
  }

  return {
    open,
    manifest,
    has,
    bytes,
    prune,
    get db() {
      return db;
    },
  };
}
