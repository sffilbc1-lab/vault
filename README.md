# Vault

A fault-tolerant distributed object store: data is chunked, replicated or
erasure-coded across independently failing storage nodes, verified end to end,
and continuously repaired and rebalanced in the background.

Pure Python 3.11+ standard library — no dependencies.

```
                    ┌──────────────────────── Gateway (vault/core.py) ────────────────────────┐
 client ──HTTP──▶   │ chunk → hash → replicate / RS-encode → quorum upload → atomic commit    │
                    │ read: locate → hedged fetch → verify sha256 → (EC decode) → stream       │
                    │                                                                          │
                    │ Membership / failure detector      Maintenance (background)             │
                    │ alive → suspect → dead             repair · scrub · anti-entropy ·      │
                    │                                     rebalance · GC                       │
                    └───────────┬──────────────────────────────┬───────────────────────────────┘
                                │ SQL (serializable txns)       │ HTTP  (blob PUT/GET/DELETE,
                          ┌─────▼──────┐                        │        inventory, scrub)
                          │  Metadata  │           ┌────────────┼────────────┬────────────┐
                          │  (SQLite)  │         node n1      node n2  …   node nN
                          └────────────┘         zone a       zone b       zone c
                                                  self-verifying immutable blob stores
```

## Quick start

```bash
# Whole cluster in one process: 6 nodes over 3 zones + gateway on :8080
python3 -m vault cluster --nodes 6 --zones 3 --dir /tmp/vault-data

# Buckets carry their durability policy
curl -X PUT localhost:8080/buckets/photos -d '{"scheme":"replicate","n":3,"w":2}'
curl -X PUT localhost:8080/buckets/archive -d '{"scheme":"erasure","k":4,"m":2}'

curl -X PUT --data-binary @big.iso localhost:8080/buckets/archive/big.iso
curl -o out.iso localhost:8080/buckets/archive/big.iso
curl localhost:8080/cluster                       # nodes, health, replica counts, stats
curl -X POST localhost:8080/cluster/nodes/n3/drain
curl 'localhost:8080/cluster/events?kind=repaired'
```

Separate processes (a real deployment shape):

```bash
python3 -m vault node --id n1 --dir /data/n1 --port 9001 --zone a
python3 -m vault gateway --meta /data/meta.db --node n1=10.0.0.1:9001@a --node n2=10.0.0.2:9001@b ...
```

Tests (unit, integration with real HTTP nodes, and a chaos test):

```bash
python3 -m unittest discover -s tests -v
```

## How each requirement is met

| Requirement | Mechanism |
|---|---|
| **Large objects** | Streamed in `chunk_size` pieces (default 4 MiB) with bounded in-flight windows on both write and read; memory is O(window × chunk), not O(object). |
| **Configurable durability** | Per-bucket `Policy`: `replicate(n, w)` or `erasure(k, m, w)`. Write quorum `w` counts *distinct nodes*, so two shards on one disk never count twice. |
| **Low storage overhead** | Reed-Solomon (Cauchy, GF(2⁸)) — e.g. 4+2 tolerates 2 failures at 1.5× vs 3× for replication. Content-addressed chunks deduplicate identical data. |
| **Placement / failure domains** | Weighted rendezvous hashing, interleaved across zones: replicas and shards land in distinct zones first. Membership changes move only ~1/N of the data. |
| **Concurrent reads & writes** | Blobs are immutable and content-addressed, and a version becomes visible only through one serializable metadata commit. Readers see the old or the new version, never a mix. Every commit gets a unique, monotonically increasing version. `If-Match` / `If-None-Match: *` give compare-and-swap. |
| **Metadata consistency** | Single transactional source of truth (SQLite WAL, `BEGIN IMMEDIATE`). Manifests, refs and locations change atomically; GC uses a lease (`touched`) plus a `deleting` state so it can never race with a dedup'd write. |
| **Replica inconsistency** | Replicas can't diverge (immutable + content hash). Inconsistency can only mean *missing* or *corrupt*, both detected and repaired. |
| **Node failures** | Heartbeat failure detector: `alive → suspect → dead`. Suspect nodes are avoided for new I/O but still count toward durability, so a blip doesn't start a repair storm. Writes fall back to the next-ranked node. |
| **Partial partitions** | The gateway treats an unreachable node the same whether it is dead or partitioned. Writes still require `w` distinct acks, so a minority side can't accept under-replicated data. Tested with per-client blocking fault injection. |
| **Data corruption** | SHA-256 is checked at every hop: the node verifies on write, on every read, and during scrubs (and quarantines bad blobs); the client re-verifies the bytes it received; the gateway verifies each chunk after EC decode. Corruption in transit, bit rot on disk and bad reconstruction are all caught. |
| **Automatic repair** | Finds chunks below target and processes the most endangered first (lowest margin above data loss). Replicas are copied from a verified survivor; EC shards are rebuilt from any *k*. Repair runs in parallel and writes straight to the target placement, so there is no second shuffle. |
| **Integrity verification** | Periodic node-side scrub + anti-entropy that reconciles each node's inventory against metadata. It finds lost copies (wiped disk), unknown copies (node returned) and orphans (uncommitted writes). |
| **Background rebalancing** | Moves blobs toward their rendezvous placement after joins/drains/returns: copy → record → trim, throttled by `max_moves`. It pauses while any node is suspect, to avoid churn. |
| **Predictable availability / latency** | Hedged reads: if a replica is slow, the next one is asked after `hedge_delay`. EC reads prefer data shards (no decode) and hedge onto parity. Reads stay available with up to n−1 (replication) / m (EC) nodes down. |
| **Minimal recovery time** | Priority by risk, parallel repair, targeted placement, and no repair until a node is actually declared dead. |

