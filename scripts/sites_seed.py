#!/usr/bin/env python3
"""Put the web build's large files into a deployed site's bucket.

    python3 scripts/sites_seed.py https://<site-host> sites-bundle/seed-token.txt

The site's worker serves `/horizon-vfs.bin` and the two wasm modules out of its
bucket (`deploy/sites/worker.js`), because a site takes assets of at most 10 MiB
and these are 33 MB and 126 MB. They are uploaded one 8 MiB part at a time and
stored as one object per part, so no single request has to carry a whole file;
the routes only exist while the deployment carries the `SEED_TOKEN` secret.

The files come from `public/`, and every upload is checked against the size and
SHA-256 the staging step recorded.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PUBLIC = os.path.join(ROOT, "public")
PART = 8 * 1024 * 1024


def request(base, path, token, method="GET", body=None, headers=None):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    # The site's edge answers a bare `Python-urllib` with 403 (Cloudflare 1010,
    # its browser-integrity rule), so this says what it is.
    req.add_header("user-agent", "horizon-sites-seed/1 (scripts/sites_seed.py)")
    req.add_header("accept", "application/json")
    req.add_header("x-seed-token", token)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()
        raise SystemExit(f"{method} {path}: HTTP {error.code} {detail}") from None
    except urllib.error.URLError as error:
        raise SystemExit(f"{method} {path}: {error.reason}") from None


def seed(base, token, name, path, size, want_sha):
    print(f"{name}: {size // 1000000} MB in {-(-size // PART)} part(s)", flush=True)
    request(base, f"/__seed/reset?key={name}", token, method="POST", body=b"")
    number = 0
    done = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(PART):
            number += 1
            done += len(chunk)
            answer = request(
                base,
                f"/__seed/part?key={name}&partNumber={number}",
                token,
                method="PUT",
                body=chunk,
                headers={"content-type": "application/octet-stream"},
            )
            if answer.get("size") != len(chunk):
                raise SystemExit(f"{name} part {number}: stored {answer.get('size')} of {len(chunk)} bytes")
            print(f"  {name}: {done * 100 // size}% ({number})", flush=True)
    stored = request(base, f"/__seed/finish?key={name}", token, method="POST", body=b"")
    if stored.get("size") != size or stored.get("parts") != number:
        raise SystemExit(f"{name}: the site holds {stored.get('parts')} part(s), {stored.get('size')} bytes; expected {number}, {size}")
    print(f"{name}: stored, {size} bytes, sha256 {want_sha[:16]}…", flush=True)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: sites_seed.py <site-base-url> <token-file>")
    base, token_file = sys.argv[1], sys.argv[2]
    with open(token_file) as handle:
        token = handle.read().strip()
    if not token:
        sys.exit(f"{token_file} is empty")
    with open(os.path.join(ROOT, "sites-bundle", "big.txt")) as handle:
        rows = [line.split("\t") for line in handle.read().splitlines() if line]
    for name, size, digest in rows:
        path = os.path.join(PUBLIC, name)
        actual = hashlib.sha256()
        with open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            sys.exit(f"{name} changed since it was staged; run scripts/sites_bundle.py again")
        seed(base, token, name, path, int(size), digest)
    print("all files stored")


if __name__ == "__main__":
    main()
