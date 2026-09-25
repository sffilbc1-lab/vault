"""HTTP client for storage nodes, mapping transport/status failures onto Vault errors."""

from __future__ import annotations

import errno
import hashlib
import http.client
import json

from .errors import (BlobCorrupt, BlobNotFound, ChecksumMismatch, LocalResourceError,
                     NodeUnavailable)

# errno values meaning the *caller* is out of resources, not that the peer failed.
_LOCAL_ERRNOS = {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM}


class NodeClient:
    def __init__(self, client_id: str = "vault", timeout: float = 5.0):
        self.client_id = client_id
        self.timeout = timeout

    def _request(self, addr: str, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None, timeout: float | None = None):
        host, port = addr.rsplit(":", 1)
        conn = http.client.HTTPConnection(host, int(port), timeout=timeout or self.timeout)
        hdrs = {"X-Client-Id": self.client_id, **(headers or {})}
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp, data
        except OSError as e:
            if e.errno in _LOCAL_ERRNOS:
                raise LocalResourceError(f"local resource exhausted talking to {addr}: {e}") \
                    from e
            raise NodeUnavailable(f"{addr}: {type(e).__name__}: {e}") from None
        except http.client.HTTPException as e:
            raise NodeUnavailable(f"{addr}: {type(e).__name__}: {e}") from None
        finally:
            conn.close()

    def _json(self, addr, method, path, body=None, timeout=None):
        status, _, data = self._request(addr, method, path, body, timeout=timeout)
        if status >= 500:
            raise NodeUnavailable(f"{addr}: HTTP {status}")
        if status >= 400:
            raise NodeUnavailable(f"{addr}: HTTP {status} {data[:200]!r}")
        return json.loads(data or b"null")

    # -- blob operations ----------------------------------------------------

    def put_blob(self, addr: str, key: str, data: bytes, sha: str) -> None:
        status, _, body = self._request(addr, "PUT", f"/blob/{key}", data, {"X-Checksum": sha})
        if status in (200, 201):
            return
        if status == 400 and b"checksum" in body:
            raise ChecksumMismatch(f"{addr}/{key}")
        raise NodeUnavailable(f"{addr}: PUT {key} -> HTTP {status}")

    def get_blob(self, addr: str, key: str, expect_sha: str | None = None) -> bytes:
        status, resp, data = self._request(addr, "GET", f"/blob/{key}")
        if status == 404:
            raise BlobNotFound(f"{addr}/{key}")
        if status == 409:
            raise BlobCorrupt(f"{addr}/{key} (detected by node)")
        if status != 200:
            raise NodeUnavailable(f"{addr}: GET {key} -> HTTP {status}")
        actual = hashlib.sha256(data).hexdigest()
        # End-to-end check: catches corruption in transit or a lying node.
        if actual != resp.getheader("X-Checksum") or (expect_sha and actual != expect_sha):
            raise BlobCorrupt(f"{addr}/{key} (detected by client)")
        return data

    def delete_blob(self, addr: str, key: str) -> bool:
        return bool(self._json(addr, "DELETE", f"/blob/{key}")["deleted"])

    # -- node-level operations ------------------------------------------------

    def health(self, addr: str, timeout: float | None = None) -> dict:
        return self._json(addr, "GET", "/health", timeout=timeout)

    def inventory(self, addr: str) -> list[dict]:
        return self._json(addr, "GET", "/inventory", timeout=max(self.timeout, 30))

    def scrub(self, addr: str, limit: int | None = None) -> dict:
        q = f"?limit={limit}" if limit is not None else ""
        return self._json(addr, "POST", f"/scrub{q}", timeout=max(self.timeout, 120))

    def set_faults(self, addr: str, **faults) -> dict:
        if "blocked" in faults:
            faults["blocked"] = list(faults["blocked"])
        return self._json(addr, "POST", "/admin/faults", json.dumps(faults).encode())

    def corrupt_blob(self, addr: str, key: str) -> None:
        self._json(addr, "POST", f"/admin/corrupt/{key}")
