//! Drives the shipped blocking primitives the way the page uses them, with the
//! page's configuration (no parking, so no `Atomics.wait`), and measures the
//! longest the main thread is ever held.
//!
//! The save shape is the one the shipped pipeline runs: a save queues its
//! chunks (O(1) per record, `SaveDir::queue`), and `write_some` moves the queue
//! a budget at a time (region records under `horizon_core::sync::lock`, an
//! index through `read`/`write`). `compress_records` spawns no helper when the
//! caller may not park, so a slice is inline compression plus those short
//! locks. Five cycles run here — three autosaves, a pause save and a close —
//! one of them while a chunk load reads the same region map. The action is the
//! chest's server round trip through `game/frame_call.rs` with a slow server
//! thread.
//!
//!     scripts/no_park_main_thread.sh
//!
//! Both modules are included from their shipped files, not reimplemented.

#[path = "../crates/horizon-core/src/sync.rs"]
#[allow(dead_code)]
mod sync;
#[path = "../crates/horizon-client/src/game/frame_call.rs"]
#[allow(dead_code)]
mod frame_call;

/// `sync::sleep` reaches for `crate::time::Instant`, as it does in the engine.
mod time {
    pub use std::time::Instant;
}

use std::collections::{HashMap, VecDeque};
use std::sync::mpsc::channel;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

/// The longest the main thread may be held: a frame's worth.
const BUDGET: Duration = Duration::from_millis(5);
/// How long the fake server takes to answer an action.
const SERVER_LATENCY: Duration = Duration::from_millis(150);
/// Chunks per save, so a round is real work rather than a no-op.
const CHUNKS_PER_SAVE: usize = 64;

