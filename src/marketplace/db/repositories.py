"""SQLAlchemy-backed repositories and Unit of Work (task 4.1).

These satisfy the protocols in :mod:`marketplace.domain.repositories` for the
**runtime** backend, mapping between the channel-neutral domain entities
(:mod:`marketplace.domain.entities`) and the SQLAlchemy ORM rows
(:mod:`marketplace.db.models`). Domain services depend only on the protocols, so
swapping the in-memory implementation for this one is transparent to them
(design.md -> Architectural Principles: "no cross-module DB poking").

All repositories operate on a single ``Session``; :class:`SqlAlchemyUnitOfWork`
owns that session and realizes the "one DB transaction per inbound update"
boundary (design.md -> Transaction Boundaries; Req 1.9): entering the ``with``
block opens the session/transaction, :meth:`SqlAlchemyUnitOfWork.commit` commits
it, and leaving the block without committing (or on any exception) rolls back.

Mapping notes:
  * Reads build **detached** domain dataclasses, so callers may freely mutate
    returned entities without affecting the session (mirroring the in-memory
    repositories' deep-copy behaviour).
  * ``Order``/``Cart`` child rows (items) are not modelled as ORM relationships
    in ``db.models``; the repositories load and synchronize them explicitly.
  * Server-assigned values (timestamps, ``order_number``) are read back via a
    ``flush`` + ``refresh`` after insert so the returned entity is faithful.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from marketplace.db import models
from marketplace.domain.entities import (
    AuditEntry,
    Cart,
    CartItem,
    Category,
    Notification,
    NotificationStatus,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Product,
    SellerSettings,
    User,
)

__all__ = [
    "SqlAlchemyUserRepository",
    "SqlAlchemyCatalogRepository",
    "SqlAlchemyCartRepository",
    "SqlAlchemyOrderRepository",
    "SqlAlchemyPaymentRepository",
    "SqlAlchemyAuditRepository",
    "SqlAlchemyNotificationRepository",
    "SqlAlchemySellerSettingsRepository",
    "SqlAlchemyUnitOfWork",
]


# ---------------------------------------------------------------------------
# Row <-> entity mappers (one pair per aggregate).
# ---------------------------------------------------------------------------
def _user_to_domain(row: models.User) -> User:
    return User(
        user_id=row.user_id,
        telegram_user_id=row.telegram_user_id,
        role=row.role,
        verified_contact=row.verified_contact,
        contact_verified_at=row.contact_verified_at,
        offline_payment_allowed=bool(row.offline_payment_allowed),
        language_preference=row.language_preference,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _category_to_domain(row: models.Category) -> Category:
    return Category(
        category_id=row.category_id, name=row.name, created_at=row.created_at
    )


def _product_to_domain(row: models.Product) -> Product:
    return Product(
        product_id=row.product_id,
        name=row.name,
        category_id=row.category_id,
        unit=row.unit,
        price_per_unit=row.price_per_unit,
        min_order_quantity=row.min_order_quantity,
        stock_quantity=row.stock_quantity,
        description=row.description,
        available=bool(row.available),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _payment_to_domain(row: models.Payment) -> Payment:
    return Payment(
        payment_id=row.payment_id,
        order_id=row.order_id,
        utr=row.utr,
        utr_submitted_at=row.utr_submitted_at,
        screenshot_object_key=row.screenshot_object_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _audit_to_domain(row: models.AuditTrail) -> AuditEntry:
    return AuditEntry(
        audit_id=row.audit_id,
        order_id=row.order_id,
        action=row.action,
        detail=dict(row.detail) if row.detail is not None else {},
        acting_user_id=row.acting_user_id,
        created_at=row.created_at,
    )


def _notification_to_domain(row: models.Notification) -> Notification:
    return Notification(
        notification_id=row.notification_id,
        order_id=row.order_id,
        recipient_id=row.recipient_id,
        kind=row.kind,
        transition_seq=row.transition_seq,
        payload=dict(row.payload) if row.payload is not None else {},
        status=row.status,
        attempts=row.attempts,
        last_attempt_at=row.last_attempt_at,
        next_attempt_at=row.next_attempt_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _seller_settings_to_domain(row: models.SellerSettings) -> SellerSettings:
    return SellerSettings(
        pickup_location=row.pickup_location,
        upi_address=row.upi_address,
        upi_qr_object_key=row.upi_qr_object_key,
        utr_pattern=row.utr_pattern,
        seller_settings_id=row.seller_settings_id,
        updated_at=row.updated_at,
        schema_version=row.schema_version,
    )


# ---------------------------------------------------------------------------
# Repositories.
# ---------------------------------------------------------------------------
class SqlAlchemyUserRepository:
    """SQLAlchemy ``UserRepository`` mapping domain <-> ``models.User``."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, user: User) -> User:
        row = models.User(
            user_id=user.user_id,
            telegram_user_id=user.telegram_user_id,
            role=user.role,
            verified_contact=user.verified_contact,
            contact_verified_at=user.contact_verified_at,
            offline_payment_allowed=user.offline_payment_allowed,
            language_preference=user.language_preference,
        )
        if user.created_at is not None:
            row.created_at = user.created_at
        if user.updated_at is not None:
            row.updated_at = user.updated_at
        self._s.add(row)
        self._s.flush()
        self._s.refresh(row)
        return _user_to_domain(row)

    def get(self, user_id) -> Optional[User]:
        row = self._s.get(models.User, user_id)
        return _user_to_domain(row) if row is not None else None

    def get_by_telegram_id(self, telegram_user_id: int) -> Optional[User]:
        row = self._s.execute(
            select(models.User).where(
                models.User.telegram_user_id == telegram_user_id
            )
        ).scalar_one_or_none()
        return _user_to_domain(row) if row is not None else None

    def update(self, user: User) -> User:
        row = self._s.get(models.User, user.user_id)
        if row is None:
            return self.add(user)
        row.telegram_user_id = user.telegram_user_id
        row.role = user.role
        row.verified_contact = user.verified_contact
        row.contact_verified_at = user.contact_verified_at
        row.offline_payment_allowed = user.offline_payment_allowed
        row.language_preference = user.language_preference
        self._s.flush()
        return _user_to_domain(row)

    def list_all(self) -> list[User]:
        rows = self._s.execute(select(models.User)).scalars().all()
        return [_user_to_domain(r) for r in rows]


