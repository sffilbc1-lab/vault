"""Deterministic, zone-aware data placement via weighted rendezvous hashing.

Every (key, node) pair gets a pseudo-random score scaled by node weight; a key's
preferred nodes are the highest-scoring ones.  Adding or removing a node only
moves the keys whose top-N set actually changed (~N/num_nodes of them), which
keeps rebalancing traffic minimal.  The ranking is then interleaved across
zones so replicas / shards land in distinct failure domains first.
"""

from __future__ import annotations

import hashlib
import math
from typing import Iterable, Protocol, Sequence, TypeVar


class _NodeLike(Protocol):
    id: str
    zone: str
    weight: float


N = TypeVar("N", bound=_NodeLike)


def score(key: str, node_id: str, weight: float = 1.0) -> float:
    h = hashlib.blake2b(f"{key}\x00{node_id}".encode(), digest_size=8).digest()
    u = (int.from_bytes(h, "big") + 0.5) / 2.0 ** 64  # uniform in (0, 1)
    return -max(weight, 1e-9) / math.log(u)


def rank(key: str, nodes: Iterable[N]) -> list[N]:
    return sorted(nodes, key=lambda n: (score(key, n.id, n.weight), n.id), reverse=True)


def zone_order(key: str, nodes: Iterable[N]) -> list[N]:
    """Full preference order for ``key``: rendezvous rank, round-robined across zones."""
    ranked = rank(key, nodes)
    out: list[N] = []
    remaining = ranked
    while remaining:
        seen: set[str] = set()
        rest: list[N] = []
        for n in remaining:
            if n.zone in seen:
                rest.append(n)
            else:
                seen.add(n.zone)
                out.append(n)
        remaining = rest
    return out


def place(key: str, nodes: Sequence[N], count: int) -> list[N]:
    return zone_order(key, nodes)[:count]
