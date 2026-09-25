import http.client
import json
import os
import threading
import unittest

from harness import ClusterTest

from vault.api import make_server


class ApiTest(ClusterTest):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.v, self.c.maint, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def test_object_lifecycle_over_http(self):
        status, _, _ = self.req("PUT", "/buckets/arch",
                                json.dumps({"scheme": "erasure", "k": 3, "m": 2,
                                            "chunk_size": 50_000}))
        self.assertEqual(status, 201)
        self.assertEqual(self.req("PUT", "/buckets/arch")[0], 409)
        data = os.urandom(333_333)
        status, _, body = self.req("PUT", "/buckets/arch/dir/file.bin", data)
        self.assertEqual(status, 200)
        version = json.loads(body)["version"]
        status, headers, got = self.req("GET", "/buckets/arch/dir/file.bin")
        self.assertEqual((status, got), (200, data))
        self.assertEqual(headers["X-Vault-Version"], str(version))
        # Conditional writes
        self.assertEqual(self.req("PUT", "/buckets/arch/dir/file.bin", b"x",
                                  {"If-None-Match": "*"})[0], 412)
        self.assertEqual(self.req("PUT", "/buckets/arch/dir/file.bin", b"x",
                                  {"If-Match": str(version + 999)})[0], 412)
        self.assertEqual(self.req("PUT", "/buckets/arch/dir/file.bin", b"x",
                                  {"If-Match": str(version)})[0], 200)
        listing = json.loads(self.req("GET", "/buckets/arch?prefix=dir/")[2])
        self.assertEqual([o["key"] for o in listing], ["dir/file.bin"])
        self.assertEqual(self.req("DELETE", "/buckets/arch/dir/file.bin")[0], 200)
        self.assertEqual(self.req("GET", "/buckets/arch/dir/file.bin")[0], 404)
        self.assertEqual(self.req("GET", "/buckets/nope/x")[0], 404)

    def test_cluster_admin_and_unavailability(self):
        self.req("PUT", "/buckets/b", json.dumps({"scheme": "erasure", "k": 4, "m": 2}))
        self.req("PUT", "/buckets/b/k", os.urandom(10_000))
        self.c.kill("n1", "n2", "n3")
        self.assertEqual(self.req("GET", "/buckets/b/k")[0], 503)      # unreadable, not 500
        self.assertEqual(self.req("PUT", "/buckets/b/k2", b"zz")[0], 503)  # quorum refused
        status = json.loads(self.req("GET", "/cluster")[2])
        dead = sorted(n["id"] for n in status["nodes"] if n["health"] == "dead")
        self.assertEqual(dead, ["n1", "n2", "n3"])
        self.assertEqual(self.req("POST", "/cluster/nodes/n1/remove")[0], 200)
        events = json.loads(self.req("GET", "/cluster/events?kind=node_health")[2])
        self.assertTrue(events)


if __name__ == "__main__":
    unittest.main()
