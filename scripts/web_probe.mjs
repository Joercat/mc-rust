// Headless smoke test for the Horizon web build.
//   node scripts/web_probe.mjs [url] [seconds] [query]
// Prints console messages, the in-page status timeline and a screenshot to
// /tmp/horizon-shot.png. Needs puppeteer-core plus a Chromium binary; both are
// pointed at by env vars when they live outside the defaults.
import puppeteer from "/tmp/probe/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js";

const url = process.argv[2] || "http://127.0.0.1:5173/";
const seconds = Number(process.argv[3] || 120);
const exe = process.env.HORIZON_CHROMIUM || "/tmp/chromium";
const libDir = process.env.HORIZON_CHROMIUM_LIB || "/tmp/al2023/lib:/tmp/swiftshader";
// SwiftShader's Vulkan ICD gives headless Chromium a WebGPU adapter.
const icd = process.env.HORIZON_VK_ICD || "/tmp/swiftshader/vk_swiftshader_icd.json";

const browser = await puppeteer.launch({
  executablePath: exe,
  headless: true,
  args: [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--enable-unsafe-webgpu",
    "--enable-features=Vulkan",
    "--use-angle=swiftshader",
    "--use-gl=angle",
    "--use-vulkan=swiftshader",
    "--enable-logging=stderr",
    "--window-size=960,600",
  ],
  defaultViewport: { width: 960, height: 600 },
  env: {
    ...process.env,
    LD_LIBRARY_PATH: `${libDir}:${process.env.LD_LIBRARY_PATH || ""}`,
    VK_ICD_FILENAMES: icd,
  },
});

const page = await browser.newPage();
const seen = [];
page.on("console", (msg) => {
  const text = `[${msg.type()}] ${msg.text()}`;
  seen.push(text);
  console.log(text);
});
page.on("pageerror", (err) => {
  const text = `[pageerror] ${err.message}`;
  seen.push(text);
  console.log(text);
});
page.on("requestfailed", (req) => console.log(`[requestfailed] ${req.url()} ${req.failure()?.errorText}`));

await page.goto(url, { waitUntil: "domcontentloaded", timeout: 120000 });

const deadline = Date.now() + seconds * 1000;
let last = "";
while (Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 2000));
  const state = await page.evaluate(() => {
    const boot = document.getElementById("boot");
    const err = document.getElementById("error");
    return {
      booting: boot ? !boot.classList.contains("hidden") : false,
      phase: document.getElementById("phase")?.textContent || "",
      hud: document.getElementById("hud")?.textContent || "",
      status: window.__horizon_status || "",
      error: err && err.style.display === "block" ? err.textContent.slice(0, 4000) : "",
    };
  });
  const line = `${state.phase} | ${state.hud} | ${state.error ? "ERROR" : ""}`;
  if (line !== last) {
    console.log("STATE", JSON.stringify(state).slice(0, 3000));
    last = line;
  }
  if (state.error) break;
}

await page.screenshot({ path: "/tmp/horizon-shot.png" });
const finalStatus = await page.evaluate(() => window.__horizon_status || "");
console.log("FINAL_STATUS", finalStatus);
console.log("screenshot: /tmp/horizon-shot.png");
console.log("console messages:", seen.length);
await browser.close();
