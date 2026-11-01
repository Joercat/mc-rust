// One of the threaded engine's page workers (see threads.js).
//
// It instantiates the page's wasm module against the same shared memory as the
// main thread, then enters the engine's job loop. The loop never returns, so
// from then on this worker is a thread in the engine's pool: terrain
// generation, lighting and meshing run here while the frame keeps drawing.
let started = false;

self.addEventListener("message", async (event) => {
  const data = event.data;
  if (started || !data || data.type !== "init") return;
  started = true;
  try {
    const glue = await import(data.glueUrl);
    // `initSync({ module, memory })`: the module the page compiled and the
    // memory it created, not a fresh one. `initSync` runs wasm-bindgen's
    // thread start for this instance: it allocates this thread's stack and
    // thread-locals in the shared memory.
    glue.initSync({ module: data.module, memory: data.memory });
    self.postMessage({ type: "ready" });
    glue.horizon_web_thread_worker();
  } catch (error) {
    self.postMessage({
      type: "error",
      message: error && error.stack ? error.stack : String(error),
    });
  }
});