## Safety invariants

1. **A write is acknowledged only after `w` distinct nodes have fsync'd it _and_ the
   manifest is committed.** A failed write is never visible; its partial uploads
   become orphans that anti-entropy removes after `orphan_grace`.
2. **No background job ever lowers redundancy.** Every move is copy → record
   location → remove old location → delete bytes.
3. **Garbage collection can't delete live data.** A blob is collected only if no
   object references it _and_ it hasn't been touched for `gc_grace`. Losing a
   reference refreshes `touched`, which protects in-flight readers of the old
   version. A commit that references a blob being deleted fails with a retryable
   conflict (retried automatically for in-memory bodies).
4. **Corrupt data is never served.** Every byte returned has been checked against
   the hash recorded at write time.

## Layout

```
vault/
  erasure.py      Reed-Solomon over GF(256) (translate-table multiply, bigint XOR)
  placement.py    weighted rendezvous hashing + zone interleaving
  policy.py       replication / erasure policies
  node.py         storage node: atomic writes, checksummed blobs, quarantine, fault injection
  client.py       node HTTP client; maps failures to typed errors, end-to-end checksum
  metadata.py     transactional metadata store (objects, refs, blobs, locations, events)
  membership.py   failure detector
  core.py         gateway: put/get/delete/list, quorum upload, hedged reads, EC decode
  maintenance.py  repair, scrub, anti-entropy, rebalance, GC
  api.py          HTTP API
  __main__.py     CLI: node | gateway | cluster
tests/
  test_units.py   erasure coding (all erasure patterns), placement, policy, blob store
  test_cluster.py real multi-node cluster: failures, partitions, corruption, repair,
                  rebalance/drain, concurrency/CAS, torn-read checks, chaos
  test_api.py     HTTP API: lifecycle, conditional writes, 503 on quorum loss, admin
```

## Known limitations / next steps

- **The metadata store isn't replicated.** It is transactional and crash-safe, but it
  is a single SQLite file: it limits metadata availability and write throughput
  (multiple gateways on one host can share it). The `MetadataStore` interface is
  narrow on purpose, so the production step is to back it with a Raft-replicated
  log (or FoundationDB/etcd-style store) sharded by bucket.
- Node-to-node transfer for repair would save gateway bandwidth; today, repair data
  flows through the maintenance process.
- No auth, multipart upload API, byte-range GETs or object versioning history
  (only the current version is kept; the version counter is still exposed for CAS).
- Python and the GIL cap single-gateway throughput. The architecture scales out
  with more gateways; the storage protocol is plain HTTP.
