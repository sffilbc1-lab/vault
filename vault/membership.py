"""Cluster membership and failure detection.

Each node moves through  alive -> suspect -> dead  based on active heartbeats
(plus passive signals from failed I/O).  The distinction matters:

* suspect: a probe or request failed recently.  New writes avoid the node and
  reads try it last, but its replicas still *count* toward durability, so a
  transient blip or GC pause does not trigger a repair storm.
* dead: unreachable for longer than ``dead_after``.  Its replicas stop counting
  and the repair service re-creates them elsewhere.

A node that comes back is marked alive again; any surplus replicas it still
holds are trimmed by the rebalancer.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .client import NodeClient
from .errors import LocalResourceError, VaultError
from .metadata import MetadataStore, NodeInfo


class Membership:
    def __init__(self, meta: MetadataStore, client: NodeClient, interval: float = 1.0,
                 dead_after: float = 10.0, probe_timeout: float = 1.0):
        self.meta = meta
        self.client = client
        self.interval = interval
        self.dead_after = dead_after
        self.probe_timeout = probe_timeout
        self._lock = threading.Lock()
        self._nodes: dict[str, NodeInfo] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(16, thread_name_prefix="probe")
        self.refresh()

    # -- view ----------------------------------------------------------------

    def refresh(self) -> None:
        nodes = {n.id: n for n in self.meta.list_nodes()}
        with self._lock:
            self._nodes = nodes

    def nodes(self) -> list[NodeInfo]:
        with self._lock:
            return [NodeInfo(**vars(n)) for n in self._nodes.values()]

    def by_id(self) -> dict[str, NodeInfo]:
        return {n.id: n for n in self.nodes()}

    def get(self, node_id: str) -> NodeInfo | None:
        with self._lock:
            n = self._nodes.get(node_id)
            return NodeInfo(**vars(n)) if n else None

    def writable(self) -> list[NodeInfo]:
        return [n for n in self.nodes() if n.writable]

    # -- admin ---------------------------------------------------------------

    def add_node(self, node_id: str, addr: str, zone: str = "zone-a", weight: float = 1.0) -> None:
        self.meta.upsert_node(node_id, addr, zone, weight)
        self.meta.log_event("node_added", node=node_id, addr=addr, zone=zone)
        self.refresh()

    def set_admin(self, node_id: str, admin: str) -> None:
        self.meta.set_node_admin(node_id, admin)
        self.meta.log_event(f"node_{admin}", node=node_id)
        self.refresh()

    # -- health --------------------------------------------------------------

    def _transition(self, node: NodeInfo, health: str, last_seen: float | None = None) -> None:
        if node.health != health:
            self.meta.log_event("node_health", node=node.id, old=node.health, new=health)
        self.meta.set_node_health(node.id, health, last_seen)
        with self._lock:
            cur = self._nodes.get(node.id)
            if cur:
                cur.health = health
                if last_seen is not None:
                    cur.last_seen = last_seen

    def report_failure(self, node_id: str) -> None:
        """Passive failure signal from the data path."""
        n = self.get(node_id)
        if n and n.health == "alive":
            self._transition(n, "suspect")

    def _probe(self, node: NodeInfo) -> None:
        now = time.time()
        try:
            info = self.client.health(node.addr, timeout=self.probe_timeout)
            if info.get("id") != node.id:
                raise VaultError(f"identity mismatch: {info.get('id')} at {node.addr}")
            self._transition(node, "alive", now)
        except LocalResourceError:
            pass  # our own problem (e.g. out of file descriptors): inconclusive
        except VaultError:
            self._transition(node, "dead" if now - node.last_seen > self.dead_after else "suspect")

    def probe_all(self) -> None:
        self.refresh()
        nodes = [n for n in self.nodes() if n.admin != "removed"]
        list(self._pool.map(self._probe, nodes))

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.probe_all()
                except Exception as e:  # never let the detector die
                    self.meta.log_event("detector_error", error=repr(e))

        self._thread = threading.Thread(target=loop, name="failure-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the heartbeat loop (restartable with start())."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def close(self) -> None:
        """Stop the loop and release the probe threads. Final."""
        self.stop()
        self._pool.shutdown(wait=True, cancel_futures=True)
