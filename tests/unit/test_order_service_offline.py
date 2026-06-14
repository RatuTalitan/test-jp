"""Unit tests for OrderService.offline_verify (task 17.1).

Covers the offline-payment approval edge added by task 17.1:

1. Flagged customer → APPROVED, stock decremented, OFFLINE_APPROVAL audit
   entry written (Req 17.5/17.8).
2. Non-flagged customer → Rejected(OFFLINE_PAYMENT_NOT_ALLOWED), state
   unchanged (still PAYMENT_PENDING), no audit entry (Req 17.7).
3. Stock conflict → Conflict(STOCK_CONFLICT), order stays PAYMENT_VERIFIED
   (not APPROVED), no audit entry (Req 7.4/17.8).
4. Non-Seller actor → NotAuthorized(NOT_AUTHORIZED_ACTOR), state unchanged
   (PAYMENT_PENDING), no audit entry.
5. Flagged customer who already has a UTR recorded → APPROVED, stock
   decremented, but NO audit entry (UTR present, so the offline-payment
   exception audit is suppressed per Req 17.8).

All tests use the in-memory UnitOfWork only (no I/O).

Requirements: 8.9, 17.4, 17.5, 17.7, 17.8.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketplace.domain import money
from marketplace.domain.entities import (
    AuditAction,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Product,
    Role,
    Unit,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import Conflict, NotAuthorized, NotFound, Rejected
from marketplace.order.service import OrderService
from marketplace.order.state_machine import (
    Actor,
    NOT_AUTHORIZED_ACTOR,
    OFFLINE_PAYMENT_NOT_ALLOWED,
    STOCK_CONFLICT,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def db() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def uow(db: InMemoryDatabase) -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork(db)


@pytest.fixture
def service(uow: InMemoryUnitOfWork) -> OrderService:
    return OrderService(uow)


def make_seller(uow: InMemoryUnitOfWork) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=1,
            role=Role.ADMIN,
            verified_contact="+910000000000",
            contact_verified_at=datetime(2024, 1, 1),
        )
    )


def make_customer(
    uow: InMemoryUnitOfWork,
    *,
    offline_payment_allowed: bool = False,
    telegram_id: int = 2,
) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=telegram_id,
            role=Role.CUSTOMER,
            verified_contact="+919999999999",
            contact_verified_at=datetime(2024, 1, 2),
            offline_payment_allowed=offline_payment_allowed,
        )
    )


def make_product(
    uow: InMemoryUnitOfWork,
    *,
    name: str = "Cotton Seed Oil Cake",
    price: str = "10.00",
    stock: str = "100",
    moq: str = "1",
) -> Product:
    return uow.catalog.add_product(
        Product(
            product_id=new_id(),
            name=name,
            category_id=new_id(),
            unit=Unit.KILOGRAM,
            price_per_unit=Decimal(price),
            min_order_quantity=Decimal(moq),
            stock_quantity=Decimal(stock),
        )
    )


def make_payment_pending_order(
    uow: InMemoryUnitOfWork,
    customer: User,
    product: Product,
    quantity: str = "5",
) -> Order:
    """Build and persist a PAYMENT_PENDING order with one line item."""
    qty = Decimal(quantity)
    unit_price = product.price_per_unit
    line = OrderItem(
        product_id=product.product_id,
        ordered_quantity=qty,
        unit_price=unit_price,
        line_amount=money.line_amount(qty, unit_price),
    )
    order = Order(
        order_id=new_id(),
        customer_id=customer.user_id,
        state=OrderState.PAYMENT_PENDING,
        total_amount=money.line_amount(qty, unit_price),
        items=[line],
        created_at=datetime(2024, 1, 3),
    )
    return uow.orders.add(order)


# --------------------------------------------------------------------------- #
# Test 1: flagged customer → APPROVED, stock decremented, audit written
# --------------------------------------------------------------------------- #


def test_offline_verify_flagged_customer_reaches_approved(uow, service):
    """Flagged customer's order transitions to APPROVED (Req 17.5)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product, quantity="10")

    result = service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    assert isinstance(result, Order), f"expected Order, got {result!r}"
    assert result.state is OrderState.APPROVED


def test_offline_verify_flagged_customer_stock_decremented(uow, service):
    """Approval decrements stock by exactly the ordered quantity (Req 7.3/17.5)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product, quantity="10")

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    stored_product = uow.catalog.get_product(product.product_id)
    assert stored_product.stock_quantity == Decimal("90")


def test_offline_verify_flagged_customer_audit_entry_written(uow, service):
    """Exactly one OFFLINE_APPROVAL audit entry is written when no UTR is present (Req 17.8)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product, quantity="10")

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    audit_entries = uow.audit.list_by_order(order.order_id)
    assert len(audit_entries) == 1
    entry = audit_entries[0]
    assert entry.action == AuditAction.OFFLINE_APPROVAL
    assert entry.acting_user_id == seller.user_id
    assert entry.detail["format_version"] == 1
    assert entry.detail["approved_without_utr"] is True
    assert entry.detail["order_id"] == str(order.order_id)
    assert entry.created_at is not None


# --------------------------------------------------------------------------- #
# Test 2: non-flagged customer → rejected, state unchanged, no audit
# --------------------------------------------------------------------------- #


