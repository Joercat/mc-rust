// Drive the deployed page in a real browser, twice: normally, and with the
// isolation headers stripped.
//
// The node harnesses (`asset_cache_check.mjs`, `no_park_main_thread.sh`) prove
// the modules in isolation. This script proves the *deployed page*, in the two
// situations a player can actually be in:
//
//   * isolated (COOP: same-origin + COEP: require-corp): the threaded engine
//     may run, and nothing may park the main thread;
//   * not isolated (the same page with those headers removed): the threaded
//     engine's shared memory is unavailable, so the page must choose the
//     single-threaded engine and never let a wait reach the main thread — a
//     page that picks the threaded build here is the "Atomics.wait cannot be
//     called in this context" crash players report.
//
// Both sessions load the live URL in a real world, force save cycles, place a
// chest and open it by clicking, and measure long tasks. The isolated session
// also reloads to count what the second load fetches.
//
//   npm i playwright && npx playwright install chromium      # in a scratch dir
//   PLAYWRIGHT_PATH=$PWD/scratch/node_modules/playwright/index.js \
//     CHROME_PATH=$HOME/chrome/chrome-linux/chrome \
//     node scripts/web_browser_check.mjs [url]

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
function fail(message) {
  problems.push(message);
  console.error(`FAIL ${message}`);
}

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
  // The page refreshes `__horizon_status` once a second, which is coarser than
  // the latencies measured here. Speed up only that one timer (in this browser
  // only) so a container open is timed to tens of milliseconds.
  const realSetInterval = window.setInterval.bind(window);
  window.setInterval = (fn, delay, ...rest) =>
    realSetInterval(fn, delay === 1000 ? 50 : delay, ...rest);
  // The page saves on `visibilitychange`; a hidden document is what a tab
  // switch looks like. Nothing else in the page can be asked for a save from
  // outside, so this is the honest way to drive one.
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

