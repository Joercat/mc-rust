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
| `nocache=1`     | Ignore the stored engine and resource pack, and refresh them.   |

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

   The textures are most of what is left after the sounds. `--texture-mode
   lossless` re-packs every PNG with oxipng (`-o2 --strip safe`): the pixels
   are identical, and each atlas shrinks by whatever its original encoder left
   behind. `--texture-mode quantize` also reduces each image to a palette
   (pngquant at `--texture-quality 90` when it is installed, Pillow's octree
   otherwise), which is where soft, shaded art loses a little banding in trade
   for a much smaller atlas. A texture that did not get smaller is kept as it
   was, so nothing is degraded twice:

   ```sh
   python3 scripts/pack_vfs.py PATH/TO/.local/horizon-client/26.3 \
     PATH/TO/.horizon-cache public/horizon-vfs.bin \
     --audio-bitrate 64k --texture-mode lossless
   ```

   Neither pass edits the prepared install: `scripts/overlay_tree.py` links
   every file into the scratch tree the pack is built from, and a re-encoded
   file is written beside its target and renamed over it, which breaks the
   link instead of rewriting the install through it.
   `scripts/compress_textures.py` runs the texture pass on its own, like
   `scripts/compress_audio.py` does for the sounds.

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
- The engine's wasm module, its glue and the resource pack are kept in a second
  IndexedDB database (`horizon-web-assets`), keyed by the SHA-256 that
  `scripts/web_manifest.sh` records in `public/assets.json` for every published
  file. A second load fetches nothing at all for a file it already has — no GET,
  not even a HEAD — and a rebuild fetches the manifest plus only the files whose
  hash changed; a stored copy cannot answer for a different hash, because the
  hash is the key. The manifest names the build stamp the page's own code
  carries, so a record from another build is never consulted. `?nocache=1`
  skips the stored copy and refreshes it, and a browser that refuses the
  storage (a private window, no quota) says so and downloads as before.
  `scripts/asset_cache_check.mjs` drives the page's real cache
  (`public/asset-cache.js`) against a stubbed host and an emulated IndexedDB and
  prints the counts: one manifest and one request per asset on a first load,
  none on a second, and after a rebuild that changed one file, the manifest and
  that file only.
  `scripts/web_browser_check.mjs` does the same in a real browser: it loads the
  live URL in headless Chromium with `?world=`, waits for the game to report
  `playing`, forces save cycles, places and opens a chest with the game's own
  command and a right-click, and reloads to count what the second load fetches
  (nothing: the engine and the 400 MB pack come from IndexedDB). It needs
  Playwright and a Chromium, which are not project dependencies:

  ```
  npm i playwright && npx playwright install chromium      # in a scratch folder
  PLAYWRIGHT_PATH=$PWD/scratch/node_modules/playwright/index.js \
    node scripts/web_browser_check.mjs https://horizon-web-pna6.onrender.com/
  ```

  The status readout the script polls is refreshed once a second; the script
  speeds that one timer up so a container open is timed to tens of milliseconds.
- An embedded page can never run the threaded engine. `crossOriginIsolated`
  needs every ancestor of the document to be cross-origin isolated too, so a
  panel iframe whose host page sends no COOP/COEP gets `false` no matter what
  headers `serve_web.py` sends, and `app.js` loads the single-threaded module
  and logs `single-threaded engine: this page is embedded by one that is not
  isolated`. The threaded build needs a **top-level** page: locally
  `python3 scripts/serve_web.py 5173` (localhost is a secure context and the
  server sends the headers), or a host that sends them, which `render.yaml`
  configures for the Render service. The panel's embed is also drawn by the
  server's GPU, not the machine the panel is open on, so it is the wrong place
  to judge how the build performs on real hardware either way.
- A public deployment that answers an error page can be the host and not the
  build. On 2026-11-19 `https://horizon-web-pna6.onrender.com/` answered
  `503` with `x-render-routing: suspend-by-user` and Render's own 256-byte
  "This service has been suspended by its owner" body for `/` and for
  `/assets.json`; the engines and the pack in this tree are untouched by that.
  Resume the service from the Render dashboard (Blueprint: `render.yaml`,
  branch `threaded-wasm`) rather than rebuilding anything. A checkout without
  the pack can fetch it the same way the Render build does — the release asset
  `https://github.com/Joercat/mc-rust/releases/download/web-assets-2026-11-01/horizon-vfs.bin`
  (419557970 bytes) is the `HORIZON_VFS_URL` target.
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
- The page starts one worker per core minus one (`?threads=N` overrides it, up
  to 32) and every long job goes through that pool: chunk generation, meshing
  and lighting, the integrated server, the connection reader. The engine's
  worker count is the pool's size (`horizon_threads::pool_size()`), so more
  cores mean more chunks meshed per frame, as on a native build. The status
  readout reports it as `threads`.
