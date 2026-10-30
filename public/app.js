// Horizon 26.3 web host: mounts the resource VFS pack, restores the player's
// `/game` folder from IndexedDB, starts the real Horizon client, then keeps the
// browser's storage in sync with the files the game writes.
const bootEl = document.getElementById("boot");
const barEl = document.querySelector("#bar > i");
const phaseEl = document.getElementById("phase");
const logEl = document.getElementById("log");
const hudEl = document.getElementById("hud");
const errorEl = document.getElementById("error");

const logLines = [];
function say(line) {
  logLines.push(line);
  while (logLines.length > 8) logLines.shift();
  logEl.textContent = logLines.join("\n");
  console.log("[horizon]", line);
}
function phase(text, progress) {
  phaseEl.textContent = text;
  if (progress !== undefined) barEl.style.width = `${Math.round(progress * 100)}%`;
}
function fail(err) {
  const message = err && err.stack ? err.stack : String(err);
  errorEl.style.display = "block";
  errorEl.textContent = `Horizon failed to start:\n\n${message}`;
  console.error(err);
}

// The engine, its glue and the resource pack are build outputs
// (`scripts/build_web.sh`, `scripts/pack_vfs.py`), not source: a fresh checkout
// has none of them. A static import would fail before a line of this file ran
// and leave the page on "starting…" forever, so they are loaded here, where a
// missing build can name the files it wants.
let wasm = null;
let init = null;
const ARTIFACTS = [
  ["./horizon-client.js", "the wasm-bindgen glue"],
  ["./horizon-client_bg.wasm", "the compiled client"],
  ["./horizon-vfs.bin", "the resource pack"],
];

async function loadEngine() {
  const missing = [];
  for (const [path, what] of ARTIFACTS) {
    try {
      // Only a definite "no such file" counts: a host that refuses HEAD
      // (405) or answers with something else must not read as a missing build.
      const response = await fetch(new URL(path, import.meta.url), { method: "HEAD" });
      if (response.status === 404 || response.status === 410) {
        missing.push(`  ${path.slice(2)} — ${what} (HTTP ${response.status})`);
      }
    } catch (error) {
      missing.push(`  ${path.slice(2)} — ${what} (${error})`);
    }
  }
  if (missing.length > 0) {
    throw new Error(
      "The web build's files are missing from ./public:\n\n" +
        missing.join("\n") +
        "\n\nThey are build outputs, not source. Build them with:\n" +
        "  scripts/build_web.sh\n\n" +
        "(needs the wasm32 Rust toolchain; see docs/WEB.md for the prerequisites)",
    );
  }
  const module = await import("./horizon-client.js");
  wasm = module;
  init = module.default;
}

// ---------------------------------------------------------------------------
// IndexedDB: one record per file under /game (path, bytes, mtime seconds).
// ---------------------------------------------------------------------------
const DB_NAME = "horizon-web";
const STORE = "files";

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: "path" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function loadAllFiles(db) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const request = tx.objectStore(STORE).getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

function writeFiles(db, records) {
  if (records.length === 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    for (const record of records) store.put(record);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

function deleteFiles(db, paths) {
  if (paths.length === 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    for (const path of paths) store.delete(path);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

// ---------------------------------------------------------------------------
// Boot.
// ---------------------------------------------------------------------------
async function fetchPack(url, onProgress) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  const total = Number(response.headers.get("content-length") || 0);
  if (!response.body || total === 0) return new Uint8Array(await response.arrayBuffer());
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

async function persist(db) {
  const [written, deleted] = wasm.horizon_web_drain_changes();
  if (written.length === 0 && deleted.length === 0) return;
  const records = [];
  for (const [path, bytes, mtime] of written) {
    records.push({ path, bytes: bytes.slice(), mtime });
  }
  await writeFiles(db, records);
  await deleteFiles(db, deleted);
  say(`saved ${records.length} file(s), removed ${deleted.length}`);
}

async function boot() {
  phase("looking for the engine…", 0.01);
  await loadEngine();
  phase("loading engine…", 0.03);
  const wasmUrl = new URL("./horizon-client_bg.wasm", import.meta.url);
  await init({ module_or_path: wasmUrl });
  say("wasm engine ready");

  phase("opening browser storage…", 0.05);
  const db = await openDb();

  phase("downloading resources…", 0.08);
  const pack = await fetchPack("./horizon-vfs.bin", (p) => phase("downloading resources…", 0.08 + p * 0.55));
  say(`resource pack: ${(pack.length / (1024 * 1024)).toFixed(1)} MB`);

  phase("mounting resources…", 0.66);
  const mounted = wasm.horizon_web_mount_pack(pack);
  say(`mounted ${mounted} virtual files`);

  phase("restoring saved worlds…", 0.70);
  const saved = await loadAllFiles(db);
  for (const record of saved) {
    wasm.horizon_web_import_file(record.path, new Uint8Array(record.bytes), record.mtime || 0);
  }
  say(`restored ${saved.length} saved file(s)`);

  phase("starting the game…", 0.74);
  const width = Math.max(320, Math.floor(window.innerWidth * (window.devicePixelRatio || 1)));
  const height = Math.max(240, Math.floor(window.innerHeight * (window.devicePixelRatio || 1)));
  const params = new URLSearchParams(window.location.search);
  const world = params.get("world");
  const creative = params.get("creative") !== "0";
  await wasm.horizon_web_start("horizon-canvas", width, height, world, creative);

  phase("ready", 1.0);
  say("event loop running");
  bootEl.classList.add("hidden");
  hudEl.style.opacity = "1";
  document.getElementById("horizon-canvas").focus();

  // Save what the game writes, and show a small status readout. `?persist=0`
  // and `?status=0` turn either off: the boot probes use them to tell a
  // storage stall from a renderer one.
  let ticks = 0;
  if (params.get("persist") !== "0") setInterval(async () => {
    try {
      await persist(db);
    } catch (e) {
      console.warn("persist failed", e);
    }
  }, 3000);
  // Also on the way out: ask the game for a save, then mirror it.
  const flush = () => {
    try {
      wasm.horizon_web_request_save();
    } catch (e) {
      /* the game is not running */
    }
    persist(db).catch(() => {});
  };
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
  setInterval(() => {
    try {
      const status = JSON.parse(wasm.horizon_web_status());
      window.__horizon_status = wasm.horizon_web_status();
      ticks += 1;
      hudEl.textContent =
        `${status.state} · ${status.fps ? status.fps.toFixed(0) : 0} fps · ` +
        `${status.chunks} chunks · ${status.meshed_sections} meshes`;
    } catch (e) {
      /* keep the last readout */
    }
  }, 500);
}

window.addEventListener("error", (event) => {
  console.error("[horizon] uncaught:", event.error && event.error.stack ? event.error.stack : event.message);
  fail(event.error || event.message);
});
window.addEventListener("unhandledrejection", (event) => fail(event.reason));

boot().catch(fail);
