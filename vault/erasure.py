"""Systematic Reed-Solomon erasure coding over GF(2^8) using a Cauchy matrix.

A chunk is split into ``k`` data shards and ``m`` parity shards.  Any ``k`` of the
``k + m`` shards reconstruct the chunk (the code is MDS), so a chunk survives the
loss of any ``m`` shards at a storage overhead of ``(k + m) / k`` instead of the
``n``x overhead of plain replication.

Performance trick: multiplying a whole buffer by a field constant is a single
``bytes.translate`` with a precomputed 256-byte table, and XOR of two buffers is
done on arbitrary-precision ints.  Both run in C, so encoding megabytes is fast
even in pure Python.
"""

from __future__ import annotations

_POLY = 0x11D
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= _POLY
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gmul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def ginv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(256)")
    return EXP[255 - LOG[a]]


# MUL[c] maps every byte x -> c*x; used with bytes.translate.
MUL = [bytes(gmul(c, x) for x in range(256)) for c in range(256)]


def _cauchy(k: int, m: int) -> list[list[int]]:
    # x_i = k + i, y_j = j  -> all distinct, so x_i ^ y_j != 0.
    return [[ginv((k + i) ^ j) for j in range(k)] for i in range(m)]


def _xor_combine(coeffs: list[int], bufs: list[bytes], size: int) -> bytes:
    acc = 0
    for c, b in zip(coeffs, bufs):
        if c == 0:
            continue
        acc ^= int.from_bytes(b if c == 1 else b.translate(MUL[c]), "little")
    return acc.to_bytes(size, "little")


def _invert(mat: list[list[int]]) -> list[list[int]]:
    n = len(mat)
    a = [row[:] + [1 if i == j else 0 for j in range(n)] for i, row in enumerate(mat)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if a[r][col]), None)
        if pivot is None:
            raise ValueError("singular matrix")
        a[col], a[pivot] = a[pivot], a[col]
        inv = ginv(a[col][col])
        a[col] = [gmul(v, inv) for v in a[col]]
        for r in range(n):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [v ^ gmul(f, p) for v, p in zip(a[r], a[col])]
    return [row[n:] for row in a]


def shard_size(length: int, k: int) -> int:
    return max(1, -(-length // k))


def encode(data: bytes, k: int, m: int) -> list[bytes]:
    """Return ``k + m`` equally sized shards (data shards first)."""
    if not (1 <= k and 0 <= m and k + m <= 256):
        raise ValueError("invalid (k, m)")
    size = shard_size(len(data), k)
    padded = data.ljust(size * k, b"\0")
    shards = [padded[i * size:(i + 1) * size] for i in range(k)]
    for row in _cauchy(k, m):
        shards.append(_xor_combine(row, shards[:k], size))
    return shards


def _generator_row(idx: int, k: int, cauchy: list[list[int]]) -> list[int]:
    if idx < k:
        return [1 if j == idx else 0 for j in range(k)]
    return cauchy[idx - k]


def reconstruct(available: dict[int, bytes], k: int, m: int,
                wanted: list[int] | None = None) -> dict[int, bytes]:
    """Rebuild the shards listed in ``wanted`` (default: all) from any ``k`` shards."""
    if len(available) < k:
        raise ValueError(f"need {k} shards, have {len(available)}")
    wanted = list(range(k + m)) if wanted is None else list(wanted)
    size = len(next(iter(available.values())))
    cauchy = _cauchy(k, m)

    # Prefer data shards: fewer multiplications, often no inversion needed.
    use = sorted(available, key=lambda i: (i >= k, i))[:k]
    data: dict[int, bytes] = {i: available[i] for i in use if i < k}
    missing_data = [j for j in range(k) if j not in data]
    if missing_data:
        inv = _invert([_generator_row(i, k, cauchy) for i in use])
        bufs = [available[i] for i in use]
        for j in missing_data:
            data[j] = _xor_combine(inv[j], bufs, size)

    out: dict[int, bytes] = {}
    ordered = [data[j] for j in range(k)]
    for idx in wanted:
        if idx < k:
            out[idx] = data[idx]
        elif idx in available:
            out[idx] = available[idx]
        else:
            out[idx] = _xor_combine(cauchy[idx - k], ordered, size)
    return out


def decode(available: dict[int, bytes], k: int, m: int, length: int) -> bytes:
    """Return the original ``length`` bytes from any ``k`` shards."""
    shards = reconstruct(available, k, m, wanted=list(range(k)))
    return b"".join(shards[i] for i in range(k))[:length]
