"""Transactional metadata store (SQLite, WAL mode).

Metadata is the single source of truth; storage nodes hold only immutable,
content-addressed blobs.  That split is what keeps the system consistent:

* An object version becomes visible only when its manifest is committed in one
  serializable transaction, after its blobs have reached write quorum.  Readers
  therefore never observe a partially written object.
* Blobs are immutable and named by content hash, so replicas can never diverge;
  "replica inconsistency" reduces to a replica being missing or corrupt, which
  is detected by checksums and fixed by repair.
* Every commit draws a monotonically increasing version from a global sequence,
  giving a total order over writes; conditional writes (If-Match) are
  compare-and-swap on that version.

Tables
  nodes      cluster membership + health/admin state
  buckets    bucket -> durability policy
  objects    (bucket, key) -> current version + manifest
  refs       (bucket, key) -> blob keys it references (for GC)
  blobs      one row per replicated chunk or per EC shard, with desired copy count
  locations  blob -> nodes believed to hold a verified copy
  events     audit log of failures, repairs and moves
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (BucketExists, BucketNotEmpty, BucketNotFound, ObjectNotFound,
                     PreconditionFailed, RetryableConflict, VaultError)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
    id TEXT PRIMARY KEY, addr TEXT NOT NULL, zone TEXT NOT NULL, weight REAL NOT NULL,
    health TEXT NOT NULL DEFAULT 'alive', admin TEXT NOT NULL DEFAULT 'active',
    last_seen REAL NOT NULL, joined REAL NOT NULL);
CREATE TABLE IF NOT EXISTS buckets(
    name TEXT PRIMARY KEY, policy TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS objects(
    bucket TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL, size INTEGER NOT NULL,
    etag TEXT NOT NULL, created REAL NOT NULL, manifest TEXT NOT NULL,
    PRIMARY KEY(bucket, key));
CREATE TABLE IF NOT EXISTS refs(
    bucket TEXT NOT NULL, key TEXT NOT NULL, blob_key TEXT NOT NULL,
    PRIMARY KEY(bucket, key, blob_key));
CREATE INDEX IF NOT EXISTS refs_blob ON refs(blob_key);
CREATE TABLE IF NOT EXISTS blobs(
    blob_key TEXT PRIMARY KEY, chunk_id TEXT NOT NULL, idx INTEGER NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, scheme TEXT NOT NULL, want INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active', touched REAL NOT NULL);
CREATE INDEX IF NOT EXISTS blobs_chunk ON blobs(chunk_id);
CREATE TABLE IF NOT EXISTS locations(
    blob_key TEXT NOT NULL, node_id TEXT NOT NULL, added REAL NOT NULL,
    PRIMARY KEY(blob_key, node_id));
CREATE INDEX IF NOT EXISTS locations_node ON locations(node_id);
CREATE TABLE IF NOT EXISTS seq(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""

# A copy "counts" toward durability only on a node that is up (or merely
# suspected, to avoid repair storms on transient blips) and not being drained.
COUNTS = "n.health IN ('alive','suspect') AND n.admin = 'active'"


@dataclass
class NodeInfo:
    id: str
    addr: str
    zone: str
    weight: float = 1.0
    health: str = "alive"      # alive | suspect | dead
    admin: str = "active"      # active | draining | drained | removed
    last_seen: float = 0.0

    @property
    def writable(self) -> bool:
        return self.health == "alive" and self.admin == "active"

    @property
    def counts(self) -> bool:
        return self.health in ("alive", "suspect") and self.admin == "active"

    @property
    def readable(self) -> bool:
        return self.health in ("alive", "suspect") and self.admin != "removed"


@dataclass
class BlobRec:
    key: str
    chunk_id: str
    idx: int
    sha256: str
    size: int
    scheme: str
    want: int
    locations: list[str] = field(default_factory=list)
    state: str = "active"


@dataclass
class ObjectMeta:
    bucket: str
    key: str
    version: int
    size: int
    etag: str
    created: float
    manifest: dict


class MetadataStore:
    """Thread-safe facade over a small pool of SQLite connections.

    Connections are *borrowed* per operation and returned, instead of being
    pinned to threads: thread-per-request servers and thread pools would
    otherwise open one connection (two file descriptors: db + WAL) per thread
    that is only released whenever the garbage collector gets to it.  At most
    ``max_idle`` connections are kept; ``close()`` closes every one of them.
    """

    def __init__(self, path: str | Path, max_idle: int = 8):
        self.path = str(path)
        self.max_idle = max_idle
        self._idle: list[sqlite3.Connection] = []
        self._all: set[sqlite3.Connection] = set()
        self._pool_lock = threading.Lock()
        self._closed = False
        # SQLite allows one writer at a time and its busy handler *polls* with
        # sleeps of up to 100ms. Queueing writers from this process on a real lock
        # instead wakes them immediately. (Writers in other processes still
        # coordinate through SQLite's own locking.)
        self._wlock = threading.RLock()
        with self._borrow() as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO seq(name, value) VALUES('version', 0)")

    def _open(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                            check_same_thread=False)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=OFF")
        except BaseException:
            c.close()
            raise
        return c

    @contextmanager
    def _borrow(self):
        with self._pool_lock:
            if self._closed:
                raise VaultError("metadata store is closed")
            conn = self._idle.pop() if self._idle else None
        if conn is None:
            conn = self._open()
            with self._pool_lock:
                self._all.add(conn)
        try:
            yield conn
        finally:
            if conn.in_transaction:  # never hand a half-finished transaction to someone else
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            with self._pool_lock:
                keep = not self._closed and len(self._idle) < self.max_idle
                if keep:
                    self._idle.append(conn)
                else:
                    self._all.discard(conn)
            if not keep:
                conn.close()

    def close(self) -> None:
        """Close every connection. Connections still borrowed close on return."""
        with self._pool_lock:
            self._closed = True
            idle, self._idle = self._idle, []
            for c in idle:
                self._all.discard(c)
        for c in idle:
            c.close()

    @property
    def open_connections(self) -> int:
        with self._pool_lock:
            return len(self._all)

    @contextmanager
    def tx(self):
        """Serializable read-write transaction (takes the write lock up front)."""
        with self._borrow() as c, self._wlock:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")

    def _w(self, sql: str, args=()) -> int:
        """Single-statement autocommit write; returns the affected row count."""
        with self._borrow() as c, self._wlock:
            return c.execute(sql, args).rowcount

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._borrow() as c:
            return c.execute(sql, args).fetchall()

    # -- events --------------------------------------------------------------

    def log_event(self, kind: str, **detail) -> None:
        self._w("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
                             (time.time(), kind, json.dumps(detail, default=str)))

    def events(self, kind: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM events" + (" WHERE kind = ?" if kind else "") + \
              " ORDER BY id DESC LIMIT ?"
        rows = self._q(sql, (kind, limit) if kind else (limit,))
        return [{"ts": r["ts"], "kind": r["kind"], **json.loads(r["detail"] or "{}")}
                for r in rows]

    # -- nodes ---------------------------------------------------------------

    def upsert_node(self, node_id: str, addr: str, zone: str, weight: float = 1.0) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute("""INSERT INTO nodes(id, addr, zone, weight, last_seen, joined)
                         VALUES(?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET addr=excluded.addr, zone=excluded.zone,
                           weight=excluded.weight,
                           admin=CASE WHEN admin='removed' THEN 'active' ELSE admin END""",
                      (node_id, addr, zone, weight, now, now))

    def list_nodes(self) -> list[NodeInfo]:
        return [NodeInfo(r["id"], r["addr"], r["zone"], r["weight"], r["health"], r["admin"],
                         r["last_seen"]) for r in self._q("SELECT * FROM nodes ORDER BY id")]

    def set_node_health(self, node_id: str, health: str, last_seen: float | None = None) -> None:
        if last_seen is None:
            self._w("UPDATE nodes SET health=? WHERE id=?", (health, node_id))
        else:
            self._w("UPDATE nodes SET health=?, last_seen=? WHERE id=?",
                                 (health, last_seen, node_id))

    def set_node_admin(self, node_id: str, admin: str) -> None:
        self._w("UPDATE nodes SET admin=? WHERE id=?", (admin, node_id))

    def forget_node_locations(self, node_id: str) -> int:
        with self.tx() as c:
            return c.execute("DELETE FROM locations WHERE node_id=?", (node_id,)).rowcount

    def node_location_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in
                self._q("SELECT node_id, COUNT(*) FROM locations GROUP BY node_id")}

    # -- buckets -------------------------------------------------------------

    def create_bucket(self, name: str, policy: dict) -> None:
        try:
            with self.tx() as c:
                c.execute("INSERT INTO buckets(name, policy, created) VALUES(?,?,?)",
                          (name, json.dumps(policy), time.time()))
        except sqlite3.IntegrityError:
            raise BucketExists(name) from None

    def get_bucket(self, name: str) -> dict:
        rows = self._q("SELECT policy FROM buckets WHERE name=?", (name,))
        if not rows:
            raise BucketNotFound(name)
        return json.loads(rows[0]["policy"])

    def list_buckets(self) -> list[dict]:
        return [{"name": r["name"], "policy": json.loads(r["policy"]), "created": r["created"]}
                for r in self._q("SELECT * FROM buckets ORDER BY name")]

    def delete_bucket(self, name: str) -> None:
        with self.tx() as c:
            if c.execute("SELECT 1 FROM objects WHERE bucket=? LIMIT 1", (name,)).fetchone():
                raise BucketNotEmpty(name)
            if not c.execute("DELETE FROM buckets WHERE name=?", (name,)).rowcount:
                raise BucketNotFound(name)

    # -- objects -------------------------------------------------------------

    @staticmethod
    def _check_precondition(c, bucket, key, if_match):
        row = c.execute("SELECT version FROM objects WHERE bucket=? AND key=?",
                        (bucket, key)).fetchone()
        current = row["version"] if row else None
        if if_match is not None:
            # if_match == 0 means "must not exist" (If-None-Match: *)
            if (if_match == 0 and current is not None) or (if_match != 0 and current != if_match):
                raise PreconditionFailed(f"{bucket}/{key}: expected {if_match}, have {current}")
        return current

    @staticmethod
    def _release_refs(c, bucket, key, now):
        # Bump 'touched' on blobs losing a reference so GC waits a full grace
        # period: in-flight readers of the old version keep working.
        c.execute("""UPDATE blobs SET touched=? WHERE blob_key IN
                     (SELECT blob_key FROM refs WHERE bucket=? AND key=?)""", (now, bucket, key))
        c.execute("DELETE FROM refs WHERE bucket=? AND key=?", (bucket, key))

    def commit_object(self, bucket: str, key: str, size: int, etag: str, manifest: dict,
                      new_blobs: list[BlobRec], ref_keys: list[str],
                      if_match: int | None = None) -> int:
        now = time.time()
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM buckets WHERE name=?", (bucket,)).fetchone():
                raise BucketNotFound(bucket)
            self._check_precondition(c, bucket, key, if_match)
            for b in new_blobs:
                c.execute("""INSERT INTO blobs(blob_key, chunk_id, idx, sha256, size, scheme, want,
                                               state, touched)
                             VALUES(?,?,?,?,?,?,?,'active',?)
                             ON CONFLICT(blob_key) DO UPDATE SET touched=?""",
                          (b.key, b.chunk_id, b.idx, b.sha256, b.size, b.scheme, b.want, now, now))
                c.executemany("INSERT OR IGNORE INTO locations(blob_key, node_id, added) "
                              "VALUES(?,?,?)", [(b.key, n, now) for n in b.locations])
            unique_refs = sorted(set(ref_keys))
            for i in range(0, len(unique_refs), 500):
                part = unique_refs[i:i + 500]
                rows = c.execute(f"SELECT blob_key, state FROM blobs WHERE blob_key IN "
                                 f"({','.join('?' * len(part))})", part).fetchall()
                states = {r["blob_key"]: r["state"] for r in rows}
                bad = [k for k in part if states.get(k) != "active"]
                if bad:
                    raise RetryableConflict(f"blob(s) garbage-collected during write: {bad[:3]}")
            version = c.execute("UPDATE seq SET value = value + 1 WHERE name='version' "
                                "RETURNING value").fetchone()[0]
            self._release_refs(c, bucket, key, now)
            c.executemany("INSERT INTO refs(bucket, key, blob_key) VALUES(?,?,?)",
                          [(bucket, key, k) for k in unique_refs])
            c.execute("""INSERT INTO objects(bucket, key, version, size, etag, created, manifest)
                         VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(bucket, key) DO UPDATE SET version=excluded.version,
                           size=excluded.size, etag=excluded.etag, created=excluded.created,
                           manifest=excluded.manifest""",
                      (bucket, key, version, size, etag, now, json.dumps(manifest)))
            return version

    def get_object(self, bucket: str, key: str) -> ObjectMeta:
        rows = self._q("SELECT * FROM objects WHERE bucket=? AND key=?", (bucket, key))
        if not rows:
            if not self._q("SELECT 1 FROM buckets WHERE name=?", (bucket,)):
                raise BucketNotFound(bucket)
            raise ObjectNotFound(f"{bucket}/{key}")
        r = rows[0]
        return ObjectMeta(bucket, key, r["version"], r["size"], r["etag"], r["created"],
                          json.loads(r["manifest"]))

    def delete_object(self, bucket: str, key: str, if_match: int | None = None) -> int:
        with self.tx() as c:
            current = self._check_precondition(c, bucket, key, if_match)
            if current is None:
                raise ObjectNotFound(f"{bucket}/{key}")
            self._release_refs(c, bucket, key, time.time())
            c.execute("DELETE FROM objects WHERE bucket=? AND key=?", (bucket, key))
            return current

    def list_objects(self, bucket: str, prefix: str = "", after: str = "",
                     limit: int = 1000) -> list[dict]:
        self.get_bucket(bucket)
        rows = self._q("""SELECT key, version, size, etag, created FROM objects
                          WHERE bucket=? AND key > ? AND substr(key, 1, ?) = ?
                          ORDER BY key LIMIT ?""", (bucket, after, len(prefix), prefix, limit))
        return [dict(r) for r in rows]

    # -- blobs & locations -----------------------------------------------------

    def claim_chunk(self, chunk_id: str) -> list[BlobRec] | None:
        """For dedup: if every blob of ``chunk_id`` is active, refresh its GC lease
        and return it (with locations); otherwise None."""
        now = time.time()
        with self.tx() as c:
            rows = c.execute("SELECT * FROM blobs WHERE chunk_id=?", (chunk_id,)).fetchall()
            if not rows or any(r["state"] != "active" for r in rows):
                return None
            c.execute("UPDATE blobs SET touched=? WHERE chunk_id=?", (now, chunk_id))
        return self.chunk_blobs(chunk_id)

    def _blob_rows(self, where: str, args) -> list[BlobRec]:
        rows = self._q(f"""SELECT b.*, l.node_id FROM blobs b
                           LEFT JOIN locations l ON l.blob_key = b.blob_key
                           WHERE {where} ORDER BY b.blob_key""", args)
        out: dict[str, BlobRec] = {}
        for r in rows:
            rec = out.get(r["blob_key"])
            if rec is None:
                rec = out[r["blob_key"]] = BlobRec(r["blob_key"], r["chunk_id"], r["idx"],
                                                   r["sha256"], r["size"], r["scheme"], r["want"],
                                                   state=r["state"])
            if r["node_id"]:
                rec.locations.append(r["node_id"])
        return list(out.values())

    def chunk_blobs(self, chunk_id: str) -> list[BlobRec]:
        return sorted(self._blob_rows("b.chunk_id = ?", (chunk_id,)), key=lambda b: b.idx)

    def blobs_page(self, after: str = "", limit: int = 500) -> list[BlobRec]:
        keys = [r[0] for r in self._q(
            "SELECT blob_key FROM blobs b WHERE blob_key > ? AND state='active' "
            "AND EXISTS (SELECT 1 FROM refs r WHERE r.blob_key = b.blob_key) "
            "ORDER BY blob_key LIMIT ?", (after, limit))]
        if not keys:
            return []
        return self._blob_rows(f"b.blob_key IN ({','.join('?' * len(keys))})", keys)

    def blob_info(self, keys: list[str]) -> dict[str, tuple[str, str]]:
        """blob_key -> (state, sha256) for the keys that exist."""
        out = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            for r in self._q(f"SELECT blob_key, state, sha256 FROM blobs WHERE blob_key IN "
                             f"({','.join('?' * len(part))})", part):
                out[r[0]] = (r[1], r[2])
        return out

    def locations_many(self, keys: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {k: [] for k in keys}
        if keys:
            for r in self._q(f"SELECT blob_key, node_id FROM locations WHERE blob_key IN "
                             f"({','.join('?' * len(keys))})", keys):
                out[r[0]].append(r[1])
        return out

    def node_blob_keys(self, node_id: str) -> set[str]:
        return {r[0] for r in self._q("SELECT blob_key FROM locations WHERE node_id=?",
                                      (node_id,))}

    def add_location(self, blob_key: str, node_id: str) -> bool:
        return self._w("INSERT OR IGNORE INTO locations(blob_key, node_id, added) "
                             "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM blobs WHERE blob_key=?)",
                             (blob_key, node_id, time.time(), blob_key)) > 0

    def remove_location(self, blob_key: str, node_id: str) -> bool:
        return self._w("DELETE FROM locations WHERE blob_key=? AND node_id=?",
                                    (blob_key, node_id)) > 0

    def deficient_chunks(self) -> list[str]:
        """Chunks with at least one blob below its desired count of healthy copies."""
        rows = self._q(f"""SELECT DISTINCT chunk_id FROM (
                             SELECT b.chunk_id, b.want, COUNT(n.id) AS healthy
                             FROM blobs b
                             LEFT JOIN locations l ON l.blob_key = b.blob_key
                             LEFT JOIN nodes n ON n.id = l.node_id AND {COUNTS}
                             WHERE b.state = 'active'
                               -- garbage awaiting GC is not worth repairing
                               AND EXISTS (SELECT 1 FROM refs r WHERE r.blob_key = b.blob_key)
                             GROUP BY b.blob_key HAVING healthy < b.want)""")
        return [r[0] for r in rows]

    # -- garbage collection ------------------------------------------------------

    def gc_candidates(self, cutoff: float, limit: int = 10000) -> list[str]:
        rows = self._q("""SELECT blob_key FROM blobs b WHERE state='active' AND touched < ?
                          AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.blob_key = b.blob_key)
                          LIMIT ?""", (cutoff, limit))
        return [r[0] for r in rows]

    def mark_deleting(self, blob_keys: list[str], cutoff: float) -> list[str]:
        """Atomically move still-unreferenced, lease-expired blobs to 'deleting'."""
        marked = []
        with self.tx() as c:
            for key in blob_keys:
                if c.execute("""UPDATE blobs SET state='deleting' WHERE blob_key=?
                                AND state='active' AND touched < ?
                                AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.blob_key = ?)""",
                             (key, cutoff, key)).rowcount:
                    marked.append(key)
        return marked

    def drop_blobs(self, blob_keys: list[str]) -> None:
        with self.tx() as c:
            c.executemany("DELETE FROM locations WHERE blob_key=?", [(k,) for k in blob_keys])
            c.executemany("DELETE FROM blobs WHERE blob_key=? AND state='deleting'",
                          [(k,) for k in blob_keys])

    # -- stats -------------------------------------------------------------------

    def stats(self) -> dict:
        one = lambda sql: self._q(sql)[0][0] or 0  # noqa: E731
        return {
            "buckets": one("SELECT COUNT(*) FROM buckets"),
            "objects": one("SELECT COUNT(*) FROM objects"),
            "logical_bytes": one("SELECT SUM(size) FROM objects"),
            "blobs": one("SELECT COUNT(*) FROM blobs"),
            "replicas": one("SELECT COUNT(*) FROM locations"),
            "physical_bytes": one("SELECT SUM(b.size) FROM locations l "
                                  "JOIN blobs b ON b.blob_key = l.blob_key"),
            "deficient_chunks": len(self.deficient_chunks()),
            "version": one("SELECT value FROM seq WHERE name='version'"),
        }
