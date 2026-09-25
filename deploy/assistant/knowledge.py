"""Ask Vault knowledge base: the only facts the assistant is allowed to state.

Every statement here was checked against the implementation in vault/, the
dashboard in dashboard/ and the website in website/. When Vault changes, this
file must change with it. If a question isn't answered here, the assistant says
it doesn't know or that the feature isn't implemented; it never fills the gap.

Structure
  TOPICS           concept answers, written in layers: simple -> detail -> console
  NOT_IMPLEMENTED  things people commonly assume a storage system has, but Vault doesn't
  SECTIONS         what each website section shows, for "what am I looking at?" questions
  HERO_STAGES      the seven stages of the hero animation
"""

from __future__ import annotations

# Shared phrasing reused across answers.
ILLUSTRATIVE = ("This is an illustrative animation on the website, not live data. "
                "The Live Console shows the real state of a running cluster.")

TOPICS: list[dict] = [
    {
        "id": "what-is-vault",
        "title": "What is Vault?",
        "keywords": {"what is vault": 6, "about vault": 4, "vault": 1, "overview": 2, "purpose": 2,
                     "what does vault do": 6, "explain vault": 5, "tell me about": 2},
        "simple": ("Vault is a distributed object store: it stores files (\"objects\") by splitting them "
                   "into pieces and spreading those pieces across several independent storage nodes. "
                   "Because it keeps extra pieces, it keeps working when some nodes fail, and it "
                   "rebuilds lost pieces on its own."),
        "detail": [
            "A gateway accepts uploads and downloads over HTTP, splits files into chunks, and writes each chunk to several nodes.",
            "Each bucket has a durability policy: full copies (replication) or erasure coding.",
            "A metadata store (SQLite) records every object, its version and where its pieces are.",
            "Background jobs detect failed nodes and damaged data, repair missing pieces, rebalance data and clean up.",
        ],
        "dashboard": "The Live Console shows a running cluster: node health, stored objects, repairs and integrity events.",
        "followups": ["What happens when a node fails?", "How is an object stored?", "What technologies are used?"],
    },
    {
        "id": "not-hashicorp",
        "title": "Is this HashiCorp Vault?",
        "keywords": {"hashicorp": 6, "secrets": 3, "secret management": 5, "secrets manager": 5, "password": 2},
        "simple": ("No. This Vault is a distributed object store for files. It is not HashiCorp Vault, "
                   "and it doesn't manage secrets, passwords or keys."),
        "detail": [],
        "dashboard": None,
        "followups": ["What is Vault?"],
    },
    {
        "id": "why-distributed",
        "title": "Why is Vault distributed?",
        "keywords": {"why distributed": 6, "distributed": 2, "why multiple": 4, "single server": 3,
                     "one machine": 3, "why not one": 4, "why spread": 4},
        "simple": ("Any single machine can fail: a disk dies, a machine reboots, a network cable is "
                   "unplugged. By keeping pieces of each file on several independent nodes, in "
                   "different zones where possible, losing one machine doesn't lose the file."),
        "detail": [
            "How many failures a file survives depends on its bucket's policy: replication with n copies survives n-1 node failures; erasure coding k+m survives m.",
            "Pieces are spread across zones first (zone-aware rendezvous hashing), so one failing zone can't hold every piece when enough zones exist.",
        ],
        "dashboard": "The console's Overview shows how many node failures the configured policies tolerate, and how many nodes are currently failed.",
        "followups": ["What is replication?", "What is erasure coding?"],
    },
    {
        "id": "object",
        "title": "What is an object?",
        "keywords": {"object": 3, "objects": 3, "what is an object": 6, "bucket": 3, "buckets": 3, "key": 1,
                     "file": 1, "version": 1},
        "simple": ("An object is a file stored in Vault, addressed by a bucket name and a key (like "
                   "\"photos/cat.jpg\"). Buckets group objects and decide how they are protected."),
        "detail": [
            "Every successful write gets a new version number from a single increasing counter, so writes have a clear order.",
            "Only the current version of an object is kept. There is no version history to browse or restore.",
            "Writes can be conditional (If-Match on a version, or If-None-Match: * for \"only if it doesn't exist\"), which acts like compare-and-swap.",
            "Each object records its size and its SHA-256, which the console shows as a short fingerprint.",
        ],
        "dashboard": "The console's Objects page lists objects per bucket, with upload, download, verify and delete.",
        "followups": ["How is an object stored?", "What is a checksum?"],
    },
    {
        "id": "how-stored",
        "title": "How is an object stored?",
        "keywords": {"how is an object stored": 8, "how stored": 5, "store": 2, "stored": 2, "upload": 3,
                     "write": 2, "put": 1, "save": 2, "write path": 6, "how does storing": 5, "uploading": 3},
        "simple": ("When you upload a file, the gateway cuts it into chunks, protects each chunk with "
                   "copies or erasure coding, sends the pieces to different nodes, and only then records "
                   "the new object. Until that final step, readers keep seeing the previous version."),
        "detail": [
            "Chunks are 4 MiB by default (configurable per bucket). Several chunks are uploaded in parallel while the file streams in.",
            "Each chunk is named by its SHA-256 plus the policy, so identical chunks under the same policy are stored once (deduplication).",
            "Pieces go to the best-ranked nodes for that chunk (zone-aware rendezvous hashing). If a node fails during the write, the next-ranked node is used instead.",
            "The write succeeds only once enough distinct nodes have stored pieces (the write quorum): by default 2 of 3 for replication ×3, and 5 of 6 for erasure coding 4+2.",
            "The object's list of chunks (its manifest) is then committed in one metadata transaction, so the new version appears all at once or not at all.",
        ],
        "dashboard": "Uploads through the console show a progress bar and the SHA-256 and version Vault returns.",
        "followups": ["What is a write quorum?", "What are chunks and shards?"],
    },
    {
        "id": "nodes",
        "title": "What are nodes?",
        "keywords": {"node": 2, "nodes": 2, "storage node": 5, "storage nodes": 5, "what are nodes": 6,
                     "what is a node": 6, "server": 1, "machine": 1},
        "simple": ("A storage node is an independent server that stores pieces of files on its disk. "
                   "Nodes don't know about objects or buckets; they just store and return pieces "
                   "(\"blobs\") and check them for damage."),
        "detail": [
            "Each node is a small HTTP server. Every blob on disk carries its SHA-256 in a header.",
            "Writes are atomic: data goes to a temporary file, is fsync'd, then renamed into place.",
            "A node re-checks the checksum on every read and during scrubs. A damaged blob is moved to quarantine and never served.",
            "Nodes belong to zones (failure domains such as racks or rooms). Vault places pieces of a chunk in different zones first.",
            "Each node also has test-only fault injection (simulate a crash, a partition, slowness, failed writes or corrupted reads) used by the tests and the console's demo controls.",
        ],
        "dashboard": ("The console's Nodes page shows each node's health (from Vault's failure detector), "
                      "its last heartbeat, how many replicas metadata tracks on it, and the bytes and blob "
                      "count the node itself reports. Disk capacity isn't reported, so the bar compares "
                      "nodes to the fullest one."),
        "followups": ["What happens when a node fails?", "What are zones?"],
    },
    {
        "id": "zones",
        "title": "What are zones?",
        "keywords": {"zone": 4, "zones": 4, "failure domain": 5, "rack": 3, "racks": 3, "data center": 2},
        "simple": ("A zone is a group of nodes that might fail together, such as one rack or one room. "
                   "Vault spreads a chunk's pieces across different zones first."),
        "detail": [
            "Placement ranks nodes per chunk with weighted rendezvous hashing, then interleaves zones, so the first pieces land in distinct zones.",
            "If there are fewer zones than pieces, some zones hold more than one piece.",
            "Zones are labels given when a node is added. Vault doesn't discover them automatically.",
        ],
        "dashboard": "The console groups nodes by zone in the topology view.",
        "followups": ["How does placement work?"],
    },
    {
        "id": "replication",
        "title": "What is replication?",
        "keywords": {"replication": 5, "replica": 4, "replicas": 4, "copies": 4, "copy": 2, "replicate": 4,
                     "what is replication": 8},
        "simple": ("Replication means keeping several full copies of each chunk on different nodes. "
                   "With 3 copies, any 2 nodes holding them can fail and the data is still readable."),
        "detail": [
            "A replicated bucket has a copy count n (default 3). It survives n-1 failed nodes and uses n times the space.",
            "A write succeeds once a majority of copies are stored (default: 2 of 3); repair creates the rest later if needed.",
            "Reads fetch any one intact copy. If the first node is slow, Vault asks another after 0.25 s by default (a hedged read).",
            "Erasure coding offers the same failure tolerance with less storage, at the cost of reading from more nodes.",
        ],
        "dashboard": "The console's Overview lists each bucket's policy, how many failures it tolerates, and its storage overhead.",
        "followups": ["What is erasure coding?", "What is a write quorum?"],
    },
    {
        "id": "redundancy",
        "title": "How does redundancy work?",
        "keywords": {"redundancy": 6, "redundant": 5, "how does redundancy work": 9, "extra copies": 5,
                     "protect": 3, "protected": 3, "protection": 4, "durability policy": 7, "policy": 3,
                     "policies": 3},
        "simple": ("Vault stores more than one piece of information about every chunk, on different nodes, "
                   "so losing some nodes loses nothing. Each bucket picks how: full copies (replication) "
                   "or data plus parity pieces (erasure coding)."),
        "detail": [
            "Replication ×n keeps n full copies: survives n-1 failed nodes, uses n× the space (default n = 3).",
            "Erasure coding k+m keeps k data pieces and m parity pieces: survives m failed nodes, uses (k+m)/k× the space (default 4+2: survives 2, uses 1.5×).",
            "Pieces are spread across zones first, so one failing zone can't hold them all when enough zones exist.",
            "When a piece is lost, repair recreates it, so the file can survive the same number of failures again.",
        ],
        "dashboard": "The console's Overview lists each bucket's policy, how many failures it tolerates, and its storage overhead.",
        "followups": ["What is replication?", "What is erasure coding?"],
    },
    {
        "id": "erasure-coding",
        "title": "What is erasure coding?",
        "keywords": {"erasure": 6, "erasure coding": 8, "reed-solomon": 6, "reed solomon": 6, "parity": 5,
                     "k+m": 5, "4+2": 5, "what is erasure coding": 9, "coding": 1},
        "simple": ("Erasure coding splits a chunk into k data pieces and adds m parity pieces computed "
                   "from them. Any k of the k+m pieces can rebuild the chunk. With 4+2, any 2 of the 6 "
                   "nodes can fail, and it uses 1.5× the space instead of the 3× of three copies."),
        "detail": [
            "Vault uses Reed-Solomon coding over GF(2⁸) with a Cauchy matrix, implemented in the project itself (no external library).",
            "Defaults are k=4 and m=2. A write needs pieces on k+1 = 5 distinct nodes by default.",
            "Reads fetch the data pieces first, so usually no decoding is needed. Parity pieces are fetched only if data pieces are missing or slow.",
            "Every piece has its own checksum, and a rebuilt chunk is checked against its original SHA-256.",
            "Spreading 6 pieces fully needs at least 6 usable nodes. With fewer, some nodes hold more than one piece, which lowers tolerance.",
        ],
        "dashboard": "Erasure-coded buckets appear in the console as \"Erasure k+m\" with their tolerance and overhead.",
        "followups": ["What is replication?", "What are chunks and shards?"],
    },
    {
        "id": "chunks-shards",
        "title": "What are chunks and shards?",
        "keywords": {"chunk": 5, "chunks": 5, "shard": 5, "shards": 5, "piece": 3, "pieces": 3, "blob": 4,
                     "blobs": 4, "chunks/shards": 6},
        "simple": ("A chunk is a slice of a file (4 MiB by default). With erasure coding, each chunk is "
                   "turned into several shards (pieces). Nodes store these pieces as \"blobs\"."),
        "detail": [
            "A replicated chunk is stored as one blob with several copies. An erasure-coded chunk becomes k+m blobs, one shard each.",
            "Blob names are content-addressed: the chunk's SHA-256 plus the policy (and the shard number for erasure coding). Blobs never change after they're written.",
            "Because blobs are immutable, copies can't drift apart. A copy can only be missing or damaged, and both are detected.",
        ],
        "dashboard": "The console's repair and integrity tables identify chunks by the start of their SHA-256 and a shard number.",
        "followups": ["What is a checksum?", "What is erasure coding?"],
    },
    {
        "id": "checksum",
        "title": "What is a checksum?",
        "keywords": {"checksum": 6, "checksums": 6, "sha-256": 6, "sha256": 6, "hash": 3, "fingerprint": 3,
                     "etag": 4},
        "simple": ("A checksum is a short fingerprint computed from data. If even one bit changes, the "
                   "fingerprint changes. Vault uses SHA-256 fingerprints to prove data is exactly what "
                   "was written."),
        "detail": [
            "Nodes reject a write whose bytes don't match the SHA-256 sent with them.",
            "Every blob stores its SHA-256 in a header; the node checks it on every read and during scrubs.",
            "The gateway re-checks the bytes it receives, and checks each chunk against its original SHA-256, including after erasure decoding.",
            "The object's own SHA-256 is returned as its ETag.",
        ],
        "dashboard": "The console's Verify button downloads an object through Vault and compares its SHA-256 with the one recorded at write time.",
        "followups": ["How does Vault detect corruption?"],
    },
    {
        "id": "corruption",
        "title": "How does Vault detect corruption?",
        "keywords": {"corruption": 7, "corrupt": 6, "corrupted": 6, "bit rot": 7, "bitrot": 7, "integrity": 5,
                     "scrub": 5, "scrubbing": 5, "quarantine": 5, "damaged": 4, "detect corruption": 9,
                     "verify": 2, "verification": 3},
        "simple": ("Every piece of data carries a SHA-256 fingerprint. Vault checks it whenever data is "
                   "written, read or scanned. A damaged copy is set aside (quarantined) and rebuilt from "
                   "healthy copies, so corrupted data is never served."),
        "detail": [
            "On write: the node rejects bytes that don't match the checksum sent with them.",
            "On read: the node re-verifies before serving, and the gateway verifies again after transfer. A bad copy is dropped and another is used.",
            "In the background: scrubs make every node re-verify everything it stores. The maintenance loop runs a scrub every 12th pass (by default about once a minute).",
            "Anti-entropy compares each node's inventory with metadata to find missing copies, copies metadata didn't know about, and orphans.",
            "Detected problems are logged as events (replica_corrupt or replica_missing); repair then restores the missing copy.",
        ],
        "dashboard": ("The console's Integrity page lists detections from Vault's event log, and whether a "
                      "repair of the same chunk followed. Its \"Run verification pass\" button runs one "
                      "maintenance pass (scrub, repair, rebalance and cleanup) and shows the result. "
                      "Vault doesn't report when its last background scrub ran."),
        "followups": ["What is repair?", "What is a checksum?"],
    },
    {
        "id": "node-failure",
        "title": "What happens when a node fails?",
        "keywords": {"node fails": 9, "node failure": 9, "node dies": 8, "node goes down": 8, "fail": 3,
                     "fails": 3, "failure": 3, "failed": 3, "down": 2, "crash": 4, "crashes": 4, "dies": 3,
                     "offline": 3, "outage": 4, "survive": 3, "what happens when": 3},
        "simple": ("Nothing is lost as long as the failures stay within the bucket's policy. Reads and "
                   "writes keep working from the remaining nodes. Vault notices the failure within "
                   "seconds and rebuilds the missing pieces on healthy nodes by itself."),
        "detail": [
            "Detection: the gateway probes every node each second. A missed probe marks the node suspect; after a timeout (10 s by default) it is declared failed (\"dead\").",
            "While suspect, the node gets no new data, but its copies still count, so a brief blip doesn't trigger unnecessary repairs.",
            "Serving: reads use other copies or any k erasure-coded pieces. Writes go to the next-ranked healthy node, but only succeed if the write quorum can still be met.",
            "Repair: once the node is declared failed, the repair service rebuilds every chunk that lost a piece, most endangered first, and places the new pieces on healthy nodes.",
            "Return: if the node comes back, it is marked alive again, and the surplus copies it still holds are trimmed.",
            "Limits: if more nodes fail than the policy tolerates, affected objects become unreadable until nodes return. Data on disk isn't deleted, and Vault logs a \"chunk at risk\" event.",
        ],
        "dashboard": ("In the console, the node turns suspect (amber), then failed (red). The Repair page "
                      "shows chunks below target redundancy and completed repairs from the event log. "
                      "The \"recovery progress\" bar is derived by the console from how many chunks were "
                      "below target at the peak versus now; Vault doesn't report per-repair progress."),
        "followups": ["What is repair?", "How does Vault detect a failed node?"],
    },
    {
        "id": "detection",
        "title": "How does Vault detect a failed node?",
        "keywords": {"heartbeat": 6, "heartbeats": 6, "failure detector": 8, "suspect": 5, "dead": 3,
                     "detect a failed node": 9, "detect failure": 7, "detected": 2, "how does vault know": 5,
                     "probe": 4},
        "simple": ("Vault's failure detector asks every node \"are you there?\" once a second. A node "
                   "that stops answering becomes suspect, and after a timeout it's declared failed."),
        "detail": [
            "States: alive → suspect (a probe or request failed) → dead (no answer for longer than dead_after, 10 s by default).",
            "Failed requests in normal traffic also mark a node suspect straight away.",
            "The probe also checks the node's identity, so a misconfigured address isn't mistaken for the right node.",
            "Failures on the gateway's own side, such as running out of file descriptors, are not blamed on nodes.",
            "Suspect nodes still count toward durability, so repair waits until a node is actually declared dead.",
        ],
        "dashboard": "The console shows each node's state and last heartbeat, and node health changes appear in the Activity feed.",
        "followups": ["What happens when a node fails?", "What is repair?"],
    },
    {
        "id": "repair",
        "title": "What is repair?",
        "keywords": {"repair": 6, "repairs": 6, "rebuild": 5, "rebuilds": 5, "rebuilding": 5, "recover": 4,
                     "recovery": 4, "restore": 3, "heal": 4, "self-healing": 5, "reconstruct": 5},
        "simple": ("Repair is Vault's background process for putting missing pieces back. When a node "
                   "fails or a copy is found damaged, repair rebuilds the missing piece from healthy "
                   "ones and stores it on a healthy node."),
        "detail": [
            "It finds every chunk with fewer healthy pieces than its policy requires, and handles the most endangered chunks first.",
            "Replicas are copied from a verified surviving copy. Erasure-coded shards are recomputed from any k surviving shards and checked against their original SHA-256 before storing.",
            "New pieces go to the chunk's preferred placement where possible, so data doesn't need to move a second time.",
            "Repair runs in parallel and automatically, as part of the maintenance loop (every 5 s by default).",
            "Garbage (data no object references) is skipped, and repair doesn't start while a node is only suspect, because its copies still count.",
        ],
        "dashboard": ("The console's Repair page shows chunks currently below target, completed repairs "
                      "(from Vault's event log), repair rate and failed repairs. Which objects a repaired "
                      "chunk belongs to, and live progress of an individual repair, aren't exposed."),
        "followups": ["What is rebalancing?", "What happens when a node fails?"],
    },
    {
        "id": "rebalancing",
        "title": "What is rebalancing?",
        "keywords": {"rebalance": 7, "rebalancing": 7, "add a node": 5, "adding a node": 5, "new node": 4,
                     "drain": 6, "draining": 6, "decommission": 6, "move data": 5, "remove a node": 5},
        "simple": ("Rebalancing moves data to where it belongs after the set of nodes changes. For "
                   "example, a new node gets its fair share, and a node being drained gets emptied."),
        "detail": [
            "Every chunk has a preferred set of nodes from rendezvous hashing. Adding a node only changes the preference for its fair share of chunks.",
            "Moves are always copy first, record the new location, and only then remove the old copy, so redundancy never drops.",
            "Rebalancing is throttled (256 moves per pass by default) and pauses while any node is suspect, to avoid churn during flaps.",
            "Draining a node makes repair copy its data elsewhere; the node is marked drained once it holds nothing.",
            "Surplus copies (for example on a node that came back) are trimmed.",
        ],
        "dashboard": ("The console has a Drain button per node. Vault doesn't log individual rebalancing "
                      "moves as events, so they don't appear in the Activity feed."),
        "followups": ["How does placement work?", "What is repair?"],
    },
    {
        "id": "placement",
        "title": "How does placement work?",
        "keywords": {"placement": 6, "rendezvous": 7, "hashing": 4, "which node": 4, "where is my file": 5,
                     "where are pieces": 5, "how does vault choose": 5},
        "simple": ("For each chunk, Vault ranks all nodes with a hash function and picks the top ones, "
                   "spreading across zones first. The ranking is stable, so the same chunk always "
                   "prefers the same nodes."),
        "detail": [
            "The method is weighted rendezvous (highest-random-weight) hashing; a node's weight scales its share.",
            "Adding or removing a node only changes placement for the chunks whose top-ranked set actually changed.",
            "Metadata records where each piece actually is, which can temporarily differ from the preference (for example after a failed write fell back to another node).",
            "Vault's API does not expose which nodes hold a specific object's pieces. The console only shows per-node replica counts.",
        ],
        "dashboard": "The console shows how many replicas each node holds, but not per-object placement.",
        "followups": ["What are zones?", "What is rebalancing?"],
    },
    {
        "id": "metadata",
        "title": "What is metadata?",
        "keywords": {"metadata": 7, "sqlite": 6, "database": 5, "manifest": 5, "transaction": 4,
                     "transactions": 4, "catalog": 3},
        "simple": ("Metadata is Vault's record of what exists: every bucket, object, version, chunk and "
                   "where each piece is stored. Storage nodes hold the bytes; metadata is the source "
                   "of truth about them."),
        "detail": [
            "It is stored in SQLite in WAL mode, with serializable write transactions.",
            "A new object version is committed in one transaction, so readers see the old or the new version, never a mix.",
            "It keeps an event log of node health changes, detections, repairs and at-risk chunks.",
            "It is a single SQLite file: transactional and crash-safe, but not replicated across machines. That is a known limitation.",
        ],
        "dashboard": "Object lists, counts, storage totals, replica counts and events in the console all come from metadata via the gateway API.",
        "followups": ["What does the gateway do?", "What are Vault's limitations?"],
    },
    {
        "id": "gateway",
        "title": "What does the gateway do?",
        "keywords": {"gateway": 7, "front door": 5, "api": 2, "entry point": 4, "what does the gateway": 9},
        "simple": ("The gateway is Vault's front door. Clients upload and download through it, and it "
                   "does the coordinating: splitting files, choosing nodes, collecting enough acks, "
                   "and verifying data on the way back out."),
        "detail": [
            "It streams uploads in chunks, applies the bucket's policy, and writes pieces to several nodes in parallel.",
            "It commits the object's manifest to metadata only after the write quorum is met.",
            "On reads it verifies every chunk's SHA-256, reads ahead, and sends a second (hedged) request if a node is slow.",
            "It runs the failure detector, and in the default setup it also runs the background maintenance jobs.",
            "It speaks plain HTTP with JSON for control operations; it is S3-flavoured but not S3-compatible.",
        ],
        "dashboard": "The console reaches Vault only through the gateway's HTTP API, plus each node's /health for disk figures and demo fault injection.",
        "followups": ["What endpoints does the API have?", "What is metadata?"],
    },
    {
        "id": "quorum",
        "title": "What is a write quorum?",
        "keywords": {"quorum": 7, "write quorum": 9, "acknowledged": 4, "acks": 4, "durable": 3,
                     "when is a write safe": 6, "partial write": 5},
        "simple": ("The write quorum is how many nodes must confirm they've safely stored pieces before "
                   "Vault calls an upload successful. If too few nodes are reachable, the upload is "
                   "refused rather than accepted with too little protection."),
        "detail": [
            "Defaults: a majority of copies for replication (2 of 3), and k+1 distinct nodes for erasure coding (5 of 6 for 4+2). Both are configurable per bucket.",
            "Only distinct nodes count: two pieces on one disk fail together, so they count once.",
            "A failed write is never visible. Pieces it already uploaded become orphans that anti-entropy removes later.",
            "With erasure coding 4+2 on six nodes, two failed nodes leave objects readable, but new uploads to that bucket are refused until a fifth node is reachable.",
        ],
        "dashboard": "A refused upload shows the error in the console's upload list (HTTP 503 from the gateway).",
        "followups": ["What happens when a node fails?", "What is erasure coding?"],
    },
    {
        "id": "consistency",
        "title": "How does Vault handle concurrent writes?",
        "keywords": {"consistency": 6, "consistent": 4, "concurrent": 6, "concurrency": 6, "race": 4,
                     "overwrite": 4, "compare-and-swap": 6, "if-match": 6, "torn": 5, "same key": 4,
                     "at the same time": 5},
        "simple": ("Each object version becomes visible in one step, and every write gets a unique, "
                   "increasing version number. Two writers to the same key never produce a mixed "
                   "result: the one that commits last wins."),
        "detail": [
            "Readers always see a complete version, old or new, never a mix of chunks.",
            "Conditional writes (If-Match with a version, or If-None-Match: *) let a client update only if nobody changed the object since it looked.",
            "Garbage collection waits a grace period (300 s by default) before deleting data an old version used, so in-flight reads of that version still work.",
            "These guarantees hold within one metadata store. Metadata is not replicated, so this isn't a multi-site consistency protocol.",
        ],
        "dashboard": "The console shows each object's current version number.",
        "followups": ["What is metadata?"],
    },
    {
        "id": "reads",
        "title": "How do reads work?",
        "keywords": {"read": 3, "reads": 3, "download": 4, "downloads": 4, "hedged": 7, "slow node": 6,
                     "latency": 4, "get": 1, "retrieve": 3},
        "simple": ("To download a file, the gateway fetches each chunk from whichever nodes hold it, "
                   "checks it, and streams it to you. If one node is slow or broken, it uses another."),
        "detail": [
            "Several chunks are fetched ahead while earlier ones are streamed.",
            "If a replica doesn't answer within 0.25 s by default, a second request is sent to another replica (a hedged read). The first good answer wins.",
            "Erasure-coded reads collect any k verified shards, data shards first.",
            "A missing or corrupt copy found during a read is dropped from metadata at once, and repair restores it.",
        ],
        "dashboard": "The console's Download and Verify actions go through the same gateway read path.",
        "followups": ["How does Vault detect corruption?"],
    },
    {
        "id": "delete-gc",
        "title": "What happens when an object is deleted?",
        "keywords": {"delete": 4, "deleted": 4, "deleting": 4, "garbage": 5, "garbage collection": 7,
                     "gc": 5, "reclaim": 4, "free space": 3},
        "simple": ("Deleting an object removes it from metadata immediately. The space is reclaimed "
                   "later by garbage collection, once nothing references those pieces anymore."),
        "detail": [
            "Garbage collection waits a grace period (300 s by default) so in-flight reads and concurrent uploads of identical data aren't affected.",
            "Pieces shared with another object (deduplication) are kept as long as any object references them.",
            "Deletes are permanent: there's no trash or version history.",
        ],
        "dashboard": "The console has a Delete action per object; the deletion appears in its activity feed as a dashboard action.",
        "followups": ["What is deduplication?"],
    },
    {
        "id": "dedup",
        "title": "What is deduplication?",
        "keywords": {"dedup": 7, "deduplication": 7, "duplicate": 5, "same file twice": 6, "identical": 4},
        "simple": ("If you upload the same data twice under the same durability policy, Vault stores "
                   "its chunks only once and lets both objects point to them."),
        "detail": [
            "It works because chunks are named by their SHA-256.",
            "Deduplication is per chunk and per policy: the same data in a replicated bucket and an erasure-coded bucket is stored separately.",
        ],
        "dashboard": None,
        "followups": ["What are chunks and shards?"],
    },
    {
        "id": "partition",
        "title": "What about network partitions?",
        "keywords": {"partition": 7, "partitions": 7, "network split": 7, "split brain": 6, "unreachable": 4,
                     "network": 2},
        "simple": ("If the gateway can't reach a node, Vault treats it like a failed node, even if the "
                   "node is actually running. It keeps working with the nodes it can reach, as long as "
                   "the bucket's write quorum can still be met."),
        "detail": [
            "Writes need acknowledgements from enough distinct reachable nodes, so an isolated minority can't accept under-protected data.",
            "There's one metadata store, so there's no split-brain between gateways over what exists.",
            "Partitions are tested with per-client fault injection on the nodes.",
        ],
        "dashboard": None,
        "followups": ["What is a write quorum?"],
    },
    {
        "id": "tolerance",
        "title": "How many failures can Vault survive?",
        "keywords": {"how many failures": 9, "how many nodes can fail": 9, "tolerate": 6, "tolerance": 6,
                     "durability": 4, "guarantee": 4, "guarantees": 4, "lose data": 5, "data loss": 6},
        "simple": ("It depends on the bucket's policy. Replication with n copies survives n-1 failed "
                   "nodes; erasure coding k+m survives m. With the defaults (3 copies, or 4+2), that's "
                   "2 failed nodes at once."),
        "detail": [
            "This assumes pieces are on distinct nodes, which needs at least as many usable nodes as the layout is wide (3 for ×3, 6 for 4+2).",
            "After repair restores full redundancy, the same number of further failures can be survived again.",
            "If failures exceed the tolerance, affected objects are unavailable until nodes return. Vault doesn't delete their data, and logs chunks at risk.",
            "Vault doesn't publish a statistical durability figure (such as \"11 nines\"). Its guarantee is the configured policy.",
        ],
        "dashboard": "The console's Overview shows the lowest tolerance across buckets and how many nodes are failed now.",
        "followups": ["What happens when a node fails?"],
    },
    {
        "id": "anti-entropy",
        "title": "What is anti-entropy?",
        "keywords": {"anti-entropy": 8, "anti entropy": 8, "orphan": 6, "orphans": 6, "reconcile": 5,
                     "inventory": 5},
        "simple": ("Anti-entropy is a background check that compares what each node actually has with "
                   "what metadata says it should have, and fixes the differences."),
        "detail": [
            "Copies metadata didn't know about are recorded; recorded copies that vanished are forgotten, so repair recreates them.",
            "Orphans (blobs no object references, for example from failed uploads) are deleted after a grace period (1 hour by default).",
            "It runs together with the scrub, every 12th maintenance pass by default.",
        ],
        "dashboard": "Missing replicas found this way appear on the console's Integrity page as \"detected by inventory reconciliation\".",
        "followups": ["How does Vault detect corruption?"],
    },
    {
        "id": "dashboard",
        "title": "What does the console show?",
        "keywords": {"dashboard": 7, "console": 7, "live console": 8, "what does the dashboard": 9,
                     "monitoring": 4, "telemetry": 4},
        "simple": ("The Live Console is a web dashboard for a running Vault cluster. It reads Vault's "
                   "API every 2 seconds and shows nodes, objects, repairs, integrity checks and events."),
        "detail": [
            "Direct from Vault: node health and last heartbeat, replica counts, objects and versions, storage totals, chunks below target, and the event log.",
            "From each node directly: bytes and blobs on disk.",
            "Computed by the console: the overall health verdict, the redundancy chart (sampled by the page since it was opened), recovery progress (peak vs current chunks below target) and repair rate.",
            "Recorded by the console itself: uploads, downloads, deletes and verifications done through it, marked \"dashboard\", because Vault doesn't log object operations.",
            "Not available: per-object placement, live progress of an individual repair, disk capacity, and when the last background scrub ran.",
            "Demo controls use each node's built-in fault injection to simulate a failure or flip a byte on disk.",
        ],
        "dashboard": None,
        "followups": ["What is illustrative versus live?", "What happens when a node fails?"],
    },
    {
        "id": "storage-map",
        "title": "What is the storage map?",
        "keywords": {"storage map": 9, "map": 4, "animation": 5, "visualization": 5, "diagram": 3,
                     "what am i looking at": 6, "these squares": 5, "the squares": 4, "lines": 2},
        "simple": ("The storage map is the website's illustration of how Vault works. On the left is the "
                   "gateway; the tiles fanning out are storage nodes, grouped into zones; the small "
                   "squares are pieces of a file."),
        "detail": [
            "Solid cyan squares are data pieces; hollow squares with a diagonal line are parity pieces (erasure coding).",
            "Node states use the same colours as the console: green alive, amber suspect, red failed.",
            "Moving dots are heartbeats and data being read or rebuilt.",
            ILLUSTRATIVE,
        ],
        "dashboard": None,
        "followups": ["What is illustrative versus live?", "What happens when a node fails?"],
    },
    {
        "id": "illustrative-vs-live",
        "title": "What is illustrative versus live?",
        "keywords": {"illustrative": 8, "live": 3, "real": 3, "fake": 4, "simulated": 4, "simulation": 4,
                     "is this real": 7, "live data": 7, "real data": 7},
        "simple": ("Everything animated on this website is illustrative: it explains how Vault behaves "
                   "but isn't connected to any cluster. The Live Console is live: it shows the real "
                   "state of a running Vault cluster."),
        "detail": [
            "The website marks its illustrations with an \"Illustrative\" label.",
            "The \"Live system\" section embeds the real console when it's running on this machine.",
            "Ask Vault can quote a live summary (node counts, objects, chunks below target) when the console is running, and labels it as live.",
        ],
        "dashboard": None,
        "followups": ["What does the console show?"],
    },
    {
        "id": "technologies",
        "title": "What technologies does Vault use?",
        "keywords": {"technologies": 8, "technology": 7, "tech stack": 9, "stack": 4, "python": 5,
                     "built with": 7, "language": 4, "dependencies": 6, "framework": 5, "libraries": 5},
        "simple": ("Vault is written in Python using only the standard library, with no third-party "
                   "runtime dependencies. Metadata lives in SQLite, and everything talks over plain HTTP."),
        "detail": [
            "Storage nodes and gateway: Python's http.server, threads and concurrent.futures.",
            "Metadata: SQLite in WAL mode.",
            "Integrity: SHA-256 (hashlib).",
            "Erasure coding: Reed-Solomon over GF(2⁸), written for this project.",
            "Placement: weighted rendezvous hashing, zone-aware.",
            "Console and website: plain HTML, CSS and JavaScript, no build step; a small standard-library proxy for the console.",
            "Ask Vault: a standard-library server; answers come from a curated knowledge base, optionally phrased by Claude through the official anthropic Python package when an operator configures it.",
            "Tests: Python's unittest (39 backend tests including a chaos test, plus console and assistant tests).",
        ],
        "dashboard": None,
        "followups": ["How is Vault tested?", "What are Vault's limitations?"],
    },
    {
        "id": "testing",
        "title": "How is Vault tested?",
        "keywords": {"test": 3, "tests": 5, "tested": 5, "testing": 5, "chaos": 6, "reliable": 3,
                     "reliability": 4},
        "simple": ("Vault has 39 automated backend tests that run real multi-node clusters over HTTP "
                   "and deliberately break them."),
        "detail": [
            "They cover crashes, network partitions, bit rot, corruption in transit, slow nodes, write-quorum refusal, repair, rebalancing, draining, garbage collection, concurrent writers and compare-and-swap.",
            "A chaos test injects random crashes, partitions, slowness and bit rot under concurrent load, then checks that every acknowledged write reads back exactly and full redundancy returns.",
            "The suite passes under a 256-open-file limit, with checks that connections and threads are released.",
            "The console and Ask Vault have their own test suites.",
        ],
        "dashboard": None,
        "followups": ["What technologies does Vault use?"],
    },
    {
        "id": "api",
        "title": "What endpoints does the API have?",
        "keywords": {"endpoint": 7, "endpoints": 7, "http api": 7, "rest": 4, "curl": 5, "api": 3,
                     "routes": 5},
        "simple": ("The gateway has a small HTTP API for buckets and objects, plus a few cluster "
                   "endpoints."),
        "detail": [
            "Buckets: create (PUT /buckets/{bucket} with a policy), list, delete an empty bucket, and list objects with prefix/after/limit.",
            "Objects: PUT, GET, HEAD and DELETE /buckets/{bucket}/{key}, with If-Match / If-None-Match for conditional writes.",
            "Cluster: GET /cluster (status), GET /cluster/events, POST /cluster/nodes (add), POST /cluster/nodes/{id}/drain or /remove, and POST /cluster/maintenance (run one pass).",
            "There's no authentication on this API, and no S3 compatibility (no signatures, multipart uploads or byte ranges).",
        ],
        "dashboard": None,
        "followups": ["What are Vault's limitations?"],
    },
    {
        "id": "running",
        "title": "How do I try Vault?",
        "keywords": {"how do i run": 8, "run it": 6, "try it": 6, "install": 5, "start": 3, "demo": 4,
                     "get started": 6, "how do i try": 8},
        "simple": ("From the project folder, python3 dashboard/run_demo.py starts a 6-node demo cluster "
                   "and the Live Console on this machine. Then open the console and upload a file."),
        "detail": [
            "The demo uses shorter timings (maintenance every 2 s, nodes declared failed after 5 s) so recovery is visible quickly.",
            "Vault needs only Python 3.11+; there's nothing to install.",
            "Everything runs locally; there's no hosted or cloud version.",
        ],
        "dashboard": None,
        "followups": ["What does the console show?"],
    },
    {
        "id": "fault-injection",
        "title": "What are the demo controls?",
        "keywords": {"fault injection": 8, "simulate": 5, "simulate failure": 8, "demo controls": 8,
                     "inject": 5, "flip a bit": 6, "corrupt a replica": 7},
        "simple": ("The console's demo controls use fault injection built into each storage node. "
                   "\"Simulate failure\" makes a node drop every request, as if it had crashed. "
                   "\"Corrupt a replica\" flips one byte of a stored piece on disk."),
        "detail": [
            "Vault then reacts for real: the failure detector marks the node suspect and then failed, repair rebuilds its pieces, and scrubs or reads catch the corrupted byte.",
            "These controls exist for testing and demos. Ask Vault can't trigger them.",
        ],
        "dashboard": "The controls are on the console's Nodes page.",
        "followups": ["What happens when a node fails?", "How does Vault detect corruption?"],
    },
    {
        "id": "limitations",
        "title": "What are Vault's limitations?",
        "keywords": {"limitation": 7, "limitations": 7, "weakness": 5, "weaknesses": 5, "missing": 3,
                     "not implemented": 6, "downside": 5, "production ready": 6},
        "simple": ("Vault is a working prototype with real fault tolerance, but several things a "
                   "production service would need aren't implemented."),
        "detail": [
            "The metadata store is a single SQLite file: transactional and crash-safe, but not replicated.",
            "No authentication, authorization or encryption.",
            "No S3 compatibility, multipart uploads, byte-range reads or version history.",
            "Repair data flows through the maintenance process rather than directly between nodes.",
            "It runs as local processes; there's no cloud deployment or autoscaling.",
        ],
        "dashboard": None,
        "followups": ["What technologies does Vault use?"],
    },
]

