# Main-thread blocking audit

The page's main thread cannot park: `memory.atomic.wait32` throws
`RuntimeError: Atomics.wait cannot be called in this context` there. Every
blocking primitive reachable from a `horizon_web_*` export (or from the eframe
frame update that runs between them) must therefore spin, poll with a deadline,
or run on a worker.

`horizon_core::sync` is the gate: `may_block()` is false on the page's main
thread and true everywhere else, `lock`/`read`/`write` spin with `try_lock`
where blocking is not allowed, and `join`/`sleep` do the same for a thread's
exit and a duration.

Sweep used for this audit (six crates the client compiles for wasm, tests and
examples excluded):

```sh
rg -n -g 'crates/horizon-client*/**/*.rs' -g 'crates/horizon-world/**/*.rs' \
      -g 'crates/horizon-core/**/*.rs' -g '!**/tests/**' -g '!**/examples/**' \
      -e '\.lock\(\)' -e '\.read\(\)' -e '\.write\(\)' -e 'recv_timeout\(' \
      -e '\.recv\(\)' -e 'thread::park' -e 'block_on' -e 'thread::sleep' \
      -e '\.join\(\)' -e '\.wait\('
```

## Fixed in this pass

Every row was a site the frame could reach; each is now spin-based, polled, or
routed through the shim.

| Crate / file | Site | Before | Now |
| --- | --- | --- | --- |
| `horizon-client/src/game.rs` | `save_for_exit` wait loop | `thread::sleep(1 ms)` | `sync::sleep` |
| `horizon-client/src/game.rs` | world-rest poll loop | `thread::sleep(10 ms)` | `sync::sleep` |
| `horizon-client/src/game/integrated.rs` | `Integrated::call` | `recv_timeout(10 s)` | `try_recv` poll, 10 s deadline |
| `horizon-client/src/game/integrated.rs` | `halt` | `JoinHandle::join` | `sync::join` |
| `horizon-client/src/game/integrated.rs` | `flags.failure` (read, 2 writes) | `Mutex` | `sync::lock` |
| `horizon-client/src/game/advancements.rs` | pending tree | `JoinHandle::join` | `sync::join` |
| `horizon-client/src/game/startup_menu.rs` | resource loader | `JoinHandle::join` | `sync::join` |
| `horizon-client/src/save.rs` | `compress_records` | scoped workers + join | no workers when `!may_block()`, chunks compressed inline |
| `horizon-client-render/src/gfx/occlusion.rs` | `collect(wait)` | `Receiver::recv` | `recv` only when `may_block()`, else `try_recv` |
| `horizon-client-render/src/streaming.rs` | `Mailbox::send`/`take`, drop check, test | `Mutex` (3) | `sync::lock` |
| `horizon-client-render/src/streaming.rs` | `shared.context` read | `RwLock` | `sync::read` |
| `horizon-client-render/src/streaming.rs` | `Drop` | `JoinHandle::join` | `sync::join` |
| `horizon-client-render/src/lod.rs` | `shared.jobs`, `worker.jobs`, `shared.observed`, `worker.observed` (21 sites) | `Mutex` | `sync::lock` |
| `horizon-client-render/src/lod.rs` | `Drop`, worker reap | `JoinHandle::join` (2) | `sync::join` |
| `horizon-world/src/worldgen/generator_structures.rs` | `RingWork::help` | `Condvar::wait` | `wait` only when `may_block()`, else poll `left` |
| `horizon-world/src/level/data/cache.rs` | `SOURCES` (3) | `Mutex` | `sync::lock` |
| `horizon-client-core/src/lang.rs` | `CURRENT` (2) | `RwLock` | `sync::read`/`sync::write` |
| `horizon-client/src/game/end.rs` | `locator` (3) | `Mutex` | `sync::lock` |
| `horizon-client/src/game/debug_keys.rs` | session counters | `Mutex` | `sync::lock` |
| `horizon-client/src/game/chat.rs` | `RESOURCE_IDS` | `Mutex` | `sync::lock` |
| `horizon-client/src/creative.rs` | `shown` (2) | `Mutex` | `sync::lock` |
| `horizon-client/src/structure_blocks.rs` | `LOOT` (3) | `Mutex` | `sync::lock` |
| `horizon-client/src/motion/fluids.rs` | fluid shape cache | `Mutex` | `sync::lock` |
| `horizon-client/src/item_render.rs` | `translucent` cache | `Mutex` | `sync::lock` |
| `horizon-client/src/item_render/gui.rs` | `gui_drawn` (2) | `Mutex` | `sync::lock` |
| `horizon-client/src/game/lan.rs` | `SYNC` (2) | `Mutex` | `sync::lock` |
| `horizon-client/src/game/world_loading.rs` | `PRELOAD` (2) | `Mutex` | `sync::lock` |
| `horizon-client/src/content_identity.rs` | `IDENTITIES` | `Mutex` | `sync::lock` |
| `horizon-client/src/vanillagen/lod.rs` | builder `Drop` | `JoinHandle::join` | `sync::join` |

