"""The Vault gateway: object API on top of storage nodes + metadata.

Write path (put_object)
  1. Stream the body in ``chunk_size`` pieces; several chunks are in flight at once.
  2. Each chunk is content-addressed (sha256 + policy signature).  If an identical
     chunk is already stored under the same policy it is deduplicated.
  3. Otherwise it is replicated / erasure-coded and uploaded in parallel to the
     top-ranked writable nodes (zone-aware rendezvous hashing).  Failed targets are
     replaced by the next-ranked node, so a write only fails when fewer than ``w``
     copies/shards can be made durable anywhere.
  4. The manifest is committed atomically in metadata -> the new version becomes
     visible to readers all at once.

Read path (get_object)
  * Chunks are fetched with read-ahead.  Each fetch verifies the SHA-256 end to end.
  * Replicas are tried in preference order; if one is slow a *hedged* request is
    sent to the next replica after ``hedge_delay`` - tail latency stays bounded
    when a node is sick but not dead.
  * EC chunks fetch any ``k`` shards (data shards first so the common case needs no
    decoding) and hedge onto parity shards.
  * Missing or corrupt copies discovered while reading are dropped from metadata
    immediately (read repair); the repair service restores them.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator

from . import erasure
from .client import NodeClient
from .errors import (BlobCorrupt, BlobNotFound, LocalResourceError, NodeUnavailable, ReadError,
                     RetryableConflict, VaultError, WriteQuorumError)
from .membership import Membership
from .metadata import BlobRec, MetadataStore, NodeInfo, ObjectMeta
from .placement import zone_order
from .policy import Policy, Scheme


@dataclass
class _Spec:
    """A blob about to be uploaded."""
    rec: BlobRec
    data: bytes


def _read_full(stream: BinaryIO, n: int) -> bytes:
    parts, got = [], 0
    while got < n:
        b = stream.read(n - got)
        if not b:
            break
        parts.append(b)
        got += len(b)
    return b"".join(parts)


class Vault:
    def __init__(self, meta_path: str, client_id: str = "gateway", node_timeout: float = 5.0,
                 io_threads: int = 64, chunk_parallelism: int = 8, hedge_delay: float = 0.25,
                 heartbeat_interval: float = 1.0, dead_after: float = 10.0,
                 dedup: bool = True):
        self.meta = MetadataStore(meta_path)
        self.client = NodeClient(client_id, timeout=node_timeout)
        self.membership = Membership(self.meta, self.client, interval=heartbeat_interval,
                                     dead_after=dead_after)
        self.hedge_delay = hedge_delay
        self.chunk_parallelism = chunk_parallelism
        self.dedup = dedup
        self._io = ThreadPoolExecutor(io_threads, thread_name_prefix="io")
        self._chunks = ThreadPoolExecutor(chunk_parallelism * 4, thread_name_prefix="chunk")
        self._stats = Counter()
        self._stats_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """Release every thread, socket and database connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.membership.close()
        # Chunk tasks wait on I/O tasks, so drain the chunk pool first. Waiting is
        # bounded: every node call has a socket timeout.
        self._chunks.shutdown(wait=True, cancel_futures=True)
        self._io.shutdown(wait=True, cancel_futures=True)
        self.meta.close()

    def stat(self, name: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] += n

    @property
    def stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    # -- cluster admin ---------------------------------------------------------

    def add_node(self, node_id: str, addr: str, zone: str = "zone-a", weight: float = 1.0):
        self.membership.add_node(node_id, addr, zone, weight)

    def drain_node(self, node_id: str) -> None:
        """Gracefully evacuate a node; repair + rebalance move its data elsewhere."""
        self.membership.set_admin(node_id, "draining")

    def remove_node(self, node_id: str) -> None:
        """Forget a permanently lost node (its replicas are written off immediately)."""
        self.membership.set_admin(node_id, "removed")
        self.meta.forget_node_locations(node_id)

    # -- buckets ---------------------------------------------------------------

    def create_bucket(self, name: str, policy: Policy | None = None) -> None:
        self.meta.create_bucket(name, (policy or Policy()).to_dict())

    def bucket_policy(self, name: str) -> Policy:
        return Policy.from_dict(self.meta.get_bucket(name))

    def list_buckets(self) -> list[dict]:
        return self.meta.list_buckets()

    def delete_bucket(self, name: str) -> None:
        self.meta.delete_bucket(name)

    # -- write path ------------------------------------------------------------

    def put_object(self, bucket: str, key: str, data: bytes | BinaryIO,
                   if_match: int | None = None) -> ObjectMeta:
        """Store an object. ``if_match``: expected current version (0 = must not exist)."""
        attempts = 3 if isinstance(data, (bytes, bytearray, memoryview)) else 1
        for attempt in range(attempts):
            stream = io.BytesIO(data) if isinstance(data, (bytes, bytearray, memoryview)) else data
            try:
                return self._put(bucket, key, stream, if_match, dedup=self.dedup and attempt == 0)
            except RetryableConflict:
                if attempt == attempts - 1:
                    raise
                self.stat("put_retries")

    def _put(self, bucket, key, stream, if_match, dedup) -> ObjectMeta:
        policy = self.bucket_policy(bucket)
        nodes = self.membership.writable()
        if not nodes:
            raise WriteQuorumError("no writable nodes")
        hasher, size = hashlib.sha256(), 0
        window: deque[Future] = deque()
        results: list[tuple[dict, list[BlobRec]]] = []
        try:
            while True:
                buf = _read_full(stream, policy.chunk_size)
                if not buf:
                    break
                hasher.update(buf)
                size += len(buf)
                window.append(self._chunks.submit(self._write_chunk, policy, buf, nodes, dedup))
                if len(window) >= self.chunk_parallelism:
                    results.append(window.popleft().result())
            while window:
                results.append(window.popleft().result())
        finally:
            for f in window:
                f.cancel()
        manifest = {"v": 1, "scheme": policy.sig, "chunk_size": policy.chunk_size,
                    "chunks": [entry for entry, _ in results]}
        new_blobs = [b for _, recs in results for b in recs]
        ref_keys = [b[0] for entry, _ in results for b in entry["blobs"]]
        etag = hasher.hexdigest()
        version = self.meta.commit_object(bucket, key, size, etag, manifest, new_blobs, ref_keys,
                                          if_match)
        self.stat("puts")
        self.stat("bytes_written", size)
        return ObjectMeta(bucket, key, version, size, etag, time.time(), manifest)

    def _write_chunk(self, policy: Policy, data: bytes, nodes: list[NodeInfo],
                     dedup: bool) -> tuple[dict, list[BlobRec]]:
        sha = hashlib.sha256(data).hexdigest()
        cid = f"{sha}.{policy.sig}"
        if policy.replicated:
            specs = [_Spec(BlobRec(cid, cid, 0, sha, len(data), policy.sig, policy.n), data)]
        else:
            shards = erasure.encode(data, policy.k, policy.m)
            specs = [_Spec(BlobRec(f"{cid}.{i}", cid, i, hashlib.sha256(s).hexdigest(), len(s),
                                   policy.sig, 1), s) for i, s in enumerate(shards)]
        entry = {"id": cid, "len": len(data), "sha": sha,
                 "blobs": [[s.rec.key, s.rec.sha256] for s in specs]}

        if dedup:
            existing = self.meta.claim_chunk(cid)
            if existing and self._durable(existing, policy):
                self.stat("chunks_deduped")
                return entry, []

        placed = self.upload(specs, nodes)
        # Count distinct nodes: two shards on one disk die together, so they only
        # buy the durability of one.
        ok = len({n for locs in placed.values() for n in locs})
        if ok < policy.write_quorum:
            self.stat("write_quorum_failures")
            raise WriteQuorumError(f"chunk {cid[:16]}: {ok}/{policy.write_quorum} durable "
                                   f"(policy {policy.sig})")
        for s in specs:
            s.rec.locations = placed[s.rec.key]
        return entry, [s.rec for s in specs if s.rec.locations]

    def _durable(self, recs: list[BlobRec], policy: Policy) -> bool:
        by_id = self.membership.by_id()
        return len({n for r in recs for n in r.locations
                    if by_id.get(n) and by_id[n].counts}) >= policy.write_quorum

    def upload(self, specs: list[_Spec], nodes: list[NodeInfo],
               exclude: set[str] = frozenset(), avoid: set[str] = frozenset(),
               prefer: list[str] = (), max_rounds: int = 4) -> dict[str, list[str]]:
        """Upload blobs to their preferred nodes, falling back down the ranking on failure.

        Replicated chunks want ``want`` copies each; EC shards want one copy each and
        prefer nodes that hold no other shard of the same chunk (independent failure).
        ``exclude`` nodes are never used; ``avoid`` nodes are used only as a last resort;
        ``prefer`` nodes (e.g. the blob's ideal placement) are tried first.
        Returns blob_key -> node ids that acknowledged a durable write.
        """
        chunk_id = specs[0].rec.chunk_id
        order = zone_order(chunk_id, [n for n in nodes if n.id not in exclude])
        placed: dict[str, list[str]] = {s.rec.key: [] for s in specs}
        tried: dict[str, set[str]] = {s.rec.key: set() for s in specs}
        bad: set[str] = set()
        holding: set[str] = set(avoid)
        for _ in range(max_rounds):
            tasks: list[tuple[_Spec, NodeInfo]] = []
            reserved = set(holding)
            for s in specs:
                need = s.rec.want - len(placed[s.rec.key])
                if need <= 0:
                    continue
                rot = s.rec.idx % len(order) if order else 0
                cands = [n for n in order[rot:] + order[:rot]
                         if n.id not in tried[s.rec.key] and n.id not in bad]
                cands = [n for n in cands if n.id in prefer] + \
                        [n for n in cands if n.id not in reserved and n.id not in prefer] + \
                        [n for n in cands if n.id in reserved and n.id not in prefer]
                for n in cands[:need]:
                    tasks.append((s, n))
                    tried[s.rec.key].add(n.id)
                    reserved.add(n.id)
            if not tasks:
                break
            futs = {self._io.submit(self.client.put_blob, n.addr, s.rec.key, s.data, s.rec.sha256):
                    (s, n) for s, n in tasks}
            local_error: LocalResourceError | None = None
            for f in as_completed(futs):
                s, n = futs[f]
                try:
                    f.result()
                    placed[s.rec.key].append(n.id)
                    holding.add(n.id)
                    self.stat("blob_writes")
                except LocalResourceError as e:
                    local_error = e  # the node is fine; we are not
                    self.stat("local_resource_errors")
                except VaultError as e:
                    bad.add(n.id)
                    self.stat("blob_write_failures")
                    if isinstance(e, NodeUnavailable):
                        self.membership.report_failure(n.id)
            if local_error is not None:
                # Retrying other nodes would fail the same way and wrongly look like a
                # cluster outage; report the real cause instead of a quorum failure.
                raise local_error
        return placed

    # -- read path -------------------------------------------------------------

    def head_object(self, bucket: str, key: str) -> ObjectMeta:
        return self.meta.get_object(bucket, key)

    def get_object(self, bucket: str, key: str) -> bytes:
        meta, it = self.open_object(bucket, key)
        return b"".join(it)

    def open_object(self, bucket: str, key: str,
                    readahead: int | None = None) -> tuple[ObjectMeta, Iterator[bytes]]:
        """Return metadata and a lazy iterator over the object's chunks."""
        meta = self.meta.get_object(bucket, key)
        scheme = Scheme.parse(meta.manifest["scheme"])
        chunks = meta.manifest["chunks"]
        depth = readahead or self.chunk_parallelism

        def gen():
            window: deque[Future] = deque()
            it = iter(chunks)
            try:
                for c in it:
                    window.append(self._chunks.submit(self.read_chunk, c, scheme))
                    if len(window) >= depth:
                        yield window.popleft().result()
                while window:
                    yield window.popleft().result()
            finally:
                for f in window:
                    f.cancel()
            self.stat("gets")
            self.stat("bytes_read", meta.size)

        return meta, gen()

    def read_chunk(self, entry: dict, scheme: Scheme) -> bytes:
        keys = [b[0] for b in entry["blobs"]]
        locs = self.meta.locations_many(keys)
        if scheme.replicated:
            key, sha = entry["blobs"][0]
            data = self._fetch_blob(key, sha, locs[key])
        else:
            shards = self._fetch_shards(entry["blobs"], locs, scheme.k)
            data = erasure.decode(shards, scheme.k, scheme.m, entry["len"])
            if any(i >= scheme.k for i in shards):
                self.stat("ec_degraded_reads")
        if hashlib.sha256(data).hexdigest() != entry["sha"]:
            raise ReadError(f"chunk {entry['id'][:16]} failed end-to-end verification")
        return data

    def _ordered(self, node_ids: list[str], key: str) -> list[NodeInfo]:
        by_id = self.membership.by_id()
        nodes = [by_id[n] for n in node_ids if n in by_id and by_id[n].admin != "removed"]
        rank = {n.id: i for i, n in enumerate(zone_order(key, nodes))}
        prio = {"alive": 0, "suspect": 1, "dead": 2}
        return sorted(nodes, key=lambda n: (prio.get(n.health, 3), rank[n.id]))

    def _fetch_from(self, key: str, sha: str, node: NodeInfo) -> bytes:
        try:
            data = self.client.get_blob(node.addr, key, sha)
            self.stat("blob_reads")
            return data
        except (BlobNotFound, BlobCorrupt) as e:
            # Read repair: forget the bad copy now; the repair service replaces it.
            if self.meta.remove_location(key, node.id):
                kind = "missing" if isinstance(e, BlobNotFound) else "corrupt"
                self.stat(f"replicas_{kind}")
                self.meta.log_event(f"replica_{kind}", blob=key, node=node.id, via="read")
            raise
        except NodeUnavailable:
            self.membership.report_failure(node.id)
            raise

    def _fetch_blob(self, key: str, sha: str, node_ids: list[str]) -> bytes:
        nodes = self._ordered(node_ids, key)
        return self._hedged([lambda n=n: self._fetch_from(key, sha, n) for n in nodes], key)

    def fetch_any(self, key: str, sha: str, node_ids: list[str]) -> bytes:
        """Sequentially try each location (used by background jobs; no hedging)."""
        errors = []
        for n in self._ordered(node_ids, key):
            try:
                return self._fetch_from(key, sha, n)
            except VaultError as e:
                errors.append(e)
        raise ReadError(f"{key}: no readable copy ({len(errors)} tried)")

    def _hedged(self, thunks: list[Callable[[], bytes]], what: str) -> bytes:
        it = iter(thunks)
        pending: set[Future] = set()
        errors: list[Exception] = []

        def launch() -> bool:
            t = next(it, None)
            if t is None:
                return False
            pending.add(self._io.submit(t))
            return True

        launch()
        while pending:
            done, _ = wait(pending, timeout=self.hedge_delay, return_when=FIRST_COMPLETED)
            if not done:
                if launch():
                    self.stat("hedged_reads")
                continue
            for f in done:
                pending.discard(f)
                try:
                    return f.result()
                except VaultError as e:
                    errors.append(e)
                    launch()
        self.stat("read_failures")
        raise ReadError(f"{what}: all {len(errors)} replica(s) failed: "
                        f"{'; '.join(map(str, errors[:3]))}")

    def _fetch_shards(self, blobs: list[list[str]], locs: dict[str, list[str]],
                      k: int) -> dict[int, bytes]:
        """Gather any ``k`` verified shards, data shards first, hedging onto parity."""
        order = list(range(len(blobs)))  # data shards (idx < k) are naturally first
        it = iter(order)
        pending: dict[Future, int] = {}
        got: dict[int, bytes] = {}
        errors = []

        def launch() -> bool:
            idx = next(it, None)
            if idx is None:
                return False
            key, sha = blobs[idx]
            pending[self._io.submit(self.fetch_any, key, sha, locs.get(key, []))] = idx
            return True

        for _ in range(k):
            launch()
        while pending and len(got) < k:
            done, _ = wait(list(pending), timeout=self.hedge_delay, return_when=FIRST_COMPLETED)
            if not done:
                if launch():
                    self.stat("hedged_reads")
                continue
            for f in done:
                idx = pending.pop(f)
                try:
                    got[idx] = f.result()
                except VaultError as e:
                    errors.append(e)
                    launch()
        if len(got) < k:
            self.stat("read_failures")
            raise ReadError(f"only {len(got)}/{k} shards readable: {errors[:3]}")
        return dict(sorted(got.items())[:k]) if len(got) > k else got

    # -- delete / list -----------------------------------------------------------

    def delete_object(self, bucket: str, key: str, if_match: int | None = None) -> int:
        """Remove an object. Space is reclaimed by GC after the grace period."""
        v = self.meta.delete_object(bucket, key, if_match)
        self.stat("deletes")
        return v

    def list_objects(self, bucket: str, prefix: str = "", after: str = "",
                     limit: int = 1000) -> list[dict]:
        return self.meta.list_objects(bucket, prefix, after, limit)

    # -- introspection -----------------------------------------------------------

    def status(self) -> dict:
        counts = self.meta.node_location_counts()
        nodes = [{**vars(n), "replicas": counts.get(n.id, 0)} for n in self.membership.nodes()]
        return {"nodes": nodes, "metadata": self.meta.stats(), "gateway": self.stats}
