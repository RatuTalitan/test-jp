"""Unit tests for the OrderService verify/approve cascade and fulfillment edges.

Covers tasks 12.1 and 12.2 against the in-memory Unit-of-Work:

* ``verify_payment`` cascades PAYMENT_SUBMITTED -> PAYMENT_VERIFIED -> APPROVED,
  decrementing each line's product stock by **exactly** the ordered quantity and
  enqueueing the Customer "approved" notification (Req 7.2/7.3/7.8/16.5).
* Approval is **blocked** when a line exceeds current stock: the order stays in
  PAYMENT_VERIFIED, no stock changes, the Seller is notified of the affected
  lines + available stock, and a Conflict is returned (Req 7.4).
* ``reject_payment`` requires a 1-500 char reason, records it, moves to REJECTED,
  and notifies the Customer; an invalid/missing reason leaves state unchanged
  (Req 7.5/7.6/7.9).
* ``mark_ready`` (APPROVED -> READY_FOR_PICKUP) includes the configured pickup
  location + line items in the Customer notification (Req 8.1/8.2);
  ``mark_collected`` (READY_FOR_PICKUP -> COMPLETED) notifies completion
  (Req 8.3/8.6).
* Wrong-state attempts return typed rejections leaving state unchanged
  (Req 7.7/8.4/8.5).

Requirements: 7.2, 7.3, 7.4, 7.5, 7.6, 7.8, 7.9, 8.1, 8.2, 8.3, 8.6, 16.5.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketplace.domain.entities import (
    Order,
    OrderItem,
    OrderState,
    NotificationKind,
    Product,
    Role,
    SellerSettings,
    Unit,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import Conflict, NotAuthorized, NotFound, Rejected
from marketplace.order.service import STATE_TRANSITION_SEQ, OrderService
from marketplace.order.state_machine import (
    REJECTION_REASON_REQUIRED,
    STOCK_CONFLICT,
    Actor,
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


def make_customer(uow: InMemoryUnitOfWork) -> User:
    return uow.users.add(
        User(
            user_id=new_id(),
            telegram_user_id=2,
            role=Role.CUSTOMER,
            verified_contact="+919999999999",
            contact_verified_at=datetime(2024, 1, 2),
        )
    )


def make_product(uow: InMemoryUnitOfWork, *, name="P", price="10.00", stock="100") -> Product:
    return uow.catalog.add_product(
        Product(
            product_id=new_id(),
            name=name,
            category_id=new_id(),
            unit=Unit.KILOGRAM,
            price_per_unit=Decimal(price),
            min_order_quantity=Decimal("1"),
            stock_quantity=Decimal(stock),
        )
    )


def make_order(
    uow: InMemoryUnitOfWork,
    customer_id,
    lines: list[tuple],
    *,
    state: OrderState = OrderState.PAYMENT_SUBMITTED,
) -> Order:
    """Persist an order in ``state`` with ``lines`` of (product, qty) tuples."""
    items = [
        OrderItem(
            product_id=p.product_id,
            ordered_quantity=Decimal(q),
            unit_price=p.price_per_unit,
            line_amount=(Decimal(q) * p.price_per_unit).quantize(Decimal("0.01")),
        )
        for p, q in lines
    ]
    total = sum((it.line_amount for it in items), Decimal("0.00"))
    return uow.orders.add(
        Order(
            order_id=new_id(),
            customer_id=customer_id,
            state=state,
            total_amount=total,
            items=items,
            created_at=datetime(2024, 1, 3),
        )
    )


# --------------------------------------------------------------------------- #
# Task 12.1 - verify -> approve cascade with atomic stock decrement
# --------------------------------------------------------------------------- #
def test_verify_payment_approves_and_decrements_each_line_exactly(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, name="A", stock="100")
    p2 = make_product(uow, name="B", stock="40")
    order = make_order(uow, customer.user_id, [(p1, "10"), (p2, "5")])

    result = service.verify_payment(order.order_id, Actor.seller())

    assert isinstance(result, Order)
    assert result.state is OrderState.APPROVED
    # Stock decremented by exactly the ordered quantities (Req 7.3).
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("90")
    assert uow.catalog.get_product(p2.product_id).stock_quantity == Decimal("35")


def test_verify_payment_notifies_customer_approved(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, stock="100")
    order = make_order(uow, customer.user_id, [(p1, "10")])

    service.verify_payment(order.order_id, Actor.seller())

    notif = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        STATE_TRANSITION_SEQ[OrderState.APPROVED],
    )
    assert notif is not None
    assert notif.recipient_id == customer.user_id
    assert notif.payload["new_state"] == OrderState.APPROVED.value


def test_approval_allows_quantity_exactly_equal_to_stock(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, stock="10")
    order = make_order(uow, customer.user_id, [(p1, "10")])

    result = service.verify_payment(order.order_id, Actor.seller())

    assert isinstance(result, Order)
    assert result.state is OrderState.APPROVED
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("0")


def test_approval_blocked_when_a_line_exceeds_stock(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    # p1 fits, p2 exceeds: nothing should change.
    p1 = make_product(uow, name="A", stock="100")
    p2 = make_product(uow, name="B", stock="3")
    order = make_order(uow, customer.user_id, [(p1, "10"), (p2, "5")])

    result = service.verify_payment(order.order_id, Actor.seller())

    # Conflict returned with affected line + available stock (Req 7.4).
    assert isinstance(result, Conflict)
    assert result.code == STOCK_CONFLICT
    affected = {ln["product_id"]: ln for ln in result.details["lines"]}
    assert set(affected) == {p2.product_id}
    assert affected[p2.product_id]["available_stock"] == Decimal("3")
    assert affected[p2.product_id]["ordered_quantity"] == Decimal("5")

    # Order left in PAYMENT_VERIFIED (verify succeeded, approval blocked).
    persisted = uow.orders.get(order.order_id)
    assert persisted.state is OrderState.PAYMENT_VERIFIED
    # No stock changed for any line (Req 7.4).
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("100")
    assert uow.catalog.get_product(p2.product_id).stock_quantity == Decimal("3")


def test_approval_conflict_notifies_seller_with_affected_lines(uow, service):
    seller = make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, name="A", stock="1")
    order = make_order(uow, customer.user_id, [(p1, "5")])

    service.verify_payment(order.order_id, Actor.seller())

    notif = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        STATE_TRANSITION_SEQ[OrderState.PAYMENT_VERIFIED],
    )
    assert notif is not None
    assert notif.recipient_id == seller.user_id
    assert notif.payload["stock_conflict"] is True
    lines = notif.payload["lines"]
    assert lines[0]["product_id"] == str(p1.product_id)
    assert lines[0]["available_stock"] == "1"


def test_restock_then_reapprove_succeeds(uow, service):
    """After a stock conflict the Seller can restock and approve from VERIFIED."""
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, stock="2")
    order = make_order(uow, customer.user_id, [(p1, "5")])

    blocked = service.verify_payment(order.order_id, Actor.seller())
    assert isinstance(blocked, Conflict)

    # Seller restocks; re-attempt approval from PAYMENT_VERIFIED.
    product = uow.catalog.get_product(p1.product_id)
    product.stock_quantity = Decimal("10")
    uow.catalog.update_product(product)

    result = service.approve(order.order_id, Actor.seller())
    assert isinstance(result, Order)
    assert result.state is OrderState.APPROVED
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("5")


def test_verify_payment_wrong_state_leaves_unchanged(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, stock="100")
    # Order is in PAYMENT_PENDING, not PAYMENT_SUBMITTED (Req 7.7).
    order = make_order(
        uow, customer.user_id, [(p1, "10")], state=OrderState.PAYMENT_PENDING
    )

    result = service.verify_payment(order.order_id, Actor.seller())

    assert isinstance(result, Rejected)
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_PENDING
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("100")


def test_verify_payment_not_found(uow, service):
    make_seller(uow)
    result = service.verify_payment(new_id(), Actor.seller())
    assert isinstance(result, NotFound)


def test_approve_from_wrong_state_does_not_decrement(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, stock="100")
    order = make_order(
        uow, customer.user_id, [(p1, "10")], state=OrderState.PAYMENT_SUBMITTED
    )

    # approve() requires PAYMENT_VERIFIED; from PAYMENT_SUBMITTED it is illegal.
    result = service.approve(order.order_id, Actor.seller())

    assert isinstance(result, Rejected)
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_SUBMITTED
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("100")


def test_verify_payment_rejects_non_seller_actor(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, stock="100")
    order = make_order(uow, customer.user_id, [(p1, "10")])

    result = service.verify_payment(order.order_id, Actor.customer(customer.user_id))

    assert isinstance(result, NotAuthorized)
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_SUBMITTED
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("100")


# --------------------------------------------------------------------------- #
# Task 12.2 - payment rejection
# --------------------------------------------------------------------------- #
def test_reject_payment_with_valid_reason(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    order = make_order(uow, customer.user_id, [(p1, "10")])

    result = service.reject_payment(
        order.order_id, Actor.seller(), "payment not received"
    )

    assert isinstance(result, Order)
    assert result.state is OrderState.REJECTED
    assert result.rejection_reason == "payment not received"
    # Customer notified of rejection + reason (Req 7.9).
    notif = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        STATE_TRANSITION_SEQ[OrderState.REJECTED],
    )
    assert notif is not None
    assert notif.recipient_id == customer.user_id
    assert notif.payload["rejection_reason"] == "payment not received"


@pytest.mark.parametrize("reason", ["", "x" * 501])
def test_reject_payment_invalid_reason_leaves_unchanged(uow, service, reason):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    order = make_order(uow, customer.user_id, [(p1, "10")])

    result = service.reject_payment(order.order_id, Actor.seller(), reason)

    assert isinstance(result, Rejected)
    assert result.code == REJECTION_REASON_REQUIRED
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_SUBMITTED


def test_reject_payment_boundary_reasons_accepted(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    o1 = make_order(uow, customer.user_id, [(p1, "1")])
    o2 = make_order(uow, customer.user_id, [(p1, "1")])

    one_char = service.reject_payment(o1.order_id, Actor.seller(), "x")
    max_char = service.reject_payment(o2.order_id, Actor.seller(), "y" * 500)

    assert isinstance(one_char, Order) and one_char.state is OrderState.REJECTED
    assert isinstance(max_char, Order) and max_char.state is OrderState.REJECTED


def test_reject_payment_wrong_state_leaves_unchanged(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    order = make_order(
        uow, customer.user_id, [(p1, "1")], state=OrderState.PAYMENT_PENDING
    )

    result = service.reject_payment(order.order_id, Actor.seller(), "valid reason")

    assert isinstance(result, Rejected)
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_PENDING


# --------------------------------------------------------------------------- #
# Task 12.2 - mark_ready / mark_collected
# --------------------------------------------------------------------------- #
def test_mark_ready_includes_pickup_location_and_items(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    uow.seller_settings.upsert(SellerSettings(pickup_location="Shop 5, Market Rd"))
    p1 = make_product(uow, name="A")
    order = make_order(
        uow, customer.user_id, [(p1, "7")], state=OrderState.APPROVED
    )

    result = service.mark_ready(order.order_id, Actor.seller())

    assert isinstance(result, Order)
    assert result.state is OrderState.READY_FOR_PICKUP
    notif = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        STATE_TRANSITION_SEQ[OrderState.READY_FOR_PICKUP],
    )
    assert notif is not None
    assert notif.recipient_id == customer.user_id
    assert notif.payload["pickup_location"] == "Shop 5, Market Rd"
    assert notif.payload["items"][0]["product_id"] == str(p1.product_id)
    assert notif.payload["items"][0]["ordered_quantity"] == "7"


def test_mark_ready_without_pickup_location_still_transitions(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    order = make_order(
        uow, customer.user_id, [(p1, "1")], state=OrderState.APPROVED
    )

    result = service.mark_ready(order.order_id, Actor.seller())

    assert isinstance(result, Order)
    assert result.state is OrderState.READY_FOR_PICKUP
    notif = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        STATE_TRANSITION_SEQ[OrderState.READY_FOR_PICKUP],
    )
    assert notif.payload["pickup_location"] is None


def test_mark_ready_wrong_state_leaves_unchanged(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    order = make_order(
        uow, customer.user_id, [(p1, "1")], state=OrderState.PAYMENT_VERIFIED
    )

    result = service.mark_ready(order.order_id, Actor.seller())

    assert isinstance(result, Rejected)
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_VERIFIED


def test_mark_collected_completes_and_notifies(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    order = make_order(
        uow, customer.user_id, [(p1, "1")], state=OrderState.READY_FOR_PICKUP
    )

    result = service.mark_collected(order.order_id, Actor.seller())

    assert isinstance(result, Order)
    assert result.state is OrderState.COMPLETED
    notif = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        STATE_TRANSITION_SEQ[OrderState.COMPLETED],
    )
    assert notif is not None
    assert notif.recipient_id == customer.user_id
    assert notif.payload["new_state"] == OrderState.COMPLETED.value


def test_mark_collected_wrong_state_leaves_unchanged(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    order = make_order(
        uow, customer.user_id, [(p1, "1")], state=OrderState.APPROVED
    )

    result = service.mark_collected(order.order_id, Actor.seller())

    assert isinstance(result, Rejected)
    assert uow.orders.get(order.order_id).state is OrderState.APPROVED


def test_full_fulfillment_happy_path(uow, service):
    """Submitted -> verified -> approved -> ready -> completed end to end."""
    make_seller(uow)
    customer = make_customer(uow)
    uow.seller_settings.upsert(SellerSettings(pickup_location="Depot"))
    p1 = make_product(uow, stock="20")
    order = make_order(uow, customer.user_id, [(p1, "8")])

    approved = service.verify_payment(order.order_id, Actor.seller())
    assert approved.state is OrderState.APPROVED
    assert uow.catalog.get_product(p1.product_id).stock_quantity == Decimal("12")

    ready = service.mark_ready(order.order_id, Actor.seller())
    assert ready.state is OrderState.READY_FOR_PICKUP

    completed = service.mark_collected(order.order_id, Actor.seller())
    assert completed.state is OrderState.COMPLETED
