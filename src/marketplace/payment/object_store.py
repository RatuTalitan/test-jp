"""Object-storage port for the Payment_Service (tasks 11.2).

The Payment_Service must store an optional payment **screenshot blob** in a
private object-storage bucket while keeping only a *reference* (the object key)
in the database -- the blob itself is **never** persisted in a DB column
(design.md -> Payment_Service: "stored in object storage; only a reference
(``screenshot_object_key``) is saved in DB", Req 6.3).

To keep the domain service decoupled from any concrete provider (S3 / GCS /
local disk / Telegram file cache), the service depends only on the small
:class:`ObjectStore` *port* defined here. The real provider client is wired in a
later infrastructure task; the fast unit/property tests use the in-process
:class:`InMemoryObjectStore` fake, which records exactly what was stored so a
test can assert "the blob went to object storage and only its key reached the
DB".

This mirrors the repository-protocol pattern used elsewhere
(``marketplace.domain.repositories``): the service is written against a typed
interface, never a concrete backend, so persistence/storage stay swappable
without touching business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "ObjectStore",
    "StoredObject",
    "InMemoryObjectStore",
]


@runtime_checkable
class ObjectStore(Protocol):
    """A minimal blob store: write bytes under a key, return the key.

    Implementations persist ``data`` under ``key`` (overwriting any existing
    object at that key) tagged with ``content_type`` and return the key the
    caller should record. The interface is intentionally tiny -- the
    Payment_Service only needs to *put* a screenshot and remember its key; reads
    happen out-of-band (the Seller's client fetches by key) and are not part of
    this port.
    """

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store ``data`` (tagged ``content_type``) under ``key`` and return ``key``."""
        ...


@dataclass(frozen=True)
class StoredObject:
    """A single object captured by :class:`InMemoryObjectStore` (test aid)."""

    key: str
    data: bytes
    content_type: str


class InMemoryObjectStore:
    """An in-process :class:`ObjectStore` fake for tests (no real I/O).

    Keeps every stored object in a dict keyed by object key so tests can assert
    the blob reached object storage (and only its key reached the DB). A single
    instance can be shared across a test to inspect ``objects``.
    """

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = StoredObject(key=key, data=bytes(data), content_type=content_type)
        return key

    # --- convenience read helpers (tests only; not part of the port) -------
    def get(self, key: str) -> StoredObject | None:
        """Return the stored object for ``key`` (or ``None``)."""
        return self.objects.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self.objects

    def __len__(self) -> int:
        return len(self.objects)