NOT_IMPLEMENTED: list[dict] = [
    {"id": "auth", "keywords": {"authentication": 8, "authorization": 8, "auth": 6, "login": 6, "log in": 5,
                                "password": 4, "users": 4, "user accounts": 7, "permissions": 6,
                                "access control": 8, "api key": 8, "api keys": 8, "your key": 7, "oauth": 7, "iam": 6, "acl": 6},
     "answer": ("Vault doesn't implement authentication or authorization. Its HTTP API has no users, "
                "passwords, tokens or permissions, so it's meant to run on a trusted network.")},
    {"id": "encryption", "keywords": {"encrypt": 8, "encrypted": 8, "encryption": 8, "tls": 7, "https": 5,
                                      "ssl": 6, "at rest": 5, "in transit": 4},
     "answer": ("Vault doesn't encrypt data, either at rest or in transit. Storage nodes keep blobs "
                "unencrypted on disk, and all traffic is plain HTTP. What it does protect is integrity: "
                "SHA-256 checksums detect any change to the data.")},
    {"id": "consensus", "keywords": {"raft": 8, "paxos": 8, "consensus": 7, "replicated metadata": 8,
                                     "metadata replication": 8, "leader election": 7, "zookeeper": 7,
                                     "etcd": 7, "high availability metadata": 7},
     "answer": ("Vault doesn't use Raft, Paxos or any consensus protocol. Metadata lives in a single "
                "SQLite database, which is transactional and crash-safe but not replicated. Replicating "
                "it is listed as future work.")},
    {"id": "s3", "keywords": {"s3 compatible": 8, "s3-compatible": 8, "s3 api": 7, "aws sdk": 7, "boto": 7,
                              "multipart": 7, "presigned": 7, "s3": 4},
     "answer": ("Vault's API is S3-flavoured (buckets and keys over HTTP) but not S3-compatible. There are "
                "no S3 signatures, multipart uploads or presigned URLs, and S3 tools and SDKs won't work "
                "with it.")},
    {"id": "range", "keywords": {"range request": 8, "byte range": 8, "byte-range": 8, "partial read": 7,
                                 "seek": 4, "range header": 8},
     "answer": "Vault doesn't support byte-range reads. A GET always returns the whole object."},
    {"id": "versions", "keywords": {"version history": 8, "previous version": 8, "old version": 7,
                                    "restore a version": 8, "undo": 5, "rollback": 6, "versioning": 6,
                                    "snapshot": 5, "trash": 5, "recycle bin": 6},
     "answer": ("Vault keeps only the current version of each object. Every write gets a version number "
                "(used for conditional writes), but older versions can't be listed or restored, and "
                "deletes are permanent.")},
    {"id": "cloud", "keywords": {"aws": 6, "azure": 6, "gcp": 6, "google cloud": 6, "kubernetes": 8, "k8s": 8,
                                 "docker": 7, "container": 4, "deployed": 4, "hosted": 5, "cloud": 4,
                                 "saas": 6, "autoscaling": 7, "autoscale": 7},
     "answer": ("Vault doesn't run on or depend on any cloud platform, containers or orchestrator. It "
                "runs as ordinary local Python processes (a gateway and storage nodes), and there's no "
                "hosted version or autoscaling.")},
    {"id": "compression", "keywords": {"compression": 8, "compress": 8, "compressed": 8, "gzip": 7, "zstd": 7},
     "answer": ("Vault doesn't compress data. It stores bytes as uploaded. It does deduplicate identical "
                "chunks within the same durability policy.")},
    {"id": "capacity", "keywords": {"capacity": 7, "free space": 6, "disk full": 7, "how full": 7, "quota": 8,
                                    "quotas": 8, "storage limit": 7},
     "answer": ("Vault doesn't track disk capacity, free space or quotas. Nodes report the bytes and blob "
                "count they store, but not how big their disk is, so the console compares nodes with "
                "each other instead of showing percentage full.")},
    {"id": "object-placement", "keywords": {"which nodes hold": 9, "which node has": 9, "where is my file": 8,
                                            "where is my object": 8, "placement of my object": 8,
                                            "which nodes store": 9},
     "answer": ("Vault's API doesn't expose which nodes hold a specific object's pieces. Metadata "
                "records it internally, but the gateway API only reports totals, such as how many "
                "replicas each node holds.")},
    {"id": "repair-progress", "keywords": {"repair progress": 8, "percent complete": 8, "eta": 6,
                                           "how long will repair": 8, "repair percentage": 8,
                                           "progress of repair": 8, "how long until": 7, "time remaining": 8,
                                           "repair finishes": 8, "repair finish": 8, "repair be done": 8,
                                           "how far along": 7},
     "answer": ("Vault doesn't report live progress or time remaining for repairs. It exposes how many "
                "chunks are currently below target and logs each completed repair. The console's "
                "recovery bar is derived from those numbers (peak versus current), not reported by Vault.")},
    {"id": "geo", "keywords": {"multi-region": 8, "multi region": 8, "geo-replication": 8, "geo replication": 8,
                               "cross-region": 8, "datacenters": 5, "cross datacenter": 8},
     "answer": ("Vault doesn't do multi-region or cross-datacenter replication. Zones are just labels "
                "for groups of nodes that might fail together; all nodes are coordinated by one gateway "
                "and one metadata store.")},
    {"id": "sla", "keywords": {"nines": 8, "11 nines": 9, "sla": 7, "uptime": 6, "99.9": 7, "durability percentage": 8},
     "answer": ("Vault doesn't publish an SLA, uptime or statistical durability figure (such as \"11 "
                "nines\"). Its guarantee is its configured policy: n-1 failures for replication ×n, m "
                "failures for erasure coding k+m.")},
    {"id": "discovery", "keywords": {"auto discovery": 8, "service discovery": 8, "discover nodes": 8,
                                     "automatically join": 7, "gossip": 7},
     "answer": ("Nodes don't discover each other or join automatically. An operator adds each node to "
                "the gateway with its address and zone.")},
    {"id": "upload-events", "keywords": {"upload history": 8, "audit log": 8, "access log": 8, "who uploaded": 8,
                                         "download history": 8},
     "answer": ("Vault doesn't log uploads, downloads or who performed them. Its event log records node "
                "health changes, detections, repairs and at-risk chunks. The console records only the "
                "operations done through the console itself, labelled \"dashboard\".")},
]

