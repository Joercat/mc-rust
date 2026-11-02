// Drive the real page in headless Chromium: boot, saves, and a warm reload.
//
// The node harnesses (`asset_cache_check.mjs`, `no_park_main_thread.sh`) prove
// the modules in isolation. This script proves the *deployed page*: it loads
// the live URL in Chromium, waits for the engine to reach its event loop,
// reports whether the document is cross-origin isolated and which engine ran,
// forces save cycles and measures the longest main-thread block, then reloads
// and counts what the second load fetches.
//
//   PLAYWRIGHT_PATH=$PWD/target/pw/node_modules/playwright/index.js \
//     node scripts/web_browser_check.mjs [url]
//
// Playwright and its Chromium are not project dependencies; install them in a
// scratch directory (`npm i playwright && npx playwright install chromium`).

// Resolved from `PLAYWRIGHT_PATH` when set: Playwright is not a project
// dependency, so a scratch install lives outside this tree.
const playwright = await import(process.env.PLAYWRIGHT_PATH || "playwright");
// `playwright` is CommonJS: named exports appear on the namespace in some
// Node versions and only under `default` in others.
const chromium = playwright.chromium || (playwright.default && playwright.default.chromium);
if (!chromium) throw new Error("playwright is not installed; see the header");

const BASE = process.argv[2] || "https://horizon-web-pna6.onrender.com/";
const STAMP = Date.now();
const HEADLESS_FLAGS = [
  "--no-sandbox",
  "--disable-dev-shm-usage",
  "--use-gl=angle",
  "--use-angle=swiftshader",
  "--enable-unsafe-swiftshader",
  "--enable-features=Vulkan",
];

const problems = [];
const fail = (message) => {
  problems.push(message);
  console.error(`FAIL ${message}`);
};

// Recorded before any page script runs: long tasks and the gap between
// animation frames are what "the game froze for a second" actually is.
function instrument() {
  window.__longtasks = [];
  window.__frames = { last: 0, worst: 0 };
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        window.__longtasks.push({ start: Math.round(entry.startTime), ms: Math.round(entry.duration) });
      }
    }).observe({ entryTypes: ["longtask"] });
  } catch {
    /* no longtask support */
  }
  const tick = (now) => {
    const previous = window.__frames.last;
    if (previous) window.__frames.worst = Math.max(window.__frames.worst, now - previous);
    window.__frames.last = now;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  // The page saves on `visibilitychange`; a hidden document is what a tab
  // switch looks like. Nothing else in the page can be asked for a save from
  // outside, so this is the honest way to drive one.
  const realSetInterval = window.setInterval.bind(window);
  window.setInterval = (fn, delay, ...rest) =>
    realSetInterval(fn, delay === 1000 ? 50 : delay, ...rest);
  window.__hide = () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  };
}

const text = (page, selector) =>
  page.evaluate((sel) => {
    const el = document.querySelector(sel);
    return el ? el.textContent : null;
  }, selector);

const log = (page) => text(page, "#log").then((value) => value || "");

async function status(page) {
  try {
    return await page.evaluate(() => {
      const raw = window.__horizon_status;
      if (!raw) return null;
      try {
        return JSON.parse(raw);
      } catch {
        return raw;
      }
    });
  } catch {
    return null;
  }
}

/// `horizon_web_status` is only refreshed once a second, so a state change is
/// read by polling rather than by waiting a fixed time.
async function waitState(page, want, limitMs = 5000) {
  const deadline = Date.now() + limitMs;
  let state = await status(page);
  while (Date.now() < deadline) {
    if (state && state.state === want) return state;
    await new Promise((r) => setTimeout(r, 100));
    state = await status(page);
  }
  return state;
}

/// What the browser's own `horizon-web` database holds: the game's saved files.
async function savedFiles(page) {
  return page.evaluate(
    () =>
      new Promise((resolve) => {
        const request = indexedDB.open("horizon-web", 1);
        request.onerror = () => resolve({ error: String(request.error) });
        request.onsuccess = () => {
          const db = request.result;
          const tx = db.transaction("files", "readonly");
          const all = tx.objectStore("files").getAll();
          all.onerror = () => resolve({ error: String(all.error) });
          all.onsuccess = () => {
            const records = all.result || [];
            const bytes = records.reduce((total, record) => total + (record.bytes ? record.bytes.byteLength : 0), 0);
            resolve({ count: records.length, bytes, mtime: Math.max(0, ...records.map((r) => r.mtime || 0)) });
            db.close();
          };
        };
      }),
  );
}

