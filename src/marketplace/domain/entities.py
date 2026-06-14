"""Channel- and DB-agnostic domain entities (task 4.1).

These dataclasses are the **in-memory domain representations** the services
operate on. They are deliberately kept **separate from the SQLAlchemy ORM rows**
(``marketplace.db.models``): the SQLAlchemy repositories
(``marketplace.db.repositories``) map between these domain entities and the ORM
models, while the in-memory repositories (``marketplace.domain.memory``) store
the domain entities directly. Domain services therefore never touch the ORM or
the database directly ("no cross-module DB poking", design.md -> Architectural
Principles).

Design alignment:
  * Money is modelled with :class:`decimal.Decimal` (never binary ``float``),
    matching the ``NUMERIC(12,2)`` / ``NUMERIC(12,3)`` columns and the
    ``Monetary_Rounding`` rule (design.md -> Domain Rules: Monetary Rounding).
  * Identifiers are channel-neutral ``uuid.UUID`` surrogates, mirroring the
    schema's surrogate keys (design.md -> Versioning Strategy).
  * Enumerations are **re-exported from the ORM module** so the domain and the
    database share a single authoritative value set (Role/Unit/OrderState/
    Language/AuditAction/NotificationKind/NotificationStatus). The task permits
    re-exporting or mirroring; re-exporting guarantees they never drift.

Serialization/versioning of these formats is **out of scope for task 4.1** and
is implemented in task 4.2; the ``schema_version`` fields are carried here only
so the entities round-trip faithfully against the ORM rows that already store
them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

# Re-export the enums + shared literals from the ORM module so the domain layer
# and the persistence layer share one authoritative value set (task 4.1 detail:
# "keep enums consistent with the ORM ... you may re-export or mirror them").
from marketplace.db.models import (  # noqa: F401  (re-exported)
    DEFAULT_UTR_PATTERN,
    AuditAction,
    Language,
    NotificationKind,
    NotificationStatus,
    OrderState,
    Role,
    Unit,
)

__all__ = [
    # Re-exported enums / literals.
    "Role",
    "Unit",
    "OrderState",
    "Language",
    "AuditAction",
    "NotificationKind",
    "NotificationStatus",
    "DEFAULT_UTR_PATTERN",
    # Entities.
    "User",
    "Category",
    "Product",
    "CartItem",
    "Cart",
    "OrderItem",
    "Order",
    "Payment",
    "AuditEntry",
    "Notification",
    "SellerSettings",
]


def new_id() -> uuid.UUID:
    """Generate a fresh channel-neutral surrogate identifier."""
    return uuid.uuid4()


@dataclass
class User:
    """A channel-neutral user (design.md -> Data Models -> Users).

    ``verified_contact``/``contact_verified_at`` are ``None`` until the Share
    Contact identity is verified (an unverified user cannot place orders, Req
    12.6). ``language_preference`` of ``None`` means "unset" and is rendered as
    the Hindi default by the presentation layer (Req 18.2/18.4).
    """

    user_id: uuid.UUID
    telegram_user_id: int
    role: Role
    verified_contact: Optional[str] = None
    contact_verified_at: Optional[datetime] = None
    offline_payment_allowed: bool = False
    language_preference: Optional[Language] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def is_authenticated(self) -> bool:
        """True once a Verified_Contact has been recorded (Req 12.6)."""
        return self.contact_verified_at is not None


@dataclass
class Category:
    """A product category (case-insensitive-unique name, Req 2.8/2.11)."""

    category_id: uuid.UUID
    name: str
    created_at: Optional[datetime] = None


@dataclass
class Product:
    """A catalog product (design.md -> Data Models -> Products).

    Quantities (``min_order_quantity``/``stock_quantity``) and money
    (``price_per_unit``) are ``Decimal`` to honour the monetary-rounding rule
    and the ``NUMERIC`` column precision.
    """

    product_id: uuid.UUID
    name: str
    category_id: uuid.UUID
    unit: Unit
    price_per_unit: Decimal
    min_order_quantity: Decimal
    stock_quantity: Decimal
    description: Optional[str] = None
    available: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class CartItem:
    """A single product line within a cart (Req 4.5)."""

    product_id: uuid.UUID
    quantity: Decimal
    cart_item_id: Optional[uuid.UUID] = None


@dataclass
class Cart:
    """A customer's single active cart (one per customer, Req 4.5)."""

    cart_id: uuid.UUID
    customer_id: uuid.UUID
    items: list[CartItem] = field(default_factory=list)


@dataclass
class OrderItem:
    """A single order line with a price snapshot (Req 5.2).

    ``unit_price`` snapshots the catalog price at order (or modification) time
    so later catalog edits never change historical totals; ``line_amount`` is
    ``round(ordered_quantity * unit_price, 2)`` half-up (Monetary_Rounding).
    """

    product_id: uuid.UUID
    ordered_quantity: Decimal
    unit_price: Decimal
    line_amount: Decimal
    order_item_id: Optional[uuid.UUID] = None


@dataclass
class Order:
    """A placed order with lifecycle ``state`` and snapshot totals (Req 5)."""

    order_id: uuid.UUID
    customer_id: uuid.UUID
    state: OrderState
    total_amount: Decimal
    items: list[OrderItem] = field(default_factory=list)
    order_number: Optional[int] = None
    rejection_reason: Optional[str] = None
    schema_version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Payment:
    """A per-order payment record with a globally-unique nullable UTR (Req 6)."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    utr: Optional[str] = None
    utr_submitted_at: Optional[datetime] = None
    screenshot_object_key: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class AuditEntry:
    """An append-only record of a significant Seller action (Req 15.12/17.8).

    ``detail`` is the additive ``{format_version, ...}`` document; its concrete
    serialization is task 4.2's concern.
    """

    audit_id: uuid.UUID
    order_id: uuid.UUID
    action: AuditAction
    detail: dict
    acting_user_id: uuid.UUID
    created_at: Optional[datetime] = None


@dataclass
class Notification:
    """A queued, retry-able notification (Req 11.1-11.5).

    Idempotent per ``(order_id, kind, transition_seq)``; the retry worker polls
    ``status in {PENDING, FAILED} AND next_attempt_at <= now``.
    """

    notification_id: uuid.UUID
    order_id: uuid.UUID
    recipient_id: uuid.UUID
    kind: NotificationKind
    transition_seq: int
    payload: dict
    status: NotificationStatus = NotificationStatus.PENDING
    attempts: int = 0
    last_attempt_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class SellerSettings:
    """The single, Seller-owned operator settings row (Req 19, 6.2/6.6)."""

    pickup_location: Optional[str] = None
    upi_address: Optional[str] = None
    upi_qr_object_key: Optional[str] = None
    utr_pattern: str = DEFAULT_UTR_PATTERN
    seller_settings_id: Optional[uuid.UUID] = None
    updated_at: Optional[datetime] = None
    schema_version: int = 1
