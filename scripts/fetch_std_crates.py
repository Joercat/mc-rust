#!/usr/bin/env python3
"""Vendor the crates that `std` itself depends on.

`scripts/fetch_crates.py`, `fetch_extra_crates.py` and `assemble_vendor.py`
vendor what the project's `Cargo.lock` names. `-Z build-std` (the threaded web
build, see docs/WEB.md) also compiles `std`, whose dependencies — `object`,
`addr2line`, `compiler_builtins`, `dlmalloc`, `libc`, `wasi`, … — are resolved
from the lockfile that ships with the `rust-src` component, so they are not in
the project's lock and not in the vendor directory. This fetches them, at the
exact versions that lockfile pins, from crates.io.

Usage: scripts/fetch_std_crates.py [VENDOR_DIR] [TOOLCHAIN]

VENDOR_DIR defaults to /tmp/horizon-vendor (what `.cargo/config.toml` points
at) and TOOLCHAIN to `nightly`. Crates already vendored are left alone, so the
script is cheap to re-run and safe to run offline: it skips everything it
already has and only reports what it could not fetch.
"""

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

VENDOR = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/horizon-vendor")
TOOLCHAIN = sys.argv[2] if len(sys.argv) > 2 else "nightly"
DOWNLOAD = "https://crates.io/api/v1/crates/{name}/{version}/download"


def std_lockfile() -> Path:
    """The `Cargo.lock` of the toolchain's `rust-src` component."""
    try:
        sysroot = subprocess.run(
            ["rustc", f"+{TOOLCHAIN}", "--print", "sysroot"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except FileNotFoundError:
        raise SystemExit("rustc is not on PATH: install Rust with rustup.rs first")
    except subprocess.CalledProcessError:
        raise SystemExit(f"no '{TOOLCHAIN}' toolchain: rustup toolchain install {TOOLCHAIN}")
    lock = Path(sysroot) / "lib/rustlib/src/rust/library/Cargo.lock"
    if not lock.is_file():
        raise SystemExit(
            f"no {lock}\n"
            f"install the component: rustup +{TOOLCHAIN} component add rust-src"
        )
    return lock


def crate_file(lock_dir: Path, entry: dict) -> Path:
    """A path dependency inside `library/`, if that is what this entry is."""
    path = entry.get("path")
    return (lock_dir / path).resolve() if path else Path()


def fetch(name: str, version: str, dest: Path) -> bool:
    """Download one crate from crates.io and vendor it into `dest`."""
    request = urllib.request.Request(
        DOWNLOAD.format(name=name, version=version),
        headers={"User-Agent": "horizon-fetch-std-crates/1"},
    )
    try:
        data = urllib.request.urlopen(request, timeout=60).read()
    except (urllib.error.URLError, urllib.error.HTTPError) as error:
        print(f"  {name} {version}: {error}", file=sys.stderr)
        return False
    # crates.io serves the `.crate` tarball; its sha256 is what `package` in
    # `.cargo-checksum.json` is checked against.
    package = hashlib.sha256(data).hexdigest()
    dest.mkdir(parents=True, exist_ok=True)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            # Everything lives under `<name>-<version>/`.
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            relative = Path(*parts[1:])
            target = dest / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            content = tar.extractfile(member).read()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            files[str(relative)] = hashlib.sha256(content).hexdigest()
    import json

    (dest / ".cargo-checksum.json").write_text(
        json.dumps({"files": files, "package": package})
    )
    return True


def main() -> int:
    lock = std_lockfile()
    lock_dir = lock.parent
    with lock.open("rb") as handle:
        packages = tomllib.load(handle).get("package", [])
    VENDOR.mkdir(parents=True, exist_ok=True)

    fetched = skipped = 0
    failed = []
    for entry in packages:
        name, version = entry.get("name"), entry.get("version")
        source = entry.get("source", "")
        if not name or not version or "registry+" not in source:
            # Path dependencies (`library/core`, `library/std`, …) are built
            # from rust-src itself, not from the vendor directory.
            continue
        dest = VENDOR / f"{name}-{version}"
        if dest.is_dir() and (dest / ".cargo-checksum.json").is_file():
            skipped += 1
            continue
        if fetch(name, version, dest):
            fetched += 1
            print(f"  vendored {name} {version}")
        else:
            failed.append(f"{name} {version}")

    print(
        f"std's crates in {VENDOR}: {fetched} fetched, {skipped} already there"
        + (f", {len(failed)} failed" if failed else "")
    )
    if failed:
        print("missing: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
