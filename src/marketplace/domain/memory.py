"""In-memory repository + Unit-of-Work implementations (task 4.1).

These satisfy the protocols in :mod:`marketplace.domain.repositories` using
plain Python dicts/lists, so the property and unit test suites can exercise the
domain services with **no database, no I/O, and no per-call setup cost**
(design.md -> Testing Strategy: services are written against "in-memory or
transactional-test repositories so property tests run fast").

Isolation & transaction semantics:
  * Stored entities are deep-copied on write and on read, so a caller mutating a
    returned entity never silently corrupts stored state (mirroring the
    detached-row behaviour of the SQLAlchemy implementation).
  * :class:`InMemoryUnitOfWork` snapshots the whole store when the ``with``
    block is entered and restores that snapshot on :meth:`rollback` or when the
    block exits without :meth:`commit` (including on an exception). This
    reproduces the "one transaction per inbound update, commit-or-rollback"
    boundary (design.md -> Transaction Boundaries; Req 1.9) without a database.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Optional

from marketplace.domain.entities import (
    AuditEntry,
    Cart,
    Category,
    Notification,
    NotificationStatus,
    Order,
    OrderState,
    Payment,
    Product,
    SellerSettings,
    User,
)

__all__ = [
    "InMemoryDatabase",
    "InMemoryUserRepository",
    "InMemoryCatalogRepository",
    "InMemoryCartRepository",
    "InMemoryOrderRepository",
    "InMemoryPaymentRepository",
    "InMemoryAuditRepository",
    "InMemoryNotificationRepository",
    "InMemorySellerSettingsRepository",
    "InMemoryUnitOfWork",
]


class InMemoryDatabase:
    """The shared, in-process store backing the in-memory repositories.

    A single instance plays the role the real database plays for the SQLAlchemy
    repositories: it survives across transactions, while each
    :class:`InMemoryUnitOfWork` reads/writes it under snapshot-isolation.
    """

    def __init__(self) -> None:
        self.users: dict = {}
        self.categories: dict = {}
        self.products: dict = {}
        self.carts: dict = {}
        self.orders: dict = {}
        self.payments: dict = {}
        self.audit: list = []
        self.notifications: dict = {}
        self.seller_settings: Optional[SellerSettings] = None

    def snapshot(self) -> dict:
        """Return a deep copy of all tables (for transaction rollback)."""
        return {
            "users": copy.deepcopy(self.users),
            "categories": copy.deepcopy(self.categories),
            "products": copy.deepcopy(self.products),
            "carts": copy.deepcopy(self.carts),
            "orders": copy.deepcopy(self.orders),
            "payments": copy.deepcopy(self.payments),
            "audit": copy.deepcopy(self.audit),
            "notifications": copy.deepcopy(self.notifications),
            "seller_settings": copy.deepcopy(self.seller_settings),
        }

    def restore(self, snap: dict) -> None:
        """Replace all tables with a previously taken :meth:`snapshot`."""
        self.users = snap["users"]
        self.categories = snap["categories"]
        self.products = snap["products"]
        self.carts = snap["carts"]
        self.orders = snap["orders"]
        self.payments = snap["payments"]
        self.audit = snap["audit"]
        self.notifications = snap["notifications"]
        self.seller_settings = snap["seller_settings"]


def _clone(entity):
    """Deep-copy an entity so stored and returned objects never alias."""
    return copy.deepcopy(entity)


class InMemoryUserRepository:
    """In-memory :class:`~marketplace.domain.repositories.UserRepository`."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, user: User) -> User:
        self._db.users[user.user_id] = _clone(user)
        return _clone(user)

    def get(self, user_id) -> Optional[User]:
        found = self._db.users.get(user_id)
        return _clone(found) if found is not None else None

    def get_by_telegram_id(self, telegram_user_id: int) -> Optional[User]:
        for user in self._db.users.values():
            if user.telegram_user_id == telegram_user_id:
                return _clone(user)
        return None

    def update(self, user: User) -> User:
        self._db.users[user.user_id] = _clone(user)
        return _clone(user)

    def list_all(self) -> list[User]:
        return [_clone(u) for u in self._db.users.values()]


