"""Fetch the extra crate sources that are not on the main fetch_crates.py list.

Some upstream repositories moved or were deleted since the first pass; this
script re-resolves the ones that still exist (wasm-bindgen's JS bindings live in
the wasm-bindgen monorepo, simd-adler32 moved to mcountryman/, ...) and records
the remaining platform-only crates in MISSING so assemble_vendor.py can emit
minimal stubs for them (they are never compiled for wasm32).
"""

import concurrent.futures
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

SRC_CACHE = Path("/tmp/horizon-gh-cache")
SRC_CACHE.mkdir(parents=True, exist_ok=True)

REPOS = [
    ("wasm-bindgen/wasm-bindgen", "0.2.129"),
    ("mcountryman/simd-adler32", "v0.3.10"),
    ("eira-fransham/crunchy", "HEAD"),
    ("storyyeller/stable_deref_trait", "HEAD"),
    ("sfackler/rust-fallible-iterator", "v0.3.0"),
    ("gimli-rs/gimli", "0.32.3"),
    ("jedisct1/rust-minisign-verify", "0.3.0"),
    ("zopfli-rs/zopfli", "v0.8.4"),
    ("rust-lang/pin-utils", "0.1.0"),
    ("Smithay/calloop", "v0.13.0"),
    ("smol-rs/polling", "v3.11.0"),
    ("sunfishcode/linux-raw-sys", "v0.12.1"),
    # Exact tags pinned by Cargo.lock (the main pass fell back to HEAD for these).
    ("katharostech/cfg_aliases", "v0.2.2"),
    ("srijs/rust-crc32fast", "v1.5.3"),
    ("RustCrypto/traits", "digest-v0.10.7"),
    ("slint-ui/document-features", "v0.2.12"),
    ("fizyk20/generic-array", "0.14.4"),
    ("rust-lang/hashbrown", "v0.17.1"),
    ("rust-lang/log", "0.4.34"),
    ("rust-lang/pin-utils", "0.1.1"),
    ("rust-lang/rustc-hash", "v1.2.0"),
    ("bytecodealliance/rustix", "v0.38.44"),
    ("dtolnay/semver", "1.0.28"),
    ("serde-rs/serde", "v1.0.229"),
    ("serde-rs/json", "v1.0.154"),
    ("bytecodealliance/wasm-tools", "v1.245.1"),
    ("hsivonen/encoding_rs", "v0.8.35"),
    # pp-rs 0.2.2: the gfx-rs repository is gone; this fork carries the same
    # crate (naga's GLSL frontend) and builds no_std.
    ("PhotomancerArt/pp-rs", "HEAD"),
    # Likewise moved/dropped upstream: dlib now lives under Smithay, x11-dl is
    # only mirrored by a fork. Both are Linux-only.
    ("Smithay/dlib", "HEAD"),
    ("inkreasing/x11-dl", "HEAD"),
    ("bitshifter/glam-rs", "0.30.10"),
    ("Amanieu/parking_lot", "lock_api-v0.4.14"),
    ("Amanieu/parking_lot", "parking_lot-v0.12.5"),
    ("Amanieu/parking_lot", "parking_lot_core-v0.9.12"),
    ("orlp/foldhash", "v0.1.5"),
    ("rust-lang/hashbrown", "v0.15.5"),
    ("sunfishcode/linux-raw-sys", "v0.4.15"),
    ("Frommi/miniz_oxide", "0.8.9"),
    ("rust-analyzer/smol_str", "v0.2.2"),
]

# Repos that 404 now; these crates are only used by Linux/Windows backends.
MISSING = ["gethostname"]


def download_one(item):
    repo, ref = item
    slug = f"extra__{repo.replace('/', '__')}__{ref.replace('/', '_')}"
    dest = SRC_CACHE / slug
    if dest.exists():
        return repo, ref, "cached"
    url = f"https://codeload.github.com/{repo}/tar.gz/{ref}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        return repo, ref, f"ERR: {exc}"
    tgz = SRC_CACHE / f"{slug}.tgz"
    tgz.write_bytes(data)
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


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(download_one, REPOS))
    for repo, ref, status in results:
        print(f"{status:8} {repo} {ref}")
    print("still unavailable (stubs will be generated):", ", ".join(MISSING))
