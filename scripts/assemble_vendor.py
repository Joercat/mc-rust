import json, os, re, shutil, subprocess, tarfile, tomllib, urllib.request
from pathlib import Path

SRC_CACHE = Path("/tmp/horizon-gh-cache")
VENDOR = Path("/tmp/horizon-vendor")

# Grab libloading (used by dlib) if not present
if not (SRC_CACHE / "extra__libloading").exists():
    data = urllib.request.urlopen(urllib.request.Request(
        "https://codeload.github.com/nagisa/rust_libloading/tar.gz/0.8.6",
        headers={"User-Agent": "curl/8.0"},
    ), timeout=15).read()
    tgz = SRC_CACHE / "ll.tgz"; tgz.write_bytes(data)
    tmp = SRC_CACHE / "ll.tmp"; tmp.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz, "r:gz") as tf: tf.extractall(tmp)
    tgz.unlink()
    list(tmp.iterdir())[0].rename(SRC_CACHE / "extra__libloading"); tmp.rmdir()

shutil.rmtree(VENDOR, ignore_errors=True)
VENDOR.mkdir(parents=True, exist_ok=True)

WANTED_NAMES = {
    "ab_glyph", "ab_glyph_rasterizer", "adler2", "ahash", "anyhow", "arbitrary", "arrayref",
    "arrayvec", "as-raw-xcb-connection", "atomic-waker", "autocfg", "base64", "bit-set", "bit-vec",
    "bitflags", "block-buffer", "bumpalo", "bytemuck", "bytemuck_derive", "calloop", "cc", "cfg-if",
    "cfg_aliases", "chrono", "cobs", "codespan-reporting", "concurrent-queue", "cpufeatures",
    "crc32fast", "crossbeam-utils", "crunchy", "crypto-common", "cursor-icon", "derive_arbitrary",
    "digest", "displaydoc", "dlib", "document-features", "dpi", "embedded-io", "encoding_rs",
    "equivalent", "errno", "fallible-iterator", "fdeflate", "find-msvc-tools", "flate2", "foldhash", "futures-core",
    "futures-task", "futures-util", "generic-array", "gethostname", "getrandom", "gimli", "glam", "glow",
    "half", "hashbrown", "heck", "iana-time-zone", "id-arena", "indexmap", "itoa", "jobserver",
    "js-sys", "lazy_static", "leb128", "leb128fmt", "libc", "libloading", "libm", "linux-raw-sys",
    "litrs", "lock_api", "log", "lz4_flex", "memchr", "memmap2", "minisign-verify", "miniz_oxide", "naga", "naga-types",
    "num-traits", "once_cell", "ordered-float", "owned_ttf_parser", "parking_lot",
    "parking_lot_core", "percent-encoding", "pin-project", "pin-project-internal",
    "pin-project-lite", "pin-utils", "pkg-config", "png", "polling", "pollster", "portable-atomic", "portable-atomic-util",
    "postcard", "pp-rs", "proc-macro2", "profiling", "quote", "rand_core", "raw-window-handle",
    "rustc-demangle", "rustc-hash", "rustix", "rustversion", "ruzstd", "scopeguard", "semver", "serde",
    "serde_core", "serde_derive", "serde_json", "serde_spanned", "sha1", "sha2", "shlex",
    "simd-adler32", "simdutf8", "slab", "slotmap", "smallvec", "smol_str", "spirv",
    "stable_deref_trait", "static_assertions", "strict-num", "subtle", "symphonia", "symphonia-codec-vorbis",
    "symphonia-core", "symphonia-format-ogg", "symphonia-metadata", "symphonia-utils-xiph", "syn",
    "termcolor", "thiserror", "thiserror-impl", "toml", "toml_datetime", "toml_edit",
    "toml_parser", "toml_writer", "tracing", "tracing-core", "ttf-parser", "twox-hash", "typenum",
    "unicode-bidi", "unicode-ident", "unicode-segmentation", "unicode-width", "unicode-xid",
    "version_check", "walrus", "walrus-macro", "wasm-bindgen", "wasm-bindgen-cli-support",
    "wasm-bindgen-futures", "wasm-bindgen-macro", "wasm-bindgen-macro-support",
    "wasm-bindgen-shared", "wasm-encoder", "wasmparser", "web-sys", "web-time", "wgpu", "wgpu-core", "walrus-macro",
    "spirv", "pp-rs", "dlib", "gethostname", "x11-dl", "calloop", "polling", "linux-raw-sys", "stable_deref_trait",
    "fallible-iterator", "gimli", "simd-adler32", "minisign-verify", "pin-utils", "zopfli", "crunchy",
    "wgpu-core-deps-apple", "wgpu-core-deps-emscripten", "wgpu-core-deps-wasm", "wgpu-core-deps-windows-linux-android",
    "wgpu-hal", "wgpu-naga-bridge", "wgpu-types", "winit", "winnow", "x11-dl", "x11rb",
    "x11rb-protocol", "xkbcommon-dl", "xkeysym", "xml-rs", "xxhash-rust", "zerocopy",
    "zerocopy-derive", "zip", "zlib-rs", "zmij", "zopfli",
}

def toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        items = []
        for k, val in v.items():
            key = k if re.match(r'^[A-Za-z0-9_-]+$', k) else json.dumps(k)
            items.append(f"{key} = {toml_val(val)}")
        return "{ " + ", ".join(items) + " }"
    raise TypeError(f"Unsupported type: {type(v)}")

def resolve_dep(dep_name, spec, ws_deps):
    if isinstance(spec, str):
        return {"version": spec}
    d = dict(spec)
    if d.pop("workspace", False):
        base = ws_deps.get(dep_name, ws_deps.get(d.get("package", dep_name), "*"))
        if isinstance(base, str):
            merged = {"version": base}
        else:
            merged = dict(base)
        # Merge features
        feats = list(merged.get("features", []))
        for f in d.get("features", []):
            if f not in feats:
                feats.append(f)
        merged.update({k: v for k, v in d.items() if k != "features"})
        if feats:
            merged["features"] = feats
        d = merged
    d.pop("path", None)
    d.pop("registry", None)
    if "version" not in d:
        d["version"] = "*"
    return d

# First pass: discover all crates in SRC_CACHE
found_crates = []  # (name, version, crate_dir, parsed_toml, ws_toml)
for repo_dir in sorted(SRC_CACHE.iterdir()):
    if not repo_dir.is_dir():
        continue
    ws_toml = {}
    root_cargo = repo_dir / "Cargo.toml"
    if root_cargo.is_file():
        try:
            ws_toml = tomllib.loads(root_cargo.read_text()).get("workspace", {})
        except Exception:
            ws_toml = {}
    for cargo_path in sorted(repo_dir.rglob("Cargo.toml")):
        rel_parts = cargo_path.relative_to(repo_dir).parts
        if any(p in ("tests", "benches", "fuzz", "test-crates", "crates-io") for p in rel_parts[:-1]):
            continue
        try:
            data = tomllib.loads(cargo_path.read_text())
        except Exception:
            continue
        pkg = data.get("package")
        if not pkg or "name" not in pkg:
            continue
        name = pkg["name"]
        if name not in WANTED_NAMES:
            continue
        ver = pkg.get("version", "0.1.0")
        if isinstance(ver, dict) and ver.get("workspace"):
            ver = ws_toml.get("package", {}).get("version", "0.1.0")
        found_crates.append((name, ver, cargo_path.parent, data, ws_toml))

# Set of available crate names
def semver_satisfied(req_str, avail_vers):
    if req_str == "*" or not req_str:
        return bool(avail_vers)
    # Check simple major/minor compatibility for ^ / bare version requirements
    m = re.search(r'(\d+)(?:\.(\d+))?(?:\.(\d+))?', req_str)
    if not m:
        return bool(avail_vers)
    rmaj = int(m.group(1))
    rmin = int(m.group(2)) if m.group(2) is not None else None
    for v in avail_vers:
        vm = re.match(r'(\d+)\.(\d+)\.(\d+)', v)
        if not vm:
            continue
        vmaj, vmin = int(vm.group(1)), int(vm.group(2))
        if rmaj == 0 and rmin is not None:
            if vmaj == 0 and vmin == rmin:
                return True
        elif rmaj == vmaj and (rmin is None or vmin >= rmin):
            return True
    return False

