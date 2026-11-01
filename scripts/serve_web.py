#!/usr/bin/env python3
"""Static host for the Horizon web build: `python3 scripts/serve_web.py [port] [--tls]`.

The page needs cross-origin isolation headers (WebGPU + SharedArrayBuffer) and
`application/wasm`; the engine is a 33 MB wasm module and the resource pack a
33 MB archive, so text assets are also served gzip-compressed (a `.gz` next to
the file, written on first request).

Isolation also needs a secure context, and only `localhost` gets one over plain
HTTP: opened from another device as `http://192.168.x.x`, the page is
single-threaded whatever its headers say. `--tls` serves the same files over
HTTPS with a self-signed certificate (made once with `openssl`, kept in
`.web-cert/`), which the browser accepts after a warning.
"""
import argparse
import functools
import gzip
import http.server
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "public")
CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".web-cert")

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


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def addresses():
    """Every name and address this certificate should answer for."""
    names = {"localhost"}
    addrs = {"127.0.0.1"}
    try:
        host = socket.gethostname()
        names.update({host, host + ".local"})
        for info in socket.getaddrinfo(host, None):
            addrs.add(info[4][0])
    except OSError:
        pass
    # The address the default route leaves from: the one other devices reach.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addrs.add(probe.getsockname()[0])
    except OSError:
        pass
    return names, {a for a in addrs if a and a != "0.0.0.0"}


def urls(scheme, port):
    names, addrs = addresses()
    hosts = ["localhost"] + sorted(a for a in addrs if ":" not in a and a != "127.0.0.1")
    return [f"{scheme}://{host}:{port}/" for host in hosts]


def certificate():
    """The self-signed certificate and key, made once in `.web-cert/`."""
    cert = os.path.join(CERT_DIR, "cert.pem")
    key = os.path.join(CERT_DIR, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    openssl = shutil.which("openssl")
    if openssl is None:
        sys.exit("--tls needs openssl to make a certificate; pass --cert and --key instead")
    names, addrs = addresses()
    san = ",".join(
        [f"DNS:{name}" for name in sorted(names)] + [f"IP:{addr}" for addr in sorted(addrs)]
    )
    # A config file rather than -addext: LibreSSL (macOS) has no -addext.
    config = f"""\
[req]
distinguished_name = subject
x509_extensions = extensions
prompt = no
[subject]
CN = {socket.gethostname() or "horizon"}
[extensions]
subjectAltName = {san}
basicConstraints = critical, CA:true
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
"""
    os.makedirs(CERT_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as file:
        file.write(config)
        path = file.name
    try:
        result = subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-days",
                "825",
                "-config",
                path,
                "-keyout",
                key,
                "-out",
                cert,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.unlink(path)
    if result.returncode != 0:
        sys.exit(f"openssl could not make a certificate:\n{result.stderr.strip()}")
    os.chmod(key, 0o600)
    return cert, key


def main():
    parser = argparse.ArgumentParser(description="Host the Horizon web build.")
    parser.add_argument("port", nargs="?", type=int, default=5173)
    parser.add_argument("--tls", action="store_true", help="serve HTTPS with a self-signed certificate")
    parser.add_argument("--cert", help="certificate PEM for --tls (with --key)")
    parser.add_argument("--key", help="private key PEM for --tls (with --cert)")
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    args = parser.parse_args()

    handler = functools.partial(Handler, directory=ROOT)
    with Server((args.bind, args.port), handler) as httpd:
        scheme = "http"
        if args.tls or (args.cert and args.key):
            cert, key = (args.cert, args.key) if args.cert and args.key else certificate()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        port = httpd.server_address[1]
        print(f"Horizon web build serving {ROOT}", flush=True)
        for url in urls(scheme, port):
            print(f"  {url}", flush=True)
        if scheme == "https":
            print("The certificate is self-signed: accept the warning in the browser once.", flush=True)
        elif len(urls(scheme, port)) > 1:
            print("Only http://localhost is a secure context: other addresses lose threads.", flush=True)
            print("Use --tls (and accept the certificate warning) to keep them.", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
