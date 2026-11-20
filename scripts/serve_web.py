#!/usr/bin/env python3
"""Static host for the Horizon web build: `python3 scripts/serve_web.py [port]`.

The page needs cross-origin isolation headers (WebGPU + SharedArrayBuffer) and
`application/wasm`; the engine is a 40 MB wasm module and the resource pack a
33 MB archive, so text assets are also served gzip-compressed (a `.gz` next to
the file, written on first request).

`--fetch` downloads the resource pack (a build output, not in the branch) from
the release before serving, so a fresh clone needs one command.
"""
import functools
import gzip
import http.server
import os
import socketserver
import sys
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public")
PACK = os.path.join(ROOT, "horizon-vfs.bin")
PACK_MAGIC = b"HZVFS001"
MIN_PACK = 50 * 1000 * 1000
PACK_URL = os.environ.get(
    "HORIZON_VFS_URL",
    "https://github.com/Joercat/mc-rust/releases/download/"
    "web-assets-2026-11-01/horizon-vfs.bin",
)


def pack_present():
    """A complete pack, not a placeholder or a half transfer."""
    try:
        with open(PACK, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            return size >= MIN_PACK and f.read(len(PACK_MAGIC)) == PACK_MAGIC
    except OSError:
        return False


def fetch_pack():
    """Download the pack into place, renamed only once it is complete."""
    if pack_present():
        print(f"resource pack already here: {PACK} ({os.path.getsize(PACK) // 1000000} MB)")
        return
    part = PACK + ".part"
    print(f"downloading the resource pack (~400 MB, once) from {PACK_URL}", flush=True)
    try:
        with urllib.request.urlopen(PACK_URL) as src, open(part, "wb") as dst:
            advertised = src.headers.get("Content-Length")
            total = int(advertised) if advertised else 0
            done = 0
            mark = 64 << 20
            while chunk := src.read(1 << 20):
                dst.write(chunk)
                done += len(chunk)
                if done >= mark:
                    percent = f", {done * 100 // total}%" if total else ""
                    print(f"  {done // 1000000} MB{percent}", flush=True)
                    mark += 64 << 20
    except OSError as error:
        sys.exit(f"download failed: {error}\n  Set HORIZON_VFS_URL to another pack URL, or put the pack at {PACK} yourself.")
    with open(part, "rb") as f:
        magic = f.read(len(PACK_MAGIC))
    size = os.path.getsize(part)
    if magic != PACK_MAGIC or size < MIN_PACK:
        os.unlink(part)
        sys.exit(
            f"{PACK_URL} did not return a resource pack "
            f"({size} bytes, starts {magic!r}); nothing was kept.\n"
            "  Point HORIZON_VFS_URL at the real asset, or put the pack there yourself."
        )
    os.replace(part, PACK)
    print(f"resource pack ready: {PACK} ({size // 1000000} MB)", flush=True)

# Everything a browser can gzip quickly enough to be worth it.
COMPRESSIBLE = {".wasm", ".js", ".mjs", ".html", ".css", ".json", ".txt", ".wgsl"}
MIN_COMPRESS = 8 * 1024


def compressed(path):
    """The gzip sibling of `path`, made on first use; None when not worth it."""
    if os.path.getsize(path) < MIN_COMPRESS:
        return None
    cache = path + ".gz"
    if not os.path.exists(cache) or os.path.getmtime(cache) < os.path.getmtime(path):
        with open(path, "rb") as src, gzip.open(cache, "wb", compresslevel=6) as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
    return cache


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".wasm": "application/wasm",
        ".bin": "application/octet-stream",
        ".json": "application/json",
        ".css": "text/css",
        ".html": "text/html",
    }

    def end_headers(self):
        # The preview iframe embeds this page from another origin.
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        self.send_file(head_only=False)

    def do_HEAD(self):
        self.send_file(head_only=True)

    def send_file(self, head_only):
        # The platform's preview bootstrap (…/__freebuff_preview?ticket=…) is
        # forwarded here by the preview edge. The ticket is checked at the edge,
        # so answer with the app itself: a 404 here leaves the panel's tab on an
        # error page instead of the game.
        if self.path.split("?", 1)[0].rstrip("/").endswith("__freebuff_preview"):
            self.path = "/"
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().do_HEAD() if head_only else super().do_GET()
        if not os.path.isfile(path):
            return super().do_HEAD() if head_only else super().do_GET()
        accept = self.headers.get("Accept-Encoding", "")
        served = path
        if "gzip" in accept and os.path.splitext(path)[1].lower() in COMPRESSIBLE:
            try:
                served = compressed(path) or path
            except OSError:
                served = path
        with open(served, "rb") as body:
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Length", str(os.fstat(body.fileno()).st_size))
            if served != path:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            if not head_only:
                while chunk := body.read(1 << 20):
                    self.wfile.write(chunk)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


USAGE = "usage: serve_web.py [port] [--fetch]"


def main():
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(USAGE)
        print("  --fetch   download public/horizon-vfs.bin from the release if it is missing")
        return
    fetch = "--fetch" in args
    args = [arg for arg in args if arg != "--fetch"]
    if len(args) > 1:
        sys.exit(USAGE)
    if args and not args[0].isdigit():
        sys.exit(USAGE)
    port = int(args[0]) if args else 5173
    if fetch:
        fetch_pack()
    elif not pack_present():
        print(
            f"no resource pack at {PACK}: the page will stop and say so.\n"
            "  Run again with --fetch to download it, or put it there yourself.",
            flush=True,
        )
    handler = functools.partial(Handler, directory=ROOT)
    socketserver.TCPServer.allow_reuse_address = True
    # The page opens ~8 sockets at once (wasm + glue + 6 workers); the
    # default backlog of 5 drops them and stalls a reload.
    socketserver.ThreadingTCPServer.request_queue_size = 128
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), handler) as httpd:
        print(f"Horizon web build on http://0.0.0.0:{port}/ (serving {ROOT})", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
