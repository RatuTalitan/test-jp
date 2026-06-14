"""Persistence layer.

SQLAlchemy models, repository implementations (in-memory for property tests,
SQLAlchemy for runtime), the session/transaction context, and Alembic
migrations. Services receive repositories and never poke the DB directly.

:data:`Base` and its ``metadata`` are the schema source-of-truth shared with the
Alembic migration environment (``migrations/env.py`` wires ``target_metadata`` to
``Base.metadata``).
"""

from marketplace.db.models import (
    AuditAction,
    AuditTrail,
    Base,
    Cart,
    CartItem,
    Category,
    Language,
    Meta,
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

metadata = Base.metadata

__all__ = [
    "Base",
    "metadata",
    "Meta",
    "User",
    "Category",
    "Product",
    "Cart",
    "CartItem",
    "Order",
    "OrderItem",
    "Payment",
    "AuditTrail",
    "Notification",
    "SellerSettings",
    "Role",
    "Unit",
    "OrderState",
    "Language",
    "AuditAction",
    "NotificationKind",
    "NotificationStatus",
]
