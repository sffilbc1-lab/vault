"""Vault dashboard adapter.

Serves the dashboard UI and relays requests to an existing, unmodified Vault
deployment. It exists because a browser page cannot talk to Vault directly:

* the gateway API sends no CORS headers (it answers OPTIONS with 501), and
* per-node stats (/health) and demo fault injection (/admin/*) live on the
  storage nodes, which are separate HTTP servers.

The adapter adds no storage logic. Everything it returns comes from existing
Vault endpoints, except two things it observes itself and labels as such:

* ``activity``: operations performed *through this dashboard* (uploads,
  downloads, deletes, fault injections, ...). The Vault backend does not log
  object operations, so these are recorded here, with ``source: "dashboard"``.
* ``verifications``: results of end-to-end integrity checks the user triggered
  (download the object through the gateway, recompute SHA-256, compare to the
  ETag Vault recorded at write time).

Access requires login (see dashboard/auth.py). Viewers get read-only access;
admins can also use the dashboard's administrative and demo controls. The
adapter forwards only the gateway operations the dashboard itself uses;
everything else is refused for everyone.

Run:  python3 dashboard/server.py --gateway http://127.0.0.1:8080 --port 8090 \
          --users ~/.vault-dashboard/users.json
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import random
import signal
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

try:
    from .auth import Auth, UserStore  # imported as package (dashboard.server)
except ImportError:
    from auth import Auth, UserStore   # run as a script from dashboard/

STATIC = Path(__file__).resolve().parent / "static"
CLIENT_ID = "vault-dashboard"
# Response headers from the gateway worth passing through to the browser.
PASS_HEADERS = ("Content-Type", "Content-Length", "ETag", "X-Vault-Version")


class UpstreamError(Exception):
    pass


def _request(host: str, port: int, method: str, path: str, body=None, headers=None,
             timeout: float = 5.0) -> tuple[int, dict, bytes]:
    """One HTTP request with a fully buffered response. Always closes the socket."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers={"X-Client-Id": CLIENT_ID,
                                                        **(headers or {})})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise UpstreamError(f"{host}:{port} {type(e).__name__}: {e}") from None
    finally:
        conn.close()


def _split_addr(addr: str) -> tuple[str, int]:
    host, port = addr.rsplit(":", 1)
    return host, int(port)


class _LimitedReader:
    """File-like view over a request body limited to Content-Length (streamed upstream)."""

    def __init__(self, rfile, length: int):
        self.rfile, self.left = rfile, length

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n < 0 else min(n, self.left)
        data = self.rfile.read(n)
        self.left -= len(data)
        return data


