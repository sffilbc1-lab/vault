import hashlib
import os
import shutil
import threading
import time
import unittest

from harness import ClusterTest

from vault import Policy
from vault.errors import (ObjectNotFound, PreconditionFailed, ReadError, WriteQuorumError)
from vault.policy import Scheme

MB = 1024 * 1024


class BasicTest(ClusterTest):
    def test_roundtrip_multi_chunk(self):
        for bucket in ("rep", "ec"):
            data = os.urandom(MB + 12345)
            om = self.v.put_object(bucket, "big", data)
            self.assertEqual(om.etag, hashlib.sha256(data).hexdigest())
            self.assertEqual(len(om.manifest["chunks"]), 17)
            self.assertEqual(self.v.get_object(bucket, "big"), data)

    def test_empty_overwrite_delete_list(self):
        self.v.put_object("rep", "a/empty", b"")
        self.assertEqual(self.v.get_object("rep", "a/empty"), b"")
        v1 = self.v.put_object("rep", "a/x", b"one").version
        v2 = self.v.put_object("rep", "a/x", b"two").version
        self.assertGreater(v2, v1)
        self.assertEqual(self.v.get_object("rep", "a/x"), b"two")
        self.v.put_object("rep", "b/y", b"z")
        self.assertEqual([o["key"] for o in self.v.list_objects("rep", prefix="a/")],
                         ["a/empty", "a/x"])
        self.v.delete_object("rep", "a/x")
        with self.assertRaises(ObjectNotFound):
            self.v.get_object("rep", "a/x")

    def test_replicas_span_zones_and_storage_overhead(self):
        self.v.put_object("rep", "k", os.urandom(200_000))
        self.v.put_object("ec", "k", os.urandom(200_000))
        for key in self.blob_keys("rep", "k"):
            holders = self.c.copies(key)
            self.assertEqual(len(holders), 3)
            self.assertEqual(len({self.c.nodes[n].zone for n in holders}), 3)
        for chunk in self.v.head_object("ec", "k").manifest["chunks"]:
            holders = [self.c.copies(b[0])[0] for b in chunk["blobs"]]
            self.assertEqual(len(set(holders)), 6, "EC shards must be on distinct nodes")
        stats = self.v.meta.stats()
        self.assertAlmostEqual(stats["physical_bytes"] / stats["logical_bytes"], (3 + 1.5) / 2,
                               delta=0.05)

    def test_dedup_shares_chunks(self):
        data = os.urandom(300_000)
        self.v.put_object("rep", "a", data)
        before = self.v.meta.stats()["blobs"]
        self.v.put_object("rep", "b", data)
        self.assertEqual(self.v.meta.stats()["blobs"], before)
        self.v.delete_object("rep", "a")
        self.c.maint.converge()
        self.assertEqual(self.v.get_object("rep", "b"), data)


