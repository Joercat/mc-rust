//! Process identifiers, for temp-file names and seeds.
//!
//! A browser has no process id: [`id`] answers 0 there, which is all the names
//! built from it need (they are made unique by a counter too).

/// `std::process::id()`, or 0 on `wasm32` (where the call panics).
#[cfg(not(target_arch = "wasm32"))]
pub fn id() -> u32 {
    std::process::id()
}

/// `std::process::id()`, or 0 on `wasm32` (where the call panics).
#[cfg(target_arch = "wasm32")]
pub fn id() -> u32 {
    0
}