- Startup reads of the pack (`par_map`, the model library and the HUD atlas)
  run on the pool only when there is enough of them: 4192 models measured
  43 ms on the frame's thread and 55 ms through the pool (the handoff and the
  workers' first touch of the code cost more than 4192 in-memory reads), so the
  pool is used from 8192 items up. The status readout reports the last load as
  `par_map` (items, threads, ms) and the library it built as `model_library`
  (files, models, fingerprint), which must be identical either way.
- The pool keeps a **reservation**, so no streamer can take the workers the
  rest of the page needs. `horizon_threads::set_reserved(reserved, essential)`
  is set as soon as the pool size is known (`game/world_loading.rs`):
  `essential` (four workers, or the pool's size on a small device) stays free
  for the jobs that must never wait — the integrated server's loop, its hub,
  the connection reader, LAN serving — and the integrated server's own
  streamer is given two more. A streamer dispatches through one of two calls:
  `spawn_reserved_detached` (the client's, which starts jobs only while
  `POOL - INFLIGHT > reserved`, so it can never eat the reservation) or
  `spawn_earmarked_detached` (the title screen's and the server's own
  streamer, which use the share already kept out of the client's reach and
  leave one spare for a blocking job that starts later). A claim is a
  compare-exchange on `INFLIGHT`, so two streamers cannot take the same last
  slot, and a streamer that gets no worker falls back to pumping jobs inline
  instead of failing. Without the two functions a page that gave its server
  workers starved its own reader, and the join never finished; with them the
  server's streamer starts its two workers (`Horizon: streaming: 2 worker(s)
  of 2 (earmarked true, pool 11)`) and the client's gets the rest.
- That is what fixes the integrated-server tick spike. Before the reservation,
  the server streamed inline (`stream_threads = 1`), so a whole atomic
  generation job ran inside a tick and the 10 ms pump budget could not
  preempt it: walking into unloaded chunks logged `slow tick 100.0 ms:
  streaming 1690.7, gen_job 1678.9`, `254.6 ms: gen_job 251.2`,
  `121.1 ms: gen_job 137.7` and `78.0 ms: gen_job 93.8`, which is the
  `73.9 / 50.0 ms` and `79.6 / 50.0 ms` the HUD showed while walking at 2-4
  fps. After it, the same kind of walk (fresh world, spawn pinned at
  4000,136,4000, flying ~10 blocks/s through ungenerated terrain for ~40 s)
  generated 22+ chunks with the server's tick at **2.4-2.8 ms average, 33 ms
  max and 2.8-4.1 ms p95**, and no `slow tick` line attributable to
  `gen_job`/`streaming` while moving. The status readout reports the window
  as `server_tick` (`avg`, `max`, `p95`, `budget: 50`), sampled every tick
  from a 200-sample ring with max and p95 recomputed every five ticks.
- A streamer never runs a generation job on the frame when the page has a
  pool. A streamer that finds no free worker when it is built — which is what
  happens while a world opens, with the server's tick, its hub and the
  client's own streamer already holding theirs — keeps the claim and starts
  its workers on a later update (`Streaming::claim_pending`, logged as
  `Horizon: streaming: N worker(s) claimed later`). Only a page without a
  pool streams inline. The load-time log says which it was:
  `streaming: 0 worker(s) of 2 (earmarked false, pool 11, deferred,
  Persistent<VanillaTerrain>)` followed by two `claimed later` lines, where it
  used to fall back to `gen_job` inside the tick.
- Every load-time burst has a per-tick budget and a carry-over queue, so no
  unit of a world opening is unbounded. The budgets are wall-clock, checked
  between items, and whatever a tick does not finish stays queued for the
  next one in the order it would have run:
  - **chunk bodies to the guest**: 6 ms per tick of encoding, the rest of the
    batch stays queued (`hub.rs`, `update_chunks`). This took the hub's share
    of the world-open tick from 82 ms to ~53 ms.
  - **light changes to the guest**: 2 ms per tick, at most 64 columns a tick,
    out of the change map (`hub.rs`, `broadcast_light_changes`). The map
    merges a later change into the same entry, so a column the budget does
    not reach is sent with its newest light on a following tick. Draining the
    whole map was 43 ms of the world-open tick.
  - **streaming apply** (`Streaming::update`): 4 ms a tick (8 ms while
    loading) of the finished protos, chunks and light, the rest in
    `deferred`; a whole view at once was 118 ms of one tick.
  - **scheduled and fluid block ticks**: 8 ms a tick (`tick_scheduled`).
    Ticks a column schedules when it generates are all due at once, which was
    up to 76 ms. The rest stay due — their deadlines are already in the past
    — and run, in the same order, on the next tick.
  - **post-processing**: 2 ms a tick, and per mark rather than per column
    (`postprocess_chunks`). One cave column can carry thousands of marks; the
    rest go back to the front of that column's mark queue. A column another
    thread is still meshing keeps its marks for a later tick rather than
    being copied to take them: `Arc::make_mut` copies the whole column, and a
    world that just opened is meshing its entire view, so 117 copies were
    14 ms of the tick.
  - **chunk tick registration**: 2 ms a tick (`sync_tick_chunks`). The state
    a column saves (block ticks, fluid ticks, block entities) is unpacked
    into the tick containers over the following ticks; `TickChunks::has_added`
    keeps what is left.
  - **natural spawning**: 4 ms a tick of `spawnForChunk` attempts
    (`spawn_natural`), the unattempted columns in `SpawnScratch::backlog` and
    tried first next tick. The spawn state still decides every attempt from
    the mobs actually in the level, so nothing is spawned twice.
  - **adding the mobs a pass made**: six a tick (`spawn_add_backlog`). A new
    mob's first tick is the expensive one, so a whole view's mobs arriving
    together made the next tick 88 ms.
  - **a generated column's creature packs**: 2 ms a tick
    (`tick_creatures`); a column keeps its `fresh` flag until its pack is
    rolled, and the pack is seeded from the column, so spreading them only
    changes when the animals appear.
  - **a tick's load work as a whole**: 25 ms (`tick_world`'s
    `TICK_LOAD_GUARD`). Once the tick has spent that long, the units that
    already carry over (`postprocess_chunks`, the loaded columns' entities)
    are left to the next tick instead of stacking another burst on a slow one.
  - **saved entities of loaded columns**: 2 ms a tick
    (`spawn_loaded_entities_tick`, `persist.rs`). A world that just opened has
    a whole view's entities to spawn at once (26.7 ms of one tick); the
    columns the budget does not reach stay in `world.loaded_entities`, in
    order, and are spawned on the following ticks. The save paths
    (`save_entities`, `unload_entities`) still spawn everything first.
  - **level events**: 2 ms a tick (`process_level_events`, `game.rs`). The
    effects a view's blocks report (a broken block's sound and drops, a
    dispenser's) arrive together when the view first becomes loaded and were
    28 ms of a tick; the event that does not fit and everything after it go
    back to the front of `level_events` for the next tick, in the same order.
  - **a guest's packets**: 4 ms a tick (`process_inbox`, `hub.rs`); the
    packets a tick does not reach stay in the inbox and are handled next tick
    in order. A client joining a fresh world acks chunk batches by the
    hundred, and handling the lot was 36 ms of the tick it landed in
    (`lan_level`).
  - **the client's light propagation**: 2 ms a tick, four block edits a call
    (`Streaming::relight_edits`), the rest in `Streaming::pending_relight`
    and applied on the following ticks in the same order. Batches are four
    edits, not 32: a cave column's flood fill made one 32-edit batch 51 ms of a
    tick (`stream_light` 51.1, `lt_block` 51.5), and a budget checked between
    batches cannot bound a batch that size. This is the
    `stream_light`/`lt_block` unit that was the last unbudgeted one at load:
    applying one batch of block edits at once was 78.7 ms of a tick
    (`light::relight`). `streaming.relight_pending` in `__horizon_status`
    shows what is left queued (0 once the view has settled).
- Budgeting the light does not change the light. Batches of 32 edits carried
  over in the same order reach the same fixed point as applying the whole
  batch at once: on a fixed world (`?world=lightref&persist=0&spawn=4000,136,4000`)
  `__horizon_status.light` — 1089 samples of `world.light(x,y,z)` on a grid
  (y 32..96 step 8, x/z ±40 step 8 around the player), rendered as one string
  plus a hash per y layer — is byte-identical with and without the budget:
  len 17786, 1089 samples, hash `40a67b7b`, and all nine layer hashes equal
  (`a81d428b 2312b1e8 5939f760 440a71a4 669435f8 4eb25c99 5eb8974e 1a488e76
  ba2dd8db`), measured on the build without the budget and on the build with
  it.
- The `slow tick` line reports ticks over 50 ms on the page (the nominal
  tick, `Options::spikes` for wasm), where it used to be 75 ms: at 75 a tick
  could miss its 20 ms budget by more than three times and never be logged.
- Resources a server builds at world load are built at world load, not by the
  first tick that needs them (`game/integrated.rs`, after
  `world loaded`): the advancement tree (38 ms when it was built by a join),
  the language pack's item names, the item rarity table (`items.json`, 26-40 ms
  as the held item's name asked for it first), the gameplay attributes, the
  entity type table the block entities resolve against
  (`entity-types.json`), the scoreboard, command storage, world border and
  stopwatch files, the boss bars, the raid save, and the block entities'
  ticking states. `tick_scoreboard` was 35.3 ms, `tick_block_entities` 13.8
  and `creatures_raids` 15.1 in the ticks that first ran them.
- A tick has no per-unit budget for the player's own join (`join_place`,
  `PlayerList.placeNewPlayer`: the login, recipe book, advancements and the
  player's chunk tickets, one atomic step in vanilla) or for a view's mobs'
  first ticks (`creatures_ticks`) — see what is left below.
- The warm-ups also cover `sky_darken`, which `spawn_phantoms` computed on
  its first tick (9.7 ms).
- The join is a queue, not a burst: `PlayerList.placeNewPlayer`'s ~200
  packets go out one step a turn (`Hub::join_step`, `JOIN_BUDGET` 4 ms a
  tick), in the order vanilla sends them (login, recipes, permission and
  command tree, recipe book, join message and teleport, player list, level
  info, chunk centre and effects). Per-stream order, the spawn position and
  the state once drained are the same as when the whole burst ran in one
  tick; the player list and the level's chunk tickets see the player placed
  a tick or two later instead of in the join's tick.
- A slow-tick line names the tick's own spans. The page runs the frame and
  the server tick on one thread, and the frame's spans used to be left in the
  list the tick line reports: a world open showed `streaming 242` inside a
  77 ms tick, which is impossible for one tick, and the number was the
  frame's streaming work. `tick_server_timed` now takes the spans before it
  starts, so a `slow tick` line lists only what the tick spent; `streaming`
  reappears at its true size (2.5 ms on the same build).
- The streaming pump the tick thread runs itself takes 8 ms while a world is
  loading, not 24: one job (a finish, a light, a mesh) is not interruptible,
  so a 24 ms budget let one tick start three and spend 60-80 ms in the pump.
- The three loops an open's spawn wave or sculk scan used to block on have
  the same due queue as the random ticks:
  - `tick_blocks`' entity-inside pass (the blocks under the players, the
    guests, the mobs, items, orbs and projectiles, plus the big dripleaf
    answer) is one queue walked for `INSIDE_BUDGET` 4 ms a tick; what it does
    not reach stays at the front for the next tick. It was 53.1 ms of a tick
    with 81 mobs.
  - A tick's host events run for `EVENTS_BUDGET` 6 ms; the event that does
    not fit and those after it carry over in order (`Game::lan_pending`). A
    join's burst was 12.9 ms (`lan_events`).
  - The `Commands` tree is built and encoded once per (permission level,
    dedicated) and copied to each guest that asks for it (`Hub`'s
    `commands_cache`, cleared when the mods' commands change) instead of
    being built per join: `join_perm` was 11.2 ms.
- The post-process marks are the one place a tick still runs an unbreakable
  unit, and it is not splittable: a mark is `update_from_neighbour_shapes`
  followed by one `set_block(pos, updated, 276)` on the level
  (`postprocess_chunks`, flags `UPDATE_INVISIBLE | UPDATE_KNOWN_SHAPE |
  UPDATE_SKIP_BLOCK_ENTITY_SIDEEFFECTS`). The phase itself is already a
  queue — columns are taken in order, marks run column then mark, a 2 ms
  `POST_BUDGET` stops the tick between marks, and the marks it does not reach
  go back to the front of their column's queue — so what a tick cannot bound
  is one mark's own `set_block`. Splitting *that* would mean applying a
  neighbour-shape result without its block write, or writing the block
  without the light update the write carries, and both change the order the
  neighbours' shapes and the light see: the order the `40a67b7b` fingerprint
  is there to pin. Measured on `2026-11-05.39` with the mark phase
  instrumented per mark (logged counts and per-mark times, sub-spans removed
  before `.40`): two fresh opens (`m1`, 101 mobs; `m2`, 109 mobs) reached
  `playing` with no mark at or above 3 ms and no `slow tick` line, and the
  one 9.3 ms `postprocess_mark_slow` ever seen (an open on `.36`, inside an
  81.1 ms tick) has not reappeared on `.38` or later — the worst tick
  measured on `.38` is 43.3 ms, under the 50 ms line even with a mark of that
  size inside it. The mark phase also only runs while the view is still
  loading (`!loaded_enough()`), so it cannot coincide with a settled world's
  ticks.
- The panel's own route into a world works end to end: on `2026-11-05.40` the
  preview tab, loaded at the dev server's `/` (the URL the panel iframes),
  ran the title screen and was driven with real mouse clicks to
  Singleplayer -> a world row -> Play Selected World, after which
  `__horizon_status` read `playing` with 117 columns in world `lightref`.
  The clicks have to be in CSS pixels: the canvas is 432x547 CSS at device
  pixel ratio 1.25 (a 540x684 drawing buffer), and the title screen's
  Singleplayer button sits at (216, 183), its first world row at (216, 52)
  and Play Selected World at (153, 513) — clicks 12 px lower miss the button
  and the screen never changes. `__horizon_status` has no `build` field in
  this build, so the session's build is read from the page it loaded:
  `app.js?v=2026-11-05.40` in that session's resource timing, and the same
  `const BUILD` inside it.
- Dumped on `2026-11-05.40` (panel route, world `lightref`, 117 columns,
  `state` `playing`): `__horizon_status` really has no `build` key. Its keys
  are `chunks error fps frames generated light meshed_sections mobs
  model_library par_map pause pending_entry pending_uploads player
  player_some renderer server_tick state streaming threads threads_idle
  title world_frames world_loader world_name`, and in that same playing
  session `const BUILD` inside `app.js` read `2026-11-05.40`, resource timing
  showed `app.js?v=2026-11-05.40`, and the served `/` carried the same stamp.
  So the build is read from the page a session loaded, not from the status
  object, and `.41` would have to add the field to change that.
- A `*.style.dev` preview answers 403 to curl or any request without the
  owner's sign-in even when the app is healthy, so a panel showing "This
  preview is private" is asking for an authenticated preview, not a broken
  server: re-request it instead of touching the build. The same path, `/` on
  the preview host, answers differently by caller and nothing else:
  unauthenticated (`curl`) 403, `text/plain`, body "This preview is private.
  Open it from the Browser panel in Freebuff, signed in as the project owner.
  A request from anywhere else, such as curl, gets this 403 even when the dev
  server is healthy.". That gate body appears nowhere in this tree (the only
  match is the note below), so it is the platform's text, not the app's.
  Authenticated (the signed-in browser session) 200,
  `text/html`, 2516 bytes, the app's own `<doctype html>` document with
  `horizon-canvas` and the build stamp. Both static ports answer 200 with the
  stamp, so the gate is the platform's, not the app's.
- Embedded, the preview host answers the gate no matter what the cookie
  jar holds. In the owner's cloud browser the context has
  `__Host-freebuff-cloud-preview` (`SameSite=None`, `Secure`), a top-level
  `GET /` returns 200 from our own `SimpleHTTP/0.6 Python/3.12.3` server
  (2521 bytes, no `X-Frame-Options`), and the same `GET /` in a cross-site
  iframe returns 403 from Cloudflare/edge (`x-freebuff-served-by: aws`,
  `X-Frame-Options: SAMEORIGIN`), so the frame renders "refused to connect"
  or the gate text. A request with no cookie at all also gets that 403. The
  app can only be shown in the panel through the ticket-bearing bootstrap
  (`/__freebuff_preview?ticket=...`, which `serve_web.py` maps to `/`), not
  through a plain `https://<preview-host>/` iframe URL.
- The title-screen click route is a real path into the game and it was never
  the input layer that was broken. The one trap is the world list: with no
  saved world the Select screen has no rows, `Play Selected World` stays
  disabled and clicking it does nothing — which looks exactly like dead
  input. Create a world once (`?world=<name>` saves one), then on a fresh `/`
  the route is Singleplayer, the first row, `Play Selected World`: on a
  540x597 drawing buffer at dpr 1.25 those are CSS (216, 166), (216, 50) and
  (153, 444) — the height, not the width, moves the buttons, so a viewport
  shorter than 547 shifts them all up. Driven that way on `2026-11-05.41`
  from a freshly restarted preview, `/` reached `playing` in world `lightref`
  with 117 columns and `frames` climbing 93 -> 564 -> 670 -> 1330, no
  `?world=` in the URL.
- The route was driven again on `2026-11-05.41` in the owner's Browser panel
  (cloud browser, 432x597.6 CSS at dpr 1.25, a **540x747** drawing buffer,
  GUI scale 1). On that buffer the buttons are `Main` Singleplayer at CSS
  (216, 195), the world rows at CSS (216, 57) and (216, 86) — buffer y = 49
  (LIST_TOP) + 4 + row*36 + 18, so a row is 28.8 CSS apart — and `Play
  Selected World` at CSS (153, 564). A profile whose `horizon-web` IndexedDB
  store is empty starts with zero saved worlds (the boot log says `restored 0
  saved file(s)`), so nothing on Select responds; `?world=<name>` once puts a
  save back (here `lightref`, spawn pinned to 4000,136,4000), and a fresh `/`
  then reaches `playing` in `lightref` at 4000.5/4000.5 with 117 chunks and
  `frames` climbing 923 -> 1499 -> 1859 -> 2339 -> 2459 -> 2579. The light
  fingerprint read there is the recorded one: 1089 samples, len 17786, FNV-1a
  32 of the whole string `40a67b7b`, and the nine per-layer FNV hashes
  `a81d428b 2312b1e8 5939f760 440a71a4 669435f8 4eb25c99 5eb8974e 1a488e76
  ba2dd8db` (the hash is FNV-1a 32 over the layer's `;`-joined samples).
  The world list is newest first: the stale `?world=lightref2&persist=0` tab
  from an earlier session saved `lightref2` on close, which put it above
  `lightref`, so the row clicked matters — check `world_name` afterwards.
- Leaving a world in that session did not come back: `Save and Quit to Title`
  logged `Saving and pausing game...` and then the save's records, after which
  the frame loop stopped (`frames` frozen, `title` still false) and the tab
  stopped answering CDP; the same happened to a navigation away from a
  running world, while navigation from the title screen was fine. The cause
  was not established (the cloud browser's WebGPU teardown is a candidate),
  and it does not affect the route above, which never leaves a world.
- A build published under an unchanged stamp can keep running the previous
  module in a browser that already loaded that stamp (`app.js?v=` is the
  same URL and the page's asset cache is keyed by the manifest hash), so a
  rebuild that must be seen gets a stamp bump: the diagnostic `ui` field only
  appeared in `__horizon_status` after `.40` became `.41`.
- After killing the supervisor and its gateway and static server (`ss` showed
  both ports closed) and starting `.local/preview_up.sh` again, `:3003` and
  `:3004` both answered 200 with `"build": "2026-11-05.40"`, and the light
  fingerprint on the re-served build was unchanged: 1089 samples, len 17786,
  `40a67b7b`, the nine layer hashes above, `relight_pending` 0 — measured on
  a fresh pinned world (`?world=lightref2&persist=0&spawn=4000,136,4000`,
  player at 4000.5/4000.5), which generates the same terrain as `lightref`.
  `2026-11-05.41` is that build with only the stamp bumped (same wasm sizes,
  33562658 / 33583067 bytes) and it reproduces the fingerprint exactly:
  1089 samples, len 17786, `40a67b7b`, the same nine layer hashes,
  `streaming.relight_pending` 0.
- `__horizon_status` reports `mobs`, the integrated server's live mob count,
  so a spawn wave is visible on a run that never logs a line.
- On `2026-11-05.38` (and its rebuild `2026-11-05.40`, the same code with the
  diagnostic sub-spans removed), six consecutive fresh-world opens (threaded preview,
  console cleared by a fresh load before each, quiet machine: one tab,
  nothing else running) logged **no `slow tick` line at all**, and every one
  of them caught a spawn wave — the peak `mobs` sampled each second while the
  open settled was `u1` 76, `u2` 90, `u3` 98, `u4` 102, `u5` 97, `u6` 100,
  all reaching `playing` with 117 columns. Those are the counts the wave
  reached on the opens that logged nothing; the condition that used to log
  (`r1` on `.36`: `56.1 ms (81 mobs)`, `tick_blocks` 53.1, and `60.4 ms
  (80 mobs)`, `creatures_ticks` 48.7) is now under the line on every one of
  them. Repeated on `2026-11-05.40`: `v1` 107 mobs, `v2` 111, `v3` 102,
  `v4` 91, `v6` 112, each at `playing` with 117 columns and no `slow tick`
  line (`v1` and `v2` with the console cleared before the load, so the whole
  open was covered).
  The runs before the cuts, for comparison: `q1` and `q2` on `.35` logged
  one line each at 77.6 and 77.7 ms; `r1` on `.36` logged three (81.1 ms
  with `tick_lan` 25.0, `tick_sculk_entities` 24.9, `lan_events` 12.9,
  `join_perm` 11.2, `postprocess_mark_slow` 9.3; 60.4 ms with
  `creatures_ticks` 48.7; 56.1 ms with the entity-inside scan 53.1).
  The interiors of `tick_creature_entities` and `tick_sculk_entities` were
  measured with temporary sub-spans on `.37` (deaths, snapshot, the mob
  loop, apply, projectiles, events, outbox; `sculk_prepare`, the warden
  tracker, darkness, step-on, entity events) and one clean open there was
  enough to show the phases no longer added up to a line; the sub-spans were
  removed before `.38` was built.
- A sustained flight is clean on `2026-11-05.38`: console cleared, a fresh
  world, 24 s of `KeyW` on the canvas from (4000.5, 39.3, 4000.5) to
  (4117.7, 39.3, 4182.7) — constant altitude, 8.3 blocks a second, the
  flight speed — with **no `slow tick` line**; the server tick over it
  measured 2.6 ms average, 13.7 ms p95 and 43.3 ms max, all under the 50 ms
  line. On `2026-11-05.40` the same flight at altitude (double-tap jump, a
  7 s climb, then `W` for 20 s) went from (4000.5, 90.45, 4006.7) to
  (4118.6, 90.45, 4192.8): 220 blocks at 11.0 a second with the altitude
  constant (y range 0.00), **no `slow tick` line**, server tick 3.6 ms
  average, 21.4 ms p95 and 45.4 ms max — still under the 50 ms line.
- Adaptive render scaling aims at the display, not at a fixed 30 fps: the page
  measures the interval rAF delivers (the platform exposes no refresh rate)
  and sets the budget to it plus a quarter (`?fps=N` overrides). The quarter
  matters: a frame that keeps up lands exactly on the interval, and the
  controller only climbs back when a frame is well inside its budget, so a
  budget equal to the interval would let the scale step down but never up.
- The autosave runs every 600 ticks (thirty seconds) in the browser instead of
  vanilla's 6000, because a page can be closed or navigated away from between
  saves. Saves encode their modified chunks in the frame, so the interval is
  the trade: shorter loses less work when a page closes, longer hitches less
  often. Hiding the page also asks the game for an immediate save, and the page
  mirrors its files to IndexedDB every eight seconds and when it unloads, so a
  reload lands on the position the player left.
- **Debug Mode** is a world type on the create screen's World tab, in the
  cycle after Single Biome: every block state once, in a square grid at y 70
  over a barrier platform at y 69 (`debuggen`, vanilla's `DebugLevelSource`).
  Alt+click on World Type and a right click on it (what ChromeOS sends for
  Alt+click) toggle it too. Its chunks come from the same worker-thread
  pipeline as any world, so the frame is not where they are built.
- The autosave is staged: each frame does at most 2 ms of it (modified
  chunks, then every resident chunk's entities, then the metadata), so the
  thirty-second save no longer encodes the whole world inside one frame. The
  pause save takes the same path on the server thread, a little per tick.
  Its writes are queued, not performed in the frame that queued them: the
  frame adds a batch to the save queue and `write_some` drains a millisecond of
  it per frame, so no autosave, pause save or closing save encodes a region
  file inside one frame.
- `public/app.js` measures the device (`hardwareConcurrency`, `deviceMemory`)
  and puts `?quality=low` on a page with four threads and a few gigabytes. The
  engine applies the Performance preset on a first run, saves it once, and caps
  the render distance for that device (8 chunks at `low`, 12 at `balanced`), so
  the slider cannot offer 128 chunks the device cannot draw.

## Threads

The threaded web build runs chunk generation, lighting and meshing on the
page's Web Workers, so a frame does not pay for them: `Streaming`'s pool
(`crates/horizon-client-render/src/streaming.rs`) hands its jobs to the page's
workers instead of running them on the render thread, and the render thread
only collects what finished. On the desktop the same pool is a set of OS
threads; nothing else about streaming changes.

The page loads the threaded module when it is built and the page is
cross-origin isolated (`crossOriginIsolated`, which `serve_web.py` provides
with its COOP/COEP headers). Otherwise it loads the single-threaded module,
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

Waiting on a thread is the same hazard: `JoinHandle::join`, `thread::sleep`
and `Condvar::wait` all end in `memory.atomic.wait32`. The frame joins through
`horizon_core::sync::join` (spin until `is_finished`, then collect) and waits
through `sync::sleep`; a `Condvar` wait is taken only when `may_block()` is
true, and where a worker's answer is needed on the frame (`Integrated::call`,
the occlusion worker) the frame polls a channel with a deadline instead. The
per-site list is [the main-thread audit](main-thread-audit.md).

A server round trip is bounded the same way. `Integrated::call` used to spin on
the page's main thread until the server thread answered; it now waits at most
2 ms there and hands the answer back on a later frame through
`game/frame_call.rs` (`request`, then `Pending::poll`). `scripts/frame_call_bound.sh`
measures that: 17 µs worst frame block against a 150 ms round trip, with the
request still answered and nothing lost or reordered.

Nothing on the frame's path waits on a lock a worker can hold. The log's
recent-line buffer (`horizon-client-render/src/logging.rs`), the region writer's
pending and outstanding bookkeeping with its `flush` (`save.rs`), the region
cache and the far-terrain maps all take their locks through the shim, and
`flush` polls the outstanding count where blocking is not allowed instead of
waiting on the condition variable. The filesystem's own lock is held only for a
lookup: `OpenOptions::open` copies (or unpacks) a file's bytes after dropping
it, so opening a sound or a texture no longer queues behind a save's writes.

The mirror to IndexedDB is sliced for the same reason.
`drain_persisted_changes(budget)` takes each changed file out of the dirty set
under the lock, copies it with the lock released, and stops at the budget;
`horizon_web_drain_changes(budget)` hands that over as `[[path, bytes, mtime]…],
[deleted…], more`, and the page loops with a `setTimeout` between slices. An
autosave that dirtied hundreds of region files therefore no longer copies all
of them in one main-thread call.

A lock that still catches the frame throws `RuntimeError: Atomics.wait cannot
be called in this context` out of the frame that took it rather than trapping
the module, so `public/app.js` reports it and keeps drawing (the world is kept)
instead of covering the page with the crash message.

Still on the frame in the web build: resource loading (`par::par_map` maps
serially, so the first load is still one thread), `WorldLoader`'s prepare step,
far terrain (`lod.rs`) and the occlusion search. They no longer park the frame,
but they still spend its time; the streamer is where walking into unloaded
chunks used to.

## Adaptive resolution: sustained slowness only

`adapt_render_scale` lowers the world's resolution scale while frames run long
and raises it again when they have room. It is deliberately slow to move and slow
to be fooled:

- A window is 12 frames. The controller compares the smoothed frame time with the
  budget (a harness-set one, else 30 fps) and leaves the configured scale only
  after **three windows in a row** over budget, so the single slow window a chunk
  arrival makes — or frame times that merely bounce around the budget — never
  blur a frame. Inside the band between 0.85x and 1.15x of the budget it does
  nothing at all.
- Once it is below the configured scale, **every** further window that is still
  over budget steps it down again, so slowness that lasts is answered with fewer
  pixels until the frames fit or `MIN_RENDER_SCALE` (0.25) is reached. It holds
  where it lands: a step is no longer judged on the next window and reverted.
- When frames have room, it climbs one step (5%) per window back to the scale the
  settings asked for (`resolutionScale`, 100% by default), and holds there.

The scaled world is copied to the window with a **nearest** sampler, so a reduced
internal resolution looks crisp rather than smoothed into mush; at native scale
the copy is 1:1 and unchanged. `__horizon_status` reports `scale`, `pixels`,
`screen_pixels`, the frame time the controller judged (`frame_ms`), its
`budget_ms`, whether that is `over_budget`, and `settled` — meaning the last
window asked for no change, i.e. the scale is holding (inside the band, or
against a floor or the ceiling); it is not a claim about the machine.

`horizon_web_pixel_cost(ms)` adds that many milliseconds of frame time at full
resolution, scaled by the square of the render scale, so a machine with no GPU
can be shown a frame that really does get cheaper at a smaller size.
