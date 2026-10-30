import concurrent.futures, json, os, re, shutil, tarfile, tomllib, urllib.request
from pathlib import Path

VENDOR = Path("/tmp/horizon-vendor")
SRC_CACHE = Path("/tmp/horizon-gh-cache")
VENDOR.mkdir(parents=True, exist_ok=True)
SRC_CACHE.mkdir(parents=True, exist_ok=True)

# Map of (repo_owner_name, ref) -> list of crate names to extract from that repo
REPOS = [
    ("alexheretic/ab-glyph", "ab_glyph-0.2.32", ["ab_glyph", "ab_glyph_rasterizer"]),
    ("Frommi/miniz_oxide", "HEAD", ["miniz_oxide"]),
    ("simd-lite/simd-adler32", "v0.3.8", ["simd-adler32"]),
    ("oyvindln/adler2", "v2.0.1", ["adler2"]),
    ("tkaitchuck/aHash", "v0.8.12", ["ahash"]),
    ("rust-fuzz/arbitrary", "HEAD", ["arbitrary", "derive_arbitrary"]),
    ("bluss/arrayref", "0.3.9", ["arrayref"]),
    ("bluss/arrayvec", "v0.7.6", ["arrayvec"]),
    ("psychon/as-raw-xcb-connection", "1.0.1", ["as-raw-xcb-connection"]),
    ("smol-rs/atomic-waker", "v1.1.2", ["atomic-waker"]),
    ("cuviper/autocfg", "1.5.0", ["autocfg"]),
    ("marshallpierce/rust-base64", "v0.22.1", ["base64"]),
    ("contain-rs/bit-set", "HEAD", ["bit-set"]),
    ("contain-rs/bit-vec", "HEAD", ["bit-vec"]),
    ("bitflags/bitflags", "1.3.2", ["bitflags"]),
    ("bitflags/bitflags", "2.11.0", ["bitflags"]),
    ("RustCrypto/utils", "block-buffer-v0.10.4", ["block-buffer"]),
    ("RustCrypto/traits", "crypto-common-v0.1.6", ["crypto-common", "digest"]),
    ("RustCrypto/hashes", "sha2-v0.10.9", ["sha1", "sha2"]),
    ("RustCrypto/utils", "cpufeatures-v0.2.17", ["cpufeatures"]),
    ("fitzgen/bumpalo", "3.20.2", ["bumpalo"]),
    ("Lokathor/bytemuck", "bytemuck-v1.25.0", ["bytemuck", "bytemuck_derive"]),
    ("rust-lang/cc-rs", "HEAD", ["cc", "find-msvc-tools"]),
    ("rust-lang/cfg-if", "HEAD", ["cfg-if"]),
    ("katharostech/cfg_aliases", "v0.2.1", ["cfg_aliases"]),
    ("chronotope/chrono", "v0.4.42", ["chrono"]),
    ("jamesmunns/cobs.rs", "v0.3.0", ["cobs"]),
    ("brendanzab/codespan", "HEAD", ["codespan-reporting"]),
    ("smol-rs/concurrent-queue", "v2.5.0", ["concurrent-queue"]),
    ("srijs/rust-crc32fast", "v1.5.0", ["crc32fast"]),
    ("crossbeam-rs/crossbeam", "crossbeam-utils-0.8.21", ["crossbeam-utils"]),
    ("EmbarkStudios/crunchy", "0.2.4", ["crunchy"]),
    ("rust-windowing/cursor-icon", "v1.2.0", ["cursor-icon"]),
    ("unicode-org/icu4x", "HEAD", []),  # skip, use displaydoc separately
    ("yaahc/displaydoc", "v0.2.5", ["displaydoc"]),
    ("elan/dlib", "0.5.2", ["dlib"]),
    ("slint-ui/document-features", "v0.2.11", ["document-features"]),
    ("rust-windowing/dpi", "v0.1.2", ["dpi"]),
    ("rust-embedded/embedded-hal", "embedded-io-v0.6.1", ["embedded-io"]),
    ("indexmap-rs/equivalent", "1.0.2", ["equivalent"]),
    ("lambda-fairy/rust-errno", "v0.3.14", ["errno"]),
    ("image-rs/fdeflate", "v0.3.7", ["fdeflate"]),
    ("rust-lang/flate2-rs", "1.1.10", ["flate2"]),
    ("orlp/foldhash", "v0.2.0", ["foldhash"]),
    ("rust-lang/futures-rs", "0.3.31", ["futures-core", "futures-task", "futures-util"]),
    ("fizyk20/generic-array", "v0.14.7", ["generic-array"]),
    ("swsnr/gethostname.rs", "HEAD", ["gethostname"]),
    ("rust-random/getrandom", "v0.2.17", ["getrandom"]),
    ("rust-random/getrandom", "v0.3.4", ["getrandom"]),
    ("rust-random/rand", "0.8.5", ["rand_core"]),
    ("bitshifter/glam-rs", "v0.30.9", ["glam"]),
    ("grovesNL/glow", "578cdfa3056727c68eb2d43c06475379cbc93d49", ["glow"]),
    ("starkat99/half-rs", "v2.7.1", ["half"]),
    ("rust-lang/hashbrown", "v0.16.1", ["hashbrown"]),
    ("strawlab/iana-time-zone", "0.1.65", ["iana-time-zone"]),
    ("indexmap-rs/indexmap", "2.13.0", ["indexmap"]),
    ("dtolnay/itoa", "1.0.17", ["itoa"]),
    ("rust-lang/jobserver-rs", "0.1.34", ["jobserver"]),
    ("rust-lang-nursery/lazy-static.rs", "1.5.0", ["lazy_static"]),
    ("rust-lang/libc", "0.2.183", ["libc"]),
    ("rust-lang/libm", "libm-v0.2.16", ["libm"]),
    ("bytecodealliance/linux-raw-sys", "HEAD", ["linux-raw-sys"]),
    ("LukasKalbertodt/litrs", "v1.0.0", ["litrs"]),
    ("Amanieu/parking_lot", "0.12.5", ["lock_api", "parking_lot", "parking_lot_core"]),
    ("Amanieu/scopeguard", "v1.2.0", ["scopeguard"]),
    ("rust-lang/log", "0.4.29", ["log"]),
    ("PSeitz/lz4_flex", "0.14.0", ["lz4_flex"]),
    ("BurntSushi/memchr", "2.8.0", ["memchr"]),
    ("RazrFalcon/memmap2-rs", "v0.9.10", ["memmap2"]),
    ("gfx-rs/wgpu", "v30.0.1", [
        "wgpu", "wgpu-core", "wgpu-types", "wgpu-hal", "naga",
        "wgpu-core-deps-apple", "wgpu-core-deps-emscripten", "wgpu-core-deps-windows-linux-android",
    ]),
    ("rust-num/num-traits", "num-traits-0.2.19", ["num-traits"]),
    ("matklad/once_cell", "v1.21.3", ["once_cell"]),
    ("reem/rust-ordered-float", "v5.1.0", ["ordered-float"]),
    ("alexheretic/owned-ttf-parser", "owned_ttf_parser-0.25.1", ["owned_ttf_parser"]),
    ("RazrFalcon/ttf-parser", "v0.25.1", ["ttf-parser"]),
    ("servo/rust-url", "percent-encoding-2.3.2", ["percent-encoding"]),
    ("taiki-e/pin-project", "v1.1.10", ["pin-project", "pin-project-internal"]),
    ("taiki-e/pin-project-lite", "v0.2.16", ["pin-project-lite"]),
    ("rust-lang/pkg-config-rs", "0.3.32", ["pkg-config"]),
    ("image-rs/image-png", "v0.18.1", ["png"]),
    ("zesterer/pollster", "v0.4.0", ["pollster"]),
    ("taiki-e/portable-atomic", "v1.13.1", ["portable-atomic", "portable-atomic-util"]),
    ("jamesmunns/postcard", "v1.1.3", ["postcard"]),
    ("gfx-rs/pp-rs", "HEAD", ["pp-rs"]),
    ("dtolnay/proc-macro2", "1.0.106", ["proc-macro2"]),
    ("aclysma/profiling", "v1.0.17", ["profiling"]),
    ("dtolnay/quote", "1.0.45", ["quote"]),
    ("rust-windowing/raw-window-handle", "v0.6.2", ["raw-window-handle"]),
    ("rust-lang/rustc-hash", "v1.1.0", ["rustc-hash"]),
    ("rust-lang/rustc-hash", "v2.1.1", ["rustc-hash"]),
    ("bytecodealliance/rustix", "v1.1.4", ["rustix"]),
    ("dtolnay/rustversion", "1.0.22", ["rustversion"]),
    ("KillingSpark/zstd-rs", "HEAD", ["ruzstd"]),
    ("serde-rs/serde", "v1.0.228", ["serde", "serde_core", "serde_derive"]),
    ("serde-rs/json", "v1.0.149", ["serde_json"]),
    ("dtolnay/zmij", "HEAD", ["zmij"]),
    ("rusticstuff/simdutf8", "v0.1.5", ["simdutf8"]),
    ("tokio-rs/slab", "v0.4.12", ["slab"]),
    ("orlp/slotmap", "v1.1.1", ["slotmap"]),
    ("servo/rust-smallvec", "v1.15.1", ["smallvec"]),
    ("rust-analyzer/smol_str", "HEAD", ["smol_str"]),
    ("gfx-rs/rspirv", "HEAD", ["spirv"]),
    ("nvzqz/static-assertions", "v1.1.0", ["static_assertions"]),
    ("dalance/strict-num", "HEAD", ["strict-num"]),
    ("dalek-cryptography/subtle", "2.6.1", ["subtle"]),
    ("pdeljanov/Symphonia", "v0.5.5", [
        "symphonia", "symphonia-codec-vorbis", "symphonia-core",
        "symphonia-format-ogg", "symphonia-metadata", "symphonia-utils-xiph",
    ]),
    ("dtolnay/syn", "2.0.117", ["syn"]),
    ("dtolnay/syn", "HEAD", ["syn"]),
    ("BurntSushi/termcolor", "1.4.1", ["termcolor"]),
    ("dtolnay/thiserror", "1.0.69", ["thiserror", "thiserror-impl"]),
    ("dtolnay/thiserror", "2.0.18", ["thiserror", "thiserror-impl"]),
    ("toml-rs/toml", "HEAD", ["toml", "toml_datetime", "toml_edit", "toml_parser", "toml_writer", "serde_spanned"]),
    ("winnow-rs/winnow", "HEAD", ["winnow"]),
    ("tokio-rs/tracing", "tracing-0.1.41", ["tracing", "tracing-core"]),
    ("shepmaster/twox-hash", "v2.1.2", ["twox-hash"]),
    ("paholg/typenum", "v1.19.0", ["typenum"]),
    ("servo/unicode-bidi", "v0.3.18", ["unicode-bidi"]),
    ("dtolnay/unicode-ident", "1.0.24", ["unicode-ident"]),
    ("unicode-rs/unicode-segmentation", "v1.12.0", ["unicode-segmentation"]),
    ("unicode-rs/unicode-width", "v0.2.2", ["unicode-width"]),
    ("unicode-rs/unicode-xid", "v0.2.6", ["unicode-xid"]),
    ("SergioBenitez/version_check", "0.9.5", ["version_check"]),
    ("daxpedda/web-time", "v1.1.0", ["web-time"]),
    ("rust-windowing/winit", "v0.30.13", ["winit"]),
    ("AltSysrq/x11-rs", "v2.21.0", ["x11-dl"]),
    ("psychon/x11rb", "v0.13.1", ["x11rb", "x11rb-protocol"]),
    ("rust-windowing/xkbcommon-dl", "v0.4.2", ["xkbcommon-dl"]),
    ("rust-windowing/xkeysym", "v0.2.1", ["xkeysym"]),
    ("kornelski/xml-rs", "HEAD", ["xml-rs"]),
    ("DoumanAsh/xxhash-rust", "0.8.19", ["xxhash-rust"]),
    ("google/zerocopy", "v0.8.39", ["zerocopy", "zerocopy-derive"]),
    ("zip-rs/zip2", "v2.4.2", ["zip"]),
    ("trifectatechfoundation/zlib-rs", "v0.6.3", ["zlib-rs"]),
    ("simd-lite/zopfli", "v0.8.3", ["zopfli"]),
    # For wasm-bindgen 0.2.129 + horizon-wasm-bindgen CLI:
    ("dtolnay/anyhow", "1.0.102", ["anyhow"]),
    ("gimli-rs/leb128", "0.2.5", ["leb128"]),
    ("bluk/leb128fmt", "HEAD", ["leb128fmt"]),
    ("rust-lang/rustc-demangle", "0.1.27", ["rustc-demangle"]),
    ("wasm-bindgen/walrus", "HEAD", ["walrus", "walrus-macro"]),
    ("fitzgen/id-arena", "HEAD", ["id-arena"]),
    ("bytecodealliance/wasm-tools", "v1.245.0", ["wasmparser"]),
    ("withoutboats/heck", "0.5.0", ["heck"]),
    ("comex/rust-shlex", "v1.3.0", ["shlex"]),
]

