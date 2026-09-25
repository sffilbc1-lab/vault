"""Storage node: a dumb, self-verifying blob store exposed over HTTP.

Nodes know nothing about objects, buckets or placement.  They store immutable
blobs under opaque keys, and every blob carries its SHA-256 in an on-disk header
so the node can detect bit rot on every read and during background scrubs.
Corrupted blobs are moved to ``quarantine/`` (never silently served), which makes
them look missing to the cluster and triggers repair.

Writes are crash-safe: payload + header go to a temp file which is fsync'd and
atomically renamed into place.

For testing and chaos experiments each node also exposes fault injection
(``/admin/faults``): simulated crash, per-client partitions, latency, failed
writes and in-flight corruption.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .errors import BlobCorrupt, BlobNotFound, ChecksumMismatch

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
MAGIC = b"VLT1"
HEADER = len(MAGIC) + 32


class BlobStore:
    def __init__(self, root: str | Path, fsync: bool = True):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.tmp = self.root / "tmp"
        self.quarantine_dir = self.root / "quarantine"
        for d in (self.blobs, self.tmp, self.quarantine_dir):
            d.mkdir(parents=True, exist_ok=True)
        for leftover in self.tmp.iterdir():  # torn writes from a previous crash
            leftover.unlink(missing_ok=True)
        self.fsync = fsync
        self._locks = [threading.Lock() for _ in range(64)]

    def _lock(self, key: str) -> threading.Lock:
        return self._locks[hash(key) % len(self._locks)]

    @staticmethod
    def check_key(key: str) -> None:
        if not KEY_RE.match(key):
            raise ValueError(f"invalid blob key {key!r}")

    def _path(self, key: str) -> Path:
        self.check_key(key)
        return self.blobs / key[:2] / key

    # -- core operations ----------------------------------------------------

    def put(self, key: str, data: bytes, sha_hex: str) -> bool:
        """Store ``data``; returns False if an identical valid blob already existed."""
        if hashlib.sha256(data).hexdigest() != sha_hex:
            raise ChecksumMismatch(key)
        path = self._path(key)
        with self._lock(key):
            if path.exists():
                try:
                    _, existing = self._read_verified(path)
                    if existing == sha_hex:
                        return False
                except BlobCorrupt:
                    self._quarantine_locked(key, path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.tmp / f"{key}.{uuid.uuid4().hex}"
            with open(tmp, "wb") as f:
                f.write(MAGIC + bytes.fromhex(sha_hex) + data)
                if self.fsync:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp, path)
            if self.fsync:
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return True

    @staticmethod
    def _read_verified(path: Path) -> tuple[bytes, str]:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise BlobNotFound(path.name) from None
        if len(raw) < HEADER or raw[:4] != MAGIC:
            raise BlobCorrupt(path.name)
        digest, data = raw[4:HEADER], raw[HEADER:]
        if hashlib.sha256(data).digest() != digest:
            raise BlobCorrupt(path.name)
        return data, digest.hex()

    def get(self, key: str) -> tuple[bytes, str]:
        path = self._path(key)
        try:
            return self._read_verified(path)
        except BlobCorrupt:
            self.quarantine(key)
            raise

    def delete(self, key: str) -> bool:
        path = self._path(key)
        with self._lock(key):
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

    def quarantine(self, key: str) -> None:
        path = self._path(key)
        with self._lock(key):
            # Re-verify under the lock: a concurrent put may have replaced it.
            try:
                self._read_verified(path)
                return
            except BlobNotFound:
                return
            except BlobCorrupt:
                self._quarantine_locked(key, path)

    def _quarantine_locked(self, key: str, path: Path) -> None:
        try:
            os.replace(path, self.quarantine_dir / f"{key}.{int(time.time() * 1000)}")
        except FileNotFoundError:
            pass

    # -- inventory / maintenance -------------------------------------------

    def keys(self):
        for sub in self.blobs.iterdir():
            if sub.is_dir():
                for p in sub.iterdir():
                    yield p.name

    def inventory(self) -> list[dict]:
        out = []
        for key in self.keys():
            path = self._path(key)
            try:
                st = path.stat()
                with open(path, "rb") as f:
                    head = f.read(HEADER)
            except FileNotFoundError:
                continue
            sha = head[4:HEADER].hex() if head[:4] == MAGIC and len(head) == HEADER else ""
            out.append({"key": key, "size": max(0, st.st_size - HEADER), "sha256": sha,
                        "mtime": st.st_mtime})
        return out

    def scrub(self, limit: int | None = None) -> dict:
        """Verify every blob's checksum; quarantine and report the bad ones."""
        checked, corrupt = 0, []
        for key in list(self.keys()):
            if limit is not None and checked >= limit:
                break
            checked += 1
            try:
                self.get(key)
            except BlobCorrupt:
                corrupt.append(key)
            except BlobNotFound:
                pass
        return {"checked": checked, "corrupt": corrupt}

    def usage(self) -> dict:
        count = size = 0
        for sub in self.blobs.iterdir():
            if sub.is_dir():
                for p in sub.iterdir():
                    try:
                        size += p.stat().st_size
                        count += 1
                    except FileNotFoundError:
                        pass
        return {"blobs": count, "bytes": size}

    def corrupt(self, key: str, offset: int = 0) -> None:
        """Test hook: flip one payload byte on disk to simulate bit rot."""
        path = self._path(key)
        with open(path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            payload = f.tell() - HEADER
            pos = HEADER + (offset % max(payload, 1))
            f.seek(pos)
            b = f.read(1)
            f.seek(pos)
            f.write(bytes([(b[0] if b else 0) ^ 0xFF]))


@dataclass
class Faults:
    down: bool = False             # behave as if crashed: drop every data-plane request
    blocked: set = field(default_factory=set)  # client ids that cannot reach us (partition)
    latency: float = 0.0           # seconds of delay added to each request
    fail_writes: bool = False      # e.g. disk full / read-only filesystem
    corrupt_reads: bool = False    # flip a bit in every response body (bad NIC / RAM)

    def to_dict(self) -> dict:
        return {"down": self.down, "blocked": sorted(self.blocked), "latency": self.latency,
                "fail_writes": self.fail_writes, "corrupt_reads": self.corrupt_reads}


class _Handler(BaseHTTPRequestHandler):
    server_version = "VaultNode/1.0"
    node: "StorageNode"

    def log_message(self, *args):  # silence default stderr logging
        pass

    # -- helpers ---------------------------------------------------------

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None,
              ctype: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), ctype="application/json")

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _admitted(self, path: str) -> bool:
        """Apply injected faults. Returns False if the request must be dropped."""
        if path.startswith("/admin/"):
            return True
        f = self.node.faults
        if f.down or self.headers.get("X-Client-Id", "") in f.blocked:
            self.close_connection = True
            # Drain the request body so the client sees a clean connection drop.
            try:
                self._body()
            except OSError:
                pass
            return False
        if f.latency:
            time.sleep(f.latency)
        return True

    def _dispatch(self):
        url = urlparse(self.path)
        path = url.path
        if not self._admitted(path):
            return
        store = self.node.store
        try:
            if path.startswith("/blob/"):
                key = path[len("/blob/"):]
                store.check_key(key)
                if self.command == "PUT":
                    if self.node.faults.fail_writes:
                        return self._json(507, {"error": "insufficient storage"})
                    created = store.put(key, self._body(), self.headers.get("X-Checksum", ""))
                    return self._json(201 if created else 200, {"key": key})
                if self.command in ("GET", "HEAD"):
                    data, sha = store.get(key)
                    if self.node.faults.corrupt_reads and data:
                        data = bytes([data[0] ^ 0x01]) + data[1:]
                    return self._send(200, data, {"X-Checksum": sha})
                if self.command == "DELETE":
                    return self._json(200, {"deleted": store.delete(key)})
            if path == "/health" and self.command == "GET":
                return self._json(200, {"id": self.node.node_id, "zone": self.node.zone,
                                        "time": time.time(), **store.usage()})
            if path == "/inventory" and self.command == "GET":
                return self._json(200, store.inventory())
            if path == "/scrub" and self.command == "POST":
                qs = parse_qs(url.query)
                limit = int(qs["limit"][0]) if "limit" in qs else None
                return self._json(200, store.scrub(limit))
            if path == "/admin/faults":
                if self.command == "POST":
                    spec = json.loads(self._body() or b"{}")
                    f = self.node.faults
                    for k in ("down", "latency", "fail_writes", "corrupt_reads"):
                        if k in spec:
                            setattr(f, k, spec[k])
                    if "blocked" in spec:
                        f.blocked = set(spec["blocked"])
                return self._json(200, self.node.faults.to_dict())
            if path.startswith("/admin/corrupt/") and self.command == "POST":
                store.corrupt(path[len("/admin/corrupt/"):])
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "no such endpoint"})
        except BlobNotFound:
            return self._json(404, {"error": "not found"})
        except BlobCorrupt:
            return self._json(409, {"error": "corrupt"})
        except ChecksumMismatch:
            return self._json(400, {"error": "checksum mismatch"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except OSError as e:
            return self._json(503, {"error": f"node I/O error: {e}"})

    do_GET = do_PUT = do_DELETE = do_POST = do_HEAD = _dispatch


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Clients that time out or hedge away close their socket early; that is
        # expected under fault injection and not worth a traceback.
        pass


class StorageNode:
    """An in-process storage node server (also runnable standalone via the CLI)."""

    def __init__(self, node_id: str, root: str | Path, host: str = "127.0.0.1",
                 port: int = 0, zone: str = "zone-a", fsync: bool = True):
        self.node_id = node_id
        self.zone = zone
        self.host = host
        self.port = port
        self.store = BlobStore(root, fsync=fsync)
        self.faults = Faults()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> "StorageNode":
        handler = type("Handler", (_Handler,), {"node": self})
        server = _Server((self.host, self.port), handler)
        self.port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name=f"node-{self.node_id}",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Hard stop (simulates a process crash; data on disk is kept)."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            self.stop()
