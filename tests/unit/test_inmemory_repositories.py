"""Unit tests for the in-memory repositories and Unit of Work (task 4.1).

Asserts that:
  * each in-memory repository structurally satisfies its
    :class:`typing.Protocol` (``isinstance`` against the runtime-checkable
    protocol), and that the in-memory Unit of Work satisfies ``UnitOfWork``;
  * every aggregate round-trips a domain entity (add/upsert -> get returns an
    equal entity);
  * returned entities are isolated copies (mutating a returned entity does not
    corrupt stored state);
  * the Unit of Work honours the "commit-or-rollback" transaction boundary
    (design.md -> Transaction Boundaries; Req 1.9): an uncommitted block (or an
    exception) leaves no writes, while a committed block persists.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from marketplace.domain import (
    AuditAction,
    AuditEntry,
    AuditRepository,
    Cart,
    CartItem,
    CartRepository,
    Category,
    CatalogRepository,
    InMemoryAuditRepository,
    InMemoryCartRepository,
    InMemoryCatalogRepository,
    InMemoryDatabase,
    InMemoryNotificationRepository,
    InMemoryOrderRepository,
    InMemoryPaymentRepository,
    InMemorySellerSettingsRepository,
    InMemoryUnitOfWork,
    InMemoryUserRepository,
    Language,
    Notification,
    NotificationKind,
    NotificationRepository,
    NotificationStatus,
    Order,
    OrderItem,
    OrderRepository,
    OrderState,
    Payment,
    PaymentRepository,
    Product,
    Role,
    SellerSettings,
    SellerSettingsRepository,
    Unit,
    UnitOfWork,
    User,
    UserRepository,
    new_id,
)

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Entity builders.
# ---------------------------------------------------------------------------
def make_user(**overrides) -> User:
    base = dict(
        user_id=new_id(),
        telegram_user_id=12345,
        role=Role.CUSTOMER,
        verified_contact="+910000000000",
        contact_verified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        offline_payment_allowed=False,
        language_preference=None,
    )
    base.update(overrides)
    return User(**base)


def make_category(**overrides) -> Category:
    base = dict(category_id=new_id(), name="Oil Cakes")
    base.update(overrides)
    return Category(**base)


def make_product(category_id=None, **overrides) -> Product:
    base = dict(
        product_id=new_id(),
        name="Cotton Seed Oil Cake",
        category_id=category_id or new_id(),
        unit=Unit.KILOGRAM,
        price_per_unit=Decimal("100.00"),
        min_order_quantity=Decimal("10.000"),
        stock_quantity=Decimal("500.000"),
        description="Premium grade",
        available=True,
    )
    base.update(overrides)
    return Product(**base)


# ---------------------------------------------------------------------------
# Protocol conformance.
# ---------------------------------------------------------------------------
def test_inmemory_repositories_satisfy_protocols():
    db = InMemoryDatabase()
    assert isinstance(InMemoryUserRepository(db), UserRepository)
    assert isinstance(InMemoryCatalogRepository(db), CatalogRepository)
    assert isinstance(InMemoryCartRepository(db), CartRepository)
    assert isinstance(InMemoryOrderRepository(db), OrderRepository)
    assert isinstance(InMemoryPaymentRepository(db), PaymentRepository)
    assert isinstance(InMemoryAuditRepository(db), AuditRepository)
    assert isinstance(InMemoryNotificationRepository(db), NotificationRepository)
    assert isinstance(
        InMemorySellerSettingsRepository(db), SellerSettingsRepository
    )


def test_inmemory_unit_of_work_satisfies_protocol():
    assert isinstance(InMemoryUnitOfWork(), UnitOfWork)


# ---------------------------------------------------------------------------
# Round-trips, per aggregate.
# ---------------------------------------------------------------------------
def test_user_round_trip():
    db = InMemoryDatabase()
    repo = InMemoryUserRepository(db)
    user = make_user()
    repo.add(user)
    assert repo.get(user.user_id) == user
    assert repo.get_by_telegram_id(user.telegram_user_id) == user


def test_user_update_round_trip():
    db = InMemoryDatabase()
    repo = InMemoryUserRepository(db)
    user = make_user(language_preference=None)
    repo.add(user)
    user.language_preference = Language.EN
    repo.update(user)
    assert repo.get(user.user_id).language_preference.value == "EN"


def test_catalog_round_trip():
    db = InMemoryDatabase()
    repo = InMemoryCatalogRepository(db)
    cat = make_category()
    repo.add_category(cat)
    prod = make_product(category_id=cat.category_id)
    repo.add_product(prod)

    assert repo.get_category(cat.category_id) == cat
    assert repo.get_category_by_name("OIL CAKES") == cat  # case-insensitive
    assert repo.get_product(prod.product_id) == prod
    assert repo.list_products_in_category(cat.category_id) == [prod]


def test_cart_round_trip():
    db = InMemoryDatabase()
    repo = InMemoryCartRepository(db)
    customer_id = new_id()
    cart = Cart(
        cart_id=new_id(),
        customer_id=customer_id,
        items=[CartItem(product_id=new_id(), quantity=Decimal("10.000"))],
    )
    repo.add(cart)
    assert repo.get_by_customer(customer_id) == cart
    repo.delete(cart.cart_id)
    assert repo.get_by_customer(customer_id) is None


def test_order_round_trip_and_queries():
    db = InMemoryDatabase()
    repo = InMemoryOrderRepository(db)
    customer_id = new_id()
    product_id = new_id()
    order = Order(
        order_id=new_id(),
        customer_id=customer_id,
        state=OrderState.PAYMENT_PENDING,
        total_amount=Decimal("1000.00"),
        items=[
            OrderItem(
                product_id=product_id,
                ordered_quantity=Decimal("10.000"),
                unit_price=Decimal("100.00"),
                line_amount=Decimal("1000.00"),
            )
        ],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    repo.add(order)
    assert repo.get(order.order_id) == order
    assert repo.list_by_customer(customer_id) == [order]
    assert repo.list_by_states([OrderState.PAYMENT_PENDING]) == [order]
    assert repo.list_by_states([OrderState.APPROVED]) == []
    assert repo.list_by_product(product_id) == [order]


def test_order_history_newest_first():
    db = InMemoryDatabase()
    repo = InMemoryOrderRepository(db)
    customer_id = new_id()
    older = Order(
        order_id=new_id(),
        customer_id=customer_id,
        state=OrderState.PLACED,
        total_amount=Decimal("0.00"),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    newer = Order(
        order_id=new_id(),
        customer_id=customer_id,
        state=OrderState.PLACED,
        total_amount=Decimal("0.00"),
        created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    repo.add(older)
    repo.add(newer)
    assert [o.order_id for o in repo.list_by_customer(customer_id)] == [
        newer.order_id,
        older.order_id,
    ]


def test_payment_round_trip_and_utr_lookup():
    db = InMemoryDatabase()
    repo = InMemoryPaymentRepository(db)
    order_id = new_id()
    payment = Payment(payment_id=new_id(), order_id=order_id, utr="ABCD12345678")
    repo.add(payment)
    assert repo.get(payment.payment_id) == payment
    assert repo.get_by_order(order_id) == payment
    assert repo.get_by_utr("ABCD12345678") == payment
    assert repo.get_by_utr("NOPE99999999") is None


def test_audit_is_append_only_and_queryable():
    db = InMemoryDatabase()
    repo = InMemoryAuditRepository(db)
    order_id = new_id()
    entry = AuditEntry(
        audit_id=new_id(),
        order_id=order_id,
        action=AuditAction.OFFLINE_APPROVAL,
        detail={"format_version": 1, "field": "state"},
        acting_user_id=new_id(),
    )
    repo.add(entry)
    listed = repo.list_by_order(order_id)
    assert listed == [entry]
    # The repository exposes only add + list (no update/delete affordance).
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")


def test_notification_round_trip_idempotency_and_due():
    db = InMemoryDatabase()
    repo = InMemoryNotificationRepository(db)
    order_id = new_id()
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    notif = Notification(
        notification_id=new_id(),
        order_id=order_id,
        recipient_id=new_id(),
        kind=NotificationKind.STATE_CHANGE,
        transition_seq=1,
        payload={"format_version": 1},
        status=NotificationStatus.PENDING,
        next_attempt_at=now,
    )
    repo.add(notif)
    assert repo.get(notif.notification_id) == notif
    assert (
        repo.find_by_idempotency_key(order_id, NotificationKind.STATE_CHANGE, 1)
        == notif
    )
    assert repo.list_due(now) == [notif]
    # A DELIVERED notification is no longer due.
    notif.status = NotificationStatus.DELIVERED
    repo.update(notif)
    assert repo.list_due(now) == []


def test_seller_settings_singleton_upsert():
    db = InMemoryDatabase()
    repo = InMemorySellerSettingsRepository(db)
    assert repo.get() is None
    settings = SellerSettings(pickup_location="Warehouse 1", upi_address="seller@upi")
    repo.upsert(settings)
    assert repo.get().pickup_location == "Warehouse 1"
    repo.upsert(SellerSettings(pickup_location="Warehouse 2"))
    assert repo.get().pickup_location == "Warehouse 2"


# ---------------------------------------------------------------------------
# Isolation.
# ---------------------------------------------------------------------------
def test_returned_entities_are_isolated_copies():
    db = InMemoryDatabase()
    repo = InMemoryCatalogRepository(db)
    prod = make_product()
    repo.add_product(prod)
    fetched = repo.get_product(prod.product_id)
    fetched.stock_quantity = Decimal("0.000")  # mutate the returned copy
    # Stored state is unchanged.
    assert repo.get_product(prod.product_id).stock_quantity == Decimal("500.000")


# ---------------------------------------------------------------------------
# Unit-of-Work transaction boundary.
# ---------------------------------------------------------------------------
def test_uow_commit_persists():
    db = InMemoryDatabase()
    user = make_user()
    with InMemoryUnitOfWork(db) as uow:
        uow.users.add(user)
        uow.commit()
    # A fresh UoW over the same store sees the committed user.
    with InMemoryUnitOfWork(db) as uow2:
        assert uow2.users.get(user.user_id) == user


def test_uow_rollback_on_no_commit():
    db = InMemoryDatabase()
    user = make_user()
    with InMemoryUnitOfWork(db) as uow:
        uow.users.add(user)
        # no commit -> rolled back on exit
    with InMemoryUnitOfWork(db) as uow2:
        assert uow2.users.get(user.user_id) is None


def test_uow_rollback_on_exception():
    db = InMemoryDatabase()
    user = make_user()
    with pytest.raises(RuntimeError):
        with InMemoryUnitOfWork(db) as uow:
            uow.users.add(user)
            raise RuntimeError("boom mid-transaction")
    with InMemoryUnitOfWork(db) as uow2:
        assert uow2.users.get(user.user_id) is None


def test_uow_explicit_rollback_discards_partial_writes():
    db = InMemoryDatabase()
    user_a = make_user(telegram_user_id=1)
    user_b = make_user(telegram_user_id=2)
    with InMemoryUnitOfWork(db) as uow:
        uow.users.add(user_a)
        uow.commit()
        uow.users.add(user_b)
        uow.rollback()  # discard user_b back to the last committed point
    with InMemoryUnitOfWork(db) as uow2:
        assert uow2.users.get(user_a.user_id) == user_a
        assert uow2.users.get(user_b.user_id) is None