def download_one(item):
    repo, ref, wanted = item
    if not wanted:
        return repo, ref, "skip"
    slug = f"{repo.replace('/', '__')}__{ref.replace('/', '_')}"
    dest = SRC_CACHE / slug
    if dest.exists():
        return repo, ref, "cached"
    urls = [
        f"https://codeload.github.com/{repo}/tar.gz/{ref}",
        f"https://codeload.github.com/{repo}/tar.gz/HEAD",
    ]
    for url in urls:
        try:
            tgz = SRC_CACHE / f"{slug}.tgz"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                tgz.write_bytes(resp.read())
            tmp = SRC_CACHE / f"{slug}.tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tgz, "r:gz") as tf:
                tf.extractall(tmp)
            tgz.unlink(missing_ok=True)
            subs = list(tmp.iterdir())
            if len(subs) == 1 and subs[0].is_dir():
                subs[0].rename(dest)
                tmp.rmdir()
            else:
                tmp.rename(dest)
            return repo, ref, "ok"
        except Exception as e:
            last_err = str(e)
    return repo, ref, f"ERR: {last_err}"

with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
    results = list(ex.map(download_one, REPOS))

errs = [r for r in results if str(r[2]).startswith("ERR")]
print(f"Downloaded {len(results)} repos, errors: {len(errs)}")
for e in errs:
    print(" ", e)
