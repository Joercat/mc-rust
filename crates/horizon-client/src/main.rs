//! Horizon: Rust client for Minecraft Java Edition 26.3 gameplay (native & WebAssembly).
#![forbid(unsafe_code)]
mod game;

/// The mod API level this game serves, kept in the program for the launcher
/// to read without running it (`horizon_mods::api_level::MARK`). `main`
/// reads it, so the linker keeps it.
static MOD_API_LEVEL: [u8; 27] = horizon_mods::api_level::MARK;

#[cfg(not(target_arch = "wasm32"))]
fn main() {
    use horizon_client::{crash, logging};
    std::hint::black_box(&MOD_API_LEVEL);
    horizon_client::telemetry::mark_start();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let game_args = args.iter().take_while(|a| *a != "--dedicated");
    let help = game_args.clone().any(|a| a == "--help" || a == "-h");
    let (home, side, thread) = if args.iter().any(|a| a == "--dedicated") {
        (std::path::PathBuf::from("."), "server", "Server thread")
    } else {
        (logging::game_home(), "client", "Render thread")
    };
    if !help {
        logging::init(&home, thread);
        log::info!("Horizon {}", crash::version());
        crash::install(home.clone(), side);
    }
    if let Err(error) = game::run() {
        log::error!("Horizon: {error}");
        if !help {
            crash::report_error(&home, side, &*error);
        }
        std::process::exit(1);
    }
    logging::flush();
}

#[cfg(target_arch = "wasm32")]
fn main() {
    std::hint::black_box(&MOD_API_LEVEL);
    web_entry::install_hooks();
}

#[cfg(target_arch = "wasm32")]
mod web_entry {
    use std::sync::Once;
    use wasm_bindgen::prelude::*;

    struct ConsoleLogger;

    impl log::Log for ConsoleLogger {
        fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
            metadata.level() <= log::Level::Info
        }

        fn log(&self, record: &log::Record<'_>) {
            if !self.enabled(record.metadata()) {
                return;
            }
            let msg = JsValue::from_str(&format!("[{}] {}", record.level(), record.args()));
            match record.level() {
                log::Level::Error => web_sys::console::error_1(&msg),
                log::Level::Warn => web_sys::console::warn_1(&msg),
                _ => web_sys::console::log_1(&msg),
            }
        }

        fn flush(&self) {}
    }

    static LOGGER: ConsoleLogger = ConsoleLogger;
    static INIT: Once = Once::new();

    pub fn install_hooks() {
        INIT.call_once(|| {
            std::panic::set_hook(Box::new(|info| {
                // A JavaScript Error's stack names the wasm frames of the
                // panic: Rust's own backtrace does not work on wasm32.
                let msg = format!("Horizon WASM panic: {info}");
                let error = js_sys::Error::new(&msg);
                let stack = js_sys::Reflect::get(&error, &JsValue::from_str("stack"))
                    .ok()
                    .and_then(|s| s.as_string())
                    .unwrap_or_else(|| msg.clone());
                web_sys::console::error_1(&JsValue::from_str(&format!("{msg}\n{stack}")));
            }));
            let _ = log::set_logger(&LOGGER);
            log::set_max_level(log::LevelFilter::Info);
        });
    }

    #[wasm_bindgen]
    pub fn horizon_web_mount_pack(bytes: Vec<u8>) -> Result<u32, JsValue> {
        install_hooks();
        horizon_core::vfs::mount_pack(bytes)
            .map(|n| n as u32)
            .map_err(|e| JsValue::from_str(&format!("mount_pack failed: {e}")))
    }

    #[wasm_bindgen]
    pub fn horizon_web_import_file(path: &str, data: Vec<u8>, mtime_secs: f64) {
        install_hooks();
        let secs = if mtime_secs.is_finite() && mtime_secs >= 0.0 {
            mtime_secs as u64
        } else {
            1_730_000_000
        };
        horizon_core::vfs::import_persisted_file(path, data, secs);
    }

    #[wasm_bindgen]
    pub fn horizon_web_drain_changes() -> js_sys::Array {
        let (written, deleted) = horizon_core::vfs::drain_persisted_changes();
        let written_arr = js_sys::Array::new();
        for (path, bytes, mtime) in written {
            let item = js_sys::Array::new();
            item.push(&JsValue::from_str(&path));
            let u8arr = js_sys::Uint8Array::from(bytes.as_slice());
            item.push(&u8arr.into());
            item.push(&JsValue::from_f64(mtime as f64));
            written_arr.push(&item.into());
        }
        let deleted_arr = js_sys::Array::new();
        for path in deleted {
            deleted_arr.push(&JsValue::from_str(&path));
        }
        let pair = js_sys::Array::new();
        pair.push(&written_arr.into());
        pair.push(&deleted_arr.into());
        pair
    }

    #[wasm_bindgen]
    pub fn horizon_web_status() -> String {
        crate::game::web_status_json()
    }

    /// Ask the game to save the world (the page is hidden or closing).
    #[wasm_bindgen]
    pub fn horizon_web_request_save() {
        install_hooks();
        crate::game::request_web_save();
    }

    /// What a page worker needs to start the engine's job pool: the compiled
    /// module and the shared memory every worker instantiates against.
    #[wasm_bindgen]
    pub fn horizon_web_thread_handles() -> js_sys::Array {
        let handles = js_sys::Array::new();
        handles.push(&wasm_bindgen::module());
        handles.push(&wasm_bindgen::memory());
        handles
    }

    /// Called once by each of the page's Web Workers as it starts: it counts
    /// itself into the pool and then runs the job loop forever (it never
    /// returns, so the worker stays out of JavaScript).
    #[wasm_bindgen]
    pub fn horizon_web_thread_worker() {
        install_hooks();
        horizon_threads::register_worker();
        horizon_threads::worker_main();
    }

    #[wasm_bindgen]
    pub async fn horizon_web_start(
        canvas_id: String,
        width: u32,
        height: u32,
        auto_world: Option<String>,
        creative: bool,
    ) -> Result<(), JsValue> {
        install_hooks();
        crate::game::run_web(&canvas_id, width, height, auto_world, creative)
            .await
            .map_err(|e| JsValue::from_str(&e))
    }
}
