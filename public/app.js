// Horizon 26.3 web host: mounts the resource VFS pack, restores the player's
// `/game` folder from IndexedDB, starts the real Horizon client, then keeps the
// browser's storage in sync with the files the game writes.
import { startThreadPool } from "./threads.js";
import { createAssetCache } from "./asset-cache.js";

const bootEl = document.getElementById("boot");
const barEl = document.querySelector("#bar > i");
const phaseEl = document.getElementById("phase");
const logEl = document.getElementById("log");
const canvasEl = document.getElementById("horizon-canvas");

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
  // The first failure is the one worth reading; a page that keeps throwing
  // every frame would replace it with a later, smaller one.
  if (errorEl.style.display === "block") {
    console.error(err);
    return;
  }
  const message = err && err.stack ? err.stack : String(err);
  errorEl.style.display = "block";
  errorEl.textContent = `Horizon failed to start:\n\n${message}`;
  console.error(err);
}

// The threaded engine's shared memory may only be waited on from a worker, and
// the page's main thread cannot block: when a lock the frame takes is held by a
// worker busy with something else (a save, terrain), the engine throws this out
// of the call it was making. It is a thrown error, not a corrupted module — the
// next frame runs — so a page that reports it and carries on keeps the world
// instead of painting the crash screen over it.
const parkedMainThread = (err) =>
  /cannot be called in this context/.test(String((err && err.message) || err)) &&
  /Atomics\.wait/.test(String((err && err.stack) || ""));
let parkWarnings = 0;
function noteParkedThread(err) {
  parkWarnings += 1;
  console.warn("[horizon] the engine blocked its main thread; the frame was dropped", err);
  if (parkWarnings === 1) say("engine stalled a frame (a save or terrain is running); recovered");
}

// The engine, its glue and the resource pack are build outputs
// (`scripts/build_web.sh`, `scripts/pack_vfs.py`), not source: a fresh checkout
// has none of them. A static import would fail before a line of this file ran
// and leave the page on "starting…" forever, so they are loaded here, where a
// missing build can name the files it wants.
//
// Two engines are built: the threaded one, whose chunk workers are Web Workers
// sharing one memory (`threads.js`), and the single-threaded one every browser
// can run. The threaded one needs cross-origin isolation for shared memory.
let wasm = null;
let init = null;
// Stamped into every engine URL: the modules are tens of megabytes and a host
// or browser will happily keep one from an older build under the same path.
// Bump this whenever `scripts/build_web.sh` publishes new engines.
const BUILD = "2026-11-01.23";
const withBuild = (path) => {
  const url = new URL(path, import.meta.url);
  url.searchParams.set("v", BUILD);
  return url;
};
const ENGINES = {
  threaded: {
    glue: "./horizon-client-threaded.js",
    wasm: "./horizon-client-threaded_bg.wasm",
  },
  single: {
    glue: "./horizon-client.js",
    wasm: "./horizon-client_bg.wasm",
  },
};

/// Whether this build has `name`. The build's manifest answers that without a
/// request; a host with no manifest is asked with a HEAD, where only a definite
/// "no such file" counts (a refused HEAD is not a missing build).
async function present(cache, name) {
  // Only a definite yes is taken from the manifest. A "no" is confirmed with
  // the host before the page calls its own build missing: a manifest can lag a
  // half-published deploy, and a stale one must not stop the page starting.
  const known = await cache.has(name).catch(() => null);
  if (known === true) return true;
  try {
    const response = await fetch(withBuild(name), { method: "HEAD" });
    return response.status !== 404 && response.status !== 410;
  } catch {
    return false;
  }
}