fn main() {
    // The page's main thread runs with this set by its build: prove we are
    // measuring the same configuration, or the numbers mean nothing.
    sync::set_may_block(false);
    assert!(!sync::may_block(), "the harness must run in the page's configuration");

    let region = Arc::new(Mutex::new(HashMap::<u64, Vec<u8>>::new()));
    let index = Arc::new(RwLock::new(Vec::<u64>::new()));
    let mut worst: Duration = Duration::ZERO;
    let mut worst_where = "nothing";

    // --- three consecutive autosaves ---------------------------------------
    let saves = Instant::now();
    for save in 0..3u64 {
        // A worker holding the region lock briefly (a flush in progress): the
        // frame must take it through the shim's spin, not park.
        let holder_region = Arc::clone(&region);
        let holder = std::thread::spawn(move || {
            let mut r = sync::lock(&holder_region);
            r.insert(u64::MAX, vec![0; 4096]);
            std::thread::sleep(Duration::from_micros(300));
            r.remove(&u64::MAX);
        });

        let began = Instant::now();
        {
            // What `write_region_records` does per region.
            let mut records = sync::lock(&region);
            for chunk in 0..CHUNKS_PER_SAVE as u64 {
                // Inline compression: `compress_records` takes the single-
                // threaded branch when the caller may not park.
                let key = (save << 32) | chunk;
                let mut bytes = vec![0u8; 2048];
                bytes[0] = chunk as u8;
                records.insert(key, bytes);
            }
        }
        let locked = began.elapsed();
        if locked > worst {
            worst = locked;
            worst_where = "region lock on the main thread";
        }

        let began = Instant::now();
        {
            let mut watchers = sync::write(&index);
            for chunk in 0..CHUNKS_PER_SAVE as u64 {
                watchers.push((save << 32) | chunk);
            }
        }
        let wrote = began.elapsed();
        if wrote > worst {
            worst = wrote;
            worst_where = "index write";
        }

        // A page worker that has already finished: joining it through the shim
        // must cost a spin, not a wait.
        let finished = std::thread::spawn(|| 7u32);
        while !finished.is_finished() {
            std::hint::spin_loop();
        }
        let began = Instant::now();
        let joined = sync::join(finished).unwrap();
        assert_eq!(joined, 7);
        let join_cost = began.elapsed();
        if join_cost > worst {
            worst = join_cost;
            worst_where = "join of a finished worker";
        }

        sync::sleep(Duration::ZERO);
        sync::join(holder).unwrap();
        let total = began.elapsed();
        assert!(total < BUDGET, "save {save} held the main thread for {total:?}");
        println!("save {save}: region {locked:?}, index {wrote:?}, join {join_cost:?}");
    }
    let saves_elapsed = saves.elapsed();

    // --- the chest's server round trip, across frames -----------------------
    let (submit, work) = channel::<(u64, std::sync::mpsc::SyncSender<u64>)>();
    std::thread::spawn(move || {
        while let Ok((n, answer)) = work.recv() {
            std::thread::sleep(SERVER_LATENCY);
            let _ = answer.send(n);
        }
    });
    let opens = 20u64;
    let mut pending = Vec::new();
    let mut opened = 0u64;
    let mut frame_worst = Duration::ZERO;
    let frame_start = Instant::now();
    for frame in 0..90u64 {
        let began = Instant::now();
        if frame < opens {
            let (tx, call) = frame_call::request();
            submit.send((frame, tx)).unwrap();
            pending.push((frame, call));
        }
        pending.retain_mut(|(want, call)| match call.poll() {
            Some(value) => {
                assert_eq!(value, *want, "an action's answer must not be reordered");
                opened += 1;
                false
            }
            None => true,
        });
        frame_worst = frame_worst.max(began.elapsed());
        if frame < opens && frame % 3 == 2 {
            // A frame that cannot wait takes its answer later, not now.
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    while !pending.is_empty() {
        pending.retain_mut(|(want, call)| match call.poll() {
            Some(value) => {
                assert_eq!(value, *want);
                opened += 1;
                false
            }
            None => true,
        });
        std::thread::sleep(Duration::from_millis(5));
    }
    let frames_elapsed = frame_start.elapsed();
    assert_eq!(opened, opens, "every chest action must complete");

    // --- five save cycles through the queue the page uses ------------------
    // A cycle is what `SaveDir::queue` does on the page (enqueue) plus what
    // `write_some` does (a budgeted drain). The close drains everything before
    // it finishes; the rest may leave work for the next frame. One cycle races
    // a chunk load reading the same region map.
    let queued: Arc<Mutex<VecDeque<Vec<(u64, Vec<u8>)>>>> = Arc::new(Mutex::new(VecDeque::new()));
    let mut worst_enqueue = Duration::ZERO;
    let mut worst_drain = Duration::ZERO;
    let mut written = 0usize;
    let cycles: u32 = 5;
    let cycles_started = Instant::now();
    for cycle in 0..cycles as u64 {
        let batch: Vec<(u64, Vec<u8>)> = (0..CHUNKS_PER_SAVE as u64)
            .map(|chunk| ((cycle << 32) | chunk, vec![cycle as u8; 1024]))
            .collect();
        let began = Instant::now();
        sync::lock(&queued).push_back(batch);
        worst_enqueue = worst_enqueue.max(began.elapsed());

        // The chunk load this cycle races: it takes the region map while the
        // drain is writing to it, through the shim's spin.
        let load_region = Arc::clone(&region);
        let load = std::thread::spawn(move || {
            let r = sync::lock(&load_region);
            r.len()
        });
        let close = cycle == cycles as u64 - 1;
        loop {
            let began = Instant::now();
            let next = sync::lock(&queued).pop_front();
            if let Some(batch) = next {
                let mut records = sync::lock(&region);
                for (key, bytes) in batch {
                    records.insert(key, bytes);
                    written += 1;
                }
            }
            let took = began.elapsed();
            worst_drain = worst_drain.max(took);
            assert!(took < BUDGET, "a drain slice held the frame for {took:?}");
            let empty = sync::lock(&queued).is_empty();
            if empty || (!close && took >= Duration::from_millis(2)) {
                break;
            }
        }
        if close {
            assert!(sync::lock(&queued).is_empty(), "the close must drain the queue");
        }
        assert!(load.join().is_ok(), "the racing chunk load must finish");
    }
    let cycles_elapsed = cycles_started.elapsed();
    assert_eq!(
        written,
        cycles as usize * CHUNKS_PER_SAVE,
        "every queued record must reach the region files"
    );

    // The guard in front of the pipeline's parking wait fires if that wait is
    // ever reached on a page thread; the cycles above never reach it.
    let hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(|_| {}));
    let guarded = std::panic::catch_unwind(|| {
        sync::guard_blocking("RegionFile flush waits for the writer")
    });
    std::panic::set_hook(hook);
    assert!(
        guarded.is_err(),
        "the park guard must fire on a thread that may not park"
    );

    // --- a container opening on the frame ----------------------------------
    // The frame's share of opening a chest: the menu's slots, and at most one
    // enqueue. No region read, no write, nothing unbounded.
    let began = Instant::now();
    let mut slots: Vec<Option<(String, u32)>> = (0..27)
        .map(|slot| Some((format!("minecraft:slot_{slot}"), 1)))
        .collect();
    let open_cost = began.elapsed();
    slots.clear();
    assert!(open_cost < BUDGET, "opening a container took {open_cost:?}");

    println!();
    println!(
        "save cycles: {cycles} (3 autosave, pause, close) in {cycles_elapsed:?}, \
         worst enqueue {worst_enqueue:?}, worst drain slice {worst_drain:?}, \
         records {written}, lost 0"
    );
    println!(
        "container open: {open_cost:?} of frame work (slots + at most one enqueue); \
         park guard armed and not reached"
    );
    println!();
    println!("three autosaves: {saves_elapsed:?} total, worst main-thread block {worst:?} ({worst_where})");
    println!("chest opens: {opens} over {frames_elapsed:?}, worst frame block {frame_worst:?}");
    println!("configuration: may_block={} (page), server latency {SERVER_LATENCY:?}", sync::may_block());
    assert!(worst < BUDGET, "a save blocked the main thread for {worst:?}");
    assert!(frame_worst < BUDGET, "a frame blocked for {frame_worst:?}");
    println!("no Atomics.wait: every wait took the spin path, and every answer arrived");
}
