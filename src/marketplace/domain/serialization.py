"""Versioned, channel-neutral serialization for the domain entities (task 4.2).

This module is the **stable, versioned data-format layer** the design relies on
for Phase-2 extensibility (design.md -> "Versioning Strategy (Req 13.7, 13.8)"
and Req 13.7: *"define stable, versioned data formats for catalog, order, and
payment data from initial release"*). It maps every
:mod:`marketplace.domain.entities` dataclass to/from a plain, JSON-safe ``dict``
(and, optionally, a JSON string), embedding a top-level ``format_version`` so
that older serialized payloads always remain readable.

Design alignment
----------------
* **Additive-only / forward-compatible.** Every payload carries
  ``format_version`` (the envelope) and, where the schema does, the row's own
  ``schema_version`` (``Order``/``SellerSettings``). Deserialization tolerates
  **unknown / extra fields** (they are ignored), and any **new optional field**
  that is absent from an older payload falls back to the entity's own default.
  Required fields existed from v1, so v1 payloads still deserialize.
* **JSON documents embed a version.** ``notification.payload`` and
  ``audit_trail.detail`` are JSON documents that carry ``"format_version": 1``
  (design.md -> Versioning Strategy). The serializer guarantees the embedded
  ``format_version`` is present on both write and read.
* **Lossless value types.** ``Decimal`` money/quantities serialize as **strings**
  (never binary ``float``) so no precision is lost; ``datetime`` values
  serialize as **timezone-aware ISO-8601** strings; ``uuid.UUID`` serialize as
  strings; enums serialize as their string ``value``.
* **Dependency-light & DB-agnostic.** Uses only the standard library
  (``json``/``decimal``/``datetime``/``uuid``) and never imports SQLAlchemy, so
  it can be reused by any channel (Telegram now; WhatsApp / web in Phase 2).

Public API
----------
* :data:`FORMAT_VERSION` - the current envelope format version.
* :func:`to_dict` / :func:`from_dict` - entity <-> JSON-safe ``dict``.
* :func:`dumps` / :func:`loads` - entity <-> JSON string.
* :func:`ensure_versioned_document` - normalise a notification/audit JSON
  document so it carries ``format_version``.
"""

from __future__ import annotations

import enum as _enum
import json
import uuid
from dataclasses import fields as _dataclass_fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Type, TypeVar

from marketplace.domain.entities import (
    AuditAction,
    AuditEntry,
    Cart,
    CartItem,
    Category,
    Language,
    Notification,
    NotificationKind,
    NotificationStatus,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Product,
    Role,
    SellerSettings,
    Unit,
    User,
)

__all__ = [
    "FORMAT_VERSION",
    "FORMAT_VERSION_KEY",
    "to_dict",
    "from_dict",
    "dumps",
    "loads",
    "ensure_versioned_document",
]

# The current envelope format version. Bumped only on a *breaking* shape change;
# additive changes (new optional fields) keep this constant because old payloads
# stay readable by construction.
FORMAT_VERSION = 1

# The key under which the version is embedded, used for the top-level envelope
# and for the notification/audit JSON documents alike.
FORMAT_VERSION_KEY = "format_version"


# ---------------------------------------------------------------------------
# Field-kind descriptors.
#
# A schema is an ordered list of (field_name, kind) pairs. ``kind`` is a tuple
# whose first element is a tag; the engine below interprets it. Keeping the
# schema declarative (rather than hand-writing 11 pairs of functions) makes the
# additive-only contract uniform across every entity.
# ---------------------------------------------------------------------------
_SCALAR = ("scalar",)  # str / int / bool / float passthrough (already JSON-safe)
_DECIMAL = ("decimal",)  # Decimal <-> string (lossless)
_DATETIME = ("datetime",)  # datetime <-> ISO-8601 (timezone preserved)
_UUID = ("uuid",)  # uuid.UUID <-> string
_JSON = ("json",)  # versioned JSON document ({format_version, ...})


def _enum_kind(enum_cls: Type[_enum.Enum]) -> tuple:
    """A kind describing an enum field serialized by its ``.value``."""
    return ("enum", enum_cls)


def _list_kind(entity_cls: type) -> tuple:
    """A kind describing a list of nested domain entities."""
    return ("list", entity_cls)