class InMemoryCatalogRepository:
    """In-memory :class:`~marketplace.domain.repositories.CatalogRepository`."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    # --- categories --------------------------------------------------------
    def add_category(self, category: Category) -> Category:
        self._db.categories[category.category_id] = _clone(category)
        return _clone(category)

    def get_category(self, category_id) -> Optional[Category]:
        found = self._db.categories.get(category_id)
        return _clone(found) if found is not None else None

    def get_category_by_name(self, name: str) -> Optional[Category]:
        target = name.casefold()
        for category in self._db.categories.values():
            if category.name.casefold() == target:
                return _clone(category)
        return None

    def list_categories(self) -> list[Category]:
        return [_clone(c) for c in self._db.categories.values()]

    # --- products ----------------------------------------------------------
    def add_product(self, product: Product) -> Product:
        self._db.products[product.product_id] = _clone(product)
        return _clone(product)

    def get_product(self, product_id) -> Optional[Product]:
        found = self._db.products.get(product_id)
        return _clone(found) if found is not None else None

    def update_product(self, product: Product) -> Product:
        self._db.products[product.product_id] = _clone(product)
        return _clone(product)

    def list_products(self) -> list[Product]:
        return [_clone(p) for p in self._db.products.values()]

    def list_products_in_category(self, category_id) -> list[Product]:
        return [
            _clone(p)
            for p in self._db.products.values()
            if p.category_id == category_id
        ]

    def lock_products_for_update(self, product_ids) -> list[Product]:
        # In-memory has no row locks; the snapshot-isolated, single-writer
        # Unit-of-Work already makes the caller's check-then-decrement atomic
        # (design.md -> Concurrency and Correctness). Re-read and return the
        # current products in ``product_id`` order so the in-memory contract
        # mirrors the SQLAlchemy ``SELECT ... FOR UPDATE ORDER BY product_id``
        # path (consistent ordering avoids deadlocks there). Unknown ids are
        # skipped; the caller surfaces them as a NotFound.
        result: list[Product] = []
        for pid in sorted(set(product_ids), key=str):
            found = self._db.products.get(pid)
            if found is not None:
                result.append(_clone(found))
        return result


class InMemoryCartRepository:
    """In-memory :class:`~marketplace.domain.repositories.CartRepository`."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def get_by_customer(self, customer_id) -> Optional[Cart]:
        for cart in self._db.carts.values():
            if cart.customer_id == customer_id:
                return _clone(cart)
        return None

    def add(self, cart: Cart) -> Cart:
        self._db.carts[cart.cart_id] = _clone(cart)
        return _clone(cart)

    def update(self, cart: Cart) -> Cart:
        self._db.carts[cart.cart_id] = _clone(cart)
        return _clone(cart)

    def delete(self, cart_id) -> None:
        self._db.carts.pop(cart_id, None)


class InMemoryOrderRepository:
    """In-memory :class:`~marketplace.domain.repositories.OrderRepository`."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, order: Order) -> Order:
        self._db.orders[order.order_id] = _clone(order)
        return _clone(order)

    def get(self, order_id) -> Optional[Order]:
        found = self._db.orders.get(order_id)
        return _clone(found) if found is not None else None

    def update(self, order: Order) -> Order:
        self._db.orders[order.order_id] = _clone(order)
        return _clone(order)

    def list_by_customer(self, customer_id) -> list[Order]:
        orders = [
            o for o in self._db.orders.values() if o.customer_id == customer_id
        ]
        # Newest-first (Req 10.1); fall back to insertion order when timestamps
        # are absent in tests by sorting on created_at with a stable key.
        orders.sort(
            key=lambda o: (o.created_at is not None, o.created_at),
            reverse=True,
        )
        return [_clone(o) for o in orders]

    def list_by_states(self, states: list[OrderState]) -> list[Order]:
        wanted = set(states)
        return [
            _clone(o) for o in self._db.orders.values() if o.state in wanted
        ]

    def list_by_product(self, product_id) -> list[Order]:
        result = []
        for order in self._db.orders.values():
            if any(item.product_id == product_id for item in order.items):
                result.append(_clone(order))
        return result


class InMemoryPaymentRepository:
    """In-memory :class:`~marketplace.domain.repositories.PaymentRepository`."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, payment: Payment) -> Payment:
        self._db.payments[payment.payment_id] = _clone(payment)
        return _clone(payment)

    def get(self, payment_id) -> Optional[Payment]:
        found = self._db.payments.get(payment_id)
        return _clone(found) if found is not None else None

    def get_by_order(self, order_id) -> Optional[Payment]:
        for payment in self._db.payments.values():
            if payment.order_id == order_id:
                return _clone(payment)
        return None

    def get_by_utr(self, utr: str) -> Optional[Payment]:
        if utr is None:
            return None
        for payment in self._db.payments.values():
            if payment.utr == utr:
                return _clone(payment)
        return None

    def update(self, payment: Payment) -> Payment:
        self._db.payments[payment.payment_id] = _clone(payment)
        return _clone(payment)


