#!/usr/bin/env python3
"""Static host for the Horizon web build: `python3 scripts/serve_web.py [port]`.

The page needs cross-origin isolation headers (WebGPU + SharedArrayBuffer) and
`application/wasm`; the engine is a 40 MB wasm module and the resource pack a
33 MB archive, so text assets are also served gzip-compressed (a `.gz` next to
the file, written on first request).
"""
import functools
import gzip
import http.server
import os
import socketserver
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public")

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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    handler = functools.partial(Handler, directory=ROOT)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), handler) as httpd:
        print(f"Horizon web build on http://0.0.0.0:{port}/ (serving {ROOT})", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