class FailureTest(ClusterTest):
    def test_reads_survive_tolerated_failures(self):
        data = os.urandom(500_000)
        self.v.put_object("rep", "k", data)
        self.v.put_object("ec", "k", data)
        self.c.kill("n1", "n2")  # 2 of 6, replication survives n-1=2, EC survives m=2
        self.assertEqual(self.v.get_object("rep", "k"), data)
        self.assertEqual(self.v.get_object("ec", "k"), data)

    def test_writes_fall_back_to_other_nodes(self):
        self.c.kill("n1", "n2", "n3")
        data = os.urandom(200_000)
        self.v.put_object("rep", "k", data)
        for key in self.blob_keys("rep", "k"):
            self.assertEqual(sorted(self.c.copies(key)), ["n4", "n5", "n6"])
        self.assertEqual(self.v.get_object("rep", "k"), data)

    def test_write_quorum_enforced_and_atomic(self):
        self.v.put_object("rep", "k", b"old")
        self.v.put_object("ec", "k", b"old")
        self.c.kill("n1", "n2", "n3", "n4", "n5")
        with self.assertRaises(WriteQuorumError):
            self.v.put_object("rep", "k", os.urandom(100_000))  # w=2, 1 node up
        self.c.revive("n1", "n2", "n3")
        self.c.kill("n6")
        with self.assertRaises(WriteQuorumError):
            self.v.put_object("ec", "k", os.urandom(100_000))  # w=5, 4 nodes up
        # A failed write must never become visible.
        self.c.revive("n4", "n5", "n6")
        self.assertEqual(self.v.get_object("rep", "k"), b"old")
        self.assertEqual(self.v.get_object("ec", "k"), b"old")

    def test_network_partition(self):
        data = os.urandom(300_000)
        self.v.put_object("rep", "a", data)
        self.v.put_object("ec", "a", data)
        # Gateway can no longer reach n1/n2, but they are still running.
        self.c.faults("n1", blocked=["gateway"])
        self.c.faults("n2", blocked=["gateway"])
        self.assertEqual(self.v.get_object("rep", "a"), data)
        self.assertEqual(self.v.get_object("ec", "a"), data)
        fresh = os.urandom(300_000)  # new content, so dedup can't reuse n1/n2 copies
        self.v.put_object("rep", "b", fresh)
        # Only 4 nodes reachable: EC 4+2 (w=5 distinct nodes) must refuse rather
        # than accept a write that could not survive another failure.
        with self.assertRaises(WriteQuorumError):
            self.v.put_object("ec", "b", fresh)
        self.assertEqual(self.v.membership.get("n1").health, "suspect")
        for key in self.blob_keys("rep", "b"):
            self.assertFalse({"n1", "n2"} & set(self.c.copies(key)))
        self.c.faults("n1", blocked=[])
        self.c.faults("n2", blocked=[])
        self.v.membership.probe_all()
        self.assertEqual(self.v.membership.get("n1").health, "alive")

    def test_unrecoverable_is_reported_not_silent(self):
        data = os.urandom(100_000)
        self.v.put_object("ec", "k", data)
        self.c.kill("n1", "n2", "n3")
        with self.assertRaises(ReadError):
            self.v.get_object("ec", "k")
        stats = self.c.maint.repair_pass()
        self.assertGreater(stats["unrecoverable_chunks"], 0)
        self.assertTrue(self.v.meta.events("chunk_at_risk"))
        self.c.revive("n1", "n2", "n3")
        self.assertEqual(self.v.get_object("ec", "k"), data)

    def test_hedged_read_bounds_tail_latency(self):
        data = os.urandom(50_000)
        self.v.put_object("rep", "k", data)
        primary = self.v._ordered(self.v.meta.locations_many([self.blob_keys("rep", "k")[0]])
                                  [self.blob_keys("rep", "k")[0]], self.blob_keys("rep", "k")[0])[0]
        self.c.faults(primary.id, latency=1.5)
        t = time.time()
        self.assertEqual(self.v.get_object("rep", "k"), data)
        self.assertLess(time.time() - t, 0.8)
        self.assertGreater(self.v.stats.get("hedged_reads", 0), 0)


class CorruptionTest(ClusterTest):
    def test_bitrot_detected_on_read_and_repaired(self):
        for bucket in ("rep", "ec"):
            data = os.urandom(150_000)
            self.v.put_object(bucket, "k", data)
            victim_key = self.blob_keys(bucket, "k")[0]
            victim = self.c.copies(victim_key)[0]
            self.c.nodes[victim].store.corrupt(victim_key)
            self.assertEqual(self.v.get_object(bucket, "k"), data)
            self.c.maint.converge()
            want = 3 if bucket == "rep" else 1
            self.assertEqual(len(self.c.copies(victim_key)), want)

    def test_scrub_finds_rot_without_reads(self):
        self.v.put_object("rep", "k", os.urandom(100_000))
        keys = self.blob_keys("rep", "k")
        for key in keys:
            self.c.nodes[self.c.copies(key)[0]].store.corrupt(key)
        stats = self.c.maint.converge()
        self.assertEqual(stats["corrupt_found"], len(keys))
        for key in keys:
            self.assertEqual(len(self.c.copies(key)), 3)

    def test_corruption_in_transit(self):
        data = os.urandom(80_000)
        self.v.put_object("rep", "k", data)
        for nid in list(self.c.nodes)[:2]:
            self.c.faults(nid, corrupt_reads=True)
        for _ in range(3):
            self.assertEqual(self.v.get_object("rep", "k"), data)


