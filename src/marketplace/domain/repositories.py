"""Repository protocols and the Unit-of-Work contract (task 4.1).

These :class:`typing.Protocol` interfaces are the **only** way domain services
reach persistence. Two implementations satisfy them:

  * an **in-memory** implementation (``marketplace.domain.memory``) used by the
    fast property tests, and
  * a **SQLAlchemy-backed** implementation (``marketplace.db.repositories``)
    used at runtime, mapping domain entities <-> ORM rows.

Because the services depend only on these protocols, they never import the ORM
or open a database connection directly (design.md -> Architectural Principles:
"clear interfaces, no cross-module DB poking").

### Transaction / session boundary contract

Design states the Bot_Interface "opens **one database transaction per inbound
update**" and the domain operation "either commits atomically or rolls back
entirely" (design.md -> Concurrency and Correctness -> Transaction Boundaries;
Req 1.9). That boundary is modelled here as :class:`UnitOfWork`: a context
manager that exposes one repository per aggregate, all participating in the same
transaction. Entering the context begins the transaction; leaving it without an
explicit :meth:`UnitOfWork.commit` rolls back (so an exception mid-flight leaves
no partial write). Services receive a ``UnitOfWork`` and call its repositories;
they never construct sessions themselves.

The protocols are ``@runtime_checkable`` so tests can assert an implementation
"is a" repository via ``isinstance``. (Runtime checks verify method presence,
not signatures - the unit tests additionally exercise behaviour.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from marketplace.domain.entities import (
    AuditEntry,
    Cart,
    Category,
    Notification,
    Order,
    OrderState,
    Payment,
    Product,
    SellerSettings,
    User,
)

__all__ = [
    "UserRepository",
    "CatalogRepository",
    "CartRepository",
    "OrderRepository",
    "PaymentRepository",
    "AuditRepository",
    "NotificationRepository",
    "SellerSettingsRepository",
    "UnitOfWork",
]


@runtime_checkable
class UserRepository(Protocol):
    """Persistence for the Users aggregate (Auth_Service)."""

    def add(self, user: User) -> User:
        """Insert a new user and return the stored entity."""
        ...

    def get(self, user_id) -> Optional[User]:
        """Return the user by internal id, or ``None``."""
        ...

    def get_by_telegram_id(self, telegram_user_id: int) -> Optional[User]:
        """Return the user by external Telegram id, or ``None`` (Req 1.3)."""
        ...

    def update(self, user: User) -> User:
        """Persist changes to an existing user and return it."""
        ...

    def list_all(self) -> list[User]:
        """Return every user (small dataset; primarily for admin/tests)."""
        ...


@runtime_checkable
class CatalogRepository(Protocol):
    """Persistence for the Catalog aggregate (categories + products)."""

    # --- categories --------------------------------------------------------
    def add_category(self, category: Category) -> Category: ...

    def get_category(self, category_id) -> Optional[Category]: ...

    def get_category_by_name(self, name: str) -> Optional[Category]:
        """Case-insensitive lookup used to enforce uniqueness (Req 2.8/2.11)."""
        ...

    def list_categories(self) -> list[Category]: ...

    # --- products ----------------------------------------------------------
    def add_product(self, product: Product) -> Product: ...

    def get_product(self, product_id) -> Optional[Product]: ...

    def update_product(self, product: Product) -> Product: ...

    def list_products(self) -> list[Product]:
        """Return every product (available or not)."""
        ...

    def list_products_in_category(self, category_id) -> list[Product]: ...

    def lock_products_for_update(self, product_ids) -> list[Product]:
        """Acquire write locks on the given products and return them.

        The atomic stock decrement at approval (design.md -> Concurrency and
        Correctness -> Atomic Stock Decrement at Approval; Req 7.3/7.4) requires
        the involved product rows to be locked **before** the stock re-read so
        two concurrent approvals can never both observe the pre-decrement stock
        and oversell (Req 16.5, the global no-oversell invariant).

        Implementations:

        * **SQLAlchemy** issues ``SELECT ... FOR UPDATE`` over the products,
          **ordered by ``product_id``** so any two transactions acquire the
          locks in the same order and never deadlock.
        * **In-memory** has no row locks; under the snapshot-isolated
          single-writer Unit-of-Work the subsequent check-then-decrement is
          already atomic, so it simply re-reads and returns the current
          products in ``product_id`` order to mirror the SQL contract.

        Returns the (deterministically ordered) products that exist; missing
        ids are silently skipped and surfaced by the caller as a NotFound.
        """
        ...


@runtime_checkable
class CartRepository(Protocol):
    """Persistence for the Carts aggregate (one cart per customer, Req 4.5)."""

    def get_by_customer(self, customer_id) -> Optional[Cart]: ...

    def add(self, cart: Cart) -> Cart: ...

    def update(self, cart: Cart) -> Cart:
        """Persist the cart and its line items (upsert/replace lines)."""
        ...

    def delete(self, cart_id) -> None:
        """Remove a cart and its items (used by ``clear``)."""
        ...


@runtime_checkable
class OrderRepository(Protocol):
    """Persistence for the Orders aggregate (orders + order items)."""

    def add(self, order: Order) -> Order: ...

    def get(self, order_id) -> Optional[Order]: ...

    def update(self, order: Order) -> Order: ...

    def list_by_customer(self, customer_id) -> list[Order]:
        """Customer order history, newest-first (Req 10.1)."""
        ...

    def list_by_states(self, states: list[OrderState]) -> list[Order]:
        """Orders in any of ``states`` (e.g. active/non-terminal, Req 10.6)."""
        ...

    def list_by_product(self, product_id) -> list[Order]:
        """Orders containing a given product (fulfillability recompute, 16.1)."""
        ...


@runtime_checkable
class PaymentRepository(Protocol):
    """Persistence for the Payments aggregate (one per order, Req 6)."""

    def add(self, payment: Payment) -> Payment: ...

    def get(self, payment_id) -> Optional[Payment]: ...

    def get_by_order(self, order_id) -> Optional[Payment]: ...

    def get_by_utr(self, utr: str) -> Optional[Payment]:
        """Lookup supporting global UTR-uniqueness checks (Req 6.4)."""
        ...

    def update(self, payment: Payment) -> Payment: ...


@runtime_checkable
class AuditRepository(Protocol):
    """Append-only persistence for the Audit_Trail (Req 15.12/17.8).

    Exposes only insert + read; there is intentionally no update/delete
    affordance, mirroring the DB privilege model.
    """

    def add(self, entry: AuditEntry) -> AuditEntry: ...

    def list_by_order(self, order_id) -> list[AuditEntry]: ...


@runtime_checkable
class NotificationRepository(Protocol):
    """Persistence for the Notifications delivery/retry queue (Req 11)."""

    def add(self, notification: Notification) -> Notification: ...

    def get(self, notification_id) -> Optional[Notification]: ...

    def update(self, notification: Notification) -> Notification: ...

    def find_by_idempotency_key(
        self, order_id, kind, transition_seq: int
    ) -> Optional[Notification]:
        """Return an existing row for the idempotency key, or ``None`` (Req 11)."""
        ...

    def list_due(self, now: datetime) -> list[Notification]:
        """Return PENDING/FAILED rows whose ``next_attempt_at <= now``."""
        ...


@runtime_checkable
class SellerSettingsRepository(Protocol):
    """Persistence for the single Seller_Settings row (Req 19, 6.2/6.6)."""

    def get(self) -> Optional[SellerSettings]:
        """Return the singleton settings row, or ``None`` if not yet seeded."""
        ...

    def upsert(self, settings: SellerSettings) -> SellerSettings:
        """Create or update the single settings row and return it."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """One atomic transaction spanning every aggregate (design.md ->
    Transaction Boundaries; Req 1.9).

    Usage::

        with uow:                       # begins a transaction
            user = uow.users.add(...)
            uow.orders.add(...)
            uow.commit()                # commit atomically
        # leaving the block without commit() rolls back

    Implementations supply one repository per aggregate, all bound to the same
    transaction, so a service's multi-repository operation commits or rolls back
    as a unit.
    """

    users: UserRepository
    catalog: CatalogRepository
    carts: CartRepository
    orders: OrderRepository
    payments: PaymentRepository
    audit: AuditRepository
    notifications: NotificationRepository
    seller_settings: SellerSettingsRepository

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]: ...

    def commit(self) -> None:
        """Commit all work done in this transaction."""
        ...

    def rollback(self) -> None:
        """Discard all work done in this transaction."""
        ...
