#!/usr/bin/env python3
"""Stage this checkout's web build for a Freebuff Site.

`python3 scripts/sites_bundle.py` writes `sites-bundle/` (gitignored):

    sites-bundle/
      worker.js            deploy/sites/worker.js, the host
      manifest.json        the deployment manifest
      assets/              index.html, app.js, the glue, assets.json, …
      big.txt              the files that must be uploaded to storage instead

A site takes assets of at most 10 MiB each, so the wasm modules and the resource
pack are not assets: `scripts/sites_seed.py` uploads them into the site's object
store in 8 MiB parts, and the worker serves one part per request. The staged
`assets.json` is what tells the page that: those three entries carry the file's
size, its SHA-256 and the part URLs, instead of a bare hash. `asset-cache.js`
fetches the parts and joins them, and refuses a join that is not the recorded
size. Every hash in the staged manifest is computed from the staged bytes.

Nothing is deployed from here — the bundle is what gets deployed.
"""
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PUBLIC = os.path.join(ROOT, "public")
OUT = os.path.join(ROOT, "sites-bundle")
ASSETS = os.path.join(OUT, "assets")

# Everything a page load fetches except the wasm modules and the pack.
ASSETS_FILES = [
    "index.html",
    "app.js",
    "threads.js",
    "thread-worker.js",
    "asset-cache.js",
    "horizon-client.js",
    "horizon-client-threaded.js",
]
# Kept in the bucket, one object per part, served by the worker
# (see deploy/sites/worker.js).
BIG_FILES = [
    "horizon-vfs.bin",
    "horizon-client-threaded_bg.wasm",
    "horizon-client_bg.wasm",
]
PART = 8 * 1024 * 1024
ASSET_LIMIT = 10 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    missing = [name for name in ASSETS_FILES + BIG_FILES if not os.path.isfile(os.path.join(PUBLIC, name))]
    if missing:
        sys.exit(
            "the web build is not in public/: missing "
            + ", ".join(missing)
            + "\n  build it with scripts/build_web.sh and scripts/pack_vfs.py,"
            + " or run scripts/serve_web.py --fetch for the pack (docs/WEB.md)"
        )

    # Only what this script stages is replaced: the seed token a deployment is
    # already using lives beside it and must survive a re-stage.
    shutil.rmtree(ASSETS, ignore_errors=True)
    os.makedirs(ASSETS, exist_ok=True)
    shutil.copy(os.path.join(ROOT, "deploy", "sites", "worker.js"), os.path.join(OUT, "worker.js"))

    for name in ASSETS_FILES:
        source = os.path.join(PUBLIC, name)
        size = os.path.getsize(source)
        if size > ASSET_LIMIT:
            sys.exit(f"{name} is {size} bytes, over the site's 10 MiB asset limit")
        shutil.copy(source, os.path.join(ASSETS, name))

    # The manifest the deployment reads: one module, the staged assets, no build.
    manifest = {
        "entrypoint": "worker.js",
        "compatibilityDate": "2026-11-20",
        "modules": [{"name": "worker.js", "type": "esm", "path": "./worker.js"}],
        "assetsDirectory": "./assets",
    }
    with open(os.path.join(OUT, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    with open(os.path.join(PUBLIC, "assets.json")) as handle:
        build = json.load(handle).get("build", "")

    # The staged manifest: what this deployment actually serves, hash for hash.
    files = {name: sha256(os.path.join(ASSETS, name)) for name in ASSETS_FILES}
    rows = []
    for name in BIG_FILES:
        source = os.path.join(PUBLIC, name)
        size = os.path.getsize(source)
        digest = sha256(source)
        count = -(-size // PART)
        files[name] = {
            "sha256": digest,
            "size": size,
            "parts": [f"__parts/{name}/{number}" for number in range(1, count + 1)],
        }
        rows.append(f"{name}\t{size}\t{digest}")
        print(f"  storage  {name:<34} {size:>10} bytes in {count} part(s)")
    with open(os.path.join(OUT, "big.txt"), "w") as handle:
        handle.write("\n".join(rows) + "\n")
    with open(os.path.join(ASSETS, "assets.json"), "w") as handle:
        json.dump({"build": build, "files": files}, handle, indent=2)
        handle.write("\n")

    for name in ASSETS_FILES:
        print(f"  asset    {name:<34} {os.path.getsize(os.path.join(ASSETS, name)):>10} bytes")
    print(f"staged {OUT}")
    print(f"deploy {os.path.join(OUT, 'manifest.json')}")


if __name__ == "__main__":
    main()
