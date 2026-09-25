import hashlib
import itertools
import os
import random
import tempfile
import unittest
from collections import Counter
from dataclasses import dataclass

from vault import erasure
from vault.errors import BlobCorrupt, BlobNotFound, ChecksumMismatch
from vault.node import BlobStore
from vault.placement import place, zone_order
from vault.policy import Policy, Scheme


class ErasureTest(unittest.TestCase):
    def test_every_erasure_pattern_recovers(self):
        k, m = 4, 2
        data = os.urandom(10_001)
        shards = erasure.encode(data, k, m)
        self.assertEqual(len(shards), k + m)
        for lost in itertools.combinations(range(k + m), m):
            avail = {i: s for i, s in enumerate(shards) if i not in lost}
            self.assertEqual(erasure.decode(avail, k, m, len(data)), data, lost)
            rebuilt = erasure.reconstruct(avail, k, m, list(lost))
            for i in lost:
                self.assertEqual(rebuilt[i], shards[i])

    def test_random_parameters(self):
        rng = random.Random(7)
        for _ in range(30):
            k, m = rng.randint(1, 10), rng.randint(0, 5)
            data = rng.randbytes(rng.randint(0, 5000))
            shards = erasure.encode(data, k, m)
            keep = rng.sample(range(k + m), k)
            self.assertEqual(erasure.decode({i: shards[i] for i in keep}, k, m, len(data)), data)

    def test_too_few_shards(self):
        shards = erasure.encode(b"hello world", 3, 2)
        with self.assertRaises(ValueError):
            erasure.decode({0: shards[0], 4: shards[4]}, 3, 2, 11)


@dataclass
class N:
    id: str
    zone: str
    weight: float = 1.0


class PlacementTest(unittest.TestCase):
    def nodes(self, count, zones=3):
        return [N(f"n{i}", f"z{i % zones}") for i in range(count)]

    def test_deterministic_and_zone_diverse(self):
        nodes = self.nodes(9)
        for key in (f"k{i}" for i in range(200)):
            p = place(key, nodes, 3)
            self.assertEqual([n.id for n in p], [n.id for n in place(key, list(reversed(nodes)), 3)])
            self.assertEqual(len({n.zone for n in p}), 3)

    def test_minimal_movement_on_join(self):
        nodes = self.nodes(10)
        keys = [f"key-{i}" for i in range(3000)]
        before = {k: place(k, nodes, 1)[0].id for k in keys}
        after = {k: place(k, nodes + [N("new", "z0")], 1)[0].id for k in keys}
        moved = [k for k in keys if before[k] != after[k]]
        self.assertTrue(all(after[k] == "new" for k in moved))  # only moves *to* the new node
        self.assertLess(len(moved) / len(keys), 0.15)          # ~1/11 expected

    def test_weights(self):
        nodes = [N("big", "z", 3.0), N("small", "z", 1.0)]
        c = Counter(place(f"k{i}", nodes, 1)[0].id for i in range(4000))
        self.assertAlmostEqual(c["big"] / 4000, 0.75, delta=0.04)

    def test_zone_order_covers_all(self):
        nodes = self.nodes(7)
        self.assertEqual(sorted(n.id for n in zone_order("x", nodes)), sorted(n.id for n in nodes))


class PolicyTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(Policy(n=3).write_quorum, 2)
        self.assertEqual(Policy(scheme="erasure", k=4, m=2).write_quorum, 5)
        self.assertEqual(Policy(scheme="erasure", k=6, m=3).overhead, 1.5)
        with self.assertRaises(ValueError):
            Policy(n=3, w=4)
        with self.assertRaises(ValueError):
            Policy(scheme="erasure", k=4, m=2, w=3)
        self.assertEqual(Scheme.parse("ec4+2"), Scheme(False, k=4, m=2))
        self.assertEqual(Policy.from_dict(Policy(n=5, w=3).to_dict()), Policy(n=5, w=3))


class BlobStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = BlobStore(self.dir, fsync=False)

    def test_roundtrip_idempotent_and_verify(self):
        data = b"x" * 1000
        sha = hashlib.sha256(data).hexdigest()
        self.assertTrue(self.s.put("ab.r3", data, sha))
        self.assertFalse(self.s.put("ab.r3", data, sha))
        self.assertEqual(self.s.get("ab.r3"), (data, sha))
        with self.assertRaises(ChecksumMismatch):
            self.s.put("cd.r3", data, "0" * 64)

    def test_bitrot_is_quarantined(self):
        data = os.urandom(512)
        self.s.put("ab.r3", data, hashlib.sha256(data).hexdigest())
        self.s.corrupt("ab.r3", 100)
        with self.assertRaises(BlobCorrupt):
            self.s.get("ab.r3")
        with self.assertRaises(BlobNotFound):
            self.s.get("ab.r3")
        self.assertEqual(len(list(self.s.quarantine_dir.iterdir())), 1)

    def test_scrub_and_torn_write_cleanup(self):
        for i in range(5):
            d = os.urandom(100)
            self.s.put(f"a{i}.r3", d, hashlib.sha256(d).hexdigest())
        self.s.corrupt("a2.r3")
        (self.s.tmp / "a9.r3.torn").write_bytes(b"partial")
        self.assertEqual(self.s.scrub()["corrupt"], ["a2.r3"])
        BlobStore(self.dir, fsync=False)  # "restart"
        self.assertEqual(list(self.s.tmp.iterdir()), [])

    def test_rejects_bad_keys(self):
        with self.assertRaises(ValueError):
            self.s.get("../etc/passwd")


if __name__ == "__main__":
    unittest.main()