# ---------------------------------------------------------------------------
# Per-entity schemas. Field order mirrors the dataclasses for readability; the
# engine only relies on the field *names* matching the dataclass attributes.
# ---------------------------------------------------------------------------
_SCHEMAS: dict[type, list[tuple[str, tuple]]] = {
    User: [
        ("user_id", _UUID),
        ("telegram_user_id", _SCALAR),
        ("role", _enum_kind(Role)),
        ("verified_contact", _SCALAR),
        ("contact_verified_at", _DATETIME),
        ("offline_payment_allowed", _SCALAR),
        ("language_preference", _enum_kind(Language)),
        ("created_at", _DATETIME),
        ("updated_at", _DATETIME),
    ],
    Category: [
        ("category_id", _UUID),
        ("name", _SCALAR),
        ("created_at", _DATETIME),
    ],
    Product: [
        ("product_id", _UUID),
        ("name", _SCALAR),
        ("category_id", _UUID),
        ("unit", _enum_kind(Unit)),
        ("price_per_unit", _DECIMAL),
        ("min_order_quantity", _DECIMAL),
        ("stock_quantity", _DECIMAL),
        ("description", _SCALAR),
        ("available", _SCALAR),
        ("created_at", _DATETIME),
        ("updated_at", _DATETIME),
    ],
    CartItem: [
        ("product_id", _UUID),
        ("quantity", _DECIMAL),
        ("cart_item_id", _UUID),
    ],
    Cart: [
        ("cart_id", _UUID),
        ("customer_id", _UUID),
        ("items", _list_kind(CartItem)),
    ],
    OrderItem: [
        ("product_id", _UUID),
        ("ordered_quantity", _DECIMAL),
        ("unit_price", _DECIMAL),
        ("line_amount", _DECIMAL),
        ("order_item_id", _UUID),
    ],
    Order: [
        ("order_id", _UUID),
        ("customer_id", _UUID),
        ("state", _enum_kind(OrderState)),
        ("total_amount", _DECIMAL),
        ("items", _list_kind(OrderItem)),
        ("order_number", _SCALAR),
        ("rejection_reason", _SCALAR),
        # The row's own schema_version travels alongside the envelope
        # format_version (design.md -> Versioning Strategy).
        ("schema_version", _SCALAR),
        ("created_at", _DATETIME),
        ("updated_at", _DATETIME),
    ],
    Payment: [
        ("payment_id", _UUID),
        ("order_id", _UUID),
        ("utr", _SCALAR),
        ("utr_submitted_at", _DATETIME),
        ("screenshot_object_key", _SCALAR),
        ("created_at", _DATETIME),
        ("updated_at", _DATETIME),
    ],
    AuditEntry: [
        ("audit_id", _UUID),
        ("order_id", _UUID),
        ("action", _enum_kind(AuditAction)),
        # detail is a {format_version, ...} JSON document (Req 13.7).
        ("detail", _JSON),
        ("acting_user_id", _UUID),
        ("created_at", _DATETIME),
    ],
    Notification: [
        ("notification_id", _UUID),
        ("order_id", _UUID),
        ("recipient_id", _UUID),
        ("kind", _enum_kind(NotificationKind)),
        ("transition_seq", _SCALAR),
        # payload is a {format_version, ...} JSON document (Req 13.7).
        ("payload", _JSON),
        ("status", _enum_kind(NotificationStatus)),
        ("attempts", _SCALAR),
        ("last_attempt_at", _DATETIME),
        ("next_attempt_at", _DATETIME),
        ("created_at", _DATETIME),
        ("updated_at", _DATETIME),
    ],
    SellerSettings: [
        ("pickup_location", _SCALAR),
        ("upi_address", _SCALAR),
        ("upi_qr_object_key", _SCALAR),
        ("utr_pattern", _SCALAR),
        ("seller_settings_id", _UUID),
        ("updated_at", _DATETIME),
        ("schema_version", _SCALAR),
    ],
}

# Name -> class lookup so JSON strings can be deserialized by entity name and so
# loads() can validate the requested type.
_NAME_TO_CLASS: dict[str, type] = {cls.__name__: cls for cls in _SCHEMAS}


# ---------------------------------------------------------------------------
# Versioned JSON-document helper (notification payload / audit detail).
# ---------------------------------------------------------------------------
def ensure_versioned_document(doc: dict | None) -> dict | None:
    """Return a shallow copy of ``doc`` guaranteed to carry ``format_version``.

    The notification ``payload`` and audit ``detail`` are free-form JSON
    documents that, per the design, embed ``"format_version": 1``. This helper
    normalises any such document so the version is present without clobbering an
    already-present (possibly newer) value, and without disturbing the rest of
    the document (unknown keys are preserved verbatim, which is what makes reads
    forward-compatible). ``None`` passes through unchanged.
    """
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise TypeError(
            f"versioned JSON document must be a dict, got {type(doc).__name__}"
        )
    normalised = dict(doc)
    normalised.setdefault(FORMAT_VERSION_KEY, FORMAT_VERSION)
    return normalised


