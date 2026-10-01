"""Frontend-only local preview: serve frontend/ and pass API reads through to production.

Lets frontend changes be tried against real events without Docker or a local database.
The browser only ever talks to localhost -- this server fetches /api/* and /feeds/* from
production on its behalf -- so every request the page makes is same-origin and
production's CORS allowlist, which deliberately excludes localhost, never applies.

Read-only by construction: only GET and HEAD are forwarded, so nothing here can write to
production. Admin pages won't work through it (they sit behind Cloudflare Access).

    python tools/preview_server.py                  # http://localhost:8090
    python tools/preview_server.py --port 8091
    python tools/preview_server.py --upstream http://localhost:8000

The Durham variant is http://durm-shows.localhost:8090 -- browsers resolve *.localhost
to loopback, and frontend/js/config.js picks the variant up from the hostname.
"""
import argparse
import http.server
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
PROXIED_PREFIXES = ("/api/", "/feeds/")


def make_handler(upstream):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FRONTEND), **kwargs)

        def do_GET(self):
            if self.path.startswith(PROXIED_PREFIXES):
                self._proxy(send_body=True)
            else:
                super().do_GET()

        def do_HEAD(self):
            if self.path.startswith(PROXIED_PREFIXES):
                self._proxy(send_body=False)
            else:
                super().do_HEAD()

        def _proxy(self, send_body):
            req = urllib.request.Request(
                upstream + self.path,
                method="GET",
                headers={"User-Agent": "triangle-shows-preview", "Accept": self.headers.get("Accept", "*/*")},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status, headers, body = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as err:
                status, headers, body = err.code, err.headers, err.read()
            except (urllib.error.URLError, TimeoutError) as err:
                print(f"  upstream unreachable for {self.path}: {err}", flush=True)
                self.send_error(502, f"Upstream unreachable: {err}")
                return
            print(f"  proxied {self.path} -> {status} ({len(body):,} bytes)", flush=True)
            self.send_response(status)
            self.send_header("Content-Type", headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8090, help="local port (default 8090)")
    parser.add_argument("--upstream", default="https://triangle-shows.net",
                        help="where API reads go (default production)")
    args = parser.parse_args()

    if not (FRONTEND / "index.html").exists():
        sys.exit(f"frontend/index.html not found under {FRONTEND}")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), make_handler(args.upstream.rstrip("/"))) as httpd:
        print(f"Serving {FRONTEND}", flush=True)
        print(f"  site:    http://localhost:{args.port}", flush=True)
        print(f"  durham:  http://durm-shows.localhost:{args.port}", flush=True)
        print(f"  API ->   {args.upstream} (GET/HEAD only)", flush=True)
        print("Ctrl+C to stop.", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