avail_names = {c[0] for c in found_crates}
avail_vers = {}
for n, v, *_ in found_crates:
    avail_vers.setdefault(n, []).append(v)
print(f"Found {len(found_crates)} crate versions across {len(avail_names)} unique crate names")
missing_wanted = sorted(WANTED_NAMES - avail_names)
if missing_wanted:
    print("Note - wanted names not in cache:", missing_wanted)

def clean_deps_table(deps, ws_deps, crate_name):
    cleaned = {}
    removed_dep_names = set()
    for k, spec in deps.items():
        d = resolve_dep(k, spec, ws_deps)
        real_pkg = d.get("package", k)
        # Drop renderdoc in wgpu-core-deps-windows-linux-android
        if crate_name == "wgpu-core-deps-windows-linux-android" and real_pkg.startswith("renderdoc"):
            removed_dep_names.add(k)
            continue
        # Drop parallel/rayon in walrus
        if crate_name == "walrus" and real_pkg == "rayon":
            removed_dep_names.add(k)
            continue
        if real_pkg not in avail_names or (d.get('optional') and not semver_satisfied(d.get('version', '*'), avail_vers.get(real_pkg, []))):
            # Unvendored optional or target dep -> drop cleanly
            removed_dep_names.add(k)
            continue
        cleaned[k] = d
    return cleaned, removed_dep_names