# ---------------------------------------------------------------------------
# Value-level encode / decode.
# ---------------------------------------------------------------------------
def _encode_value(value: Any, kind: tuple) -> Any:
    """Encode a single field value to its JSON-safe representation."""
    tag = kind[0]
    if tag == "list":
        # Lists are never None on the entities (default_factory=list); guard
        # anyway so a None is encoded as an empty list rather than crashing.
        return [to_dict(item) for item in (value or [])]
    if value is None:
        return None
    if tag == "scalar":
        return value
    if tag == "decimal":
        # str(Decimal("5.00")) -> "5.00": the exact digits are preserved, with
        # no binary-float rounding. Accept a stray non-Decimal defensively.
        return str(value if isinstance(value, Decimal) else Decimal(str(value)))
    if tag == "datetime":
        # isoformat() emits the UTC offset for timezone-aware datetimes, so the
        # zone round-trips. Naive datetimes round-trip as naive.
        return value.isoformat()
    if tag == "uuid":
        return str(value)
    if tag == "enum":
        # Store the stable string value, not the member name.
        return value.value
    if tag == "json":
        return ensure_versioned_document(value)
    raise ValueError(f"unknown field kind: {kind!r}")  # pragma: no cover


def _decode_value(value: Any, kind: tuple) -> Any:
    """Decode a single JSON-safe value back to its in-memory representation."""
    tag = kind[0]
    if tag == "list":
        entity_cls = kind[1]
        return [from_dict(entity_cls, item) for item in (value or [])]
    if value is None:
        return None
    if tag == "scalar":
        return value
    if tag == "decimal":
        # Build from the string form to stay exact; tolerate ints/floats that an
        # older or hand-written payload might carry.
        return Decimal(value) if isinstance(value, str) else Decimal(str(value))
    if tag == "datetime":
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if tag == "uuid":
        return uuid.UUID(value) if isinstance(value, str) else value
    if tag == "enum":
        enum_cls = kind[1]
        return value if isinstance(value, enum_cls) else enum_cls(value)
    if tag == "json":
        return ensure_versioned_document(value)
    raise ValueError(f"unknown field kind: {kind!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Entity-level to_dict / from_dict.
# ---------------------------------------------------------------------------
def to_dict(entity: Any) -> dict:
    """Serialize a domain entity to a JSON-safe ``dict`` with ``format_version``.

    Every field declared in the entity's schema is emitted (optional fields as
    ``null``), and a top-level :data:`FORMAT_VERSION_KEY` envelope is added.
    Nested entities (cart / order line items) and versioned JSON documents
    (notification payload, audit detail) are encoded recursively.

    Raises:
        TypeError: if ``entity`` is not a known domain entity type.
    """
    schema = _SCHEMAS.get(type(entity))
    if schema is None:
        raise TypeError(f"not a serializable domain entity: {type(entity).__name__}")
    out: dict[str, Any] = {FORMAT_VERSION_KEY: FORMAT_VERSION}
    for name, kind in schema:
        out[name] = _encode_value(getattr(entity, name), kind)
    return out


_T = TypeVar("_T")


def from_dict(entity_cls: Type[_T], data: dict) -> _T:
    """Deserialize a ``dict`` produced by :func:`to_dict` back into ``entity_cls``.

    Forward-compatibility guarantees:
      * **Unknown / extra keys are ignored** - only fields named in the entity's
        schema are read, so a payload written by a newer version (with extra
        fields) still loads against this version.
      * **Absent optional fields fall back to the entity's own default** - the
        kwargs dict only includes keys actually present in ``data``, so the
        dataclass default (``None``, ``[]``, ``schema_version=1`` ...) applies.

    Raises:
        TypeError: if ``entity_cls`` is not a known domain entity type.
    """
    schema = _SCHEMAS.get(entity_cls)
    if schema is None:
        raise TypeError(f"not a serializable domain entity: {entity_cls.__name__}")
    if not isinstance(data, dict):
        raise TypeError(f"expected a dict to deserialize, got {type(data).__name__}")

    # Only construct kwargs for fields that are actually present, so missing
    # optional fields defer to the dataclass default (additive-read contract).
    valid_names = {f.name for f in _dataclass_fields(entity_cls)}
    kwargs: dict[str, Any] = {}
    for name, kind in schema:
        if name not in valid_names:  # pragma: no cover - schema/dataclass drift guard
            continue
        if name in data:
            kwargs[name] = _decode_value(data[name], kind)
    return entity_cls(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JSON string convenience wrappers.
# ---------------------------------------------------------------------------
def dumps(entity: Any, **json_kwargs: Any) -> str:
    """Serialize an entity to a JSON string (UTF-8, non-ASCII preserved)."""
    json_kwargs.setdefault("ensure_ascii", False)
    return json.dumps(to_dict(entity), **json_kwargs)


def loads(entity_cls: Type[_T], payload: str) -> _T:
    """Deserialize a JSON string (as produced by :func:`dumps`) into an entity."""
    return from_dict(entity_cls, json.loads(payload))