class SqlAlchemyCatalogRepository:
    """SQLAlchemy ``CatalogRepository`` (categories + products)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    # --- categories --------------------------------------------------------
    def add_category(self, category: Category) -> Category:
        row = models.Category(category_id=category.category_id, name=category.name)
        if category.created_at is not None:
            row.created_at = category.created_at
        self._s.add(row)
        self._s.flush()
        self._s.refresh(row)
        return _category_to_domain(row)

    def get_category(self, category_id) -> Optional[Category]:
        row = self._s.get(models.Category, category_id)
        return _category_to_domain(row) if row is not None else None

    def get_category_by_name(self, name: str) -> Optional[Category]:
        # Case-insensitive match (Req 2.8/2.11). The DB enforces a functional
        # unique index on lower(name); this lookup mirrors it.
        rows = self._s.execute(select(models.Category)).scalars().all()
        target = name.casefold()
        for row in rows:
            if row.name.casefold() == target:
                return _category_to_domain(row)
        return None

    def list_categories(self) -> list[Category]:
        rows = self._s.execute(select(models.Category)).scalars().all()
        return [_category_to_domain(r) for r in rows]

    # --- products ----------------------------------------------------------
    def add_product(self, product: Product) -> Product:
        row = self._product_row(product)
        self._s.add(row)
        self._s.flush()
        self._s.refresh(row)
        return _product_to_domain(row)

    def get_product(self, product_id) -> Optional[Product]:
        row = self._s.get(models.Product, product_id)
        return _product_to_domain(row) if row is not None else None

    def update_product(self, product: Product) -> Product:
        row = self._s.get(models.Product, product.product_id)
        if row is None:
            return self.add_product(product)
        row.name = product.name
        row.category_id = product.category_id
        row.unit = product.unit
        row.price_per_unit = product.price_per_unit
        row.min_order_quantity = product.min_order_quantity
        row.stock_quantity = product.stock_quantity
        row.description = product.description
        row.available = product.available
        self._s.flush()
        return _product_to_domain(row)

    def list_products(self) -> list[Product]:
        rows = self._s.execute(select(models.Product)).scalars().all()
        return [_product_to_domain(r) for r in rows]

    def list_products_in_category(self, category_id) -> list[Product]:
        rows = (
            self._s.execute(
                select(models.Product).where(
                    models.Product.category_id == category_id
                )
            )
            .scalars()
            .all()
        )
        return [_product_to_domain(r) for r in rows]

    def lock_products_for_update(self, product_ids) -> list[Product]:
        # Atomic stock decrement at approval (design.md -> Concurrency and
        # Correctness; Req 7.3/7.4/16.5): lock the involved rows with
        # ``SELECT ... FOR UPDATE`` **ordered by product_id** so concurrent
        # approvals acquire locks in a consistent order (deadlock-free) and
        # serialize, preventing oversell. (On backends without row locks, e.g.
        # SQLite, ``with_for_update`` is a harmless no-op.)
        ids = sorted(set(product_ids), key=str)
        if not ids:
            return []
        rows = (
            self._s.execute(
                select(models.Product)
                .where(models.Product.product_id.in_(ids))
                .order_by(models.Product.product_id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        return [_product_to_domain(r) for r in rows]

    @staticmethod
    def _product_row(product: Product) -> models.Product:
        row = models.Product(
            product_id=product.product_id,
            name=product.name,
            category_id=product.category_id,
            unit=product.unit,
            price_per_unit=product.price_per_unit,
            min_order_quantity=product.min_order_quantity,
            stock_quantity=product.stock_quantity,
            description=product.description,
            available=product.available,
        )
        if product.created_at is not None:
            row.created_at = product.created_at
        if product.updated_at is not None:
            row.updated_at = product.updated_at
        return row


class SqlAlchemyCartRepository:
    """SQLAlchemy ``CartRepository`` (cart + its line items)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def _load_items(self, cart_id) -> list[CartItem]:
        rows = (
            self._s.execute(
                select(models.CartItem).where(models.CartItem.cart_id == cart_id)
            )
            .scalars()
            .all()
        )
        return [
            CartItem(
                product_id=r.product_id,
                quantity=r.quantity,
                cart_item_id=r.cart_item_id,
            )
            for r in rows
        ]

    def get_by_customer(self, customer_id) -> Optional[Cart]:
        row = self._s.execute(
            select(models.Cart).where(models.Cart.customer_id == customer_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return Cart(
            cart_id=row.cart_id,
            customer_id=row.customer_id,
            items=self._load_items(row.cart_id),
        )

    def add(self, cart: Cart) -> Cart:
        row = models.Cart(cart_id=cart.cart_id, customer_id=cart.customer_id)
        self._s.add(row)
        self._s.flush()
        self._sync_items(cart)
        return self.get_by_customer(cart.customer_id)  # type: ignore[return-value]

    def update(self, cart: Cart) -> Cart:
        row = self._s.get(models.Cart, cart.cart_id)
        if row is None:
            return self.add(cart)
        self._sync_items(cart)
        return self.get_by_customer(cart.customer_id)  # type: ignore[return-value]

    def delete(self, cart_id) -> None:
        for item in (
            self._s.execute(
                select(models.CartItem).where(models.CartItem.cart_id == cart_id)
            )
            .scalars()
            .all()
        ):
            self._s.delete(item)
        row = self._s.get(models.Cart, cart_id)
        if row is not None:
            self._s.delete(row)
        self._s.flush()

    def _sync_items(self, cart: Cart) -> None:
        # Replace-all strategy: clear existing lines then insert the domain set.
        for existing in (
            self._s.execute(
                select(models.CartItem).where(models.CartItem.cart_id == cart.cart_id)
            )
            .scalars()
            .all()
        ):
            self._s.delete(existing)
        self._s.flush()
        for item in cart.items:
            self._s.add(
                models.CartItem(
                    cart_item_id=item.cart_item_id or uuid.uuid4(),
                    cart_id=cart.cart_id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
            )
        self._s.flush()


class SqlAlchemyOrderRepository:
    """SQLAlchemy ``OrderRepository`` (order + its line items)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def _load_items(self, order_id) -> list[OrderItem]:
        rows = (
            self._s.execute(
                select(models.OrderItem).where(
                    models.OrderItem.order_id == order_id
                )
            )
            .scalars()
            .all()
        )
        return [
            OrderItem(
                product_id=r.product_id,
                ordered_quantity=r.ordered_quantity,
                unit_price=r.unit_price,
                line_amount=r.line_amount,
                order_item_id=r.order_item_id,
            )
            for r in rows
        ]

    def _row_to_domain(self, row: models.Order) -> Order:
        return Order(
            order_id=row.order_id,
            customer_id=row.customer_id,
            state=row.state,
            total_amount=row.total_amount,
            items=self._load_items(row.order_id),
            order_number=row.order_number,
            rejection_reason=row.rejection_reason,
            schema_version=row.schema_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def add(self, order: Order) -> Order:
        row = models.Order(
            order_id=order.order_id,
            customer_id=order.customer_id,
            state=order.state,
            total_amount=order.total_amount,
            rejection_reason=order.rejection_reason,
        )
        if order.order_number is not None:
            row.order_number = order.order_number
        if order.schema_version is not None:
            row.schema_version = order.schema_version
        if order.created_at is not None:
            row.created_at = order.created_at
        if order.updated_at is not None:
            row.updated_at = order.updated_at
        self._s.add(row)
        self._s.flush()
        self._sync_items(order)
        self._s.refresh(row)
        return self._row_to_domain(row)

    def get(self, order_id) -> Optional[Order]:
        row = self._s.get(models.Order, order_id)
        return self._row_to_domain(row) if row is not None else None

    def update(self, order: Order) -> Order:
        row = self._s.get(models.Order, order.order_id)
        if row is None:
            return self.add(order)
        row.customer_id = order.customer_id
        row.state = order.state
        row.total_amount = order.total_amount
        row.rejection_reason = order.rejection_reason
        if order.schema_version is not None:
            row.schema_version = order.schema_version
        self._s.flush()
        self._sync_items(order)
        return self._row_to_domain(row)

    def list_by_customer(self, customer_id) -> list[Order]:
        rows = (
            self._s.execute(
                select(models.Order)
                .where(models.Order.customer_id == customer_id)
                .order_by(models.Order.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [self._row_to_domain(r) for r in rows]

    def list_by_states(self, states: list[OrderState]) -> list[Order]:
        rows = (
            self._s.execute(
                select(models.Order)
                .where(models.Order.state.in_(states))
                .order_by(models.Order.created_at.asc())
            )
            .scalars()
            .all()
        )
        return [self._row_to_domain(r) for r in rows]

    def list_by_product(self, product_id) -> list[Order]:
        order_ids = (
            self._s.execute(
                select(models.OrderItem.order_id)
                .where(models.OrderItem.product_id == product_id)
                .distinct()
            )
            .scalars()
            .all()
        )
        result = []
        for oid in order_ids:
            row = self._s.get(models.Order, oid)
            if row is not None:
                result.append(self._row_to_domain(row))
        return result

    def _sync_items(self, order: Order) -> None:
        for existing in (
            self._s.execute(
                select(models.OrderItem).where(
                    models.OrderItem.order_id == order.order_id
                )
            )
            .scalars()
            .all()
        ):
            self._s.delete(existing)
        self._s.flush()
        for item in order.items:
            self._s.add(
                models.OrderItem(
                    order_item_id=item.order_item_id or uuid.uuid4(),
                    order_id=order.order_id,
                    product_id=item.product_id,
                    ordered_quantity=item.ordered_quantity,
                    unit_price=item.unit_price,
                    line_amount=item.line_amount,
                )
            )
        self._s.flush()


class SqlAlchemyPaymentRepository:
    """SQLAlchemy ``PaymentRepository`` (one payment per order)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, payment: Payment) -> Payment:
        row = models.Payment(
            payment_id=payment.payment_id,
            order_id=payment.order_id,
            utr=payment.utr,
            utr_submitted_at=payment.utr_submitted_at,
            screenshot_object_key=payment.screenshot_object_key,
        )
        if payment.created_at is not None:
            row.created_at = payment.created_at
        if payment.updated_at is not None:
            row.updated_at = payment.updated_at
        self._s.add(row)
        self._s.flush()
        self._s.refresh(row)
        return _payment_to_domain(row)

    def get(self, payment_id) -> Optional[Payment]:
        row = self._s.get(models.Payment, payment_id)
        return _payment_to_domain(row) if row is not None else None

    def get_by_order(self, order_id) -> Optional[Payment]:
        row = self._s.execute(
            select(models.Payment).where(models.Payment.order_id == order_id)
        ).scalar_one_or_none()
        return _payment_to_domain(row) if row is not None else None

    def get_by_utr(self, utr: str) -> Optional[Payment]:
        if utr is None:
            return None
        row = self._s.execute(
            select(models.Payment).where(models.Payment.utr == utr)
        ).scalar_one_or_none()
        return _payment_to_domain(row) if row is not None else None

    def update(self, payment: Payment) -> Payment:
        row = self._s.get(models.Payment, payment.payment_id)
        if row is None:
            return self.add(payment)
        row.order_id = payment.order_id
        row.utr = payment.utr
        row.utr_submitted_at = payment.utr_submitted_at
        row.screenshot_object_key = payment.screenshot_object_key
        self._s.flush()
        return _payment_to_domain(row)


class SqlAlchemyAuditRepository:
    """SQLAlchemy append-only Audit_Trail repository (Req 15.12/17.8)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, entry: AuditEntry) -> AuditEntry:
        row = models.AuditTrail(
            audit_id=entry.audit_id,
            order_id=entry.order_id,
            action=entry.action,
            detail=dict(entry.detail),
            acting_user_id=entry.acting_user_id,
        )
        if entry.created_at is not None:
            row.created_at = entry.created_at
        self._s.add(row)
        self._s.flush()
        self._s.refresh(row)
        return _audit_to_domain(row)

    def list_by_order(self, order_id) -> list[AuditEntry]:
        rows = (
            self._s.execute(
                select(models.AuditTrail)
                .where(models.AuditTrail.order_id == order_id)
                .order_by(models.AuditTrail.created_at.asc())
            )
            .scalars()
            .all()
        )
        return [_audit_to_domain(r) for r in rows]


class SqlAlchemyNotificationRepository:
    """SQLAlchemy ``NotificationRepository`` (delivery/retry queue, Req 11)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, notification: Notification) -> Notification:
        row = models.Notification(
            notification_id=notification.notification_id,
            order_id=notification.order_id,
            recipient_id=notification.recipient_id,
            kind=notification.kind,
            transition_seq=notification.transition_seq,
            payload=dict(notification.payload),
            status=notification.status,
            attempts=notification.attempts,
            last_attempt_at=notification.last_attempt_at,
        )
        if notification.next_attempt_at is not None:
            row.next_attempt_at = notification.next_attempt_at
        if notification.created_at is not None:
            row.created_at = notification.created_at
        if notification.updated_at is not None:
            row.updated_at = notification.updated_at
        self._s.add(row)
        self._s.flush()
        self._s.refresh(row)
        return _notification_to_domain(row)

    def get(self, notification_id) -> Optional[Notification]:
        row = self._s.get(models.Notification, notification_id)
        return _notification_to_domain(row) if row is not None else None

    def update(self, notification: Notification) -> Notification:
        row = self._s.get(models.Notification, notification.notification_id)
        if row is None:
            return self.add(notification)
        row.order_id = notification.order_id
        row.recipient_id = notification.recipient_id
        row.kind = notification.kind
        row.transition_seq = notification.transition_seq
        row.payload = dict(notification.payload)
        row.status = notification.status
        row.attempts = notification.attempts
        row.last_attempt_at = notification.last_attempt_at
        if notification.next_attempt_at is not None:
            row.next_attempt_at = notification.next_attempt_at
        self._s.flush()
        return _notification_to_domain(row)

    def find_by_idempotency_key(
        self, order_id, kind, transition_seq: int
    ) -> Optional[Notification]:
        row = self._s.execute(
            select(models.Notification).where(
                models.Notification.order_id == order_id,
                models.Notification.kind == kind,
                models.Notification.transition_seq == transition_seq,
            )
        ).scalar_one_or_none()
        return _notification_to_domain(row) if row is not None else None

    def list_due(self, now: datetime) -> list[Notification]:
        rows = (
            self._s.execute(
                select(models.Notification)
                .where(
                    models.Notification.status.in_(
                        [NotificationStatus.PENDING, NotificationStatus.FAILED]
                    ),
                    models.Notification.next_attempt_at <= now,
                )
                .order_by(models.Notification.next_attempt_at.asc())
            )
            .scalars()
            .all()
        )
        return [_notification_to_domain(r) for r in rows]


class SqlAlchemySellerSettingsRepository:
    """SQLAlchemy singleton Seller_Settings repository (Req 19)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def _row(self) -> Optional[models.SellerSettings]:
        return self._s.execute(
            select(models.SellerSettings).where(models.SellerSettings.singleton == 1)
        ).scalar_one_or_none()

    def get(self) -> Optional[SellerSettings]:
        row = self._row()
        return _seller_settings_to_domain(row) if row is not None else None

    def upsert(self, settings: SellerSettings) -> SellerSettings:
        row = self._row()
        if row is None:
            row = models.SellerSettings(
                seller_settings_id=settings.seller_settings_id or uuid.uuid4(),
                singleton=1,
                pickup_location=settings.pickup_location,
                upi_address=settings.upi_address,
                upi_qr_object_key=settings.upi_qr_object_key,
                utr_pattern=settings.utr_pattern,
            )
            self._s.add(row)
        else:
            row.pickup_location = settings.pickup_location
            row.upi_address = settings.upi_address
            row.upi_qr_object_key = settings.upi_qr_object_key
            row.utr_pattern = settings.utr_pattern
        self._s.flush()
        self._s.refresh(row)
        return _seller_settings_to_domain(row)


class SqlAlchemyUnitOfWork:
    """SQLAlchemy Unit of Work realizing the per-update transaction boundary.

    Construct with a ``sessionmaker``; entering the ``with`` block opens a
    session (and its transaction). The aggregate repositories are then available
    as attributes, all sharing the one session. Call :meth:`commit` to persist;
    leaving the block without committing (or on any exception) rolls back, so a
    partial multi-repository operation never persists (Req 1.9).
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Optional[Session] = None

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._session_factory()
        self.users = SqlAlchemyUserRepository(self._session)
        self.catalog = SqlAlchemyCatalogRepository(self._session)
        self.carts = SqlAlchemyCartRepository(self._session)
        self.orders = SqlAlchemyOrderRepository(self._session)
        self.payments = SqlAlchemyPaymentRepository(self._session)
        self.audit = SqlAlchemyAuditRepository(self._session)
        self.notifications = SqlAlchemyNotificationRepository(self._session)
        self.seller_settings = SqlAlchemySellerSettingsRepository(self._session)
        return self

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]:
        try:
            if exc_type is not None:
                self.rollback()
            else:
                # Default to rollback if the caller never committed explicitly.
                self._session.rollback()  # type: ignore[union-attr]
        finally:
            self._session.close()  # type: ignore[union-attr]
            self._session = None
        return None  # never suppress exceptions

    @property
    def session(self) -> Session:
        """The active session (valid only inside the ``with`` block)."""
        if self._session is None:
            raise RuntimeError("UnitOfWork session accessed outside its context")
        return self._session

    def commit(self) -> None:
        if self._session is not None:
            self._session.commit()

    def rollback(self) -> None:
        if self._session is not None:
            self._session.rollback()