class Adapter:
    def __init__(self, gateway: str, node_timeout: float = 1.0, gateway_timeout: float = 120.0):
        u = urlsplit(gateway if "://" in gateway else f"http://{gateway}")
        self.gateway_url = f"{u.scheme}://{u.hostname}:{u.port or 80}"
        self.gw_host, self.gw_port = u.hostname, u.port or 80
        self.node_timeout = node_timeout
        self.gateway_timeout = gateway_timeout
        self.started = time.time()
        self._pool = ThreadPoolExecutor(16, thread_name_prefix="dash-probe")
        self._lock = threading.Lock()
        self._activity: deque[dict] = deque(maxlen=500)
        self._verifications: dict[str, dict] = {}
        self._last_maintenance: dict | None = None
        self._actor = threading.local()  # who is making the current request (per thread)

    def set_actor(self, username: str | None) -> None:
        self._actor.name = username

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)

    # -- dashboard-observed state -----------------------------------------------

    def record(self, kind: str, **detail) -> None:
        user = getattr(self._actor, "name", None)
        with self._lock:
            self._activity.appendleft({"ts": time.time(), "kind": kind, "source": "dashboard",
                                       **({"user": user} if user else {}), **detail})

    def activity(self) -> list[dict]:
        with self._lock:
            return list(self._activity)

    # -- gateway -----------------------------------------------------------------

    def gw(self, method: str, path: str, body=None, headers=None, timeout=None):
        return _request(self.gw_host, self.gw_port, method, path, body, headers,
                        timeout or self.gateway_timeout)

    def gw_json(self, path: str, timeout: float = 5.0):
        status, _, data = self.gw("GET", path, timeout=timeout)
        if status != 200:
            raise UpstreamError(f"gateway {path} -> HTTP {status}: {data[:200]!r}")
        return json.loads(data)

    def cluster_nodes(self) -> list[dict]:
        return self.gw_json("/cluster")["nodes"]

    def node(self, node_id: str) -> dict:
        for n in self.cluster_nodes():
            if n["id"] == node_id:
                return n
        raise KeyError(node_id)

    # -- storage nodes (existing node HTTP API) -----------------------------------

    def node_live(self, node: dict) -> dict:
        """Ask a node directly for its own view: /health and current injected faults."""
        host, port = _split_addr(node["addr"])
        out: dict = {"reachable": False}
        t = time.time()
        try:
            status, _, data = _request(host, port, "GET", "/health", timeout=self.node_timeout)
            if status == 200:
                out.update(reachable=True, health=json.loads(data))
            else:
                out["error"] = f"HTTP {status}"
        except UpstreamError as e:
            out["error"] = str(e)
        out["latency_ms"] = round((time.time() - t) * 1000, 1)
        try:  # /admin/* answers even while a node simulates being down
            status, _, data = _request(host, port, "POST", "/admin/faults", b"{}",
                                       {"Content-Type": "application/json"},
                                       timeout=self.node_timeout)
            if status == 200:
                out["faults"] = json.loads(data)
        except UpstreamError:
            pass
        return out

    def set_faults(self, node_id: str, faults: dict) -> dict:
        allowed = {"down", "latency", "fail_writes", "corrupt_reads"}
        spec = {k: v for k, v in faults.items() if k in allowed}
        host, port = _split_addr(self.node(node_id)["addr"])
        status, _, data = _request(host, port, "POST", "/admin/faults",
                                   json.dumps(spec).encode(),
                                   {"Content-Type": "application/json"},
                                   timeout=self.node_timeout)
        if status != 200:
            raise UpstreamError(f"node {node_id} faults -> HTTP {status}")
        self.record("fault_injected", node=node_id, faults=spec)
        return json.loads(data)

    def corrupt_random(self, node_id: str) -> dict:
        host, port = _split_addr(self.node(node_id)["addr"])
        status, _, data = _request(host, port, "GET", "/inventory", timeout=5.0)
        if status != 200:
            raise UpstreamError(f"node {node_id} inventory -> HTTP {status}")
        inv = json.loads(data)
        if not inv:
            return {"corrupted": None, "reason": "node stores no blobs"}
        key = random.choice(inv)["key"]
        status, _, _ = _request(host, port, "POST", f"/admin/corrupt/{key}", b"",
                                timeout=self.node_timeout)
        if status != 200:
            raise UpstreamError(f"node {node_id} corrupt -> HTTP {status}")
        self.record("replica_corrupted", node=node_id, blob=key)
        return {"corrupted": key, "node": node_id}

    # -- aggregate snapshot ---------------------------------------------------------

    def snapshot(self, event_limit: int = 300) -> dict:
        t = time.time()
        snap: dict = {"fetched_at": t, "gateway": {"url": self.gateway_url, "ok": False},
                      "adapter_started": self.started}
        try:
            cluster = self.gw_json("/cluster")
            snap["gateway"]["ok"] = True
        except (UpstreamError, ValueError) as e:
            snap["gateway"]["error"] = str(e)
            snap.update(activity=self.activity(), verifications=self._verifications_copy())
            return snap
        snap["gateway"]["latency_ms"] = round((time.time() - t) * 1000, 1)
        snap["cluster"] = cluster
        futures = {n["id"]: self._pool.submit(self.node_live, n) for n in cluster["nodes"]
                   if n["admin"] != "removed"}
        for key, path in (("buckets", "/buckets"),
                          ("events", f"/cluster/events?limit={event_limit}")):
            try:
                snap[key] = self.gw_json(path)
            except (UpstreamError, ValueError) as e:
                snap[key] = None
                snap.setdefault("errors", {})[key] = str(e)
        snap["nodes_live"] = {nid: f.result() for nid, f in futures.items()}
        snap["activity"] = self.activity()
        snap["verifications"] = self._verifications_copy()
        with self._lock:
            snap["last_maintenance"] = self._last_maintenance
        return snap

    def _verifications_copy(self) -> dict:
        with self._lock:
            return dict(self._verifications)

    # -- end-to-end verification ------------------------------------------------------

    def verify(self, bucket: str, key: str) -> dict:
        """Download through the gateway, recompute SHA-256, compare with the stored ETag."""
        path = f"/buckets/{_q(bucket)}/{_q(key)}"
        t = time.time()
        conn = http.client.HTTPConnection(self.gw_host, self.gw_port,
                                          timeout=self.gateway_timeout)
        result = {"bucket": bucket, "key": key, "ts": t}
        try:
            conn.request("GET", path, headers={"X-Client-Id": CLIENT_ID})
            resp = conn.getresponse()
            if resp.status != 200:
                body = resp.read()
                result.update(ok=False, error=f"HTTP {resp.status}: {body[:200].decode(errors='replace')}")
            else:
                expected = (resp.getheader("ETag") or "").strip('"')
                length = int(resp.getheader("Content-Length") or -1)
                h, n = hashlib.sha256(), 0
                while True:
                    block = resp.read(1 << 20)
                    if not block:
                        break
                    h.update(block)
                    n += len(block)
                actual = h.hexdigest()
                ok = actual == expected and (length < 0 or n == length)
                result.update(ok=ok, expected=expected, actual=actual, bytes=n,
                              version=int(resp.getheader("X-Vault-Version") or 0))
                if not ok:
                    result["error"] = "checksum mismatch" if actual != expected else "truncated"
        except (OSError, http.client.HTTPException) as e:
            result.update(ok=False, error=f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        result["ms"] = round((time.time() - t) * 1000, 1)
        with self._lock:
            self._verifications[f"{bucket}/{key}"] = result
        self.record("object_verified", bucket=bucket, key=key, ok=result["ok"],
                    error=result.get("error"))
        return result

    def maintenance_ran(self, stats: dict) -> None:
        with self._lock:
            self._last_maintenance = {"ts": time.time(), "stats": stats}
        self.record("maintenance_pass", stats=stats)


def _q(s: str) -> str:
    return quote(s, safe="")


# ---------------------------------------------------------------- access control
PUBLIC = "public"
VIEWER = "viewer"
ADMIN = "admin"
DENY = "deny"
RANK = {VIEWER: 1, ADMIN: 2}
LOGIN_ASSETS = {"/login", "/static/login.html", "/static/login.js", "/static/app.css"}


def required_role(method: str, path: str) -> str | None:
    """Minimum role for a request, DENY for things the dashboard never offers, None = unknown."""
    if method in ("GET", "HEAD") and path in LOGIN_ASSETS:
        return PUBLIC
    if path == "/api/login":
        return PUBLIC if method == "POST" else DENY
    if path == "/api/logout":
        return VIEWER if method == "POST" else DENY
    if path in ("/api/session", "/api/snapshot"):
        return VIEWER if method == "GET" else DENY
    if path.startswith("/api/verify/"):
        return VIEWER if method == "POST" else DENY
    if path.startswith("/api/nodes/"):
        _, _, action = path[len("/api/nodes/"):].partition("/")
        return ADMIN if method == "POST" and action in ("faults", "corrupt-random") else DENY
    if path.startswith("/api/gw/"):
        return gateway_role(method, path[len("/api/gw"):])
    if path.startswith("/api/"):
        return None
    return VIEWER if method in ("GET", "HEAD") else DENY  # the dashboard app itself


def gateway_role(method: str, gw_path: str) -> str:
    """Only the gateway operations the dashboard UI uses are forwarded."""
    if any(unquote(seg) in ("", ".", "..") for seg in gw_path.strip("/").split("/")):
        return DENY  # no dot-segments or empty segments, encoded or not
    parts = gw_path.strip("/").split("/", 2)
    if parts[0] == "buckets":
        if len(parts) == 1 or (len(parts) == 2 and parts[1]):
            if method in ("GET", "HEAD"):
                return VIEWER                     # list buckets / list objects
            if method == "PUT" and len(parts) == 2:
                return ADMIN                      # create bucket
        elif len(parts) == 3 and parts[1] and parts[2]:
            if method in ("GET", "HEAD"):
                return VIEWER                     # download
            if method in ("PUT", "DELETE"):
                return ADMIN                      # upload / delete object
    if method == "POST" and parts == ["cluster", "maintenance"]:
        return ADMIN
    if method == "POST" and len(parts) == 3 and parts[:2] == ["cluster", "nodes"] and parts[2].endswith("/drain") \
            and parts[2].count("/") == 1:
        return ADMIN
    return DENY  # e.g. node remove/add, bucket delete, raw cluster status


def for_viewer(snap: dict) -> dict:
    """Remove detailed records (addresses, fault state, errors, who did what) for viewers."""
    s = copy.deepcopy(snap)
    gw = s.get("gateway", {})
    s["gateway"] = {k: gw[k] for k in ("ok", "latency_ms") if k in gw}
    if not gw.get("ok"):
        s["gateway"]["error"] = "gateway unavailable"
    for n in (s.get("cluster") or {}).get("nodes", []):
        n.pop("addr", None)
    s["nodes_live"] = {nid: {k: v for k, v in live.items() if k in ("reachable", "latency_ms", "health")}
                       for nid, live in (s.get("nodes_live") or {}).items()}
    for live in s["nodes_live"].values():
        if isinstance(live.get("health"), dict):
            live["health"] = {k: live["health"][k] for k in ("blobs", "bytes") if k in live["health"]}
    s["activity"] = [{k: v for k, v in a.items() if k != "user"} for a in s.get("activity", [])]
    # Vault's own events carry node addresses too (e.g. node_added)
    if isinstance(s.get("events"), list):
        s["events"] = [{k: v for k, v in e.items() if k != "addr"} for e in s["events"]]
    s.pop("errors", None)
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = "VaultDashboard/1.0"
    protocol_version = "HTTP/1.1"
    adapter: Adapter
    auth: Auth
    frame_ancestors: str = "'none'"
    session = None
    role: str | None = None

    def log_message(self, *args):
        pass

    # -- helpers --------------------------------------------------------------------

    def _body_pending(self) -> bool:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 1
        return length > 0 and not self._consumed

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        # A request body we didn't read would otherwise be parsed as the next request on
        # this keep-alive connection. Close it instead.
        if self._body_pending():
            self.close_connection = True
            extra = {**(extra or {}), "Connection": "close"}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; form-action 'self'; base-uri 'none'; "
                         f"frame-ancestors {self.frame_ancestors}")

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        self._consumed = True
        return json.loads(data or b"{}")

    # -- routing --------------------------------------------------------------------

    def _dispatch(self):
        url = urlsplit(self.path)
        path = url.path
        self.adapter.set_actor(None)
        self.session, self.role, self._consumed = None, None, False
        try:
            need = required_role(self.command, path)
            if need is None:
                return self._json(404, {"error": "not found"})
            if need == DENY:
                return self._json(403, {"error": "not available through the dashboard"})
            if need != PUBLIC:
                resolved = self.auth.resolve(Auth.token_from(self.headers.get("Cookie")))
                if resolved is None:
                    if self.command == "GET" and path in ("/", "/index.html", "/static/index.html"):
                        return self._send(302, b"", "text/plain", {"Location": "/login"})
                    return self._json(401, {"error": "login required"})
                self.session, self.role = resolved
                if RANK[self.role] < RANK[need]:
                    return self._json(403, {"error": "admin access required"})
                if self.command not in ("GET", "HEAD") and not self._csrf_ok():
                    return self._json(403, {"error": "missing or invalid CSRF token"})
                self.adapter.set_actor(self.session.username)
            if path == "/login":
                return self._static("/login.html")
            if path == "/api/login":
                return self._login()
            if path == "/api/logout":
                self.auth.logout(self.session.token)
                return self._send(200, b'{"ok": true}', "application/json",
                                  {"Set-Cookie": self.auth.clear_cookie()})
            if path == "/api/session":
                return self._json(200, {"username": self.session.username, "role": self.role,
                                        "csrf": self.session.csrf})
            if path.startswith("/api/gw/"):
                return self._proxy(path[len("/api/gw"):], url.query)
            if path == "/api/snapshot" and self.command == "GET":
                snap = self.adapter.snapshot()
                return self._json(200, snap if self.role == ADMIN else for_viewer(snap))
            if path.startswith("/api/verify/") and self.command == "POST":
                bucket, _, key = path[len("/api/verify/"):].partition("/")
                return self._json(200, self.adapter.verify(unquote(bucket), unquote(key)))
            if path.startswith("/api/nodes/") and self.command == "POST":
                node_id, _, action = path[len("/api/nodes/"):].partition("/")
                node_id = unquote(node_id)
                if action == "faults":
                    return self._json(200, self.adapter.set_faults(node_id, self._read_json()))
                if action == "corrupt-random":
                    return self._json(200, self.adapter.corrupt_random(node_id))
                return self._json(404, {"error": "unknown node action"})
            if self.command in ("GET", "HEAD"):
                return self._static(path)
            return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(404, {"error": f"unknown node {e}"})
        except UpstreamError as e:
            return self._json(502, {"error": "upstream unavailable", "detail": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            self.adapter.set_actor(None)

    do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _dispatch

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == self.headers.get("Host", "")

    def _csrf_ok(self) -> bool:
        sent = self.headers.get("X-CSRF-Token") or ""
        return self._same_origin() and bool(sent) and hmac.compare_digest(sent, self.session.csrf)

    def _login(self):
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin login refused"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "expected application/json"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= 4096:
            self.close_connection = True
            return self._json(400, {"error": "bad request"})
        raw = self.rfile.read(n)
        self._consumed = True
        try:
            body = json.loads(raw)
        except ValueError:
            return self._json(400, {"error": "bad request"})
        username = body.get("username") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(username, str) or not isinstance(password, str):
            return self._json(400, {"error": "username and password required"})
        ip = self.client_address[0]
        if self.auth.throttled(ip, username):
            return self._json(429, {"error": "Too many failed attempts. Try again in a few minutes."})
        session = self.auth.login(username, password, ip)
        if session is None:
            return self._json(401, {"error": "Invalid username or password."})
        role = self.auth.users.get(username)["role"]
        body = json.dumps({"username": username, "role": role, "csrf": session.csrf}).encode()
        return self._send(200, body, "application/json", {"Set-Cookie": self.auth.cookie(session.token)})

    def _static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        target = (STATIC / rel).resolve()
        if STATIC not in target.parents or not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _proxy(self, gw_path: str, query: str):
        """Relay a request to the gateway, streaming both request and response bodies."""
        params = parse_qsl(query, keep_blank_values=True)
        download = any(k == "dl" for k, _ in params)
        params = [(k, v) for k, v in params if k != "dl"]
        target = gw_path + (f"?{urlencode(params)}" if params else "")
        headers = {"X-Client-Id": CLIENT_ID}
        for h in ("Content-Type", "If-Match", "If-None-Match"):
            if self.headers.get(h):
                headers[h] = self.headers[h]
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if self.command in ("PUT", "POST"):
            headers["Content-Length"] = str(length)
            body = _LimitedReader(self.rfile, length)
            self._consumed = True  # streamed upstream (or the connection is closed on error)

        a = self.adapter
        conn = http.client.HTTPConnection(a.gw_host, a.gw_port, timeout=a.gateway_timeout)
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for h in PASS_HEADERS:
                if resp.getheader(h) is not None:
                    self.send_header(h, resp.getheader(h))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            parts = [unquote(p) for p in gw_path.strip("/").split("/", 2)]
            is_object = parts[0] == "buckets" and len(parts) == 3
            if download and is_object:
                fname = parts[2].rsplit("/", 1)[-1].replace('"', "")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            if resp.getheader("Content-Length") is None:
                self.close_connection = True
            self.end_headers()
            small = b""
            sent = 0
            while True:
                block = resp.read(1 << 16)
                if not block:
                    break
                if self.command != "HEAD":
                    self.wfile.write(block)
                sent += len(block)
                if len(small) < 65536 and not is_object:
                    small += block
            self._observe(parts, resp.status, sent, small)
        except (OSError, http.client.HTTPException) as e:
            self.close_connection = True  # the request body may be partly unread
            if not self.wfile.closed:
                try:
                    self._json(502, {"error": "gateway unavailable", "detail": str(e)})
                except OSError:
                    self.close_connection = True
        finally:
            conn.close()

    def _observe(self, parts: list[str], status: int, sent: int, body: bytes):
        """Record operations that the backend does not log itself."""
        a, m = self.adapter, self.command
        ok = 200 <= status < 300
        if parts[0] == "buckets" and len(parts) == 3:
            bucket, key = parts[1], parts[2]
            if m == "PUT":
                a.record("object_uploaded" if ok else "upload_failed", bucket=bucket, key=key,
                         status=status)
            elif m == "GET":
                a.record("object_downloaded" if ok else "download_failed", bucket=bucket,
                         key=key, bytes=sent, status=status)
            elif m == "DELETE":
                a.record("object_deleted" if ok else "delete_failed", bucket=bucket, key=key,
                         status=status)
        elif parts[0] == "buckets" and len(parts) == 2 and m == "PUT":
            a.record("bucket_created" if ok else "bucket_create_failed", bucket=parts[1],
                     status=status)
        elif parts[:2] == ["cluster", "maintenance"] and m == "POST" and ok:
            try:
                a.maintenance_ran(json.loads(body))
            except ValueError:
                pass
        elif parts[:2] == ["cluster", "nodes"] and len(parts) == 3 and m == "POST":
            node_id, _, action = parts[2].partition("/")
            a.record(f"node_{action}_requested", node=node_id, status=status)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # browsers abort requests (navigation, polling) all the time


def make_server(gateway: str, host: str = "127.0.0.1", port: int = 8090, *, users,
                secure_cookie: bool = False, frame_ancestors=(), idle_timeout: float = 30 * 60,
                max_age: float = 12 * 3600) -> tuple[_Server, Adapter]:
    """Build the dashboard server. ``users`` is a users file path or UserStore (required)."""
    store = users if isinstance(users, UserStore) else UserStore(users)
    if store.count() == 0:
        raise ValueError(f"no dashboard users in {store.path}; add one with: "
                         f"python3 dashboard/auth.py --users {store.path} add NAME --role admin")
    auth = Auth(store, idle_timeout=idle_timeout, max_age=max_age, secure_cookie=secure_cookie)
    adapter = Adapter(gateway)
    ancestors = " ".join(["'self'", *frame_ancestors])
    handler = type("DashboardHandler", (Handler,), {"adapter": adapter, "auth": auth,
                                                    "frame_ancestors": ancestors})
    return _Server((host, port), handler), adapter


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Vault dashboard")
    p.add_argument("--gateway", default="http://127.0.0.1:8080",
                   help="URL of a running Vault gateway")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--users", default=os.environ.get("VAULT_DASHBOARD_USERS"),
                   help="users file (or VAULT_DASHBOARD_USERS); manage it with dashboard/auth.py")
    p.add_argument("--secure-cookie", action="store_true",
                   help="mark the session cookie Secure (use when served over HTTPS)")
    p.add_argument("--frame-ancestor", action="append", default=[],
                   help="origin allowed to embed the dashboard in an iframe (repeatable)")
    args = p.parse_args(argv)
    if not args.users:
        p.error("--users is required (see dashboard/auth.py to create users)")
    try:
        server, adapter = make_server(args.gateway, args.host, args.port, users=args.users,
                                      secure_cookie=args.secure_cookie,
                                      frame_ancestors=args.frame_ancestor)
    except ValueError as e:
        p.error(str(e))
    print(f"Vault dashboard on http://{args.host}:{server.server_address[1]}  "
          f"(gateway {adapter.gateway_url})", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    server.server_close()
    adapter.close()


if __name__ == "__main__":
    main()