// Why the page cannot be cross-origin isolated, in the words that name the
// thing to fix: the threaded engine's shared memory needs all of it.
async function isolationProblem() {
  if (globalThis.isSecureContext !== true) {
    return `${location.origin} is not a secure context: open it over HTTPS, or on localhost`;
  }
  if (typeof SharedArrayBuffer === "undefined") {
    return "this browser has no SharedArrayBuffer";
  }
  // Same origin, so the response's headers are readable: neither isolation
  // header is a forbidden response header name.
  let coop = null;
  let coep = null;
  try {
    const response = await fetch(location.href, { method: "HEAD", cache: "no-store" });
    coop = response.headers.get("cross-origin-opener-policy");
    coep = response.headers.get("cross-origin-embedder-policy");
  } catch {
    // A host that refuses HEAD keeps the generic wording below.
  }
  if (!coop || !coep) {
    return "the server sends no COOP/COEP headers (scripts/serve_web.py does)";
  }
  if (coop !== "same-origin" || (coep !== "require-corp" && coep !== "credentialless")) {
    return `the server's COOP/COEP headers (${coop}, ${coep}) do not isolate this page`;
  }
  return "this page is embedded by one that is not isolated";
}

async function chooseEngine(cache) {
  const isolated =
    globalThis.crossOriginIsolated === true && typeof SharedArrayBuffer !== "undefined";
  if (isolated && (await present(cache, ENGINES.threaded.glue))) {
    return { ...ENGINES.threaded, threaded: true };
  }
  if (isolated) say("threaded engine not built: using the single-threaded one");
  else say(`single-threaded engine: ${await isolationProblem()}`);
  return { ...ENGINES.single, threaded: false };
}

// A browser without a WebGPU adapter still lets a page ask for one, and that
// call binds a `webgpu` context to the canvas. wgpu's WebGL2 fallback then says
// "canvas already in use" and no backend is left at all. Ask for an adapter
// first, and when there is none select WebGL2 before the engine starts: it
// reads the query string itself, so putting it in the URL is enough.
async function pickRenderer(params) {
  if (params.get("backend") === "webgl") return;
  if (!navigator.gpu || typeof navigator.gpu.requestAdapter !== "function") {
    say("no WebGPU in this browser: starting on WebGL2");
  } else {
    let adapter = null;
    try {
      adapter = await navigator.gpu.requestAdapter();
    } catch (error) {
      console.warn("[horizon] requestAdapter failed", error);
    }
    if (adapter) return;
    say("no WebGPU adapter: starting on WebGL2");
  }
  const url = new URL(window.location.href);
  url.searchParams.set("backend", "webgl");
  window.history.replaceState(null, "", url);
}

// A device with four threads and a few gigabytes cannot draw a 32-chunk view:
// ChromeOS' Kompanio-class parts sit here. The engine reads `quality` from the
// query string, applies the fast preset on a first run and caps the render
// distance for every later change, so the setting sticks instead of snapping
// back to whatever options.txt holds.
function pickQuality(params) {
  if (params.get("quality")) return;
  const cores = navigator.hardwareConcurrency || 4;
  const memory = navigator.deviceMemory || 8;
  if (cores > 4 && memory > 4) return;
  const url = new URL(window.location.href);
  url.searchParams.set("quality", "low");
  window.history.replaceState(null, "", url);
  say(`fast settings: ${cores} threads, ${memory} GB`);
}

