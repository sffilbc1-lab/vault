"""Tests for the dashboard adapter against a real cluster and the unmodified gateway API.

Run from vault-v2:  python3 -m unittest discover -s dashboard/tests
(Kept separate from the frozen backend suite in tests/.)
"""

import hashlib
import http.client
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]            # vault-v2
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "dashboard")]

from harness import Cluster                           # noqa: E402  (read-only use)
from vault.api import make_server as make_gateway     # noqa: E402  (frozen, used as-is)
from vault import Policy                              # noqa: E402
import server as dash                                 # noqa: E402


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.c = Cluster(6, 3)
        self.c.vault.create_bucket("files", Policy(n=3, chunk_size=64 * 1024))
        self.c.vault.create_bucket("archive", Policy(scheme="erasure", k=4, m=2, chunk_size=64 * 1024))
        self.gw = make_gateway(self.c.vault, self.c.maint, port=0)
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()
        gw_url = f"http://127.0.0.1:{self.gw.server_address[1]}"
        self.srv, self.adapter = dash.make_server(gw_url, port=0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.adapter.close()
        if self.gw is not None:
            self.gw.shutdown(); self.gw.server_close()
        self.c.close()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def json(self, method, path, body=None):
        status, _, data = self.req(method, path, body)
        return status, json.loads(data)

    # --------------------------------------------------------------------------

    def test_serves_ui_and_blocks_traversal(self):
        status, h, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"VAULT", body)
        self.assertTrue(h["Content-Type"].startswith("text/html"))
        for path in ("/static/app.js", "/static/app.css"):
            self.assertEqual(self.req("GET", path)[0], 200)
        for bad in ("/static/../server.py", "/../server.py", "/static/%2e%2e/server.py"):
            self.assertEqual(self.req("GET", bad)[0], 404, bad)

    def test_snapshot_reflects_real_cluster_state(self):
        data = os.urandom(150_000)
        self.c.vault.put_object("archive", "a.bin", data)
        status, snap = self.json("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertTrue(snap["gateway"]["ok"])
        truth = self.c.vault.status()
        self.assertEqual(snap["cluster"]["metadata"], truth["metadata"])
        self.assertEqual(sorted(n["id"] for n in snap["cluster"]["nodes"]), sorted(self.c.nodes))
        self.assertEqual({b["name"] for b in snap["buckets"]}, {"files", "archive"})
        # Per-node disk stats come straight from each node's own /health.
        for nid, node in self.c.nodes.items():
            live = snap["nodes_live"][nid]
            self.assertTrue(live["reachable"])
            self.assertEqual(live["health"]["blobs"], node.store.usage()["blobs"])
            self.assertEqual(live["faults"]["down"], False)
        self.assertEqual({e["kind"] for e in snap["events"]}, {"node_added"})

    def test_upload_download_verify_delete_through_proxy(self):
        data = os.urandom(300_000)
        status, r = self.json("PUT", "/api/gw/buckets/files/docs%2Freport.bin", data)
        self.assertEqual(status, 200)
        self.assertEqual(r["etag"], hashlib.sha256(data).hexdigest())
        # Stored in the real backend under the decoded key.
        self.assertEqual(self.c.vault.get_object("files", "docs/report.bin"), data)
        status, h, body = self.req("GET", "/api/gw/buckets/files/docs%2Freport.bin?dl=1")
        self.assertEqual((status, body), (200, data))
        self.assertIn('filename="report.bin"', h["Content-Disposition"])
        status, v = self.json("POST", "/api/verify/files/docs%2Freport.bin")
        self.assertTrue(v["ok"])
        self.assertEqual(v["actual"], v["expected"])
        status, listing = self.json("GET", "/api/gw/buckets/files?prefix=docs/")
        self.assertEqual([o["key"] for o in listing], ["docs/report.bin"])
        self.assertEqual(self.json("DELETE", "/api/gw/buckets/files/docs%2Freport.bin")[0], 200)
        _, snap = self.json("GET", "/api/snapshot")
        kinds = [a["kind"] for a in snap["activity"]]
        for k in ("object_uploaded", "object_downloaded", "object_verified", "object_deleted"):
            self.assertIn(k, kinds)
        self.assertTrue(all(a["source"] == "dashboard" for a in snap["activity"]))
        self.assertTrue(snap["verifications"]["files/docs/report.bin"]["ok"])

    def test_gateway_errors_pass_through(self):
        self.assertEqual(self.req("GET", "/api/gw/buckets/files/missing")[0], 404)
        self.assertEqual(self.req("GET", "/api/gw/buckets/nope")[0], 404)
        status, v = self.json("POST", "/api/verify/files/missing")
        self.assertFalse(v["ok"])

    def test_simulated_failure_is_seen_by_vault_and_restored(self):
        self.c.vault.put_object("files", "k", os.urandom(50_000))
        status, f = self.json("POST", "/api/nodes/n2/faults", json.dumps({"down": True}))
        self.assertEqual((status, f["down"]), (200, True))
        self.c.vault.membership.probe_all()  # the gateway's own failure detector
        _, snap = self.json("GET", "/api/snapshot")
        n2 = next(n for n in snap["cluster"]["nodes"] if n["id"] == "n2")
        self.assertEqual(n2["health"], "dead")            # harness uses dead_after=0
        self.assertFalse(snap["nodes_live"]["n2"]["reachable"])
        self.assertTrue(snap["nodes_live"]["n2"]["faults"]["down"])
        self.assertEqual(self.c.vault.get_object("files", "k").__len__(), 50_000)
        self.json("POST", "/api/nodes/n2/faults", json.dumps({"down": False}))
        self.c.vault.membership.probe_all()
        _, snap = self.json("GET", "/api/snapshot")
        n2 = next(n for n in snap["cluster"]["nodes"] if n["id"] == "n2")
        self.assertEqual(n2["health"], "alive")
        # Only whitelisted fault keys are forwarded.
        self.json("POST", "/api/nodes/n2/faults", json.dumps({"blocked": ["gateway"]}))
        self.assertEqual(self.c.nodes["n2"].faults.blocked, set())

    def test_corrupt_random_is_detected_by_vault_scrub(self):
        self.c.vault.put_object("files", "k", os.urandom(50_000))
        holder = next(n for n, node in self.c.nodes.items() if node.store.usage()["blobs"])
        status, r = self.json("POST", f"/api/nodes/{holder}/corrupt-random")
        self.assertEqual(status, 200)
        status, stats = self.json("POST", "/api/gw/cluster/maintenance")
        self.assertEqual(stats["corrupt_found"], 1)
        _, snap = self.json("GET", "/api/snapshot")
        corrupt = [e for e in snap["events"] if e["kind"] == "replica_corrupt"]
        self.assertEqual(corrupt[0]["blob"], r["corrupted"])
        self.assertEqual(snap["last_maintenance"]["stats"]["corrupt_found"], 1)

    def test_unknown_node_and_gateway_down(self):
        self.assertEqual(self.req("POST", "/api/nodes/zz/faults", b"{}")[0], 404)
        self.gw.shutdown(); self.gw.server_close()
        status, snap = self.json("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertFalse(snap["gateway"]["ok"])
        self.assertEqual(self.req("GET", "/api/gw/cluster")[0], 502)
        self.gw = None  # already shut down

    def test_adapter_does_not_leak(self):
        for _ in range(3):  # warm up: the probe pool starts its worker threads on first use
            self.json("GET", "/api/snapshot")
        fds = len(os.listdir("/dev/fd"))
        threads = threading.active_count()
        for _ in range(30):
            self.json("GET", "/api/snapshot")
        time.sleep(0.2)
        self.assertLessEqual(len(os.listdir("/dev/fd")), fds + 2)
        self.assertLessEqual(threading.active_count(), threads + 2)
        # close() must release the pool's threads deterministically.
        pool_threads = lambda: [t for t in threading.enumerate() if t.name.startswith("dash-probe")]
        self.assertTrue(pool_threads())
        self.adapter.close()
        self.assertEqual(pool_threads(), [])


if __name__ == "__main__":
    unittest.main()