written = set()
for name, ver, cdir, data, ws_toml in found_crates:
    vdir_name = f"{name}-{ver}"
    if vdir_name in written:
        continue
    written.add(vdir_name)
    dest = VENDOR / vdir_name
    shutil.copytree(cdir, dest, symlinks=False, ignore=shutil.ignore_patterns(".git", "target", "benches"))

    # Special case: crates that include!("../../LICENSE...") or parent files (e.g. serde, wgpu, etc.)
    ws_pkg = ws_toml.get("package", {})
    ws_deps = ws_toml.get("dependencies", {})

    pkg = dict(data["package"])
    out_pkg = {}
    for pk, pv in pkg.items():
        if pk in ("workspace", "default-run", "publish", "metadata", "readme", "license-file", "autobenches", "autoexamples", "autotests"):
            continue
        if isinstance(pv, dict) and pv.get("workspace"):
            if pk in ws_pkg:
                out_pkg[pk] = ws_pkg[pk]
        else:
            out_pkg[pk] = pv
    out_pkg["name"] = name
    out_pkg["version"] = ver
    out_pkg["autobenches"] = False
    out_pkg["autoexamples"] = False
    out_pkg["autotests"] = False
    # If build.rs does not exist in dest, strip build key; if build.rs exists, keep
    if "build" in out_pkg and out_pkg["build"] is not False:
        bpath = dest / (out_pkg["build"] if isinstance(out_pkg["build"], str) else "build.rs")
        if not bpath.exists():
            out_pkg.pop("build", None)

    removed_all = set()
    deps, rem = clean_deps_table(data.get("dependencies", {}), ws_deps, name)
    removed_all |= rem
    bdeps, rem = clean_deps_table((data.get("build-dependencies") or data.get("build_dependencies") or {}), ws_deps, name)
    removed_all |= rem

    t_sections = {}
    for tgt, tdata in data.get("target", {}).items():
        # Skip fuzzing / windows / apple / android / redox / wasi targets whose deps aren't vendored
        if "fuzzing" in tgt:
            continue
        tdeps, rem = clean_deps_table(tdata.get("dependencies", {}), ws_deps, name)
        removed_all |= rem
        tbdeps, rem = clean_deps_table((tdata.get("build-dependencies") or tdata.get("build_dependencies") or {}), ws_deps, name)
        removed_all |= rem
        if tdeps or tbdeps:
            t_sections[tgt] = (tdeps, tbdeps)

    # Clean features referencing removed optional deps
    feats = {}
    raw_feats = dict(data.get("features", {}))
    if name == "walrus":
        raw_feats.pop("parallel", None)
    if name == "wgpu-core-deps-windows-linux-android":
        raw_feats = {fk: [] for fk in raw_feats}
    all_kept = set(deps) | set(bdeps) | {k for td, tb in t_sections.values() for k in (*td, *tb)}
    for fk, fv in raw_feats.items():
        new_fv = []
        for item in fv:
            if "/" in item or item.startswith("dep:"):
                ref = item.split("/")[0].removeprefix("dep:").removesuffix("?")
                if ref not in all_kept:
                    continue
            elif item not in raw_feats and item not in all_kept:
                continue
            new_fv.append(item)
        feats[fk] = new_fv
    # Ensure optional deps still referenced via `dep:foo` or implicit feature exist
    for dk, dv in deps.items():
        if dv.get("optional") and dk not in feats and not any(f"dep:{dk}" in fv for fv in feats.values()):
            feats[dk] = [f"dep:{dk}"]

    # Emit clean Cargo.toml
    lines = ["[package]"]
    for pk, pv in out_pkg.items():
        k_str = pk if re.match(r'^[A-Za-z0-9_-]+$', pk) else json.dumps(pk)
        lines.append(f"{k_str} = {toml_val(pv)}")

    if "lib" in data:
        lines.append("\n[lib]")
        for lk, lv in data["lib"].items():
            if lk in ("bench", "doctest", "test"):
                continue
            lines.append(f"{lk} = {toml_val(lv)}")

    for b in data.get("bin", []):
        bpath = dest / b.get("path", f"src/bin/{b['name']}.rs")
        if bpath.exists() or (dest / "src/main.rs").exists():
            lines.append("\n[[bin]]")
            for bk, bv in b.items():
                if bk == "required-features":
                    continue
                lines.append(f"{bk} = {toml_val(bv)}")

    if feats:
        lines.append("\n[features]")
        for fk, fv in feats.items():
            k_str = fk if re.match(r'^[A-Za-z0-9_-]+$', fk) else json.dumps(fk)
            lines.append(f"{k_str} = {toml_val(fv)}")

    if deps:
        lines.append("\n[dependencies]")
        for dk, dv in deps.items():
            k_str = dk if re.match(r'^[A-Za-z0-9_-]+$', dk) else json.dumps(dk)
            lines.append(f"{k_str} = {toml_val(dv)}")

    if bdeps:
        lines.append("\n[build-dependencies]")
        for dk, dv in bdeps.items():
            k_str = dk if re.match(r'^[A-Za-z0-9_-]+$', dk) else json.dumps(dk)
            lines.append(f"{k_str} = {toml_val(dv)}")

    for tgt, (tdeps, tbdeps) in t_sections.items():
        if tdeps:
            lines.append(f"\n[target.{json.dumps(tgt)}.dependencies]")
            for dk, dv in tdeps.items():
                k_str = dk if re.match(r'^[A-Za-z0-9_-]+$', dk) else json.dumps(dk)
                lines.append(f"{k_str} = {toml_val(dv)}")
        if tbdeps:
            lines.append(f"\n[target.{json.dumps(tgt)}.build-dependencies]")
            for dk, dv in tbdeps.items():
                k_str = dk if re.match(r'^[A-Za-z0-9_-]+$', dk) else json.dumps(dk)
                lines.append(f"{k_str} = {toml_val(dv)}")

    (dest / "Cargo.toml").write_text("\n".join(lines) + "\n")
    (dest / ".cargo-checksum.json").write_text('{"files":{}}\n')

print(f"Assembled {len(written)} vendored crates into {VENDOR}!")