async function loadEngine(cache) {
  const engine = await chooseEngine(cache);
  const missing = [];
  for (const [path, what] of [
    [engine.glue, "the wasm-bindgen glue"],
    [engine.wasm, "the compiled client"],
    ["./horizon-vfs.bin", "the resource pack"],
  ]) {
    if ((await present(cache, path)) === true) continue;
    missing.push(`  ${path.slice(2)} — ${what}`);
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
  // The glue is a build output like the wasm, so it comes through the same
  // content cache: a blob module is the one way to run stored bytes as a
  // module, and it has no relative imports of its own to resolve.
  let module = null;
  let glueUrl = withBuild(engine.glue).href;
  try {
    const glue = await cache.bytes(engine.glue);
    glueUrl = URL.createObjectURL(new Blob([glue.bytes], { type: "text/javascript" }));
    module = await import(glueUrl);
    say(`wasm-bindgen glue${glue.cached ? " from the cache" : ""}`);
  } catch (error) {
    // A browser that will not import a blob module, or an unusable cache: the
    // glue's own URL is still a module.
    console.warn("[horizon] glue from the cache failed; importing it by URL", error);
    glueUrl = withBuild(engine.glue).href;
    module = await import(glueUrl);
  }
  wasm = module;
  init = module.default;
  return { ...engine, glueUrl };
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

// A save makes the game hand over every file it wrote (region files and all) in
// one call, and a second drain started behind a slow IndexedDB write would only
// copy the same bytes later. One batch at a time — but a drain asked for while
// one runs (the page being hidden, say) is not dropped: the loop takes it next.
// Bytes copied out of the engine per call. This copy is the part of the mirror
// that runs on the page's main thread, and one autosave dirties hundreds of
// region files at once. At 4 MiB a single slice was a multi-hundred-millisecond
// task on a slow device, which the player feels as the game freezing while it
// saves; a quarter of a megabyte is a task of a couple of milliseconds, and the
// loop simply takes more turns of the event loop to finish. A slice that still
// takes longer than 20 ms says so in the console, so the next report of a
// hitch has a number.
const PERSIST_SLICE = 256 << 10;
let persisting = false;
let persistQueued = false;
async function persist(db) {
  if (persisting) {
    persistQueued = true;
    return;
  }
  persisting = true;
  try {
    let files = 0;
    let removed = 0;
    let more = true;
    for (let batch = 0; batch < 256; batch += 1) {
      persistQueued = false;
      const started = performance.now();
      const [written, deleted, next] = wasm.horizon_web_drain_changes(PERSIST_SLICE);
      const copied = performance.now() - started;
      if (copied > 20) {
        console.log(`[horizon] mirror slice copied in ${copied.toFixed(0)} ms`);
      }
      more = next;
      if (written.length > 0 || deleted.length > 0) {
        const records = [];
        for (const [path, bytes, mtime] of written) {
          // `bytes` is already a copy in the JS heap (`horizon_web_drain_changes`
          // builds a Uint8Array); IndexedDB clones it again on the way in.
          records.push({ path, bytes, mtime });
        }
        await writeFiles(db, records);
        await deleteFiles(db, deleted);
        files += records.length;
        removed += deleted.length;
      }
      // More to mirror, or a drain asked for while this one ran (the page
      // being hidden asks for one): either way another slice is due.
      if (!more && !persistQueued) break;
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    // The readout says what the whole drain did, not what each slice did: one
    // DOM write per drain instead of one per quarter megabyte.
    if (files > 0 || removed > 0) say(`saved ${files} file(s), removed ${removed}`);
  } finally {
    persisting = false;
  }
}

/// Starts the threaded engine's workers before the game does: a streaming
/// world takes them as its chunk workers. A failure leaves the game playable
/// (it runs the jobs on the frame), so it is reported, not fatal.
async function startEngineThreads(engine) {
  if (!engine.threaded || typeof wasm.horizon_web_thread_handles !== "function") return;
  const handles = wasm.horizon_web_thread_handles();
  // One core left for the frame and the integrated server, as on the desktop.
  const count = Math.max(1, Math.min((navigator.hardwareConcurrency || 4) - 1, 16));
  try {
    const workers = await startThreadPool({
      module: handles[0],
      memory: handles[1],
      glueUrl: engine.glueUrl || withBuild(engine.glue).href,
      count,
    });
    say(`worker threads: ${workers.length}`);
  } catch (error) {
    say(`worker threads failed: ${error}`);
    console.warn("[horizon] worker threads failed", error);
  }
}

async function boot() {
  phase("looking for the engine…", 0.01);
  // The build's assets, from the content-addressed cache when this browser
  // already has those bytes. A cache it cannot open leaves every `bytes` call
  // on the network, which is slow but works.
  const cache = createAssetCache({
    fetchImpl: (url, options) => fetch(url, options),
    origin: import.meta.url,
    build: BUILD,
    nocache: new URLSearchParams(location.search).get("nocache") === "1",
  });
  await cache.open();
  const engine = await loadEngine(cache);

  phase("opening browser storage…", 0.02);
  const db = await openDb();

  phase("loading engine…", 0.03);
  const engineAsset = await cache.bytes(engine.wasm, (p) =>
    phase("downloading the engine…", 0.03 + p * 0.03),
  );
  // A compiled module, not a URL: the bytes may have come from IndexedDB.
  await init({ module_or_path: await WebAssembly.compile(engineAsset.bytes) });
  const flavour = engine.threaded ? `threaded, build ${BUILD}` : `build ${BUILD}`;
  say(`wasm engine ready (${flavour})${engineAsset.cached ? " from the cache" : ""}`);

  phase("starting worker threads…", 0.04);
  await startEngineThreads(engine);

  phase("downloading resources…", 0.08);
  const pack = await cache.bytes("horizon-vfs.bin", (p) =>
    phase("downloading resources…", 0.08 + p * 0.55),
  );
  say(
    `resource pack: ${(pack.bytes.length / (1024 * 1024)).toFixed(1)} MB` +
      (pack.cached ? " from the cache" : ""),
  );
  // Anything else this cache holds is a build this page cannot use.
  void cache.prune([engine.glue, engine.wasm, "horizon-vfs.bin"]);

  phase("mounting resources…", 0.66);
  const mounted = wasm.horizon_web_mount_pack(pack.bytes);
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
  await pickRenderer(params);
  pickQuality(params);
  await wasm.horizon_web_start("horizon-canvas", width, height, world, creative);

  phase("ready", 1.0);
  say("event loop running");
  // A Chromebook's top row sends Back, Refresh, Fullscreen and the volume and
  // brightness keys, not F1-F12, unless "Use the top row keys as function
  // keys" is on. The Alt aliases below are the same keys on every keyboard.
  say("F1 HUD · F2 screenshot · F3 debug · F5 camera (ChromeOS: hold F + 1/2/3/5, F + 0 fullscreen)");
  bootEl.classList.add("hidden");
  canvasEl.focus();
  running = true;

  // Save what the game writes. `?persist=0` turns it off: the boot probes use
  // it to tell a storage stall from a renderer one. The autosave only writes
  // every minute, so eight seconds catches it without copying on every tick.
  if (params.get("persist") !== "0") setInterval(async () => {
    try {
      await persist(db);
    } catch (e) {
      console.warn("persist failed", e);
    }
  }, 8000);
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
  // Kept for the boot probes and the console; the page draws no readout of its
  // own, so nothing is written to the DOM here.
  setInterval(() => {
    try {
      window.__horizon_status = wasm.horizon_web_status();
    } catch (e) {
      /* keep the last readout */
    }
  }, 1000);
}

// The browser owns most function keys: F1 opens its help, F3 find, F5 reloads
// the page (the client cycles the camera on it), F11 its own fullscreen, and
// Tab moves focus instead of opening the player list. Once the game runs, the
// keys it binds have to reach winit, so they are taken from the browser here.
// F12 stays the browser's: closing the devtools would leave a player stuck.
const GAME_KEYS = new Set([
  "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11",
  "Tab", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
]);
let running = false;
window.addEventListener(
  "keydown",
  (event) => {
    if (!running) return;
    const target = event.target;
    if (
      target instanceof HTMLElement &&
      (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
    ) {
      return;
    }
    if (GAME_KEYS.has(event.key) || GAME_KEYS.has(event.code)) event.preventDefault();
  },
  { capture: true },
);

// Function keys are re-sent from the page rather than passed through. The
// engine binds on the event's `code`, and a Chromebook's top row (and some
// other keyboards) can report F1-F12 with a code the engine does not know, or
// not deliver them at all: F3 then opens the browser's find bar instead of the
// debug screen. Dispatching on the canvas, where winit listens, gives the
// engine one event with both `key` and `code` set.
//
// A top row that never sends the key has no F to press, so the letter F is the
// modifier: hold it (or tap it) and press a digit, and the digit becomes the
// function key — F+3 is F3, F+0 is fullscreen. The digit is stopped before the
// game sees it, or it would also pick a hotbar slot; the F itself is left
// alone, as the game may bind it.
const FUNCTION_DIGITS = new Map([
  ["Digit1", "F1"],
  ["Digit2", "F2"],
  ["Digit3", "F3"],
  ["Digit4", "F4"],
  ["Digit5", "F5"],
  ["Digit6", "F6"],
  ["Digit7", "F7"],
  ["Digit8", "F8"],
  ["Digit9", "F9"],
  // F10 is unbound, and fullscreen (F11) needs a key that exists on the top
  // row's replacement layout.
  ["Digit0", "F11"],
]);
let synthesizing = false;
function sendKey(key) {
  synthesizing = true;
  try {
    // Press and release: F3 switches the debug screen on the release, and the
    // other bindings clear their held state there too.
    for (const type of ["keydown", "keyup"]) {
      canvasEl.dispatchEvent(
        new KeyboardEvent(type, { key, code: key, bubbles: true, cancelable: true }),
      );
    }
  } finally {
    synthesizing = false;
  }
}
const isFunctionKey = (key) => /^F([1-9]|1[0-2])$/.test(key) && key !== "F12";
const isTyping = (target) =>
  target instanceof HTMLElement &&
  (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
const isF = (event) => event.code === "KeyF" || event.key === "f" || event.key === "F";
let fHeld = false;
let fPressedAt = 0;
let lastEscapeAt = 0;
// A tap of F still counts for half a second after its release, which is enough
// to hit a digit and short enough not to eat ordinary hotbar keys.
const F_GRACE_MS = 500;
const fArmed = () => fHeld || performance.now() - fPressedAt < F_GRACE_MS;
window.addEventListener(
  "keydown",
  (event) => {
    if (!running || synthesizing || isTyping(event.target)) return;
    if (event.key === "Escape") lastEscapeAt = performance.now();
    if (isFunctionKey(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) sendKey(event.key);
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (isF(event)) {
      fHeld = true;
      fPressedAt = performance.now();
      return;
    }
    const key = FUNCTION_DIGITS.get(event.code);
    if (!key || !fArmed()) return;
    event.preventDefault();
    event.stopPropagation();
    if (!event.repeat) {
      fPressedAt = 0;
      sendKey(key);
    }
  },
  { capture: true },
);
// The matching release is swallowed too: the synthetic one already cleared the
// key, and the real one would toggle F3's debug screen a second time.
window.addEventListener(
  "keyup",
  (event) => {
    if (!running || synthesizing || isTyping(event.target)) return;
    if (isFunctionKey(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (isF(event)) {
      fHeld = false;
      fPressedAt = performance.now();
      return;
    }
    if (FUNCTION_DIGITS.has(event.code) && fArmed()) {
      event.preventDefault();
      event.stopPropagation();
    }
  },
  { capture: true },
);

// Escape is swallowed by the browser to release the pointer, so the game never
// sees the press that closed it and the pause menu takes a second Escape. When
// the capture ends while the world is running, send the Escape the game missed.
// The delay lets a game that did see the key report its own pause first.
document.addEventListener("pointerlockchange", () => {
  if (!running || document.pointerLockElement) return;
  setTimeout(() => {
    if (!running || document.pointerLockElement) return;
    // The game saw the key itself if the page got the press; only the browser
    // eating it leaves this handler as the one that must act.
    if (performance.now() - lastEscapeAt < 500) return;
    let status = {};
    try {
      status = JSON.parse(
        (wasm && wasm.horizon_web_status && wasm.horizon_web_status()) ||
          window.__horizon_status ||
          "{}",
      );
    } catch (error) {
      return;
    }
    if (status.state !== "playing" || status.pause) return;
    sendKey("Escape");
  }, 200);
});

window.addEventListener("error", (event) => {
  if (parkedMainThread(event.error || event.message)) {
    noteParkedThread(event.error || event.message);
    return;
  }
  console.error("[horizon] uncaught:", event.error && event.error.stack ? event.error.stack : event.message);
  fail(event.error || event.message);
});
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  if (parkedMainThread(reason)) {
    noteParkedThread(reason);
    return;
  }
  // A policy answer, not a crash: Chrome rejects `requestPointerLock` when no
  // user gesture is live (SecurityError, or NotAllowedError on newer builds),
  // and winit's web backend drops that promise. The game takes the pointer on
  // the next click, so say so and keep running.
  if (
    reason &&
    (reason.name === "SecurityError" || reason.name === "NotAllowedError") &&
    /pointer lock/i.test(String(reason.message || ""))
  ) {
    say(`pointer lock refused: ${reason.message}`);
    return;
  }
  fail(reason);
});

boot().catch(fail);
