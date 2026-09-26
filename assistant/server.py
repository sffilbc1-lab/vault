"""Vault website server with the Ask Vault endpoint.

Serves the static website (../website) and adds:

  GET  /api/ask/status   which answer mode is active (never exposes keys)
  POST /api/ask          {"question": str, "context": {...}, "history": [{"q","a"}]}

The assistant only reads. For live questions it fetches the Live Console
adapter's /api/snapshot (read-only) and passes on aggregate numbers only; it
never calls any endpoint that changes Vault.

Run from vault-v2:
  python3 assistant/server.py                     # http://127.0.0.1:8095
  python3 assistant/server.py --port 8095 --console http://127.0.0.1:8090/
AI phrasing is optional; see assistant/provider.py for the ASK_VAULT_* variables.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assistant.engine import Assistant, summarize_snapshot  # noqa: E402
from assistant.provider import from_env  # noqa: E402

SITE = ROOT / "website"
MAX_BODY = 16 * 1024


class RateLimiter:
    """Sliding window per client address."""

    def __init__(self, limit: int = 20, window: float = 60.0):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # bound memory under abuse
                self._hits.clear()
            return True


def make_live_fetch(console_url: str | None, timeout: float = 2.0):
    if not console_url:
        return None
    url = console_url.rstrip("/") + "/api/snapshot"

    def fetch():
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # GET only
                return summarize_snapshot(json.loads(r.read(4_000_000)))
        except (OSError, ValueError, urllib.error.URLError):
            return None
    return fetch


class Handler(BaseHTTPRequestHandler):
    server_version = "VaultSite/1.0"
    protocol_version = "HTTP/1.1"
    assistant: Assistant
    status: dict
    limiter: RateLimiter
    console_origin: str

    def log_message(self, *args):
        pass

    # -- responses ---------------------------------------------------------------
    def _headers(self, ctype: str, length: int, status: int):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if self.close_connection:
            self.send_header("Connection", "close")  # request body left unread
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; "
                         f"connect-src 'self'{' ' + self.console_origin if self.console_origin else ''}; "
                         f"frame-src {self.console_origin or chr(39) + 'none' + chr(39)}; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store"):
        self._headers(ctype, len(body), status)
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj).encode(), "application/json")

    # -- routing -----------------------------------------------------------------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/ask/status":
            return self._json(200, {"available": True, **self.status})
        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        return self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != "/api/ask":
            return self._json(404, {"error": "not found"})
        # Same-origin only: the endpoint isn't a public API for other sites.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlsplit(origin).netloc != host:
            self.close_connection = True  # body unread: don't let it become the next request
            return self._json(403, {"error": "cross-origin requests are not allowed"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.close_connection = True
            return self._json(415, {"error": "expected application/json"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return self._json(400, {"error": "bad length"})
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": "request too large"})
        if not self.limiter.allow(self.client_address[0]):
            self.rfile.read(length)
            return self._json(429, {"error": "Too many questions at once. Please wait a moment."})
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(body, dict) or not isinstance(body.get("question"), str):
            return self._json(400, {"error": "question must be a string"})
        history = body.get("history") if isinstance(body.get("history"), list) else []
        answer = self.assistant.ask(body["question"], body.get("context"), history)
        return self._json(200, answer)

    def _static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (SITE / rel).resolve()
        if SITE not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, cache="no-cache")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def make_server(host="127.0.0.1", port=8095, console="http://127.0.0.1:8090/", env=None,
                provider_override=None):
    provider, status, secrets = from_env(env)
    if provider_override is not None:  # tests
        provider, status = provider_override, {"mode": "ai", "provider": "test"}
    assistant = Assistant(provider, make_live_fetch(console), secrets)
    c = urlsplit(console) if console else None
    # Empty when no console is configured: then connect-src is 'self' only and frame-src 'none'
    # ('none' may not be combined with other sources in a CSP directive).
    console_origin = f"{c.scheme}://{c.netloc}" if c and c.netloc else ""
    public = {**status, "live_console": bool(console)}
    handler = type("SiteHandler", (Handler,), {"assistant": assistant, "status": public,
                                               "limiter": RateLimiter(), "console_origin": console_origin})
    return _Server((host, port), handler), public


def main(argv=None):
    p = argparse.ArgumentParser(description="Vault website + Ask Vault")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8095)
    p.add_argument("--console", default="http://127.0.0.1:8090/",
                   help="Live Console URL used for live answers (read-only); '' to disable")
    args = p.parse_args(argv)
    server, status = make_server(args.host, args.port, args.console or None)
    mode = f"AI ({status.get('model')})" if status["mode"] == "ai" else f"curated answers ({status.get('reason')})"
    print(f"Vault website on http://{args.host}:{server.server_address[1]}  ·  Ask Vault: {mode}", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
