"""Background maintenance: repair, scrubbing, anti-entropy, rebalancing and GC.

All jobs are idempotent and safe to run concurrently with client traffic:
data is always *copied first, then the new location recorded, and only then the
old copy removed*, so no job ever lowers the redundancy of a chunk.

repair        Finds chunks with fewer healthy copies/shards than desired and restores
              them, most-at-risk first (lowest margin above data loss).  Replicas
              are copied from a verified survivor; EC shards are rebuilt from any
              k surviving shards.  Parallel, so recovery time scales with cluster size.
scrub         Asks every node to re-verify checksums of everything it stores; rotten
              blobs are quarantined and their locations dropped -> repair fixes them.
anti_entropy  Reconciles each node's actual inventory with metadata: records copies
              metadata did not know about, forgets copies that vanished, and deletes
              orphans (e.g. from writes that never committed) after a grace period.
rebalance     Moves data toward its rendezvous-hash placement after membership
              changes (node joins, drains, returns from the dead), trimming surplus
              replicas.  Throttled by ``max_moves`` per pass.
gc            Deletes blobs no object version references any more, after a grace
              period that protects in-flight readers and concurrent dedup.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import erasure
from .core import Vault, _Spec
from .errors import VaultError
from .metadata import BlobRec, NodeInfo
from .placement import zone_order
from .policy import Scheme


@dataclass
class ChunkState:
    chunk_id: str
    scheme: Scheme
    blobs: list[BlobRec]
    healthy: dict[str, list[str]]    # blob_key -> nodes whose copy counts
    readable: dict[str, list[str]]   # blob_key -> nodes we may read from

    @property
    def margin(self) -> int:
        """How many more losses the chunk survives (negative = currently unreadable)."""
        if self.scheme.replicated:
            return len(self.readable[self.blobs[0].key]) - 1
        return sum(1 for b in self.blobs if self.readable[b.key]) - self.scheme.k


class Maintenance:
    def __init__(self, vault: Vault, interval: float = 5.0, gc_grace: float = 300.0,
                 orphan_grace: float = 3600.0, max_moves: int = 256, parallelism: int = 8,
                 scrub_every: int = 12):
        self.vault = vault
        self.meta = vault.meta
        self.client = vault.client
        self.interval = interval
        self.gc_grace = gc_grace
        self.orphan_grace = orphan_grace
        self.max_moves = max_moves
        self.scrub_every = scrub_every
        self._pool = ThreadPoolExecutor(parallelism, thread_name_prefix="repair")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._passes = 0
        self.totals: Counter = Counter()

    # -- helpers -----------------------------------------------------------------

    def _state(self, chunk_id: str, by_id: dict[str, NodeInfo]) -> ChunkState | None:
        blobs = self.meta.chunk_blobs(chunk_id)
        if not blobs:
            return None
        healthy = {b.key: [n for n in b.locations if n in by_id and by_id[n].counts]
                   for b in blobs}
        readable = {b.key: [n for n in b.locations if n in by_id and by_id[n].readable]
                    for b in blobs}
        return ChunkState(chunk_id, Scheme.parse(blobs[0].scheme), blobs, healthy, readable)

    def _desired(self, blob: BlobRec, scheme: Scheme, active: list[NodeInfo]) -> list[str]:
        order = zone_order(blob.chunk_id, active)
        if not order:
            return []
        if scheme.replicated:
            return [n.id for n in order[:blob.want]]
        return [order[blob.idx % len(order)].id]

    # -- repair ------------------------------------------------------------------

    def repair_pass(self) -> Counter:
        stats: Counter = Counter()
        by_id = self.vault.membership.by_id()
        states = [s for s in (self._state(c, by_id) for c in self.meta.deficient_chunks()) if s]
        states.sort(key=lambda s: s.margin)  # most endangered first
        for result in self._pool.map(lambda s: self._repair_chunk(s, by_id), states):
            stats.update(result)
        return stats

    def _repair_chunk(self, st: ChunkState, by_id: dict[str, NodeInfo]) -> Counter:
        stats: Counter = Counter()
        writable = [n for n in by_id.values() if n.writable]
        if not writable:
            return stats
        chunk_nodes = {n for b in st.blobs for n in b.locations}
        deficient = [b for b in st.blobs if len(st.healthy[b.key]) < b.want]
        rebuilt: dict[int, bytes] = {}
        if not st.scheme.replicated:
            # Shards that have no readable copy at all must be recomputed from k others.
            lost = [b for b in deficient if not st.readable[b.key]]
            if lost:
                try:
                    rebuilt = self._reconstruct(st, [b.idx for b in lost])
                    stats["shards_reconstructed"] += len(lost)
                except VaultError as e:
                    stats["unrecoverable_chunks"] += 1
                    self.meta.log_event("chunk_at_risk", chunk=st.chunk_id, margin=st.margin,
                                        error=str(e))
                    return stats
        for b in deficient:
            try:
                data = rebuilt.get(b.idx)
                if data is None:
                    data = self.vault.fetch_any(b.key, b.sha256, st.readable[b.key])
            except VaultError as e:
                stats["unrecoverable_blobs"] += 1
                self.meta.log_event("blob_at_risk", blob=b.key, error=str(e))
                continue
            need = b.want - len(st.healthy[b.key])
            spec = _Spec(BlobRec(b.key, b.chunk_id, b.idx, b.sha256, b.size, b.scheme, need), data)
            placed = self.vault.upload([spec], writable, exclude=set(b.locations),
                                       avoid=chunk_nodes,
                                       prefer=self._desired(b, st.scheme, writable))[b.key]
            for n in placed:
                self.meta.add_location(b.key, n)
                chunk_nodes.add(n)
            stats["replicas_created"] += len(placed)
            if placed:
                self.meta.log_event("repaired", blob=b.key, to=placed, margin=st.margin)
        return stats

    def _reconstruct(self, st: ChunkState, wanted: list[int]) -> dict[int, bytes]:
        k, m = st.scheme.k, st.scheme.m
        got: dict[int, bytes] = {}
        for b in st.blobs:
            if len(got) >= k:
                break
            if b.idx in wanted or not st.readable[b.key]:
                continue
            try:
                got[b.idx] = self.vault.fetch_any(b.key, b.sha256, st.readable[b.key])
            except VaultError:
                continue
        if len(got) < k:
            raise VaultError(f"only {len(got)}/{k} shards available")
        out = erasure.reconstruct(got, k, m, wanted)
        for b in st.blobs:
            if b.idx in out and hashlib.sha256(out[b.idx]).hexdigest() != b.sha256:
                raise VaultError(f"reconstructed shard {b.key} failed verification")
        return out

    # -- scrub / anti-entropy ------------------------------------------------------

    def scrub_pass(self) -> Counter:
        stats: Counter = Counter()
        for node in self.vault.membership.nodes():
            if node.health != "alive" or node.admin == "removed":
                continue
            try:
                res = self.client.scrub(node.addr)
            except VaultError:
                continue
            stats["scrubbed"] += res["checked"]
            for key in res["corrupt"]:
                if self.meta.remove_location(key, node.id):
                    self.meta.log_event("replica_corrupt", blob=key, node=node.id, via="scrub")
                stats["corrupt_found"] += 1
        return stats

    def anti_entropy_pass(self) -> Counter:
        stats: Counter = Counter()
        now = time.time()
        for node in self.vault.membership.nodes():
            if node.health != "alive" or node.admin == "removed":
                continue
            known = self.meta.node_blob_keys(node.id)  # snapshot *before* inventory
            try:
                inv = self.client.inventory(node.addr)
            except VaultError:
                continue
            present = {item["key"] for item in inv}
            info = self.meta.blob_info(list(present))
            for item in inv:
                key = item["key"]
                state, sha = info.get(key, (None, None))
                if state == "active" and item["sha256"] == sha:
                    if key not in known and self.meta.add_location(key, node.id):
                        stats["replicas_rediscovered"] += 1
                elif now - item["mtime"] > self.orphan_grace:
                    try:
                        self.client.delete_blob(node.addr, key)
                        stats["orphans_deleted"] += 1
                    except VaultError:
                        pass
            for key in known - present:
                if self.meta.remove_location(key, node.id):
                    self.meta.log_event("replica_missing", blob=key, node=node.id,
                                        via="anti_entropy")
                    stats["replicas_lost"] += 1
        return stats

    # -- rebalance ------------------------------------------------------------------

    def rebalance_pass(self) -> Counter:
        stats: Counter = Counter()
        by_id = self.vault.membership.by_id()
        active = [n for n in by_id.values() if n.writable]
        if not active:
            return stats
        # Rebalancing while nodes are flapping would churn data; wait for a stable view.
        if any(n.health == "suspect" for n in by_id.values() if n.admin == "active"):
            return stats
        after, moves = "", 0
        while moves < self.max_moves:
            page = self.meta.blobs_page(after)
            if not page:
                break
            after = page[-1].key
            for res in self._pool.map(
                    lambda b: self._rebalance_blob(b, Scheme.parse(b.scheme), by_id, active),
                    page):
                stats.update(res)
                moves += res["replicas_moved"]
        # Draining nodes that are now empty are done.
        counts = self.meta.node_location_counts()
        for n in by_id.values():
            if n.admin == "draining" and not counts.get(n.id):
                self.vault.membership.set_admin(n.id, "drained")
                stats["nodes_drained"] += 1
        return stats

    def _rebalance_blob(self, b: BlobRec, scheme: Scheme, by_id: dict[str, NodeInfo],
                        active: list[NodeInfo]) -> Counter:
        stats: Counter = Counter()
        healthy = [n for n in b.locations if n in by_id and by_id[n].counts]
        if len(healthy) < b.want:
            return stats  # under-replicated: repair's job, not ours
        desired = self._desired(b, scheme, active)
        have = set(b.locations)
        missing = [d for d in desired if d not in have]
        if missing:
            sources = [n for n in b.locations if n in by_id and by_id[n].readable]
            try:
                data = self.vault.fetch_any(b.key, b.sha256, sources)
            except VaultError:
                return stats
            for d in missing:
                try:
                    self.client.put_blob(by_id[d].addr, b.key, data, b.sha256)
                except VaultError:
                    continue
                self.meta.add_location(b.key, d)
                have.add(d)
                stats["replicas_moved"] += 1
        if not all(d in have for d in desired):
            return stats
        # Every desired node has a copy: surplus copies elsewhere can go.
        for n in b.locations:
            if n in desired:
                continue
            node = by_id.get(n)
            if node is None or node.health != "alive":
                # Unreachable copies are left alone: the node may come back, and
                # then this same pass trims them for real. Nodes that are gone for
                # good are handled by remove_node().
                continue
            # Location first, then the bytes: readers never chase a known-deleted copy.
            self.meta.remove_location(b.key, n)
            stats["replicas_trimmed"] += 1
            try:
                self.client.delete_blob(node.addr, b.key)
            except VaultError:
                pass  # anti-entropy will clean the orphan later
        return stats

    # -- garbage collection --------------------------------------------------------

    def gc_pass(self, batch: int = 1000) -> Counter:
        stats: Counter = Counter()
        cutoff = time.time() - self.gc_grace
        by_id = self.vault.membership.by_id()
        candidates = self.meta.gc_candidates(cutoff)
        for i in range(0, len(candidates), batch):
            # Once marked 'deleting', concurrent writes can no longer dedup onto these.
            marked = self.meta.mark_deleting(candidates[i:i + batch], cutoff)
            locs = self.meta.locations_many(marked)
            jobs = [(key, by_id[n]) for key in marked for n in locs[key]
                    if n in by_id and by_id[n].health == "alive"]

            def delete(job):
                key, node = job
                try:
                    self.client.delete_blob(node.addr, key)
                except VaultError:
                    pass  # becomes an orphan; anti-entropy removes it

            list(self._pool.map(delete, jobs))
            self.meta.drop_blobs(marked)
            stats["blobs_collected"] += len(marked)
        return stats

    # -- scheduling --------------------------------------------------------------------

    def run_once(self, scrub: bool = True) -> Counter:
        stats: Counter = Counter()
        self.vault.membership.probe_all()
        if scrub:
            stats.update(self.scrub_pass())
            stats.update(self.anti_entropy_pass())
        stats.update(self.repair_pass())
        stats.update(self.rebalance_pass())
        stats.update(self.gc_pass())
        self.totals.update(stats)
        return stats

    def converge(self, max_rounds: int = 10) -> Counter:
        """Run passes until a pass does no work (used by tests and the CLI)."""
        total: Counter = Counter()
        for _ in range(max_rounds):
            s = self.run_once()
            total.update(s)
            work = {k: v for k, v in s.items() if k not in ("scrubbed",) and v}
            if not work:
                break
        return total

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(self.interval):
                self._passes += 1
                try:
                    self.run_once(scrub=self._passes % self.scrub_every == 0)
                except Exception as e:
                    self.meta.log_event("maintenance_error", error=repr(e))

        self._thread = threading.Thread(target=loop, name="maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background loop (restartable with start())."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def close(self) -> None:
        """Stop the loop and release the worker threads. Final."""
        self.stop()
        self._pool.shutdown(wait=True, cancel_futures=True)