class InMemoryAuditRepository:
    """In-memory append-only Audit_Trail repository (Req 15.12/17.8)."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, entry: AuditEntry) -> AuditEntry:
        self._db.audit.append(_clone(entry))
        return _clone(entry)

    def list_by_order(self, order_id) -> list[AuditEntry]:
        return [_clone(e) for e in self._db.audit if e.order_id == order_id]


class InMemoryNotificationRepository:
    """In-memory :class:`~...repositories.NotificationRepository` (Req 11)."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, notification: Notification) -> Notification:
        self._db.notifications[notification.notification_id] = _clone(notification)
        return _clone(notification)

    def get(self, notification_id) -> Optional[Notification]:
        found = self._db.notifications.get(notification_id)
        return _clone(found) if found is not None else None

    def update(self, notification: Notification) -> Notification:
        self._db.notifications[notification.notification_id] = _clone(notification)
        return _clone(notification)

    def find_by_idempotency_key(
        self, order_id, kind, transition_seq: int
    ) -> Optional[Notification]:
        for n in self._db.notifications.values():
            if (
                n.order_id == order_id
                and n.kind == kind
                and n.transition_seq == transition_seq
            ):
                return _clone(n)
        return None

    def list_due(self, now: datetime) -> list[Notification]:
        due_statuses = {NotificationStatus.PENDING, NotificationStatus.FAILED}
        return [
            _clone(n)
            for n in self._db.notifications.values()
            if n.status in due_statuses
            and (n.next_attempt_at is None or n.next_attempt_at <= now)
        ]


class InMemorySellerSettingsRepository:
    """In-memory singleton Seller_Settings repository (Req 19)."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def get(self) -> Optional[SellerSettings]:
        return _clone(self._db.seller_settings) if self._db.seller_settings else None

    def upsert(self, settings: SellerSettings) -> SellerSettings:
        self._db.seller_settings = _clone(settings)
        return _clone(settings)


class InMemoryUnitOfWork:
    """Snapshot-isolated in-memory Unit of Work (design.md -> Transaction
    Boundaries; Req 1.9).

    Entering the ``with`` block snapshots the store; :meth:`commit` keeps the
    changes; exiting without committing (or any exception) restores the
    snapshot, so a partial multi-repository operation never persists.
    """

    def __init__(self, db: Optional[InMemoryDatabase] = None) -> None:
        self._db = db if db is not None else InMemoryDatabase()
        self.users = InMemoryUserRepository(self._db)
        self.catalog = InMemoryCatalogRepository(self._db)
        self.carts = InMemoryCartRepository(self._db)
        self.orders = InMemoryOrderRepository(self._db)
        self.payments = InMemoryPaymentRepository(self._db)
        self.audit = InMemoryAuditRepository(self._db)
        self.notifications = InMemoryNotificationRepository(self._db)
        self.seller_settings = InMemorySellerSettingsRepository(self._db)
        self._snapshot: Optional[dict] = None
        self._committed = False

    @property
    def db(self) -> InMemoryDatabase:
        """The underlying shared store (useful for test assertions)."""
        return self._db

    def __enter__(self) -> "InMemoryUnitOfWork":
        self._snapshot = self._db.snapshot()
        self._committed = False
        return self

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]:
        # Roll back on exception or when the caller never committed.
        if exc_type is not None or not self._committed:
            self.rollback()
        self._snapshot = None
        return None  # never suppress exceptions

    def commit(self) -> None:
        self._committed = True
        # Refresh the rollback baseline so a subsequent rollback within the same
        # (reused) context returns to this committed point rather than entry.
        self._snapshot = self._db.snapshot()

    def rollback(self) -> None:
        if self._snapshot is not None:
            self._db.restore(self._snapshot)
        self._committed = False