## Already non-parking (checked, left as they are)

| Site | Why it cannot park the frame |
| --- | --- |
| `streaming.rs` `Mailbox::recv_timeout` | already polls with `try_lock`/`try_recv` under `wasm32` + atomics |
| `streaming.rs` `Streaming::wait` | already polls with a bounded deadline |
| `streaming.rs:627` `shared.ready.wait(queue)` | jobs-thread loop; loops only on workers |
| `lod.rs:692` `shared.wake.wait(jobs)` | jobs-thread loop |
| `save.rs` `flush` | polls `writes_finished()` when `!may_block()` (30 s cap) |
| `save.rs:2339` `outstanding.done.wait` | behind the `may_block()` branch above |
| `hud.rs:493` decode pool | `#[cfg(not(target_arch = "wasm32"))]`; wasm decodes serially |
| `par.rs` `par_map` | `threads = 1` on wasm, returns before any scope |
| `world_loading.rs` `ready`/`finish` | threaded path is `#[cfg(not(wasm32))]`; wasm is inline |
| `generator.rs` / `generator_blocks.rs` joins | `#[cfg(not(wasm32))]`; wasm inline |
| `horizon-client-input/src/window.rs` sleeps | `#[cfg(not(wasm32))]` |
| `game/commands/pregen.rs` `streaming.wait` | bounded poll (above) |
| `mobs/*::block_on_legacy` | pathfinding shim, not a thread wait |

## Worker-only (they may park)

`net.rs` reader/receiver threads, `lan/conn.rs` host and client threads,
`lan/chunks.rs` `pool.ready.wait`, `lan/discovery.rs`, `game/dedicated.rs`
(dedicated server role), `heartbeat.rs` `park_timeout`, `benchmark.rs`,
`control.rs`, `game/command_harness.rs`, `game/net_harness.rs`, `packs/cache.rs`
worker, `packs` test helpers, `gfx.rs` screenshot `recv` (the readback already
`poll`s indefinitely and never runs in the frame loop), `title/world_management.rs`
child reaper (desktop `std::process` path), and the `#[test]` sleeps in
`lod.rs`, `spans.rs`, `sprite_cache.rs`, `title/tests.rs`.

## Not converted, and not proven single-owner

The rest of the sweep is `Mutex`es that only the frame takes — render caches,
HUD state, upload queues, `game.rs` world state. A lock only parks when another
thread holds it, so those are safe *as long as* no worker reaches them. That is
true by construction for the ones in `horizon-client-render/src/gfx/` (upload
and atlas rebuilds run on the frame) but I did not trace every remaining site to
a single owner; they are the gap this audit does not close.

## Not observed

There is no browser in this environment: no save was run, no chest was opened,
and the live Render deploy was not inspected. What is observable here is that
the module compiles, instantiates, exposes its entry points and shares its
memory; whether the page survives a third autosave is for the browser to say.
