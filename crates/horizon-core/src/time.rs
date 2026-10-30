//! Cross-platform time primitives (`std::time` on native, `web_time` on `wasm32`).

#[cfg(not(target_arch = "wasm32"))]
pub use std::time::{
    Duration, Instant, SystemTime, SystemTimeError, TryFromFloatSecsError, UNIX_EPOCH,
};

#[cfg(target_arch = "wasm32")]
pub use web_time::{
    Duration, Instant, SystemTime, SystemTimeError, TryFromFloatSecsError, UNIX_EPOCH,
};

#[cfg(not(target_arch = "wasm32"))]
#[inline]
pub fn to_std_system_time(t: SystemTime) -> std::time::SystemTime {
    t
}

#[cfg(target_arch = "wasm32")]
#[inline]
pub fn to_std_system_time(t: SystemTime) -> std::time::SystemTime {
    let dur = t.duration_since(UNIX_EPOCH).unwrap_or_default();
    std::time::UNIX_EPOCH + dur
}
