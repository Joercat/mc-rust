// The threaded engine's worker pool.
//
// `std::thread::spawn` panics on wasm32: the standard library cannot create a
// Web Worker. So the page creates the workers itself, one per worker thread,
// and each instantiates the same wasm module over the page's one shared
// memory. `horizon_web_thread_worker` inside each counts it into the pool and
// then runs the engine's job loop (see public/thread-worker.js).
//
// Shared memory needs cross-origin isolation: serve the page with
// `Cross-Origin-Opener-Policy: same-origin` and
// `Cross-Origin-Embedder-Policy: require-corp` (scripts/serve_web.py does).
export async function startThreadPool({ module, memory, glueUrl, count }) {
  const script = new URL("./thread-worker.js", import.meta.url);
  const workers = [];
  // One at a time: each worker allocates its stack and thread-locals in the
  // shared memory as it starts, which is quick but not instant.
  for (let index = 0; index < count; index += 1) {
    const worker = new Worker(script, {
      type: "module",
      name: `horizon-worker-${index}`,
    });
    const ready = new Promise((resolve, reject) => {
      worker.addEventListener("message", (event) => {
        const data = event.data || {};
        if (data.type === "ready") resolve();
        else if (data.type === "error") reject(new Error(data.message));
      });
      worker.addEventListener("error", (event) => {
        reject(new Error(event.message || "worker failed to start"));
      });
    });
    worker.postMessage({ type: "init", module, memory, glueUrl });
    await ready;
    workers.push(worker);
  }
  return workers;
}
