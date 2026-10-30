# Horizon on the web

The real Horizon client (the Rust Minecraft client in this repository) built as
a WebAssembly module and served in the browser. Nothing is installed: the page
loads the engine (a 33 MB wasm module, ~11 MB gzipped), a 33 MB resource pack,
and the game runs the same code paths as the desktop build — world generation,
terrain meshing, entities, the HUD, the menus, and saving to browser storage.

```sh
python3 scripts/serve_web.py 5173     # then open http://localhost:5173/
```

Query parameters:

| Parameter       | Meaning                                                        |
| --------------- | -------------------------------------------------------------- |
| `?world=<name>` | Skip the title screen and open (creating it if needed) a world. |
| `creative=0`    | Start a new world in Survival (default is Creative).           |
| `persist=0`     | Do not mirror the game's `/game` folder into IndexedDB.        |

The bottom-right readout shows the state, FPS, chunks and meshed sections; the
browser console carries the engine's log.

## Offline only

The web build has no online features. Multiplayer, LAN, Realms and the profile
services need sockets and threads a browser page does not have, so:

- the title screen shows only **Singleplayer** and **Options**;
- `Game::connect_server` returns immediately with a warning instead of starting
  a network session (`crates/horizon-client/src/game/remote.rs`), and Open to
  LAN does nothing (`game/lan.rs`);
- Options has no **Online** page and the pause menu no server links.

Singleplayer worlds (an integrated server in the same page) work exactly as on
the desktop.

## How it is built

The pieces, in build order:

1. **Crates.** Every dependency is vendored (`scripts/fetch_crates.py`,
   `scripts/fetch_extra_crates.py`, `scripts/assemble_vendor.py`) and
   `.cargo/config.toml` points at the vendor directory. A few crates needed
   wasm work: `encoding_rs` 0.8.35, `pp-rs` 0.2.2 (the naga GLSL front end),
   `dlib`/`x11-dl`, and `gethostname`.
2. **Portable filesystem.** `crates/horizon-core/src/vfs.rs` has an in-memory
   filesystem used by every `std::fs`/`std::env` call on wasm32: the resource
   pack is mounted into `/resources`, the game's working directory is `/game`
   (restored from IndexedDB at boot and drained back every three seconds), and
   `saves/`, `options.txt` and the world files live there.
3. **Web entry point.** `horizon_web_start` (`crates/horizon-client/src/main.rs`
   → `game::run_web`) mounts the pack, loads the resources, opens the wgpu
   WebGPU device and enters the normal winit event loop
   (`EventLoopExtWebSys`).
4. **Renderer.** The terrain vertex widened its layer to `u32`
   (`crates/horizon-client-render/src/mesher.rs`, `gfx.rs`): WebGPU has no
   single-component `uint16` vertex format. Sprite-array and screen-effect
   pipeline overrides are now attached per pipeline, because WebGPU rejects a
   pipeline that supplies an override its entry point does not use.
5. **Frame loop.** On wasm the loop waits for the browser (`ControlFlow::Wait`)
   instead of polling: a redraw is a `requestAnimationFrame` callback, and
   polling starved it, so frames stopped after a few seconds.
6. **Resources.** `scripts/bake_anims_and_models.py`, `scripts/build_all_resources.py`,
   then `horizon-web-prepare` and `scripts/pack_vfs.py` pack the game data into
   `public/horizon-vfs.bin` (`HZVFS001`). The attribute catalog needs the
   per-entity `DefaultAttributes` suppliers; `scripts/gen_attribute_defaults.py`
   extracts them (from Pumpkin's generated entity table) and
   `scripts/fill_attribute_defaults.py` merges them into an existing archive.

`scripts/build_web.sh` runs the wasm build, `wasm-bindgen`
(`crates/horizon-wasm-bindgen`, a wrapper around `wasm-bindgen-cli-support`),
and copies the glue into `public/`.

## Known limitations

- The renderer needs **WebGPU** (Chrome/Edge 113+, Safari 26, Firefox with
  `dom.webgpu.enabled`). The WebGL2 fallback (`?backend=webgl`) only works
  when the browser has `WEBGL_draw_instanced_base_vertex_base_instance`;
  otherwise the page says so before starting instead of panicking mid-world.
- Software WebGPU (SwiftShader) renders a frame in ~300 ms; a real GPU adapter
  is far faster. The FPS readout reflects that.
- Worlds live in the browser's IndexedDB, not in files: clearing site data
  deletes them.
- The build is single-threaded (see *Threads* above): terrain streams in on the
  render thread, so walking into unloaded chunks costs frames.
- The autosave runs every 200 ticks (ten seconds) in the browser instead of
  vanilla's 6000, because a page can be closed or navigated away from between
  saves. Hiding the page also asks the game for an immediate save, and the page
  mirrors its files to IndexedDB every three seconds and when it unloads, so a
  reload lands on the position the player left.

## Threads (single-threaded today)

The browser build runs on one thread. The desktop client generates, lights and
meshes chunks on a pool of worker threads, loads its resources in parallel and
builds a world on its own thread; the web build does all of that on the thread
that also draws. The cost is not the steady frame (a still scene is fine) but
the edges:

- `Streaming::pump_inline` spends up to 24 ms per frame generating and meshing
  while a world loads and 10 ms otherwise (`horizon-client-render/src/streaming.rs`),
  out of a 16.7 ms frame;
- `load_resources` uses one thread (`game.rs`), so the first load reads
  thousands of textures and models serially;
- `WorldLoader` runs its prepare step inline (`game/world_loading.rs`);
- `par::par_map` maps serially (`horizon-client-render/src/par.rs`).

These are `#[cfg(target_arch = "wasm32")]` branches, and they are not there to
be conservative: `std::thread` does not work on `wasm32-unknown-unknown` at all.
Its platform layer is `library/std/src/sys/pal/wasm/mod.rs`, which is
`#[path = "../unsupported/common.rs"]` — every OS call is a stub that returns
`Unsupported`, which is where the "operation not supported on this platform"
messages come from. There is no `Thread` behind `std::thread::spawn` to enable.

Real threads need three things:

1. **A standard library built with atomics.** Rust ships `wasm32-unknown-unknown`
   without them, so the module has to be built with nightly
   `-Z build-std=std,panic_abort` and
   `RUSTFLAGS='-C target-feature=+atomics,+bulk-memory,+mutable-globals'`, plus
   `-C link-arg=--import-memory -C link-arg=--shared-memory` so every instance
   shares one `SharedArrayBuffer`. `wasm-bindgen` then rewrites the module for
   threads by itself (it detects the shared, imported memory).
2. **A worker pool**, because std will not spawn one: N Web Workers, each
   instantiating the same module with the same memory and entering a Rust entry
   point that runs the job loop. That is what the `wasm_thread` and
   `wasm-bindgen-rayon` crates do; the chunk streamer's existing `Shared` job
   queue (mutex + condvar + mailbox) would work unchanged on the other side,
   since all of that state is in the shared heap.
3. **No blocking waits on the main thread.** `Atomics.wait` is illegal there,
   so the render thread has to keep polling (`try_recv`, as the inline path
   already does) while only the workers block.

Cross-origin isolation is already in place (`serve_web.py` sends `COOP:
same-origin` and `COEP: require-corp`), which is what makes `SharedArrayBuffer`
available in the first place. A threaded build cannot run without it, so the
plan is to ship both modules and have `public/app.js` pick the threaded one
when `crossOriginIsolated && typeof SharedArrayBuffer !== "undefined"`, and the
single-threaded one otherwise.
