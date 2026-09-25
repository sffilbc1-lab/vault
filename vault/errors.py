"""Exception hierarchy shared by every Vault component."""


class VaultError(Exception):
    """Base class for all Vault errors."""


# --- storage-node level -----------------------------------------------------

class NodeUnavailable(VaultError):
    """The node could not be reached (down, partitioned, timed out, refusing writes)."""


class LocalResourceError(VaultError):
    """*This* process ran out of a local resource (file descriptors, buffers, memory)
    while talking to a node. Says nothing about the node's health, so it must never
    be reported as a node failure."""


class BlobNotFound(VaultError):
    """The node answered but does not hold the requested blob."""


class BlobCorrupt(VaultError):
    """The blob exists but failed checksum verification."""


class ChecksumMismatch(VaultError):
    """Payload did not match the checksum supplied with it (corrupted in transit)."""


# --- cluster level ----------------------------------------------------------

class WriteQuorumError(VaultError):
    """Not enough replicas/shards could be written to satisfy the durability policy."""


class ReadError(VaultError):
    """No readable copy of the data could be assembled."""


class BucketNotFound(VaultError):
    pass


class BucketExists(VaultError):
    pass


class BucketNotEmpty(VaultError):
    pass


class ObjectNotFound(VaultError):
    pass


class PreconditionFailed(VaultError):
    """Conditional write/delete lost a race (If-Match / If-None-Match)."""


class RetryableConflict(VaultError):
    """A concurrent background operation invalidated this request; safe to retry."""
