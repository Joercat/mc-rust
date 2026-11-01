//! Asking another thread for an answer without stopping the frame.
//!
//! The page's main thread may not wait: blocking it for a server round trip is
//! the difference between a hitch and a game that stops drawing. So a request
//! is issued now (`request`) and its answer is taken on a later frame
//! (`Pending::poll`), which keeps rendering and input running in between.
//!
//! `Pending` is deliberately small and free of `Game`: it is the whole of the
//! deferral, so `tests/frame_call_bound.rs` can drive it against a slow worker
//! and measure the time the frame spends in it.

use std::sync::mpsc::{sync_channel, Receiver, SyncSender};

/// A request whose answer has not arrived yet.
pub struct Pending<T> {
    rx: Receiver<T>,
    /// Set once the answer was taken, or the worker let go of the sender.
    done: bool,
}

/// A request: the sender goes to the thread that will answer it.
pub fn request<T: Send + 'static>() -> (SyncSender<T>, Pending<T>) {
    let (tx, rx) = sync_channel(1);
    (tx, Pending { rx, done: false })
}

impl<T> Pending<T> {
    /// The answer, if the other thread has given it. Never waits.
    pub fn poll(&mut self) -> Option<T> {
        if self.done {
            return None;
        }
        match self.rx.try_recv() {
            Ok(value) => {
                self.done = true;
                Some(value)
            }
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                self.done = true;
                None
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => None,
        }
    }

    /// Whether the answer was taken already, or never will be.
    pub fn settled(&self) -> bool {
        self.done
    }
}
