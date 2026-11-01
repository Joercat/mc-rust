# Main-thread blocking audit

The page's main thread cannot park: `memory.atomic.wait32` throws
`RuntimeError: Atomics.wait cannot be called in this context` there. So no code
reachable from a `horizon_web_*` export, or from the eframe frame update that
runs between them, may block.

`scripts/audit_main_thread.py` is the audit. It scans every crate compiled into
the threaded wasm build (`cargo tree --target wasm32-unknown-unknown -p
horizon-client`), finds every blocking primitive, and requires an explicit
verdict for each site, exiting non-zero if any site is unclassified:

```sh
scripts/audit_main_thread.py            # classify; 0 means every site is labelled
scripts/audit_main_thread.py --groups   # the same, grouped by file::function
scripts/audit_main_thread.py --rwlock   # RwLocks taken without the shim
```

Primitives it counts: `.lock()`, `.wait(`/`.wait_timeout(` (condvar),
`.recv(`/`.recv_timeout(` (channel), `thread::park`/`park_timeout`,
`thread::sleep`, `block_on`, `.join()`. `std::sync::RwLock` has its own check:
every `RwLock`-typed field or static is looked up by name and any bare
`.read()`/`.write()` on it is reported. `.read()`/`.write()` are not in the main
sweep because the client also uses those names for item-stack accessors, which
do not block.

Verdicts: `frame-nonparking` (reachable, cannot park by construction),
`frame-single-owner` (reachable, but only that thread takes the primitive),
`worker` (a named thread that may block freely), `unreachable` (compiled in, no
page entry point), `not-wasm` (cfg'd out of the module), `not-a-wait` (named
like a wait, is not one).

## Where it stands

On the shipped tree the script labels all 280 sites it finds in the 20 crates:
67 `frame-nonparking`, 43 `frame-single-owner`, 45 `worker`, 112 `not-wasm`,
6 `unreachable`, 7 `not-a-wait`, and 0 unclassified. `--rwlock` reports 0 sites
taken without the shim.

## What changed to get there

`horizon_core::sync` now covers every primitive the frame can reach:

- `lock`/`read`/`write` accept a `Mutex`/`RwLock` or an `Arc` around one
  (`MutexLike`, `RwLockLike`), so a call site does not have to know which it
  holds, and spin with `try_lock` where `may_block()` is false.
- `join` waits for a thread's `is_finished` before joining, and `sleep` spins out
  the duration, both only where the caller may not park.
- Every `.lock()`/`.read()`/`.write()` and every `thread::sleep` in the 20 wasm
  crates now goes through those, which is why the lock and sleep classes have no
  unlabelled sites left. A page worker marks itself with `set_may_block(true)`
  in `horizon_threads::worker_main`, so it still blocks efficiently rather than
  spinning: the shim changes behaviour on the main thread only.
- `.join()` call sites the frame reaches go through `sync::join`; `thread::scope`
  handles (`ScopedJoinHandle`) keep their own `join`, since they are not
  `JoinHandle`s.
- Channel waits on the frame poll: `Integrated::call` (`executeBlocking`) uses
  `try_recv` with a 10 s deadline, the occlusion collector only takes the
  blocking `recv` when `may_block()`, and the screenshot readback uses
  `try_recv` because `poll(wait_indefinitely)` has already run the callback.
- `Condvar::wait` is taken only inside a `may_block()` branch (the save
  `flush`, the ring search), and `Streaming::wait`/the streamer mailbox poll.

## What the labels rest on

`frame-nonparking` sites are checked by the script for the shim, a
`try_`/`is_finished` poll or a `may_block()` guard on the line.
`frame-single-owner` and `worker` rest on reading the code: the file-level table
in the script names the owning thread and, for `worker`, a `thread:` label. The
`pollster::block_on` sites at renderer and pipeline creation are labelled
`frame-nonparking` because the shipped page runs them at start-up without
trapping; that is an observation about the product, not a proof from the source.

## Not observed

There is no browser here. No autosave was run, no chest was opened, and the live
Render service was not checked. What is proven is the list above, that the
module compiles and links, that it instantiates with shared memory, and that the
published bytes are the committed bytes. Whether the page now survives a third
autosave is for the browser to say.
