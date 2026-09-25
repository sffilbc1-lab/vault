"""Durability policies, attached per bucket."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_SIG = re.compile(r"^(?:r(\d+)|ec(\d+)\+(\d+))$")


@dataclass(frozen=True)
class Policy:
    """How a bucket's data is laid out.

    ``replicate``: every chunk is stored as ``n`` full copies; a write succeeds
    once ``w`` copies are durable.  Survives ``n - 1`` losses; overhead ``n``x.

    ``erasure``: every chunk is Reed-Solomon coded into ``k`` data + ``m`` parity
    shards; a write succeeds once ``w`` (>= k) shards are durable.  Survives ``m``
    losses; overhead ``(k + m) / k``x.
    """

    scheme: str = "replicate"
    n: int = 3
    k: int = 4
    m: int = 2
    w: int | None = None
    chunk_size: int = 4 * 1024 * 1024

    def __post_init__(self):
        if self.scheme not in ("replicate", "erasure"):
            raise ValueError("scheme must be 'replicate' or 'erasure'")
        if self.chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if self.scheme == "replicate":
            if self.n < 1:
                raise ValueError("n must be >= 1")
            if not 1 <= self.write_quorum <= self.n:
                raise ValueError("w must satisfy 1 <= w <= n")
        else:
            if self.k < 1 or self.m < 0 or self.k + self.m > 256:
                raise ValueError("invalid k/m")
            if not self.k <= self.write_quorum <= self.k + self.m:
                raise ValueError("w must satisfy k <= w <= k + m")

    @property
    def replicated(self) -> bool:
        return self.scheme == "replicate"

    @property
    def write_quorum(self) -> int:
        if self.w is not None:
            return self.w
        if self.replicated:
            return self.n // 2 + 1
        return min(self.k + 1, self.k + self.m)

    @property
    def width(self) -> int:
        return self.n if self.replicated else self.k + self.m

    @property
    def sig(self) -> str:
        return f"r{self.n}" if self.replicated else f"ec{self.k}+{self.m}"

    @property
    def overhead(self) -> float:
        return float(self.n) if self.replicated else (self.k + self.m) / self.k

    def to_dict(self) -> dict:
        d = asdict(self)
        d["w"] = self.write_quorum
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        fields = {k: d[k] for k in ("scheme", "n", "k", "m", "w", "chunk_size") if k in d}
        return cls(**fields)


@dataclass(frozen=True)
class Scheme:
    """The part of a policy baked into stored blobs (parsed back from ``sig``)."""

    replicated: bool
    n: int = 0
    k: int = 0
    m: int = 0

    @classmethod
    def parse(cls, sig: str) -> "Scheme":
        mt = _SIG.match(sig)
        if not mt:
            raise ValueError(f"bad scheme signature {sig!r}")
        if mt.group(1):
            return cls(True, n=int(mt.group(1)))
        return cls(False, k=int(mt.group(2)), m=int(mt.group(3)))

    @property
    def min_shards(self) -> int:
        return 1 if self.replicated else self.k