def test_offline_verify_non_flagged_customer_rejected(uow, service):
    """Non-flagged customer's order is rejected with OFFLINE_PAYMENT_NOT_ALLOWED (Req 17.7)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=False)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product)

    result = service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    assert isinstance(result, Rejected)
    assert result.code == OFFLINE_PAYMENT_NOT_ALLOWED


def test_offline_verify_non_flagged_state_unchanged(uow, service):
    """Non-flagged rejection leaves the order in PAYMENT_PENDING (Req 17.7)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=False)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product)

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    stored_order = uow.orders.get(order.order_id)
    assert stored_order.state is OrderState.PAYMENT_PENDING


def test_offline_verify_non_flagged_no_audit(uow, service):
    """Non-flagged rejection writes no audit entry."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=False)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product)

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    assert uow.audit.list_by_order(order.order_id) == []


def test_offline_verify_non_flagged_stock_unchanged(uow, service):
    """Non-flagged rejection leaves stock unchanged."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=False)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product)

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    stored_product = uow.catalog.get_product(product.product_id)
    assert stored_product.stock_quantity == Decimal("100")


# --------------------------------------------------------------------------- #
# Test 3: stock conflict → PAYMENT_VERIFIED, no audit
# --------------------------------------------------------------------------- #


def test_offline_verify_stock_conflict_returns_conflict(uow, service):
    """A stock shortfall after OFFLINE_VERIFY returns STOCK_CONFLICT (Req 7.4)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="3")
    order = make_payment_pending_order(uow, customer, product, quantity="10")

    result = service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    assert isinstance(result, Conflict)
    assert result.code == STOCK_CONFLICT


def test_offline_verify_stock_conflict_order_stays_payment_verified(uow, service):
    """Order stays in PAYMENT_VERIFIED when approval is blocked by a stock conflict."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="3")
    order = make_payment_pending_order(uow, customer, product, quantity="10")

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    stored_order = uow.orders.get(order.order_id)
    assert stored_order.state is OrderState.PAYMENT_VERIFIED


def test_offline_verify_stock_conflict_no_audit(uow, service):
    """No OFFLINE_APPROVAL audit entry when the order never reaches APPROVED."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="3")
    order = make_payment_pending_order(uow, customer, product, quantity="10")

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    assert uow.audit.list_by_order(order.order_id) == []


def test_offline_verify_stock_conflict_no_stock_change(uow, service):
    """Stock is unchanged after a PAYMENT_VERIFIED stock conflict (Req 7.4)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="3")
    order = make_payment_pending_order(uow, customer, product, quantity="10")

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    stored_product = uow.catalog.get_product(product.product_id)
    assert stored_product.stock_quantity == Decimal("3")


# --------------------------------------------------------------------------- #
# Test 4: non-Seller actor → rejected, state unchanged
# --------------------------------------------------------------------------- #


def test_offline_verify_non_seller_rejected(uow, service):
    """A non-Seller actor receives NOT_AUTHORIZED_ACTOR."""
    make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product)

    result = service.offline_verify(order.order_id, Actor.customer(customer.user_id))

    assert isinstance(result, NotAuthorized)
    assert result.code == NOT_AUTHORIZED_ACTOR


def test_offline_verify_non_seller_state_unchanged(uow, service):
    """Non-Seller actor leaves order in PAYMENT_PENDING."""
    make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product)

    service.offline_verify(order.order_id, Actor.customer(customer.user_id))

    stored_order = uow.orders.get(order.order_id)
    assert stored_order.state is OrderState.PAYMENT_PENDING


def test_offline_verify_non_seller_no_audit(uow, service):
    """Non-Seller actor writes no audit entry."""
    make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product)

    service.offline_verify(order.order_id, Actor.customer(customer.user_id))

    assert uow.audit.list_by_order(order.order_id) == []


# --------------------------------------------------------------------------- #
# Test 5: flagged customer with existing UTR → APPROVED, no audit entry
# --------------------------------------------------------------------------- #


def test_offline_verify_flagged_with_utr_reaches_approved(uow, service):
    """Offline_verify still works for a flagged customer who optionally has a UTR (Req 17.6)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product, quantity="5")

    # Simulate a flagged customer who optionally submitted a UTR (Req 17.6);
    # a payment record with a UTR is stored against the order.
    payment = Payment(
        payment_id=new_id(),
        order_id=order.order_id,
        utr="123456789012",
        utr_submitted_at=datetime(2024, 1, 4),
    )
    uow.payments.add(payment)

    result = service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    assert isinstance(result, Order)
    assert result.state is OrderState.APPROVED


def test_offline_verify_flagged_with_utr_stock_decremented(uow, service):
    """Stock is still decremented when offline_verify is used with an existing UTR."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product, quantity="5")

    payment = Payment(
        payment_id=new_id(),
        order_id=order.order_id,
        utr="123456789012",
        utr_submitted_at=datetime(2024, 1, 4),
    )
    uow.payments.add(payment)

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    stored_product = uow.catalog.get_product(product.product_id)
    assert stored_product.stock_quantity == Decimal("95")


def test_offline_verify_flagged_with_utr_no_audit_entry(uow, service):
    """No OFFLINE_APPROVAL audit when a UTR is already recorded (Req 17.8)."""
    seller = make_seller(uow)
    customer = make_customer(uow, offline_payment_allowed=True)
    product = make_product(uow, stock="100")
    order = make_payment_pending_order(uow, customer, product, quantity="5")

    payment = Payment(
        payment_id=new_id(),
        order_id=order.order_id,
        utr="123456789012",
        utr_submitted_at=datetime(2024, 1, 4),
    )
    uow.payments.add(payment)

    service.offline_verify(order.order_id, Actor.seller(seller.user_id))

    # UTR exists → no offline-exception audit entry (Req 17.8).
    audit_entries = uow.audit.list_by_order(order.order_id)
    assert audit_entries == []
