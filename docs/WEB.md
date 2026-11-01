# Horizon on the web

The real Horizon client (the Rust Minecraft client in this repository) built as
a WebAssembly module and served in the browser. Nothing is installed: the page
loads the engine (a 33 MB wasm module, ~11 MB gzipped), a 33 MB resource pack,
and the game runs the same code paths as the desktop build — world generation,
terrain meshing, entities, the HUD, the menus, and saving to browser storage.

```sh
python3 scripts/serve_web.py 5173     # then open http://localhost:5173/
```

Cross-origin isolation — what lets the page load the threaded engine — needs a
secure context, and only `localhost` is one over plain HTTP. Opened from
another device as `http://<host-ip>:5173`, the page is single-threaded however
correct its headers are, and terrain then streams in on the render thread. Add
`--tls` to serve HTTPS with a self-signed certificate instead:

```sh
python3 scripts/serve_web.py 5173 --tls   # https://<host-ip>:5173/, accept the warning once
```

The certificate is made on first use (it needs `openssl`) in `.web-cert/`, for
`localhost`, the machine's name and its addresses; pass `--cert`/`--key` for
your own.

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
   extracts them   (from Pumpkin's generated entity table) and
   `scripts/fill_attribute_defaults.py` merges them into an existing archive.

   An install that is already prepared — the launcher's folder, or one made
   with `horizon-prepare --prepare --server-jar …` — needs none of that: its
   resource folder (`.local/horizon-client/<version>`, the one holding
   `horizon-resources.json`) already has the binary tables, so the pack is one
   command:

   ```sh
   python3 scripts/pack_vfs.py PATH/TO/.local/horizon-client/26.3 \
     PATH/TO/.horizon-cache public/horizon-vfs.bin
   ```

   (An empty directory works for the cache argument; it is packed as
   `/game/.horizon-cache` and rebuilt on the first world load.)

   The sound library is most of the pack. `--audio-bitrate 64k` re-encodes the
   Ogg Vorbis effects at 64 kbit/s (96 kbit/s for files over 30 seconds) into a
   scratch folder before packing, which roughly halves the archive; the
   prepared install keeps its full-quality audio. Files already at or below the
   target are copied, so nothing is encoded twice:

   ```sh
   python3 scripts/pack_vfs.py PATH/TO/.local/horizon-client/26.3 \
     PATH/TO/.horizon-cache public/horizon-vfs.bin --audio-bitrate 64k
   ```

   `scripts/compress_audio.py` runs the same pass on its own, writing the
   re-encoded tree elsewhere (`--jobs` sets the encoders), if a pack is built
   some other way.

The page checks for the engine it picked (`public/horizon-client.js` and
`public/horizon-client_bg.wasm`, or the threaded pair) and
`public/horizon-vfs.bin` before it starts and, when they are absent (they are
build outputs and gitignored, so a fresh checkout has none), says which ones are
missing and how to build them instead of hanging on the loading screen.

`scripts/build_web.sh` runs the wasm build, `wasm-bindgen`
(`crates/horizon-wasm-bindgen`, a wrapper around `wasm-bindgen-cli-support`),
and copies the glue into `public/`. It builds the single-threaded module first
and then, with a nightly toolchain, the threaded one (see *Threads*).
`-Z build-std` also compiles `std`, whose own dependencies (`object`,
`addr2line`, `compiler_builtins`, `dlmalloc`, `wasi*`, …) are pinned by
rust-src's lockfile rather than `Cargo.lock`. `scripts/fetch_std_crates.py`
vendors exactly those versions into the same directory, and
`scripts/build_web.sh` runs it before the threaded build; without them that
step fails with `no matching package named object found` and the page falls
back to the single-threaded module. It also repairs crates that
`assemble_vendor.py` copied from GitHub checkouts: their `.cargo-checksum.json`
is `{"files":{}}`, with no `package` checksum, which the project's build
tolerates but the `std` build does not (`error: checksum for `cfg-if v1.0.4`
could not be calculated, but a checksum is listed in the existing lock file`).

A threaded build that fails is reported as a failure: `scripts/build_web.sh`
still publishes the single-threaded module, but exits non-zero, so a page that
quietly fell back is not mistaken for one running on workers. Without a nightly
toolchain at all the threaded module is skipped with a warning and the exit
status stays 0.

## Building it from a blank machine

Everything below assumes no toolchain at all. On Windows do it inside WSL2
(Ubuntu): the build and the scripts are bash and Python.

```sh
# 1. System packages. macOS: brew install python3 git xz.
sudo apt update && sudo apt install -y build-essential curl git python3 xz-utils

# 2. Rust: stable builds the fallback module, a pinned nightly + rust-src the
#    threaded one. The nightly is pinned to the one this project builds with:
#    2026-10-31 breaks in the compiler itself (`error[E0391]: cycle detected
#    when computing when `ItemStack` needs async drop`), and `build_web.sh`
#    defaults to the pin. `HORIZON_NIGHTLY` overrides it.
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env"
rustup toolchain install stable
rustup target add wasm32-unknown-unknown
rustup toolchain install nightly-2026-10-30 -c rust-src -t wasm32-unknown-unknown

# 3. Vendor every crate (~800 MB in /tmp/horizon-vendor, the directory
#    .cargo/config.toml points at; change both if you want another one).
python3 scripts/fetch_crates.py
python3 scripts/fetch_extra_crates.py
python3 scripts/assemble_vendor.py

# 4. Build both modules into public/ (~15 min the first time).
scripts/build_web.sh "$HOME/.cargo"
```

Step 4 also vendors `std`'s own crates through `scripts/fetch_std_crates.py`.
The resource pack cannot be built from nothing: it comes from a prepared
install (step 5 of *How it is built*). On Linux one can be made from the
release archive in this repository's root:

```sh
mkdir -p ~/horizon-native && cd ~/horizon-native
tar -xJf /path/to/mc-rust/horizon-complete-linux-x86_64.tar.xz
cd horizon-game/data_payload-game
# Needs Minecraft Java Edition 26.3 played once, a Java 25 runtime, and that
# version's server jar: Minecraft's own data archive, imported (never run).
./horizon-prepare --prepare --server-jar ~/Downloads/server.jar
# Writes .local/horizon-client/26.3 beside research-lock.json.
```

Then pack that folder and serve:

```sh
cd /path/to/mc-rust
python3 scripts/pack_vfs.py \
  ~/horizon-native/horizon-game/data_payload-game/.local/horizon-client/26.3 \
  ~/horizon-native/horizon-game/data_payload-game/.horizon-cache public/horizon-vfs.bin
python3 scripts/serve_web.py 5173     # http://localhost:5173/
```

If you do not know where the prepared files are — a launcher install keeps
them in its own folder — the manifest finds them:

```sh
python3 scripts/pack_vfs.py \
  "$(dirname "$(find ~ -name horizon-resources.json 2>/dev/null | head -1)")" \
  /tmp/empty-cache public/horizon-vfs.bin
```

A correct pack reports thousands of files and hundreds of megabytes; a folder
without the manifest is refused rather than packed empty. With no recorded game
files at all, the page stops with the same message the desktop client gives.

## Known limitations

- The renderer prefers **WebGPU** (Chrome/Edge 113+, Safari 26, Firefox with
  `dom.webgpu.enabled`), and `app.js` selects WebGL2 itself (it rewrites the
  URL to `?backend=webgl`) when the browser has no adapter — ChromeOS devices
  without a Vulkan driver, for instance. Nothing in the renderer needs
  `WEBGL_draw_instanced_base_vertex_base_instance`: sections bind their arena
  and origin slices instead of using a base vertex or first instance, and the
  quad passes draw index ranges.
- Pointer lock is granted only from a live user gesture, and world generation
  outlasts one. A world therefore opens with the cursor free, and the first
  click inside it takes the pointer; walking away with Escape releases it again.
- Software WebGPU (SwiftShader) renders a frame in ~300 ms; a real GPU adapter
  is far faster. The FPS readout reflects that.
- Worlds live in the browser's IndexedDB, not in files: clearing site data
  deletes them.
- The threaded module needs a nightly toolchain with `rust-src`
  (`-Z build-std`) and a cross-origin isolated page. The toolchain is pinned
  to `nightly-2026-10-30` because `-Z build-std` and these flags are tied to
  one; later nightlies fail inside the compiler on this crate. The threaded
  build's target directory is keyed by that nightly
  (`target/threads/<hash of rustc --version>`), because build-std artifacts from
  two nightlies in one directory fail with `error[E0152]: duplicate lang item in
  crate `core``, and a keyed path cannot mix them. Without either, the page
  builds and runs the single-threaded module, and terrain streams in on the
  render thread: walking into unloaded chunks costs frames.
- The autosave runs every 600 ticks (thirty seconds) in the browser instead of
  vanilla's 6000, because a page can be closed or navigated away from between
  saves. Saves encode their modified chunks in the frame, so the interval is
  the trade: shorter loses less work when a page closes, longer hitches less
  often. Hiding the page also asks the game for an immediate save, and the page
  mirrors its files to IndexedDB every three seconds and when it unloads, so a
  reload lands on the position the player left.

## Threads

The threaded web build runs chunk generation, lighting and meshing on the
page's Web Workers, so a frame does not pay for them: `Streaming`'s pool
(`crates/horizon-client-render/src/streaming.rs`) hands its jobs to the page's
workers instead of running them on the render thread, and the render thread
only collects what finished. On the desktop the same pool is a set of OS
threads; nothing else about streaming changes.

The page loads the threaded module when it is built and the page is
cross-origin isolated (`crossOriginIsolated`: `serve_web.py` provides it with
its COOP/COEP headers, and the page must be a secure context — `localhost` or
HTTPS, as `--tls` serves). Otherwise it loads the single-threaded module,
which behaves as before: jobs run on the frame, up to 24 ms of each frame while
a world loads and 10 ms otherwise.

Real threads need three things, and the build and page provide all three:

1. **A standard library built with atomics.** `std::thread::spawn` panics on
   `wasm32-unknown-unknown` with or without them — its `Thread::new` returns
   `Unsupported` because the module cannot create a Web Worker — but the
   synchronisation primitives become real: `Mutex`, `Condvar`, `Once` and
   `thread::sleep` wait on `memory.atomic.wait32`. So the module is built with
   nightly `-Z build-std=std,panic_abort` and
   `RUSTFLAGS='-C target-feature=+atomics,+bulk-memory,+mutable-globals'`.
2. **A worker pool, because std cannot create one.** `public/threads.js` starts
   N module workers; each imports the glue, calls
   `initSync({ module, memory })` with the page's compiled module and its
   memory, and enters `horizon_web_thread_worker`. That counts the worker into
   `horizon_threads`' pool and runs `worker_main`, which takes closures from one
   shared queue forever (`crates/horizon-threads/src/lib.rs`).
3. **Shared memory.** The module imports its memory (`--import-memory`,
   `--shared-memory`, `--max-memory` and the `__tls_*` exports wasm-bindgen's
   thread transform needs), wasm-bindgen gives each worker instance its own
   stack and thread-locals in it, and every static — the job queue, the world,
   the VFS — is shared. That is why the streamer's existing `Shared` queue
   (mutex, condvar, mailbox) works unchanged on the other side.

The page's main thread must never park: `memory.atomic.wait32` traps there, so
a contended `Mutex::lock` would abort the page. Locks that the frame shares
with workers — the streamer's queue and `RwLock`, and the in-memory
filesystem's global lock, which workers take when they load and save chunks —
go through `horizon_core::sync` (`lock`, `read`, `write`), which spins on the
page's main thread and parks only on workers; `horizon_threads` re-exports
them. The mailbox polls (`try_recv`) rather than waits. Workers may block
freely.

Still on the frame in the web build: resource loading (`par::par_map` maps
serially, so the first load is still one thread), `WorldLoader`'s prepare step,
far terrain (`lod.rs`) and the occlusion search. They are the next candidates;
the streamer is where walking into unloaded chunks spent the frame.
