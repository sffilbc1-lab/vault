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

Run:  python3 dashboard/server.py --gateway http://127.0.0.1:8080 --port 8090
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import mimetypes
import random
import signal
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

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

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)

    # -- dashboard-observed state -----------------------------------------------

    def record(self, kind: str, **detail) -> None:
        with self._lock:
            self._activity.appendleft({"ts": time.time(), "kind": kind, "source": "dashboard",
                                       **detail})

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


class Handler(BaseHTTPRequestHandler):
    server_version = "VaultDashboard/1.0"
    protocol_version = "HTTP/1.1"
    adapter: Adapter

    def log_message(self, *args):
        pass

    # -- helpers --------------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # -- routing --------------------------------------------------------------------

    def _dispatch(self):
        url = urlsplit(self.path)
        path = url.path
        try:
            if path.startswith("/api/gw/"):
                return self._proxy(path[len("/api/gw"):], url.query)
            if path == "/api/snapshot" and self.command == "GET":
                return self._json(200, self.adapter.snapshot())
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

    do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _dispatch

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


def make_server(gateway: str, host: str = "127.0.0.1", port: int = 8090) -> tuple[_Server, Adapter]:
    adapter = Adapter(gateway)
    handler = type("DashboardHandler", (Handler,), {"adapter": adapter})
    return _Server((host, port), handler), adapter


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Vault dashboard")
    p.add_argument("--gateway", default="http://127.0.0.1:8080",
                   help="URL of a running Vault gateway")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    args = p.parse_args(argv)
    server, adapter = make_server(args.gateway, args.host, args.port)
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