class RepairTest(ClusterTest):
    NODES = 8  # EC 4+2 needs >= 6 live failure domains to be fully spread after 2 deaths
    def assert_fully_replicated(self, bucket, key):
        policy = self.v.bucket_policy(bucket)
        alive = {i for i, n in self.c.nodes.items() if n.running}
        for chunk in self.v.head_object(bucket, key).manifest["chunks"]:
            holders = [set(self.c.copies(b[0])) & alive for b in chunk["blobs"]]
            if policy.replicated:
                self.assertEqual(len(holders[0]), policy.n)
            else:
                self.assertTrue(all(len(h) == 1 for h in holders))

    def test_repair_restores_redundancy_after_node_death(self):
        data = os.urandom(400_000)
        self.v.put_object("rep", "k", data)
        self.v.put_object("ec", "k", data)
        self.c.kill("n1", "n2")
        stats = self.c.maint.converge()
        self.assertGreater(stats["replicas_created"], 0)
        self.assert_fully_replicated("rep", "k")
        self.assert_fully_replicated("ec", "k")
        self.assertEqual(self.v.meta.stats()["deficient_chunks"], 0)
        # Redundancy was fully rebuilt, so we can now lose two *more* nodes.
        self.c.kill("n3", "n4")
        self.assertEqual(self.v.get_object("rep", "k"), data)
        self.assertEqual(self.v.get_object("ec", "k"), data)

    def test_returning_node_surplus_is_trimmed(self):
        data = os.urandom(200_000)
        self.v.put_object("rep", "k", data)
        self.c.kill("n1")
        self.c.maint.converge()
        self.c.revive("n1")
        self.c.maint.converge()
        for key in self.blob_keys("rep", "k"):
            self.assertEqual(len(self.c.copies(key)), 3)

    def test_wiped_disk_detected_by_anti_entropy(self):
        self.v.put_object("rep", "k", os.urandom(200_000))
        # Wipe whichever node holds the most replicas (placement is hash-based).
        counts = self.v.meta.node_location_counts()
        victim = max(counts, key=counts.get)
        node = self.c.nodes[victim]
        before = counts[victim]
        self.assertGreater(before, 0)
        shutil.rmtree(node.store.blobs)
        node.store.blobs.mkdir()
        stats = self.c.maint.converge()
        self.assertEqual(stats["replicas_lost"], before)
        for key in self.blob_keys("rep", "k"):
            self.assertEqual(len(self.c.copies(key)), 3)

    def test_orphans_from_uncommitted_writes_are_deleted(self):
        orphan = b"orphan"
        self.c.nodes["n1"].store.put("ff00.r3", orphan, hashlib.sha256(orphan).hexdigest())
        stats = self.c.maint.converge()
        self.assertEqual(stats["orphans_deleted"], 1)
        self.assertEqual(self.c.copies("ff00.r3"), [])


class RebalanceTest(ClusterTest):
    def test_scale_out_moves_data_to_new_nodes(self):
        objs = {f"o{i}": os.urandom(70_000) for i in range(20)}
        for k, d in objs.items():
            self.v.put_object("rep", k, d)
            self.v.put_object("ec", k, d)
        for _ in range(3):
            self.c.add_node()
        stats = self.c.maint.converge()
        self.assertGreater(stats["replicas_moved"], 0)
        counts = self.v.meta.node_location_counts()
        for new in ("n7", "n8", "n9"):
            self.assertGreater(counts.get(new, 0), 0)
        for k, d in objs.items():
            self.assertEqual(self.v.get_object("rep", k), d)
            self.assertEqual(self.v.get_object("ec", k), d)
            for key in self.blob_keys("rep", k):
                self.assertEqual(len(self.c.copies(key)), 3)
        # A second pass has nothing left to do: placement is stable.
        again = self.c.maint.run_once()
        self.assertEqual(again["replicas_moved"] + again["replicas_trimmed"], 0)

    def test_drain_evacuates_node(self):
        data = os.urandom(300_000)
        self.v.put_object("rep", "k", data)
        self.v.put_object("ec", "k", data)
        self.v.drain_node("n1")
        self.c.maint.converge()
        self.assertEqual(self.v.membership.get("n1").admin, "drained")
        self.assertEqual(self.v.meta.node_location_counts().get("n1", 0), 0)
        self.c.kill("n1", "n2")  # n1 gone for good + one more failure
        self.assertEqual(self.v.get_object("rep", "k"), data)
        self.assertEqual(self.v.get_object("ec", "k"), data)


