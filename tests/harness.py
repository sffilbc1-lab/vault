"""Spin up a real multi-node cluster (HTTP servers on ephemeral ports) in-process."""

from __future__ import annotations

import faulthandler
import shutil
import tempfile
import unittest
from pathlib import Path

from vault import Maintenance, Policy, StorageNode, Vault


class Cluster:
    def __init__(self, nodes: int = 6, zones: int = 3, **vault_kw):
        self.root = Path(tempfile.mkdtemp(prefix="vault-test-"))
        vault_kw.setdefault("hedge_delay", 0.05)
        vault_kw.setdefault("node_timeout", 2.0)
        vault_kw.setdefault("dead_after", 0.0)  # tests decide death explicitly via probes
        self.vault = Vault(str(self.root / "meta.db"), **vault_kw)
        self.maint = Maintenance(self.vault, gc_grace=0.0, orphan_grace=0.0)
        self.nodes: dict[str, StorageNode] = {}
        self.zones = zones
        for _ in range(nodes):
            self.add_node()

    def add_node(self, zone: str | None = None) -> StorageNode:
        i = len(self.nodes) + 1
        zone = zone or f"zone-{chr(ord('a') + (i - 1) % self.zones)}"
        node = StorageNode(f"n{i}", self.root / f"n{i}", zone=zone, fsync=False).start()
        self.nodes[node.node_id] = node
        self.vault.add_node(node.node_id, node.addr, zone)
        return node

    def kill(self, *ids: str) -> None:
        for i in ids:
            self.nodes[i].stop()
        self.vault.membership.probe_all()

    def revive(self, *ids: str) -> None:
        for i in ids:
            n = self.nodes[i]
            self.nodes[i] = StorageNode(i, n.store.root, port=n.port, zone=n.zone,
                                        fsync=False).start()
        self.vault.membership.probe_all()

    def faults(self, node_id: str, **f) -> None:
        self.vault.client.set_faults(self.nodes[node_id].addr, **f)

    def copies(self, blob_key: str) -> list[str]:
        """Ground truth: which nodes physically hold a valid copy."""
        out = []
        for nid, n in self.nodes.items():
            try:
                n.store.get(blob_key)
                out.append(nid)
            except Exception:
                pass
        return out

    def close(self) -> None:
        self.maint.close()
        self.vault.close()
        for n in self.nodes.values():
            n.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class ClusterTest(unittest.TestCase):
    NODES = 6
    ZONES = 3

    TIMEOUT = 120  # watchdog: dump every thread's stack and abort if a test hangs

    def setUp(self):
        faulthandler.dump_traceback_later(self.TIMEOUT, exit=True)
        self.c = Cluster(self.NODES, self.ZONES)
        self.v = self.c.vault
        self.v.create_bucket("rep", Policy(n=3, chunk_size=64 * 1024))
        self.v.create_bucket("ec", Policy(scheme="erasure", k=4, m=2, chunk_size=64 * 1024))

    def tearDown(self):
        faulthandler.cancel_dump_traceback_later()
        self.c.close()

    def blob_keys(self, bucket: str, key: str) -> list[str]:
        m = self.v.head_object(bucket, key).manifest
        return [b[0] for c in m["chunks"] for b in c["blobs"]]