/// What the asset cache holds, and how big it is.
async function assetCache(page) {
  return page.evaluate(
    () =>
      new Promise((resolve) => {
        const request = indexedDB.open("horizon-web-assets", 1);
        request.onerror = () => resolve({ error: String(request.error) });
        request.onsuccess = () => {
          const db = request.result;
          const tx = db.transaction("assets", "readonly");
          const keys = tx.objectStore("assets").getAllKeys();
          keys.onerror = () => resolve({ error: String(keys.error) });
          keys.onsuccess = () => {
            resolve({ keys: (keys.result || []).map(String) });
            db.close();
          };
        };
      }),
  );
}

async function boot(page, label) {
  const started = Date.now();
  let lastPhase = "";
  const deadline = started + 240_000;
  for (;;) {
    const [phase, errorBox, bootHidden] = await Promise.all([
      text(page, "#phase"),
      page.evaluate(() => {
        const el = document.getElementById("error");
        return el && el.style.display === "block" ? el.textContent : null;
      }),
      page.evaluate(() => document.getElementById("boot").classList.contains("hidden")),
    ]);
    if (errorBox) throw new Error(`${label}: page showed an error:\n${errorBox}`);
    if (bootHidden && (await log(page)).includes("event loop running")) {
      return { ms: Date.now() - started };
    }
    if (phase !== lastPhase) {
      lastPhase = phase;
      console.log(`  [${label}] ${phase}`);
    }
    if (Date.now() > deadline) throw new Error(`${label}: not running after 240s (phase: ${lastPhase})`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

/// Force a save through the page's own hidden-tab path and wait for the bytes
/// to reach IndexedDB.
async function forceSave(page, label) {
  const before = await savedFiles(page);
  const t0 = Date.now();
  await page.evaluate(() => window.__hide());
  let after = before;
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 500));
    after = await savedFiles(page);
    if (after.count > (before.count || 0) || (after.mtime || 0) > (before.mtime || 0)) break;
  }
  const ms = Date.now() - t0;
  console.log(
    `  [${label}] save: ${after.count} file(s), ${((after.bytes || 0) / 1024).toFixed(0)} KiB, ` +
      `${ms} ms to reach IndexedDB`,
  );
  return { ...after, ms };
}

// `CHROME_PATH` lets a scratch install of the browser be used by name; the
// bundled headless shell is a separate download this VM may not have.
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CHROME_PATH || undefined,
  args: HEADLESS_FLAGS,
});

async function openPage(context, url, label) {
  const page = await context.newPage();
  const consoleLines = [];
  const requests = [];
  const errors = [];
  page.on("console", (message) => {
    const line = `${message.type()}: ${message.text()}`;
    consoleLines.push(line);
    if (message.type() === "error" || /Atomics\.wait/.test(line)) console.log(`  [${label}] ${line}`);
  });
  page.on("pageerror", (error) => {
    errors.push(String(error && error.stack ? error.stack : error));
    console.log(`  [${label}] pageerror: ${error}`);
  });
  page.on("request", (request) => requests.push(request.url()));
  page.on("response", (response) => {
    if (response.status() >= 400) console.log(`  [${label}] HTTP ${response.status()} ${response.url()}`);
  });
  page.on("requestfailed", (request) => console.log(`  [${label}] request failed: ${request.url()}`));
  await page.addInitScript(instrument);
  await page.addInitScript(() => {
    window.__phases = [];
    addEventListener("DOMContentLoaded", () => {
      const el = document.getElementById("phase");
      const record = () => window.__phases.push({ at: Math.round(performance.now()), text: el.textContent });
      record();
      new MutationObserver(record).observe(el, { childList: true, characterData: true, subtree: true });
    });
  });
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 120_000 });
  return { page, consoleLines, requests, errors };
}

console.log(`live page: ${BASE}`);
const context = await browser.newContext({ viewport: { width: 1024, height: 768 } });
const WORLD = `selftest${STAMP}`;
const first = await openPage(context, `${BASE}?bust=${STAMP}&world=${WORLD}&quality=low`, "first");
const page = first.page;

const bootResult = await boot(page, "first");