class ConcurrencyTest(ClusterTest):
    def test_concurrent_writers_last_commit_wins(self):
        payloads = {}
        errors = []

        def writer(i):
            try:
                for j in range(5):
                    d = os.urandom(20_000 + i)
                    om = self.v.put_object("rep", "hot", d)
                    payloads[om.version] = d
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(payloads), 40)  # every commit got a unique version
        head = self.v.head_object("rep", "hot")
        self.assertEqual(head.version, max(payloads))
        self.assertEqual(self.v.get_object("rep", "hot"), payloads[head.version])

    def test_compare_and_swap(self):
        v1 = self.v.put_object("rep", "cas", b"v1", if_match=0).version
        with self.assertRaises(PreconditionFailed):
            self.v.put_object("rep", "cas", b"again", if_match=0)
        results = []

        def attempt(i):
            try:
                self.v.put_object("rep", "cas", f"w{i}".encode(), if_match=v1)
                results.append(i)
            except PreconditionFailed:
                pass

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 1)
        self.assertEqual(self.v.get_object("rep", "cas"), f"w{results[0]}".encode())

    def test_readers_never_see_torn_objects_during_overwrite_and_gc(self):
        versions = [os.urandom(150_000) for _ in range(4)]
        valid = {hashlib.sha256(d).hexdigest() for d in versions}
        self.v.put_object("ec", "k", versions[0])
        self.c.maint.gc_grace = 1.0
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    got = self.v.get_object("ec", "k")
                    if hashlib.sha256(got).hexdigest() not in valid:
                        bad.append("torn")
                except Exception as e:
                    bad.append(repr(e))

        readers = [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in readers]
        try:
            for i in range(12):
                self.v.put_object("ec", "k", versions[i % 4])
                self.c.maint.gc_pass()
        finally:
            # Always stop the readers, so a failing write fails the test instead of
            # hanging the whole run.
            stop.set()
            [t.join() for t in readers]
        self.assertEqual(bad, [])


class ChaosTest(ClusterTest):
    """Random crashes, partitions, slowness and bit rot under concurrent load."""

    NODES = 8
    ZONES = 4

    def test_chaos(self):
        import random
        rng = random.Random(1234)
        v, c = self.v, self.c
        c.maint.orphan_grace = 30.0  # never reap blobs of writes still in flight
        v.create_bucket("ec63", Policy(scheme="erasure", k=3, m=3, chunk_size=32 * 1024))
        written: dict[tuple[str, str], set[str]] = {}
        lock = threading.Lock()
        stop = threading.Event()
        load = threading.Event()  # clients generate load in bursts between passes
        errors = []

        def client(cid):
            r = random.Random(cid)
            while not stop.is_set():
                if not load.wait(0.05):
                    continue
                bucket = r.choice(["rep", "ec", "ec63"])
                key = f"obj{r.randint(0, 15)}"
                if r.random() < 0.5:
                    d = r.randbytes(r.randint(0, 100_000))
                    try:
                        v.put_object(bucket, key, d)
                        with lock:
                            written.setdefault((bucket, key), set()).add(
                                hashlib.sha256(d).hexdigest())
                    except WriteQuorumError:
                        pass  # acceptable under chaos; old value must remain
                    except Exception as e:
                        errors.append(repr(e))
                else:
                    try:
                        v.get_object(bucket, key)
                    except (ObjectNotFound, ReadError):
                        pass
                    except Exception as e:
                        errors.append(repr(e))

        # At most 2 nodes impaired at a time: within every policy's tolerance.
        clients = [threading.Thread(target=client, args=(i,)) for i in range(4)]
        [t.start() for t in clients]
        ids = list(c.nodes)
        try:
            for round_ in range(8):
                victims = rng.sample(ids, 2)
                mode = rng.choice(["crash", "partition", "slow", "rot"])
                if mode == "crash":
                    c.kill(*victims)
                elif mode == "partition":
                    for n in victims:
                        c.faults(n, blocked=["gateway"])
                elif mode == "slow":
                    for n in victims:
                        c.faults(n, latency=0.3)
                else:
                    for n in victims:
                        keys = list(c.nodes[n].store.keys())
                        for k in rng.sample(keys, min(5, len(keys))):
                            c.nodes[n].store.corrupt(k)
                load.set()
                time.sleep(0.5)   # traffic while the fault is active
                load.clear()
                c.maint.run_once(scrub=(mode == "rot"))
                if mode == "crash":
                    c.revive(*victims)
                else:
                    for n in victims:
                        c.faults(n, blocked=[], latency=0.0)
        finally:
            stop.set()
            load.clear()
            [t.join() for t in clients]
        self.assertEqual(errors, [])

        c.maint.converge(max_rounds=15)
        self.assertEqual(v.meta.stats()["deficient_chunks"], 0)
        self.assertGreater(len(written), 10)
        for (bucket, key), shas in written.items():
            # Writers raced; the visible value must be byte-exact one that was acknowledged.
            got = hashlib.sha256(v.get_object(bucket, key)).hexdigest()
            self.assertIn(got, shas)
            self.assertEqual(got, v.head_object(bucket, key).etag)
        # And after all that, every chunk is at full redundancy.
        for bucket in ("rep", "ec", "ec63"):
            scheme = Scheme.parse(v.bucket_policy(bucket).sig)
            for o in v.list_objects(bucket):
                for chunk in v.head_object(bucket, o["key"]).manifest["chunks"]:
                    held = [len(c.copies(b[0])) for b in chunk["blobs"]]
                    if scheme.replicated:
                        self.assertEqual(held, [scheme.n])
                    else:
                        self.assertTrue(all(h == 1 for h in held), held)



