"""HTTP API for the gateway (S3-flavoured, JSON for control operations).

  PUT    /buckets/{b}                  body: policy JSON (optional)   create bucket
  GET    /buckets                                                     list buckets
  DELETE /buckets/{b}                                                 delete empty bucket
  GET    /buckets/{b}?prefix=&after=&limit=                           list objects
  PUT    /buckets/{b}/{key}            body: bytes, If-Match / If-None-Match: *
  GET    /buckets/{b}/{key}                                           streamed response
  HEAD   /buckets/{b}/{key}
  DELETE /buckets/{b}/{key}            If-Match
  GET    /cluster                                                     status
  POST   /cluster/nodes                {id, addr, zone, weight}       add node
  POST   /cluster/nodes/{id}/drain | /remove
  POST   /cluster/maintenance                                         run one pass now
  GET    /cluster/events?kind=&limit=
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .core import Vault
from .errors import (BucketExists, BucketNotEmpty, BucketNotFound, ObjectNotFound,
                     PreconditionFailed, ReadError, RetryableConflict, VaultError,
                     WriteQuorumError)
from .maintenance import Maintenance
from .policy import Policy

_STATUS = [
    (BucketNotFound, 404), (ObjectNotFound, 404), (BucketExists, 409), (BucketNotEmpty, 409),
    (PreconditionFailed, 412), (WriteQuorumError, 503), (ReadError, 503),
    (RetryableConflict, 503), (ValueError, 400), (VaultError, 500),
]


class _Body:
    """File-like view over a request body limited to Content-Length."""

    def __init__(self, rfile, length: int):
        self.rfile, self.left = rfile, length

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n < 0 else min(n, self.left)
        data = self.rfile.read(n)
        self.left -= len(data)
        return data


class _Handler(BaseHTTPRequestHandler):
    server_version = "Vault/1.0"
    vault: Vault
    maint: Maintenance

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, default=str).encode())

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def _if_match(self):
        if self.headers.get("If-None-Match") == "*":
            return 0
        v = self.headers.get("If-Match")
        return int(v.strip('"')) if v else None

    def _dispatch(self):
        try:
            self._route()
        except Exception as e:  # map domain errors onto HTTP status codes
            for cls, code in _STATUS:
                if isinstance(e, cls):
                    return self._json(code, {"error": type(e).__name__, "detail": str(e)})
            return self._json(500, {"error": type(e).__name__, "detail": str(e)})

    def _route(self):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/", 2)]
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        v, m = self.vault, self.command

        if parts[0] == "buckets":
            if len(parts) == 1:
                return self._json(200, v.list_buckets())
            bucket = parts[1]
            if len(parts) == 2:
                if m == "PUT":
                    v.create_bucket(bucket, Policy.from_dict(self._read_json()))
                    return self._json(201, {"bucket": bucket})
                if m == "GET":
                    return self._json(200, v.list_objects(bucket, qs.get("prefix", ""),
                                                          qs.get("after", ""),
                                                          int(qs.get("limit", 1000))))
                if m == "DELETE":
                    v.delete_bucket(bucket)
                    return self._json(200, {"deleted": bucket})
            else:
                key = parts[2]
                if m == "PUT":
                    body = _Body(self.rfile, int(self.headers.get("Content-Length") or 0))
                    om = v.put_object(bucket, key, body, if_match=self._if_match())
                    return self._json(200, {"key": key, "version": om.version, "size": om.size,
                                            "etag": om.etag})
                if m in ("GET", "HEAD"):
                    meta, chunks = v.open_object(bucket, key) if m == "GET" else \
                        (v.head_object(bucket, key), iter(()))
                    # Fetch the first chunk before committing to a 200 so that an
                    # unreadable object yields a proper error status.
                    first = next(chunks, b"")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(meta.size))
                    self.send_header("ETag", f'"{meta.etag}"')
                    self.send_header("X-Vault-Version", str(meta.version))
                    self.end_headers()
                    try:
                        if m == "GET":
                            self.wfile.write(first)
                        for c in chunks:
                            self.wfile.write(c)
                    except VaultError:
                        self.close_connection = True  # headers already sent; truncate
                    return
                if m == "DELETE":
                    return self._json(200, {"deleted": key,
                                            "version": v.delete_object(bucket, key,
                                                                       self._if_match())})
        elif parts[0] == "cluster":
            if len(parts) == 1 and m == "GET":
                return self._json(200, v.status())
            if parts[1:] == ["nodes"] and m == "POST":
                spec = self._read_json()
                v.add_node(spec["id"], spec["addr"], spec.get("zone", "zone-a"),
                           float(spec.get("weight", 1.0)))
                return self._json(201, {"node": spec["id"]})
            if len(parts) == 3 and parts[1] == "nodes" and m == "POST":
                node_id, _, action = parts[2].partition("/")
                if action == "drain":
                    v.drain_node(node_id)
                elif action == "remove":
                    v.remove_node(node_id)
                else:
                    return self._json(404, {"error": "unknown action"})
                return self._json(200, {"node": node_id, "action": action})
            if parts[1:] == ["maintenance"] and m == "POST":
                return self._json(200, dict(self.maint.run_once()))
            if parts[1:] == ["events"] and m == "GET":
                return self._json(200, v.meta.events(qs.get("kind"), int(qs.get("limit", 100))))
        return self._json(404, {"error": "not found"})

    do_GET = do_PUT = do_DELETE = do_POST = do_HEAD = _dispatch


def make_server(vault: Vault, maint: Maintenance, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    handler = type("GatewayHandler", (_Handler,), {"vault": vault, "maint": maint})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
