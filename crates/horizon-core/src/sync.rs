//! Locks the browser's main thread may take.
//!
//! A threaded wasm build shares one memory between the page's main thread and
//! its Web Workers. `std::sync`'s locking then parks a contended thread on
//! `memory.atomic.wait32`, and that instruction traps on the browser's main
//! thread: the page would die. So state that both sides share (the in-memory
//! filesystem, the streamer's job queue) takes its lock through [`lock`],
//! [`read`] or [`write`]: on the page's main thread they spin, and everywhere
//! else — the desktop, a page worker — they park as usual. The holders only
//! ever hold these locks for a short section, so the spin is short too.
//!
//! A page worker marks itself with [`set_may_block`] as it starts
//! (`horizon_threads::worker_main`).
use std::sync::{Mutex, MutexGuard, RwLock, RwLockReadGuard, RwLockWriteGuard};

#[cfg(all(target_arch = "wasm32", target_feature = "atomics"))]
mod imp {
    use std::cell::Cell;

    thread_local! {
        /// Whether this wasm instance may park in a lock: a page worker, yes;
        /// the page's main thread, no.
        static MAY_BLOCK: Cell<bool> = const { Cell::new(false) };
    }

    pub fn may_block() -> bool {
        MAY_BLOCK.with(Cell::get)
    }

    pub fn set_may_block(may: bool) {
        MAY_BLOCK.with(|w| w.set(may));
    }
}

#[cfg(not(all(target_arch = "wasm32", target_feature = "atomics")))]
mod imp {
    pub fn may_block() -> bool {
        true
    }

    pub fn set_may_block(_: bool) {}
}

/// Whether the calling thread may park in a lock (always, except on the
/// browser's main thread in the threaded build).
pub fn may_block() -> bool {
    imp::may_block()
}

/// See [`may_block`]. Called by a page worker as it starts.
pub fn set_may_block(may: bool) {
    imp::set_may_block(may);
}

/// `Mutex::lock`, spinning where the caller may not park.
pub fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    if imp::may_block() {
        return m.lock().unwrap_or_else(|e| e.into_inner());
    }
    loop {
        if let Ok(guard) = m.try_lock() {
            return guard;
        }
        std::hint::spin_loop();
    }
}

/// `RwLock::read`, spinning where the caller may not park.
pub fn read<T>(m: &RwLock<T>) -> RwLockReadGuard<'_, T> {
    if imp::may_block() {
        return m.read().unwrap_or_else(|e| e.into_inner());
    }
    loop {
        if let Ok(guard) = m.try_read() {
            return guard;
        }
        std::hint::spin_loop();
    }
}

/// `RwLock::write`, spinning where the caller may not park.
pub fn write<T>(m: &RwLock<T>) -> RwLockWriteGuard<'_, T> {
    if imp::may_block() {
        return m.write().unwrap_or_else(|e| e.into_inner());
    }
    loop {
        if let Ok(guard) = m.try_write() {
            return guard;
        }
        std::hint::spin_loop();
    }
}

/// Join `handle` without parking a thread that may not: a page's main thread
/// spins until the thread has finished, by which point `join` returns at once.
pub fn join<T>(handle: std::thread::JoinHandle<T>) -> std::thread::Result<T> {
    if !may_block() {
        while !handle.is_finished() {
            std::hint::spin_loop();
        }
    }
    handle.join()
}

/// Wait for `duration` without parking a thread that may not.
pub fn sleep(duration: std::time::Duration) {
    if may_block() {
        std::thread::sleep(duration);
        return;
    }
    let until = crate::time::Instant::now() + duration;
    while crate::time::Instant::now() < until {
        std::hint::spin_loop();
    }
}