class ResourceTest(unittest.TestCase):
    """Regression tests for leaked descriptors/threads and misattributed local errors."""

    @staticmethod
    def _fds() -> int:
        return len(os.listdir("/dev/fd"))

    def test_close_releases_every_connection_thread_and_fd(self):
        from harness import Cluster
        import gc

        def cycle():
            c = Cluster(6, 3)
            c.vault.create_bucket("b", Policy(scheme="erasure", k=4, m=2, chunk_size=16 * 1024))
            for i in range(4):
                c.vault.put_object("b", f"k{i}", os.urandom(80_000))
                c.vault.get_object("b", f"k{i}")
            c.maint.run_once()
            meta = c.vault.meta
            self.assertGreater(meta.open_connections, 0)
            c.close()
            self.assertEqual(meta.open_connections, 0)

        cycle()  # warm up lazily created module-level state
        gc.collect()
        # Release must be deterministic: with the cyclic GC off, nothing may rely
        # on a collection to close connections or stop pool threads. (Leaks that
        # only the GC reclaims are what exhausted a 256-fd limit mid-suite: the GC
        # runs on allocation counts and knows nothing about file descriptors.)
        gc.disable()
        try:
            fds, threads = self._fds(), threading.active_count()
            for _ in range(5):
                cycle()
            # Before the fix each cycle left ~20 fds and ~14 threads behind.
            self.assertLessEqual(self._fds(), fds + 2)
            self.assertLessEqual(threading.active_count(), threads)
        finally:
            gc.enable()

    def test_local_fd_exhaustion_is_not_blamed_on_nodes(self):
        import errno
        import http.client
        from harness import Cluster
        from vault.errors import LocalResourceError

        c = Cluster(6, 3)
        try:
            c.vault.create_bucket("b", Policy(scheme="erasure", k=4, m=2))
            orig = http.client.HTTPConnection.connect

            def emfile(self):
                raise OSError(errno.EMFILE, "Too many open files")

            http.client.HTTPConnection.connect = emfile
            try:
                with self.assertRaises(LocalResourceError):
                    c.vault.put_object("b", "k", os.urandom(50_000))
                c.vault.membership.probe_all()
            finally:
                http.client.HTTPConnection.connect = orig
            # The gateway's own exhaustion must not mark healthy nodes suspect/dead.
            self.assertEqual({n.health for n in c.vault.membership.nodes()}, {"alive"})
            data = os.urandom(50_000)
            c.vault.put_object("b", "k", data)
            self.assertEqual(c.vault.get_object("b", "k"), data)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
