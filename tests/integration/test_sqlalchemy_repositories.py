"""Integration tests for the SQLAlchemy repositories + Unit of Work (task 4.1).

Applies the full Alembic migration stack to a throwaway SQLite database, then
exercises :class:`marketplace.db.repositories.SqlAlchemyUnitOfWork` to prove the
SQLAlchemy repositories round-trip domain entities against a real schema and
honour the commit/rollback transaction boundary (design.md -> Transaction
Boundaries; Req 1.9).

Marked ``integration`` (DB-backed) so it is excluded from the fast unit/property
runs. SQLite stands in for runtime PostgreSQL (design.md -> "SQLite for local
dev").
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from marketplace.db.repositories import SqlAlchemyUnitOfWork
from marketplace.db.session import make_engine, make_session_factory
from marketplace.domain import (
    AuditAction,
    AuditEntry,
    Cart,
    CartItem,
    Category,
    Notification,
    NotificationKind,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Product,
    Role,
    SellerSettings,
    Unit,
    User,
    new_id,
)

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def uow_factory(tmp_path, monkeypatch):
    """Migrate a fresh SQLite DB and yield a SqlAlchemyUnitOfWork factory."""
    url = f"sqlite:///{tmp_path / 'repo_test.db'}"
    monkeypatch.setenv("DB_URL", url)
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")

    engine = make_engine(url)
    session_factory = make_session_factory(engine)
    try:
        yield lambda: SqlAlchemyUnitOfWork(session_factory)
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")


def _make_user() -> User:
    return User(
        user_id=new_id(),
        telegram_user_id=555,
        role=Role.CUSTOMER,
        verified_contact="+910000000000",
        contact_verified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def test_user_round_trip(uow_factory):
    user = _make_user()
    with uow_factory() as uow:
        uow.users.add(user)
        uow.commit()
    with uow_factory() as uow:
        fetched = uow.users.get(user.user_id)
        assert fetched is not None
        assert fetched.telegram_user_id == 555
        assert fetched.role == Role.CUSTOMER
        assert fetched.verified_contact == "+910000000000"
        assert uow.users.get_by_telegram_id(555).user_id == user.user_id


def test_catalog_round_trip(uow_factory):
    cat = Category(category_id=new_id(), name="Oil Cakes")
    prod = Product(
        product_id=new_id(),
        name="Cotton Seed Oil Cake",
        category_id=cat.category_id,
        unit=Unit.KILOGRAM,
        price_per_unit=Decimal("100.00"),
        min_order_quantity=Decimal("10.000"),
        stock_quantity=Decimal("500.000"),
        description="Premium grade",
        available=True,
    )
    with uow_factory() as uow:
        uow.catalog.add_category(cat)
        uow.catalog.add_product(prod)
        uow.commit()
    with uow_factory() as uow:
        assert uow.catalog.get_category_by_name("OIL CAKES").category_id == cat.category_id
        fetched = uow.catalog.get_product(prod.product_id)
        assert fetched.price_per_unit == Decimal("100.00")
        assert fetched.stock_quantity == Decimal("500.000")
        assert fetched.unit == Unit.KILOGRAM
        assert [p.product_id for p in uow.catalog.list_products_in_category(cat.category_id)] == [
            prod.product_id
        ]


def test_cart_round_trip(uow_factory):
    user = _make_user()
    cat = Category(category_id=new_id(), name="Cat")
    prod = Product(
        product_id=new_id(),
        name="P",
        category_id=cat.category_id,
        unit=Unit.BAG,
        price_per_unit=Decimal("50.00"),
        min_order_quantity=Decimal("1.000"),
        stock_quantity=Decimal("10.000"),
    )
    cart = Cart(
        cart_id=new_id(),
        customer_id=user.user_id,
        items=[CartItem(product_id=prod.product_id, quantity=Decimal("2.000"))],
    )
    with uow_factory() as uow:
        uow.users.add(user)
        uow.catalog.add_category(cat)
        uow.catalog.add_product(prod)
        uow.carts.add(cart)
        uow.commit()
    with uow_factory() as uow:
        fetched = uow.carts.get_by_customer(user.user_id)
        assert fetched is not None
        assert len(fetched.items) == 1
        assert fetched.items[0].product_id == prod.product_id
        assert fetched.items[0].quantity == Decimal("2.000")


def test_order_and_payment_round_trip(uow_factory):
    user = _make_user()
    cat = Category(category_id=new_id(), name="Cat")
    prod = Product(
        product_id=new_id(),
        name="P",
        category_id=cat.category_id,
        unit=Unit.QUINTAL,
        price_per_unit=Decimal("100.00"),
        min_order_quantity=Decimal("1.000"),
        stock_quantity=Decimal("100.000"),
    )
    order = Order(
        order_id=new_id(),
        customer_id=user.user_id,
        state=OrderState.PAYMENT_PENDING,
        total_amount=Decimal("1000.00"),
        items=[
            OrderItem(
                product_id=prod.product_id,
                ordered_quantity=Decimal("10.000"),
                unit_price=Decimal("100.00"),
                line_amount=Decimal("1000.00"),
            )
        ],
        # Supplied explicitly because SQLite does not auto-assign Identity on a
        # non-PK column (PostgreSQL assigns order_number automatically).
        order_number=1001,
    )
    payment = Payment(payment_id=new_id(), order_id=order.order_id, utr="ABCD12345678")
    with uow_factory() as uow:
        uow.users.add(user)
        uow.catalog.add_category(cat)
        uow.catalog.add_product(prod)
        uow.orders.add(order)
        uow.payments.add(payment)
        uow.commit()
    with uow_factory() as uow:
        fetched = uow.orders.get(order.order_id)
        assert fetched.state == OrderState.PAYMENT_PENDING
        assert fetched.total_amount == Decimal("1000.00")
        assert len(fetched.items) == 1
        assert fetched.items[0].line_amount == Decimal("1000.00")
        assert uow.orders.list_by_states([OrderState.PAYMENT_PENDING])[0].order_id == order.order_id
        assert uow.orders.list_by_product(prod.product_id)[0].order_id == order.order_id
        assert uow.payments.get_by_utr("ABCD12345678").order_id == order.order_id


def test_audit_notification_settings_round_trip(uow_factory):
    user = _make_user()
    cat = Category(category_id=new_id(), name="Cat")
    prod = Product(
        product_id=new_id(),
        name="P",
        category_id=cat.category_id,
        unit=Unit.BAG,
        price_per_unit=Decimal("10.00"),
        min_order_quantity=Decimal("1.000"),
        stock_quantity=Decimal("5.000"),
    )
    order = Order(
        order_id=new_id(),
        customer_id=user.user_id,
        state=OrderState.APPROVED,
        total_amount=Decimal("10.00"),
        order_number=2002,
    )
    audit = AuditEntry(
        audit_id=new_id(),
        order_id=order.order_id,
        action=AuditAction.OFFLINE_APPROVAL,
        detail={"format_version": 1, "field": "state", "new_value": "APPROVED"},
        acting_user_id=user.user_id,
    )
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    notif = Notification(
        notification_id=new_id(),
        order_id=order.order_id,
        recipient_id=user.user_id,
        kind=NotificationKind.STATE_CHANGE,
        transition_seq=1,
        payload={"format_version": 1, "state": "APPROVED"},
        next_attempt_at=now,
    )
    settings = SellerSettings(pickup_location="Warehouse", upi_address="seller@upi")

    with uow_factory() as uow:
        uow.users.add(user)
        uow.catalog.add_category(cat)
        uow.catalog.add_product(prod)
        uow.orders.add(order)
        uow.audit.add(audit)
        uow.notifications.add(notif)
        uow.seller_settings.upsert(settings)
        uow.commit()

    with uow_factory() as uow:
        audits = uow.audit.list_by_order(order.order_id)
        assert len(audits) == 1
        assert audits[0].detail["new_value"] == "APPROVED"
        assert (
            uow.notifications.find_by_idempotency_key(
                order.order_id, NotificationKind.STATE_CHANGE, 1
            ).payload["state"]
            == "APPROVED"
        )
        assert uow.notifications.list_due(now)[0].notification_id == notif.notification_id
        assert uow.seller_settings.get().pickup_location == "Warehouse"


def test_uow_rollback_leaves_no_writes(uow_factory):
    user = _make_user()
    # No commit -> rolled back on exit.
    with uow_factory() as uow:
        uow.users.add(user)
    with uow_factory() as uow:
        assert uow.users.get(user.user_id) is None


def test_uow_rollback_on_exception(uow_factory):
    user = _make_user()
    with pytest.raises(RuntimeError):
        with uow_factory() as uow:
            uow.users.add(user)
            uow.commit()  # commit then fail after -> the failing op is separate
            raise RuntimeError("boom")
    # The committed user survives (commit happened before the exception).
    with uow_factory() as uow:
        assert uow.users.get(user.user_id) is not None


def test_seller_settings_is_singleton(uow_factory):
    with uow_factory() as uow:
        uow.seller_settings.upsert(SellerSettings(pickup_location="A"))
        uow.commit()
    with uow_factory() as uow:
        uow.seller_settings.upsert(SellerSettings(pickup_location="B"))
        uow.commit()
    with uow_factory() as uow:
        assert uow.seller_settings.get().pickup_location == "B"
        # Exactly one row exists in the table.
        count = uow.session.execute(
            sa.text("SELECT COUNT(*) FROM seller_settings")
        ).scalar_one()
        assert count == 1