// The event loop runs on the title screen; only a loaded world saves, so wait
// for the game itself to report that it is playing.
async function waitPlaying(page, label, limitMs = 180_000) {
  const deadline = Date.now() + limitMs;
  for (;;) {
    const state = await status(page);
    const playing = state && state.state === "playing" && state.player_some;
    if (playing) {
      console.log(`  [${label}] playing: world "${state.world_name}", ${state.chunks} chunk(s), ${state.fps.toFixed(1)} fps`);
      return state;
    }
    if (Date.now() > deadline) {
      console.log(`  [${label}] never reached play: ${JSON.stringify(state)}`);
      return state;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}
const worldState = await waitPlaying(page, "first");
const isolation = await page.evaluate(() => ({
  crossOriginIsolated: window.crossOriginIsolated,
  sharedArrayBuffer: typeof SharedArrayBuffer !== "undefined",
  secureContext: window.isSecureContext,
}));
const engineLine = (await log(page)).split("\n").find((line) => line.includes("wasm engine ready")) || "(none)";
const readyLine = (await log(page)).split("\n").find((line) => line.includes("event loop running")) || "(none)";
console.log(`  boot: ${bootResult.ms} ms`);
console.log(`  crossOriginIsolated: ${isolation.crossOriginIsolated}, SharedArrayBuffer: ${isolation.sharedArrayBuffer}`);
console.log(`  engine: ${engineLine}`);
console.log(`  ready: ${readyLine}`);
if (isolation.crossOriginIsolated !== true) fail("the document is not cross-origin isolated");
if (!/threaded/.test(engineLine)) fail(`the threaded engine did not start (${engineLine})`);

console.log(`  status: ${JSON.stringify(await status(page))}`);

// Save cycles: two forced (the hidden-tab path) plus whatever the engine's own
// autosave does while the page runs.
// From here on a stall is a stall in play, not in the loader.
const playAt = await page.evaluate(() => {
  window.__playAt = performance.now();
  return window.__playAt;
});
const saves = [];
for (let i = 1; i <= 3; i += 1) saves.push(await forceSave(page, `save ${i}`));

// A container: chests at eye level in every direction, placed with the game's
// own chat command, then used with a right-click. The engine reports a screen by
// leaving "playing" while not paused — but chat and inventory screen would too,
// so the chest is closed again and reopened by a second click: a click cannot
// open chat, so a screen that appears from a click is a container.
async function useOnce(target, label) {
  const box = await target.evaluate(() => {
    const rect = document.getElementById("horizon-canvas").getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  });
  const t0 = Date.now();
  await target.mouse.click(box.x, box.y, { button: "right" });
  for (let i = 0; i < 150; i += 1) {
    const state = await status(target);
    if (state && state.state === "menu" && state.pause === false) {
      const ms = Date.now() - t0;
      console.log(`  [${label}] container screen after ${ms} ms`);
      return { opened: true, ms };
    }
    await new Promise((r) => setTimeout(r, 20));
  }
  const state = await status(target);
  console.log(`  [${label}] no container screen: state ${state && state.state}, pause ${state && state.pause}`);
  return { opened: false, ms: Date.now() - t0 };
}

/// Back to play: a chat screen left open by the commands, or a pause menu an
/// Escape opened, both close with Escape.
async function backToPlay(target) {
  let state = await status(target);
  for (let attempt = 0; attempt < 2 && state && state.state !== "playing"; attempt += 1) {
    await target.keyboard.press("Escape");
    state = await waitState(target, "playing", 2500);
  }
  return state;
}

async function placeChests(target) {
  for (const at of ["~1 ~1 ~", "~-1 ~1 ~", "~ ~1 ~1", "~ ~1 ~-1", "~1 ~1 ~1", "~-1 ~1 ~-1"]) {
    await target.keyboard.press("Slash");
    await target.waitForTimeout(120);
    await target.keyboard.type(`setblock ${at} minecraft:chest`, { delay: 6 });
    await target.keyboard.press("Enter");
    await target.waitForTimeout(200);
  }
  await target.waitForTimeout(400);
  return backToPlay(target);
}

const placedState = await placeChests(page);
console.log(`  [chest] after placing: state ${placedState && placedState.state}, pause ${placedState && placedState.pause}`);
if (!placedState || placedState.state !== "playing") fail(`the game did not return to play after the commands (${placedState && placedState.state})`);

const chest = await useOnce(page, "chest");
if (chest.opened) {
  await page.keyboard.press("Escape");
  const closed = await waitState(page, "playing");
  if (!closed || closed.state !== "playing") fail(`Escape did not return to play (${closed && closed.state})`);
  const again = await useOnce(page, "chest again");
  if (!again.opened) fail("the second click did not reopen the container");
  if (again.ms > 1000) fail(`the second container open took ${again.ms} ms`);
  await page.keyboard.press("Escape");
  await waitState(page, "playing", 3000);
}

console.log("  running for autosaves…");
await new Promise((r) => setTimeout(r, 75_000));
const autosaved = await savedFiles(page);
console.log(`  after autosaves: ${autosaved.count} file(s), ${((autosaved.bytes || 0) / 1024).toFixed(0)} KiB`);

const phases = await page.evaluate(() => window.__phases);
console.log(`  phases: ${phases.map((p) => `${p.at}ms ${p.text}`).join(" | ")}`);
const longTasks = await page.evaluate(() => window.__longtasks);
const frames = await page.evaluate(() => window.__frames);
const inPlay = longTasks.filter((task) => task.start >= playAt);
const atBoot = longTasks.filter((task) => task.start < playAt);
const worst = (list) => list.reduce((top, task) => Math.max(top, task.ms), 0);
console.log(`  long tasks over 50 ms: ${longTasks.length} (${atBoot.length} while loading, ${inPlay.length} in play)`);
console.log(`  worst while loading: ${worst(atBoot)} ms; worst in play: ${worst(inPlay)} ms`);
console.log(`  play stalls: ${inPlay.map((t) => `${t.start}ms/${t.ms}ms`).join(", ") || "none"}`);
if (worst(inPlay) > 1000) fail(`a ${worst(inPlay)} ms block happened while playing`);

const cacheBefore = await assetCache(page);
console.log(`  asset cache before reload: ${cacheBefore.keys.length} record(s): ${cacheBefore.keys.join(", ")}`);

const atomics = [...first.consoleLines, ...first.errors].filter((line) => /Atomics\.wait/.test(line));
if (atomics.length > 0) fail(`the page reported Atomics.wait: ${atomics[0]}`);
if (first.errors.length > 0) fail(`page error: ${first.errors[0].split("\n")[0]}`);
if (!chest.opened) fail("no container screen opened from the right-click");
if (chest.ms > 1000) fail(`the container took ${chest.ms} ms to appear`);

// A warm reload: the engine, its glue and the pack must come from IndexedDB.
const second = await openPage(context, `${BASE}?bust=${STAMP + 1}&world=${WORLD}&quality=low`, "reload");
const reloadBoot = await boot(second.page, "reload");
const engineRequests = second.requests.filter((url) =>
  /horizon-client(?:-threaded)?(_bg\.wasm|\.js|)?|horizon-vfs\.bin/.test(url),
);
console.log(`  reload boot: ${reloadBoot.ms} ms, ${second.requests.length} request(s) total`);
console.log(`  reload engine/pack requests: ${engineRequests.length}`);
for (const url of engineRequests) console.log(`    ${url}`);
const reloadCache = await assetCache(second.page);
console.log(`  asset cache after reload: ${reloadCache.keys.length} record(s): ${reloadCache.keys.join(", ")}`);
const reloadLog = await log(second.page);
console.log(`  reload engine line: ${reloadLog.split("\n").find((l) => l.includes("wasm engine ready")) || "(none)"}`);
console.log(`  reload pack line:   ${reloadLog.split("\n").find((l) => l.includes("resource pack")) || "(none)"}`);
if (engineRequests.length > 0) fail(`the reload downloaded engine files: ${engineRequests[0]}`);
if (reloadCache.keys.length === 0) fail("the reload found no asset cache records");
const reloadAtomics = [...second.consoleLines, ...second.errors].filter((line) => /Atomics\.wait/.test(line));
if (reloadAtomics.length > 0) fail(`the reload reported Atomics.wait: ${reloadAtomics[0]}`);

await browser.close();

console.log("");
console.log(`saves: ${saves.length} forced, autosaves wrote ${autosaved.count} file(s)`);
console.log(`chest: ${chest.opened ? `opened in ${chest.ms} ms` : "not opened"}`);
if (problems.length > 0) {
  for (const problem of problems) console.error(`FAIL ${problem}`);
  process.exit(1);
}
console.log("BROWSER_OK");