# Post-fixes on vendored directory
(VENDOR / "README.md").write_text("")
zc_orig = Path("/tmp/horizon-gh-cache/google__zerocopy__v0.8.39/Cargo.toml")
if zc_orig.is_file() and (VENDOR / "zerocopy-0.8.39/Cargo.toml").is_file():
    ot = zc_orig.read_text()
    idx = ot.index("[package.metadata.build-rs]")
    meta_block = ot[idx:].split("\n[")[0]
    zp = VENDOR / "zerocopy-0.8.39/Cargo.toml"
    zp.write_text(zp.read_text() + "\n" + meta_block + "\n")
for rh in VENDOR.glob("rustc-hash-*/src/lib.rs"):
    rt = rh.read_text()
    rt = rt.replace("mod seeded_state;", '#[cfg(feature = "std")]\nmod seeded_state;')
    rt = rt.replace("pub use seeded_state::", '#[cfg(feature = "std")]\npub use seeded_state::')
    rh.write_text(rt)

# ---------------------------------------------------------------------------
# Stubs for crates whose upstream repositories disappeared from GitHub and that
# are only ever built for Linux/Windows targets (never wasm32).  They must exist
# so Cargo can resolve the full graph, but their bodies are never compiled.
# ---------------------------------------------------------------------------
STUBS = {
    "gethostname": ("0.4.3", ["default"], []),
}
for stub_name, (stub_ver, stub_feats, stub_deps) in STUBS.items():
    if (VENDOR / f"{stub_name}-{stub_ver}").is_dir():
        continue
    dest = VENDOR / f"{stub_name}-{stub_ver}"
    (dest / "src").mkdir(parents=True, exist_ok=True)
    lines = [
        "[package]",
        f'name = "{stub_name}"',
        f'version = "{stub_ver}"',
        'edition = "2021"',
        "",
        "[lib]",
        'path = "src/lib.rs"',
        "",
        "[features]",
    ]
    lines += [f'{f} = []' for f in stub_feats]
    dep_lines = [f'{d} = "*"' for d in stub_deps]
    if dep_lines:
        lines += ["", "[dependencies]"] + dep_lines
    (dest / "Cargo.toml").write_text("\n".join(lines) + "\n")
    (dest / "src/lib.rs").write_text("// offline stub: this crate is never compiled for wasm32\n")
    (dest / ".cargo-checksum.json").write_text('{"files":{}}\n')
    print(f"Stubbed {stub_name}-{stub_ver}")

# `libm`'s repository tags releases before bumping the crate version, so the
# libm-v0.2.16 tag still declares 0.2.11. horizon-core pins `=0.2.16`.
libm_dir = VENDOR / "libm-0.2.11"
if libm_dir.is_dir() and not (VENDOR / "libm-0.2.16").exists():
    ct = libm_dir / "Cargo.toml"
    ct.write_text(ct.read_text().replace('version = "0.2.11"', 'version = "0.2.16"', 1))
    libm_dir.rename(VENDOR / "libm-0.2.16")
    print("Retagged libm-0.2.11 -> libm-0.2.16")

# ---------------------------------------------------------------------------
# Version-requirement alignments: the crates.io releases these manifests ask for
# were never tagged upstream (contain-rs only tags v0.5.3), so point them at the
# vendored releases instead of failing resolution.
REQ_PATCHES = [
    ("naga-30.0.1/Cargo.toml", 'bit-set = { version = "0.10"', 'bit-set = { version = "0.11"'),
    ("wgpu-core-30.0.1/Cargo.toml", 'bit-vec = { version = "0.9.1"', 'bit-vec = { version = "0.10.1"'),
    ("wgpu-core-30.0.1/Cargo.toml", 'bit-set = { version = "0.10"', 'bit-set = { version = "0.11"'),
    # walrus at HEAD dropped its feature table; the CLI support only needs the
    # serial code paths, which walrus picks when `parallel` is off.
    ("wasm-bindgen-cli-support-0.2.129/Cargo.toml",
     'walrus = { version = "0.27.2", features = ["parallel"] }',
     'walrus = { version = "0.27.2" }'),
]
for rel, old, new in REQ_PATCHES:
    f = VENDOR / rel
    if f.is_file():
        txt = f.read_text()
        if old in txt:
            f.write_text(txt.replace(old, new))
            print(f"Aligned dependency in {rel}")
