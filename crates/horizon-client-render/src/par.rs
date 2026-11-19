//! Loading many small files at once: startup reads tens of thousands of
//! textures and models with a content pack, and one at a time it waits on
//! each open and read in turn. On a native build that is one thread per core;
//! in a threaded page it is the page's Web Worker pool, the same one the
//! chunk streamer and the integrated server run on.
use std::sync::atomic::{AtomicUsize, Ordering};

/// `f` over `items` on the machine's threads, results in `items`' order.
/// Threads take the next item as they finish one, so a run of slow items
/// doesn't hold back the others.
/// What the last `par_map` did, for the status readout: a page's console is not
/// where a worker's log line shows up, so the numbers are read from there.
static LAST_CALLS: AtomicUsize = AtomicUsize::new(0);
static LAST_ITEMS: AtomicUsize = AtomicUsize::new(0);
static LAST_THREADS: AtomicUsize = AtomicUsize::new(0);
static LAST_MICROS: AtomicUsize = AtomicUsize::new(0);

/// `(calls, items, threads, ms)` of the last `par_map`: how many times one ran,
/// how much it read, how many threads did it, and what it cost.
pub fn last_report() -> (usize, usize, usize, f64) {
    (
        LAST_CALLS.load(Ordering::Relaxed),
        LAST_ITEMS.load(Ordering::Relaxed),
        LAST_THREADS.load(Ordering::Relaxed),
        LAST_MICROS.load(Ordering::Relaxed) as f64 / 1000.0,
    )
}

pub fn par_map<T: Sync, R: Send + 'static>(items: &[T], f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    let started = horizon_core::time::Instant::now();
    let (out, threads) = run(items, &f);
    LAST_CALLS.fetch_add(1, Ordering::Relaxed);
    LAST_ITEMS.store(items.len(), Ordering::Relaxed);
    LAST_THREADS.store(threads, Ordering::Relaxed);
    LAST_MICROS.store(started.elapsed().as_micros() as usize, Ordering::Relaxed);
    // Startup only: the two callers read tens of thousands of pack files, and
    // this is the line that says how many threads did it and what it cost.
    if items.len() >= 256 {
        log::info!(
            "Horizon: par_map {} items on {} thread(s) in {} ms",
            items.len(),
            threads,
            started.elapsed().as_millis()
        );
    }
    out
}

#[cfg(not(target_arch = "wasm32"))]
fn run<T: Sync, R: Send + 'static, F: Fn(&T) -> R + Sync>(
    items: &[T],
    f: &F,
) -> (Vec<R>, usize) {
    let threads = std::thread::available_parallelism()
        .map_or(1, |n| n.get())
        .min(items.len())
        .max(1);
    if threads <= 1 {
        return (items.iter().map(f).collect(), 1);
    }
    let next = AtomicUsize::new(0);
    let mut out: Vec<(usize, R)> = std::thread::scope(|scope| {
        let workers: Vec<_> = (0..threads)
            .map(|_| {
                scope.spawn(|| {
                    let mut done = Vec::new();
                    loop {
                        let i = next.fetch_add(1, Ordering::Relaxed);
                        let Some(item) = items.get(i) else {
                            return done;
                        };
                        done.push((i, f(item)));
                    }
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|w| w.join().unwrap_or_else(|e| std::panic::resume_unwind(e)))
            .collect()
    });
    out.sort_unstable_by_key(|(i, _)| *i);
    (out.into_iter().map(|(_, r)| r).collect(), threads)
}

/// In a page the machine's threads are the page's Web Workers, which the
/// renderer cannot reach itself (their jobs are `'static`; a scope is not), so
/// the pool does the borrowing and the waiting. `None` there means no pool is
/// free and the items are read here, in the frame's own thread.
/// Below this many items the pool is slower than this thread: measured on the
/// runtime pack, 4192 models cost 43 ms here and 55 ms through the pool, so the
/// handoff and the workers' first touch of the code cost more than the reads.
/// At the ~11 µs an item this measures, the pool starts to pay near 8192.
#[cfg(target_arch = "wasm32")]
const POOL_MIN_ITEMS: usize = 8192;

#[cfg(target_arch = "wasm32")]
fn run<T: Sync, R: Send + 'static, F: Fn(&T) -> R + Sync>(
    items: &[T],
    f: &F,
) -> (Vec<R>, usize) {
    if items.len() >= POOL_MIN_ITEMS {
        let threads = horizon_threads::idle_workers().min(items.len());
        if let Some(out) = horizon_threads::par_map(items, f) {
            return (out, threads.max(1));
        }
    }
    (items.iter().map(f).collect(), 1)
}

#[cfg(test)]
mod tests {
    use super::par_map;

    #[test]
    fn results_keep_the_items_order() {
        let items: Vec<u32> = (0..1000).collect();
        assert_eq!(
            par_map(&items, |x| x * 2),
            items.iter().map(|x| x * 2).collect::<Vec<_>>()
        );
        assert!(par_map(&[] as &[u32], |x| *x).is_empty());
    }

    #[test]
    fn a_shared_closure_and_its_captures_are_usable() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let factor = 3;
        let calls = AtomicUsize::new(0);
        let items: Vec<u32> = (0..997).collect();
        let out = par_map(&items, |x| {
            calls.fetch_add(1, Ordering::Relaxed);
            x * factor
        });
        assert_eq!(out.len(), items.len());
        assert_eq!(calls.load(Ordering::Relaxed), items.len());
        assert!(out.iter().zip(&items).all(|(a, b)| *a == b * factor));
    }
}
