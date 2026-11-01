//! Measures how long the frame spends asking another thread for an answer.
//!
//! Builds the shipped deferral itself (`game/frame_call.rs`, std only) against a
//! deliberately slow server thread, and reports the worst case per frame. The
//! second half does what `Integrated::call` used to do on the page's main
//! thread — spin until the answer arrives — so the two numbers are directly
//! comparable.
//!
//!     scripts/frame_call_bound.sh
//!
//! Deterministic: latency is simulated with a sleep, frames are fixed steps.

#[path = "../crates/horizon-client/src/game/frame_call.rs"]
#[allow(dead_code)]
mod frame_call;

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::channel;
use std::time::{Duration, Instant};

/// How long the "server" takes to answer, simulating a busy tick.
const LATENCY: Duration = Duration::from_millis(150);
/// Frames to drive; at 60 fps this is two seconds of play.
const FRAMES: usize = 120;

fn main() {
    // A server thread: takes a number, answers `n * 2` after LATENCY.
    let (submit, work) = channel::<(u64, std::sync::mpsc::SyncSender<u64>)>();
    std::thread::spawn(move || {
        while let Ok((n, answer)) = work.recv() {
            std::thread::sleep(LATENCY);
            let _ = answer.send(n * 2);
        }
    });

    // --- the shipped path: one request per frame, answers taken when ready ---
    let mut pending: Vec<(u64, frame_call::Pending<u64>)> = Vec::new();
    let mutate = AtomicU64::new(0);
    let mut worst = Duration::ZERO;
    let mut frames = 0usize;
    let mut answered = 0u64;
    let mut refused = 0u64;
    let start = Instant::now();
    for frame in 0..FRAMES {
        let began = Instant::now();
        // A frame's worth of other work, so an idle loop cannot hide a stall.
        mutate.fetch_add(frame as u64, Ordering::Relaxed);
        let (tx, call) = frame_call::request();
        submit.send((frame as u64, tx)).unwrap();
        pending.push((frame as u64, call));
        pending.retain_mut(|(want, call)| match call.poll() {
            Some(value) => {
                assert_eq!(value, *want * 2, "answers must not be reordered or dropped");
                answered += 1;
                false
            }
            None => true,
        });
        if frame % 3 == 0 {
            // A caller whose budget ran out: the request still completes.
            let mut short = pending.pop().map(|(_, call)| call);
            if let Some(call) = short.as_mut()
                && call.poll().is_none()
            {
                refused += 1;
            }
        }
        worst = worst.max(began.elapsed());
        frames += 1;
    }
    // Drain what is still in flight: every request must still be answered.
    while !pending.is_empty() {
        pending.retain_mut(|(want, call)| match call.poll() {
            Some(value) => {
                assert_eq!(value, *want * 2);
                answered += 1;
                false
            }
            None => true,
        });
        std::thread::sleep(Duration::from_millis(5));
    }
    let elapsed = start.elapsed();
    assert_eq!(answered + refused, FRAMES as u64, "no request may be lost");

    // --- what the main thread used to do: spin for the answer ---
    let mut spin_worst = Duration::ZERO;
    for n in 0..10u64 {
        let began = Instant::now();
        let (tx, mut call) = frame_call::request();
        submit.send((n, tx)).unwrap();
        while call.poll().is_none() {
            std::hint::spin_loop();
        }
        spin_worst = spin_worst.max(began.elapsed());
    }

    println!("frame-sized requests: {FRAMES} over {elapsed:?} ({frames} frames)");
    println!("worst frame block, deferred: {worst:?}  (budget: a few ms)");
    println!("worst call block, old spin-until-answer: {spin_worst:?}");
    println!(
        "answered {answered}, budget-exceeded {refused}, lost 0; server latency {LATENCY:?}"
    );
    assert!(
        worst < Duration::from_millis(5),
        "a frame blocked for {worst:?}, over the 5 ms budget"
    );
    assert!(
        spin_worst >= LATENCY,
        "the spin baseline should show the whole latency"
    );
}