/// `horizon_web_status` is only refreshed once a second by the page (50 ms with
/// the probe's timer override), so a state change is read by polling.
async function waitState(page, want, limitMs = 5000) {
  const deadline = Date.now() + limitMs;
  let state = await status(page);
  while (Date.now() < deadline) {
    if (state && state.state === want) return state;
    await new Promise((r) => setTimeout(r, 50));
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

/// The asset cache's keys: the manifest plus one hash per stored file.
async function assetCache(page) {
  return page.evaluate(
    () =>
      new Promise((resolve) => {
        const request = indexedDB.open("horizon-web-assets", 1);
        request.onerror = () => resolve({ keys: [], error: String(request.error) });
        request.onsuccess = () => {
          const db = request.result;
          const tx = db.transaction("assets", "readonly");
          const keys = tx.objectStore("assets").getAllKeys();
          keys.onerror = () => resolve({ keys: [], error: String(keys.error) });
          keys.onsuccess = () => {
            resolve({ keys: (keys.result || []).map(String) });
            db.close();
          };
        };
      }),
  );
}

async function boot(page, label, limitMs = 300_000) {
  const started = Date.now();
  let lastPhase = "";
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
    if (bootHidden && (await log(page)).includes("event loop running")) return { ms: Date.now() - started };
    if (phase !== lastPhase) {
      lastPhase = phase;
      console.log(`  [${label}] ${phase}`);
    }
    if (Date.now() > started + limitMs) throw new Error(`${label}: not running after ${limitMs} ms (phase: ${lastPhase})`);
    await new Promise((r) => setTimeout(r, 500));
  }
}

/// Only a loaded world saves, so wait for the game itself to report playing.
async function waitPlaying(page, label, limitMs = 240_000) {
  const deadline = Date.now() + limitMs;
  for (;;) {
    const state = await status(page);
    if (state && state.state === "playing" && state.player_some) {
      console.log(
        `  [${label}] playing: world "${state.world_name}", ${state.chunks} chunk(s), ` +
          `${state.fps.toFixed(1)} fps, ${state.frames} frame(s)`,
      );
      return state;
    }
    if (Date.now() > deadline) {
      console.log(`  [${label}] never reached play: ${JSON.stringify(state)}`);
      return state;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

/// Force a save through the page's own hidden-tab path and wait for the bytes
/// to reach IndexedDB.
async function forceSave(page, label) {
  const before = await savedFiles(page);
  const t0 = Date.now();
  await page.evaluate(() => window.__hide());
  let after = before;
  for (let i = 0; i < 60; i += 1) {
    await new Promise((r) => setTimeout(r, 250));
    after = await savedFiles(page);
    if (after.count > (before.count || 0) || (after.mtime || 0) > (before.mtime || 0)) break;
  }
  const ms = Date.now() - t0;
  console.log(
    `  [${label}] save: ${after.count} file(s), ${((after.bytes || 0) / 1024).toFixed(0)} KiB, ${ms} ms to reach IndexedDB`,
  );
  return { ...after, ms };
}

/// The worst long task that overlaps [from, to] in page time (performance.now).
async function worstTask(page, from, to) {
  const tasks = await page.evaluate(() => window.__longtasks);
  let worst = { start: 0, ms: 0 };
  for (const task of tasks) {
    if (task.start + task.ms >= from && task.start <= to && task.ms > worst.ms) worst = task;
  }
  return worst;
}

/// A left click in the middle of the game view, timed with the worst main
/// thread task it caused: this is what "it freezes when I click" means.
async function clickCenter(page, label) {
  const box = await page.evaluate(() => {
    const rect = document.getElementById("horizon-canvas").getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  });
  const from = await page.evaluate(() => performance.now());
  await page.mouse.click(box.x, box.y, { button: "left" });
  const to = await page.evaluate(() => performance.now());
  const task = await worstTask(page, from, to);
  console.log(
    `  [${label}] click took ${Math.round(to - from)} ms end to end, worst main-thread task ${task.ms} ms`,
  );
  return { ms: Math.round(to - from), task: task.ms };
}

/// Put a couple of stacks in every chest placed in front of the player, so the
/// first container a session opens really has item models to draw (an empty
/// chest never draws one, and the first item drawn is the expensive one).
async function fillChests(page) {
  for (const at of ["~1 ~1 ~", "~-1 ~1 ~", "~ ~1 ~1", "~ ~1 ~-1", "~1 ~1 ~1", "~-1 ~1 ~-1"]) {
    for (const line of [
      `item replace block ${at} container.0 with minecraft:diamond 3`,
      `item replace block ${at} container.1 with minecraft:apple 5`,
    ]) {
      await page.keyboard.press("Slash");
      await page.waitForTimeout(120);
      await page.keyboard.type(line, { delay: 4 });
      await page.keyboard.press("Enter");
      await page.waitForTimeout(180);
    }
  }
  await page.waitForTimeout(300);
  return backToPlay(page);
}

/// One use of whatever is in front of the player. The engine reports a screen
/// by leaving "playing" while not paused.
async function useOnce(page, label) {
  const box = await page.evaluate(() => {
    const rect = document.getElementById("horizon-canvas").getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  });
  const t0 = Date.now();
  const from = await page.evaluate(() => performance.now());
  await page.mouse.click(box.x, box.y, { button: "right" });
  const clickMs = Date.now() - t0;
  for (let i = 0; i < 300; i += 1) {
    const state = await status(page);
    if (state && state.state === "menu" && state.pause === false) {
      const ms = Date.now() - t0;
      const to = await page.evaluate(() => performance.now());
      const task = await worstTask(page, from, to);
      console.log(
        `  [${label}] container screen after ${ms} ms (worst main-thread task ${task.ms} ms, click dispatch ${clickMs} ms)`,
      );
      return { opened: true, ms, clickMs, task: task.ms };
    }
    await new Promise((r) => setTimeout(r, 10));
  }
  const state = await status(page);
  console.log(`  [${label}] no container screen: state ${state && state.state}, pause ${state && state.pause}`);
  return { opened: false, ms: Date.now() - t0, task: 0 };
}

/// Back to play: a chat screen left open by the commands, or a pause menu an
/// Escape opened, both close with Escape.
async function backToPlay(page) {
  let state = await status(page);
  for (let attempt = 0; attempt < 2 && state && state.state !== "playing"; attempt += 1) {
    await page.keyboard.press("Escape");
    state = await waitState(page, "playing", 2500);
  }
  return state;
}

/// Chests at eye level in every direction, placed with the game's own chat
/// command: whichever way the player faces, one is in front of the crosshair.
async function placeChests(page) {
  for (const at of ["~1 ~1 ~", "~-1 ~1 ~", "~ ~1 ~1", "~ ~1 ~-1", "~1 ~1 ~1", "~-1 ~1 ~-1"]) {
    await page.keyboard.press("Slash");
    await page.waitForTimeout(120);
    await page.keyboard.type(`setblock ${at} minecraft:chest`, { delay: 6 });
    await page.keyboard.press("Enter");
    await page.waitForTimeout(200);
  }
  await page.waitForTimeout(400);
  return backToPlay(page);
}

async function openPage(context, url, label) {
  const page = await context.newPage();
  const consoleLines = [];
  const requests = [];
  const errors = [];
  page.on("console", (message) => {
    const line = `${message.type()}: ${message.text()}`;
    consoleLines.push(line);
    // The engine's own log (a slow frame or save step says so itself) and every
    // error: the timing lines are the point of running the page for real.
    if (
      message.type() === "error" ||
      /Atomics\.wait|(container opened|frame update|slow frame|integrated server|lan: |session:|handler took|save step|autosave step|save write|save drain|mirror slice|took [0-9])/.test(
        line,
      )
    ) {
      console.log(`  [${label}] ${line}`);
    }
  });
  page.on("pageerror", (error) => {
    errors.push(String(error && error.stack ? error.stack : error));
    console.log(`  [${label}] pageerror: ${error}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) console.log(`  [${label}] HTTP ${response.status()} ${response.url()}`);
  });
  page.on("requestfailed", (request) => console.log(`  [${label}] request failed: ${request.url()}`));
  page.on("request", (request) => requests.push(request.url()));
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
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 180_000 });
  return { page, consoleLines, requests, errors };
}

const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CHROME_PATH || undefined,
  args: HEADLESS_FLAGS,
});

/// Run across chunk borders holding the keys a player holds: the path that
/// generates, meshes and uploads chunks under the player's feet.
async function runForward(page, label, { forceFailures = 0 } = {}) {
  // The canvas takes the keys: click it first, as a player does.
  const box = await page.evaluate(() => {
    const rect = document.getElementById("horizon-canvas").getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  });
  await page.mouse.click(box.x, box.y);
  const before = await status(page);
  await page.evaluate(() => {
    window.__frames.worst = 0;
  });
  if (forceFailures > 0) await page.evaluate((n) => window.__horizonForceUploads(n), forceFailures);
  const from = await page.evaluate(() => performance.now());
  // Control sprints: a walk covers too few columns for a chunk-crossing test.
  await page.keyboard.down("Control");
  await page.keyboard.down("w");
  let after = before;
  const started = Date.now();
  while (Date.now() - started < 24_000) {
    await new Promise((r) => setTimeout(r, 2000));
    after = await status(page);
  }
  await page.keyboard.up("w");
  await page.keyboard.up("Control");
  const to = await page.evaluate(() => performance.now());
  const task = await worstTask(page, from, to);
  const gap = await page.evaluate(() => Math.round(window.__frames.worst));
  // Movement is proven from the game's own player position, not assumed.
  const pos = (state) => (state && Array.isArray(state.player) ? state.player : null);
  const a = pos(before);
  const b = pos(after);
  const moved = a && b ? Math.hypot(b[0] - a[0], b[2] - a[2]) : 0;
  // The page publishes `generated`; the control readout's `generated_chunks`
  // never appears here, which is why this counted zero for every run.
  const generated = (after.generated || 0) - (before.generated || 0);
  // Everything forced to wait must find room again: the phase is not over
  // until the upload queue has drained.
  let drained = after;
  for (let i = 0; i < 80; i += 1) {
    if ((drained.pending_uploads || 0) === 0) break;
    await new Promise((r) => setTimeout(r, 250));
    drained = await status(page);
  }
  const renderer = drained.renderer || {};
  console.log(
    `  [${label}] run: moved ${moved.toFixed(1)} blocks, ${generated} chunk(s) generated, ` +
      `${before.chunks} -> ${after.chunks} resident, worst main-thread task ${task.ms} ms, ` +
      `worst frame gap ${gap} ms, forced ${forceFailures} upload failure(s), ` +
      `deferred ${renderer.deferred_uploads || 0}, pending ${drained.pending_uploads || 0}, ` +
      `sections ${renderer.sections || 0} drawn ${renderer.detail_sections || 0}`,
  );
  return {
    ms: Math.round(to - from),
    task: task.ms,
    gap,
    moved,
    generated,
    chunks: after.chunks,
    resident: (after.chunks || 0) - (before.chunks || 0),
    deferred: renderer.deferred_uploads || 0,
    pending: drained.pending_uploads || 0,
    sections: renderer.sections || 0,
    detail: renderer.detail_sections || 0,
    forced: forceFailures,
  };
}

/// One full session. `stripIsolation` removes COOP/COEP from the document
/// response, which is exactly what makes a browser not isolate the page — the
/// situation a player is in when the threaded engine's wait throws.
async function runSession({ label, stripIsolation, reload }) {
  console.log(`\n=== ${label} ===`);
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 } });
  if (stripIsolation) {
    await context.route("**/*", async (route) => {
      if (route.request().resourceType() !== "document") {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      const headers = { ...response.headers() };
      for (const name of Object.keys(headers)) {
        if (/^cross-origin-(opener|embedder|resource)-policy$/i.test(name)) delete headers[name];
      }
      console.log(`  [${label}] document headers stripped: ${JSON.stringify(headers)}`);
      await route.fulfill({ response, headers });
    });
  }

  const world = `selftest${STAMP}${stripIsolation ? "n" : "i"}`;
  const first = await openPage(context, `${BASE}?bust=${STAMP}&world=${world}&quality=low`, label);
  const page = first.page;
  const bootResult = await boot(page, label);
  const isolation = await page.evaluate(() => ({
    crossOriginIsolated: window.crossOriginIsolated,
    sharedArrayBuffer: typeof SharedArrayBuffer !== "undefined",
    secureContext: window.isSecureContext,
  }));
  // The page's own log keeps its last eight lines, so an early line (the reason
  // it chose the single-threaded engine) is read from the console stream, which
  // keeps everything.
  const lines = (await log(page)).split("\n");
  const everything = [...first.consoleLines, ...lines].join("\n");
  const engineLine = everything.split("\n").find((line) => line.includes("wasm engine ready")) || "(none)";
  const choiceLine =
    everything
      .split("\n")
      .find((line) => /threaded engine not built|single-threaded engine|engine stalled/.test(line)) || "(none)";
  console.log(`  [${label}] boot: ${bootResult.ms} ms`);
  console.log(`  [${label}] crossOriginIsolated: ${isolation.crossOriginIsolated}, SharedArrayBuffer: ${isolation.sharedArrayBuffer}`);
  console.log(`  [${label}] engine: ${engineLine}`);
  console.log(`  [${label}] engine choice: ${choiceLine}`);

  const worldState = await waitPlaying(page, label);
  const playAt = await page.evaluate(() => {
    window.__playAt = performance.now();
    return window.__playAt;
  });

  const saves = [];
  for (let i = 1; i <= 4; i += 1) saves.push(await forceSave(page, `${label} save ${i}`));

  const placedState = await placeChests(page);
  console.log(`  [${label}] after placing chests: state ${placedState && placedState.state}, pause ${placedState && placedState.pause}`);
  const filledState = await fillChests(page);
  console.log(`  [${label}] after filling chests: state ${filledState && filledState.state}`);
  const chest = await useOnce(page, `${label} chest cold`);
  let again = { opened: false, ms: 0, task: 0 };
  const moves = [];
  if (chest.opened) {
    // Pick a stack up and put it back: the first item the container draws.
    moves.push(await clickCenter(page, `${label} chest take`));
    moves.push(await clickCenter(page, `${label} chest put`));
    await page.keyboard.press("Escape");
    const closed = await waitState(page, "playing");
    if (!closed || closed.state !== "playing") fail(`${label}: Escape did not return to play (${closed && closed.state})`);
    again = await useOnce(page, `${label} chest again`);
    await page.keyboard.press("Escape");
    await waitState(page, "playing", 3000);
  }

  const travel = await runForward(page, label, { forceFailures: 60 });
  console.log(`  [${label}] running for autosaves…`);
  const beforeAutosave = await savedFiles(page);
  await new Promise((r) => setTimeout(r, 70_000));
  const autosaved = await savedFiles(page);
  console.log(
    `  [${label}] autosave: ${beforeAutosave.count} → ${autosaved.count} file(s), ` +
      `${((autosaved.bytes || 0) / 1024).toFixed(0)} KiB`,
  );

  const phases = await page.evaluate(() => window.__phases);
  console.log(`  [${label}] phases: ${phases.map((p) => `${p.at}ms ${p.text}`).join(" | ")}`);
  const longTasks = await page.evaluate(() => window.__longtasks);
  const frames = await page.evaluate(() => window.__frames);
  const inPlay = longTasks.filter((task) => task.start >= playAt);
  const atBoot = longTasks.filter((task) => task.start < playAt);
  const worst = (list) => list.reduce((top, task) => Math.max(top, task.ms), 0);
  console.log(`  [${label}] long tasks >50 ms: ${longTasks.length} (${atBoot.length} loading, ${inPlay.length} in play)`);
  console.log(`  [${label}] worst while loading: ${worst(atBoot)} ms; worst in play: ${worst(inPlay)} ms`);
  console.log(`  [${label}] worst frame gap: ${Math.round(frames.worst)} ms`);

  const cache = await assetCache(page);
  console.log(`  [${label}] asset cache: ${cache.keys.length} record(s): ${cache.keys.join(", ")}`);

  const atomics = [...first.consoleLines, ...first.errors].filter((line) => /Atomics\.wait/.test(line));
  if (atomics.length > 0) fail(`${label}: the page reported Atomics.wait: ${atomics[0]}`);
  if (first.errors.length > 0) fail(`${label}: page error: ${first.errors[0].split("\n")[0]}`);
  if (worst(inPlay) > 1000) fail(`${label}: a ${worst(inPlay)} ms block happened while playing`);
  if (!chest.opened) fail(`${label}: no container screen opened from the right-click`);
  if (chest.opened && chest.ms > 1000) fail(`${label}: the container took ${chest.ms} ms to appear`);
  for (const move of moves) {
    if (move.task > 1000) fail(`${label}: a container click held the frame for ${move.task} ms`);
  }
  if (!again.opened) fail(`${label}: the second click did not reopen the container`);
  if (again.opened && again.ms > 1000) fail(`${label}: the second container open took ${again.ms} ms`);
  if (!worldState || worldState.state !== "playing") fail(`${label}: the page never reached the game's playing state`);
  if (!(autosaved.count > beforeAutosave.count)) fail(`${label}: the engine's autosave wrote nothing`);
  if (travel.moved < 8) {
    fail(`${label}: holding W moved ${travel.moved.toFixed(1)} blocks, so the chunk-crossing phase never ran`);
  }
  if (travel.generated === 0 && travel.resident <= 0) fail(`${label}: holding W loaded no chunk, so nothing was measured`);
  if (travel.pending > 0) fail(`${label}: ${travel.pending} section upload(s) never found room after a load burst`);
  if (travel.detail < travel.sections) {
    fail(`${label}: ${travel.sections - travel.detail} section(s) placed but not drawn after a load burst`);
  }
  if (travel.forced > 0 && travel.deferred === 0) {
    fail(`${label}: the forced upload failures never reached the retry path`);
  }

  let reloadResult = null;
  if (reload) {
    const second = await openPage(context, `${BASE}?bust=${STAMP + 1}&world=${world}&quality=low`, `${label} reload`);
    const reloadBoot = await boot(second.page, `${label} reload`);
    await waitPlaying(second.page, `${label} reload`);
    const engineRequests = second.requests.filter((url) =>
      /horizon-client(?:-threaded)?(?:_bg\.wasm|\.js)?|horizon-vfs\.bin/.test(url),
    );
    const reloadCache = await assetCache(second.page);
    const reloadLog = await log(second.page);
    console.log(`  [${label} reload] boot: ${reloadBoot.ms} ms, ${second.requests.length} request(s) total`);
    console.log(`  [${label} reload] engine/pack requests: ${engineRequests.length}${engineRequests.length ? ` (${engineRequests.join(", ")})` : ""}`);
    console.log(`  [${label} reload] engine: ${reloadLog.split("\n").find((l) => l.includes("wasm engine ready")) || "(none)"}`);
    console.log(`  [${label} reload] pack:   ${reloadLog.split("\n").find((l) => l.includes("resource pack")) || "(none)"}`);
    console.log(`  [${label} reload] asset cache: ${reloadCache.keys.length} record(s)`);
    const reloadAtomics = [...second.consoleLines, ...second.errors].filter((line) => /Atomics\.wait/.test(line));
    if (engineRequests.length > 0) fail(`${label} reload: downloaded engine files: ${engineRequests[0]}`);
    if (reloadCache.keys.length === 0) fail(`${label} reload: no asset cache records`);
    if (reloadAtomics.length > 0) fail(`${label} reload: Atomics.wait: ${reloadAtomics[0]}`);
    reloadResult = { engineRequests: engineRequests.length, cacheKeys: reloadCache.keys, bootMs: reloadBoot.ms };
  }

  await context.close();
  return {
    label,
    isolation,
    engineLine,
    choiceLine,
    bootMs: bootResult.ms,
    worldState,
    saves,
    autosaved,
    travel,
    chest,
    again,
    moves,
    worstInPlay: worst(inPlay),
    worstLoading: worst(atBoot),
    cacheKeys: cache.keys,
    atomics: atomics.length,
    errors: first.errors.length,
    reload: reloadResult,
  };
}

const isolated = await runSession({ label: "isolated", stripIsolation: false, reload: true });
const stripped = await runSession({ label: "no-isolation", stripIsolation: true, reload: false });
await browser.close();

// The engine choice is the thing that has to follow the capability, not the
// build: the threaded module may only run where its shared memory exists.
if (isolated.isolation.crossOriginIsolated !== true) fail("isolated session is not cross-origin isolated");
if (!/threaded/.test(isolated.engineLine)) fail(`isolated session did not run the threaded engine (${isolated.engineLine})`);
if (stripped.isolation.crossOriginIsolated !== false) fail("the stripped session is still cross-origin isolated");
if (stripped.isolation.sharedArrayBuffer !== false) fail("the stripped session still has SharedArrayBuffer");
if (/threaded/.test(stripped.engineLine)) fail(`the stripped session ran the threaded engine (${stripped.engineLine})`);
if (!/single-thread/.test(stripped.choiceLine)) fail(`the stripped session did not say why it is single-threaded (${stripped.choiceLine})`);

const summary = (session) =>
  [
    `${session.label}:`,
    `crossOriginIsolated=${session.isolation.crossOriginIsolated}`,
    `SharedArrayBuffer=${session.isolation.sharedArrayBuffer}`,
    `engine="${session.engineLine.replace("wasm engine ready ", "")}"`,
    `state=${session.worldState && session.worldState.state}`,
    `saves=${session.saves.length}`,
    `chest=${session.chest.opened ? `${session.chest.ms}ms(tasks ${session.chest.task}ms)` : "failed"}`,
    `chest again=${session.again.opened ? `${session.again.ms}ms(tasks ${session.again.task}ms)` : "failed"}`,
    `chest pick=${session.moves.length > 0 ? `${session.moves.map((m) => m.task).join("/")}ms` : "failed"}`,
    `run: ${session.travel.generated} chunk(s), worst ${session.travel.task}ms`,
    `Atomics.wait=${session.atomics}`,
    `worst in play=${session.worstInPlay}ms`,
    `worst loading=${session.worstLoading}ms`,
    session.reload ? `reload engine/pack requests=${session.reload.engineRequests}` : "",
  ]
    .filter(Boolean)
    .join(" ");

console.log("\n=== summary ===");
console.log(summary(isolated));
console.log(summary(stripped));
console.log(`save timings isolated: ${isolated.saves.map((s) => `${s.ms}ms/${s.count}f`).join(", ")}`);
console.log(`save timings no-isolation: ${stripped.saves.map((s) => `${s.ms}ms/${s.count}f`).join(", ")}`);

if (problems.length > 0) {
  for (const problem of problems) console.error(`FAIL ${problem}`);
  process.exit(1);
}
console.log("BROWSER_OK");