SECTIONS: dict[str, dict] = {
    "top": {
        "title": "The life of one file (hero)",
        "kind": "illustrative",
        "simple": ("This animation follows one file through Vault: it arrives at the gateway, is split "
                   "into six pieces on six nodes, survives a node failure, and has its missing piece "
                   "rebuilt."),
        "detail": [
            "The layout shows 8 storage nodes in 4 zones and uses erasure coding 4+2: four data pieces (solid) and two parity pieces (hollow). Any four rebuild the file.",
            "The stepper underneath shows the seven stages; you can click one to jump to it or pause.",
            ILLUSTRATIVE,
        ],
        "elements": {
            "nodes": ("The eight tiles (n1-n8) are storage nodes: independent servers that store pieces of "
                      "files. The brackets on the right group them into zones. The dot in each corner is "
                      "the node's state: green alive, amber suspect, red failed."),
            "pieces": ("The small squares are pieces of the file. Solid ones labelled 1-4 are data pieces; "
                       "hollow ones labelled P1 and P2 are parity pieces computed from the data. Any four "
                       "of the six are enough to rebuild the file."),
            "gateway": ("The box on the left is the gateway, Vault's front door. Files enter there, and it "
                        "coordinates where pieces go. The text above it (such as \"read ok\" or "
                        "\"rebuilding\") describes what it's doing in the illustration."),
            "lines": ("The lines are network connections from the gateway to each node. Moving dots are "
                      "heartbeats (grey) or data travelling (cyan)."),
        },
    },
    "how": {
        "title": "01 · Machines fail",
        "kind": "illustrative",
        "simple": ("This section shows the four kinds of failure Vault is built around, each as a small "
                   "map with one troubled node: a crashed node, a network split, a missing copy and "
                   "rotting bits."),
        "detail": [
            "Crash: the node is failed (red). Vault notices through missed heartbeats.",
            "Network split: the node may be fine but can't be reached (amber, dashed link). Writes need enough reachable nodes, so it isn't treated as a place data was saved.",
            "Missing copy: the node is up but a piece that should be there isn't. Anti-entropy finds it.",
            "Bit rot: the piece's bytes changed (red square). SHA-256 checks catch it on read or during a scrub.",
            ILLUSTRATIVE,
        ],
        "elements": {
            "nodes": "Each vignette has a gateway and three storage nodes; the middle node is the one with the problem.",
        },
    },
    "distribution": {
        "title": "02 · One file, many nodes",
        "kind": "illustrative",
        "simple": ("This section compares the two durability policies a bucket can use. Toggle between "
                   "replication ×3 (three full copies) and erasure coding 4+2 (four data pieces plus two "
                   "parity pieces)."),
        "detail": [
            "Both survive 2 node failures. Replication ×3 uses 3× the file size; erasure coding 4+2 uses 1.5×.",
            "Pieces are spread across the three zones first, so one zone failing can't take them all.",
            "The facts beside the map (write quorum, what a read needs) match Vault's defaults.",
            ILLUSTRATIVE,
        ],
        "elements": {
            "nodes": "Six storage nodes in three zones; the brackets on the right mark the zones.",
            "pieces": "\"A\" squares are full copies (replication). Numbered squares are data pieces and P1/P2 are parity pieces (erasure coding).",
        },
    },
    "failure": {
        "title": "03 · Failure is expected",
        "kind": "illustrative",
        "simple": ("This is an interactive illustration: click nodes to take them offline and see whether "
                   "the file (erasure coding 4+2) can still be read."),
        "detail": [
            "Four of the six pieces are needed. With 5 or 6 reachable it's readable; with exactly 4 it's readable with no margin; with 3 or fewer it's unavailable until a node returns. Nothing is deleted.",
            "With two nodes offline, new uploads to a 4+2 bucket are refused, because the write quorum needs 5 reachable nodes.",
            "Repair is paused here on purpose. In a real cluster, repair would start rebuilding once a node is declared failed.",
            ILLUSTRATIVE,
        ],
        "elements": {
            "nodes": "Six storage nodes, each holding one piece of the file. Click one to toggle it offline (red).",
            "pieces": "Four solid data pieces and two hollow parity pieces; the bar on the right shows which are reachable.",
        },
    },
    "recovery": {
        "title": "04 · Detect. Repair. Restore.",
        "kind": "illustrative",
        "simple": ("These three small maps show Vault's recovery sequence after a node fails: detect the "
                   "failure, repair the missing copy, and return to full redundancy."),
        "detail": [
            "Detect: heartbeats go out each second; a node that misses one becomes suspect, then failed after a timeout (10 s by default).",
            "Repair: a surviving copy is read through the gateway-side repair service and a new copy is written to a healthy node (for erasure coding, a missing piece is rebuilt from any k survivors).",
            "Restore: three copies exist again. If the failed node returns, its extra copy is trimmed.",
            "The maps use replication (\"A\" = one full copy) to keep the picture simple.",
            ILLUSTRATIVE,
        ],
        "elements": {
            "nodes": "Five storage nodes: three start with a copy (\"A\"), two are empty spares. The red node is the one that failed.",
        },
    },
    "architecture": {
        "title": "05 · How it's built",
        "kind": "static",
        "simple": ("This diagram shows Vault's real components and the files they live in: the client "
                   "talks HTTP to the gateway, which uses metadata and placement, and stores data on "
                   "storage nodes. The failure detector, maintenance and erasure coding run alongside."),
        "detail": [
            "Gateway (core.py, api.py): chunking, quorum writes, verified and hedged reads.",
            "Metadata (metadata.py): SQLite with transactions. Placement (placement.py): zone-aware rendezvous hashing.",
            "Storage nodes (node.py): checksummed, immutable blobs; corrupt ones are quarantined.",
            "Background: failure detector (membership.py) and maintenance (maintenance.py): repair, scrub, anti-entropy, rebalance and garbage collection.",
        ],
        "elements": {},
    },
    "live": {
        "title": "06 · Running for real",
        "kind": "live",
        "simple": ("This section embeds the real Live Console when it's running on this machine. Unlike "
                   "the animations above, it shows actual cluster state, refreshed every 2 seconds."),
        "detail": [
            "If the console isn't running, the section shows how to start it: python3 dashboard/run_demo.py.",
            "What the console shows directly, derives, or records itself is explained under \"What does the console show?\".",
        ],
        "elements": {},
    },
    "engineering": {
        "title": "07 · Engineering",
        "kind": "static",
        "simple": ("This section lists the technologies Vault actually uses: Python's standard library, "
                   "SQLite, SHA-256, a Reed-Solomon implementation written for the project, and "
                   "rendezvous hashing."),
        "detail": [
            "No third-party runtime dependencies.",
            "39 backend tests, including a chaos test, pass under a 256-open-file limit.",
        ],
        "elements": {},
    },
}

HERO_STAGES: dict[str, str] = {
    "Upload": "The file arrives at the gateway. Nothing is stored yet.",
    "Distribute": "The file is split into four data pieces plus two parity pieces, each sent to a different node, spread across zones.",
    "Redundancy": "All six pieces are stored. Any four rebuild the file, so any two nodes can fail without losing it.",
    "Failure": "Node n3 stops responding. Five pieces are still reachable, and reads keep working from any four.",
    "Detect": "Heartbeats to n3 go unanswered. Vault marks it suspect, then failed after a timeout (10 s by default).",
    "Repair": "The missing piece is recomputed from four surviving pieces, verified, and stored on a healthy node (n4).",
    "Recover": "All six pieces exist again, so the file can survive two more failures, without anyone stepping in.",
}

# Element words people use when pointing at something in a section.
ELEMENT_WORDS: dict[str, list[str]] = {
    "nodes": ["node", "nodes", "tile", "tiles", "boxes", "servers", "machines"],
    "pieces": ["piece", "pieces", "square", "squares", "shard", "shards", "blocks", "p1", "p2", "parity"],
    "gateway": ["gateway", "box on the left", "left box"],
    "lines": ["line", "lines", "dots", "dot", "arrows", "connections"],
}
