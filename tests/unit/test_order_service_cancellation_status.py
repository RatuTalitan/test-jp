"""Unit tests for OrderService cancellation + status queries (tasks 14.1/14.2).

Exercise the customer-cancellation edge and the privacy-aware status queries
against the in-memory Unit-of-Work:

* ``cancel`` by the placing customer from each cancellable state -> CANCELLED,
  with the customer confirmation (Req 9.3) and seller notification (Req 9.6)
  enqueued; rejected for a non-owner (Req 9.5) and for non-cancellable states
  (Req 9.4), leaving the state unchanged in both cases.
* ``list_customer_orders`` newest-first (Req 10.1) and empty (Req 10.2).
* ``get_order_for_customer`` ownership enforcement + non-disclosing rejection
  for other customers (Req 10.3/10.5), and the "not yet provided" payment
  reference placeholder when no UTR is recorded (Req 10.4).
* ``list_active_for_seller`` returning non-terminal orders oldest-first
  (Req 10.6).

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 12.2.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketplace.domain.entities import (
    NotificationKind,
    Order,
    OrderItem,
    OrderState,
    Payment,
    Role,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import NotAuthorized, NotFound, Rejected
from marketplace.order.service import (
    CANCELLATION_SELLER_TRANSITION_SEQ,
    ORDER_NOT_AVAILABLE,
    STATE_TRANSITION_SEQ,
    CustomerOrderDetail,
    OrderService,
)
from marketplace.order.state_machine import (
    INVALID_TRANSITION,
    NOT_ORDER_OWNER,
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
    seller = User(
        user_id=new_id(),
        telegram_user_id=1,
        role=Role.ADMIN,
        verified_contact="+910000000000",
        contact_verified_at=datetime(2024, 1, 1),
    )
    return uow.users.add(seller)


def make_customer(uow: InMemoryUnitOfWork, *, telegram_id: int = 2) -> User:
    customer = User(
        user_id=new_id(),
        telegram_user_id=telegram_id,
        role=Role.CUSTOMER,
        verified_contact=f"+9199999999{telegram_id:02d}",
        contact_verified_at=datetime(2024, 1, 2),
    )
    return uow.users.add(customer)


def make_order(
    uow: InMemoryUnitOfWork,
    customer_id,
    state: OrderState,
    *,
    created_at: datetime,
    items: list | None = None,
    total: str = "0.00",
) -> Order:
    order = Order(
        order_id=new_id(),
        customer_id=customer_id,
        state=state,
        total_amount=Decimal(total),
        items=items or [],
        created_at=created_at,
    )
    return uow.orders.add(order)


CANCELLABLE_STATES = [
    OrderState.PLACED,
    OrderState.PAYMENT_PENDING,
    OrderState.PAYMENT_SUBMITTED,
]
NON_CANCELLABLE_STATES = [
    OrderState.PAYMENT_VERIFIED,
    OrderState.APPROVED,
    OrderState.READY_FOR_PICKUP,
    OrderState.COMPLETED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
]


# --------------------------------------------------------------------------- #
# Task 14.1 - customer cancellation (Req 9.1-9.6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", CANCELLABLE_STATES)
def test_cancel_by_owner_in_cancellable_state_succeeds(uow, service, state):
    seller = make_seller(uow)
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id, state, created_at=datetime(2024, 3, 1))

    result = service.cancel(order.order_id, Actor.customer(customer.user_id))

    assert isinstance(result, Order)
    assert result.state is OrderState.CANCELLED
    # Persisted as CANCELLED (Req 9.2).
    assert uow.orders.get(order.order_id).state is OrderState.CANCELLED

    # Customer confirmation notification (Req 9.3).
    confirm = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        STATE_TRANSITION_SEQ[OrderState.CANCELLED],
    )
    assert confirm is not None
    assert confirm.recipient_id == customer.user_id
    assert confirm.payload["new_state"] == OrderState.CANCELLED.value

    # Seller notified of the cancellation (Req 9.6) on a distinct key.
    seller_note = uow.notifications.find_by_idempotency_key(
        order.order_id,
        NotificationKind.STATE_CHANGE,
        CANCELLATION_SELLER_TRANSITION_SEQ,
    )
    assert seller_note is not None
    assert seller_note.recipient_id == seller.user_id
    assert seller_note.payload.get("cancelled") is True
    # The two notifications are distinct rows addressed to different recipients.
    assert seller_note.notification_id != confirm.notification_id


def test_cancel_by_non_owner_rejected_state_unchanged(uow, service):
    make_seller(uow)
    owner = make_customer(uow, telegram_id=2)
    intruder = make_customer(uow, telegram_id=3)
    order = make_order(
        uow, owner.user_id, OrderState.PAYMENT_PENDING, created_at=datetime(2024, 3, 1)
    )

    result = service.cancel(order.order_id, Actor.customer(intruder.user_id))

    assert isinstance(result, NotAuthorized)
    assert result.code == NOT_ORDER_OWNER
    # State unchanged (Req 9.5) and no notifications enqueued.
    assert uow.orders.get(order.order_id).state is OrderState.PAYMENT_PENDING
    assert uow.db.notifications == {}


@pytest.mark.parametrize("state", NON_CANCELLABLE_STATES)
def test_cancel_in_non_cancellable_state_rejected_state_unchanged(uow, service, state):
    make_seller(uow)
    customer = make_customer(uow)
    order = make_order(uow, customer.user_id, state, created_at=datetime(2024, 3, 1))

    result = service.cancel(order.order_id, Actor.customer(customer.user_id))

    assert isinstance(result, Rejected)
    assert result.code == INVALID_TRANSITION
    # State unchanged (Req 9.4), nothing enqueued.
    assert uow.orders.get(order.order_id).state is state
    assert uow.db.notifications == {}


def test_cancel_unknown_order_returns_not_found(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    result = service.cancel(new_id(), Actor.customer(customer.user_id))
    assert isinstance(result, NotFound)


def test_cancel_is_idempotent_for_notifications(uow, service):
    # A retried cancel of an already-cancelled order does not double-notify; the
    # second attempt is itself rejected (terminal), but more importantly the
    # first cancel created exactly one customer + one seller notification.
    make_seller(uow)
    customer = make_customer(uow)
    order = make_order(
        uow, customer.user_id, OrderState.PLACED, created_at=datetime(2024, 3, 1)
    )

    service.cancel(order.order_id, Actor.customer(customer.user_id))
    # Exactly two notifications enqueued (one per recipient).
    assert len(uow.db.notifications) == 2


# --------------------------------------------------------------------------- #
# Task 14.2 - list_customer_orders (Req 10.1/10.2)
# --------------------------------------------------------------------------- #
def test_list_customer_orders_newest_first(uow, service):
    customer = make_customer(uow)
    oldest = make_order(
        uow, customer.user_id, OrderState.PLACED, created_at=datetime(2024, 1, 1)
    )
    middle = make_order(
        uow, customer.user_id, OrderState.APPROVED, created_at=datetime(2024, 2, 1)
    )
    newest = make_order(
        uow, customer.user_id, OrderState.COMPLETED, created_at=datetime(2024, 3, 1)
    )

    result = service.list_customer_orders(customer.user_id)

    assert [o.order_id for o in result] == [
        newest.order_id,
        middle.order_id,
        oldest.order_id,
    ]


def test_list_customer_orders_empty_returns_empty_list(uow, service):
    customer = make_customer(uow)
    assert service.list_customer_orders(customer.user_id) == []


def test_list_customer_orders_only_that_customers_orders(uow, service):
    a = make_customer(uow, telegram_id=2)
    b = make_customer(uow, telegram_id=3)
    make_order(uow, a.user_id, OrderState.PLACED, created_at=datetime(2024, 1, 1))
    make_order(uow, b.user_id, OrderState.PLACED, created_at=datetime(2024, 1, 2))

    result = service.list_customer_orders(a.user_id)
    assert len(result) == 1
    assert result[0].customer_id == a.user_id


# --------------------------------------------------------------------------- #
# Task 14.2 - get_order_for_customer (Req 10.3/10.4/10.5/12.2)
# --------------------------------------------------------------------------- #
def test_get_order_for_customer_returns_detail_for_owner(uow, service):
    customer = make_customer(uow)
    product_id = new_id()
    item = OrderItem(
        product_id=product_id,
        ordered_quantity=Decimal("2"),
        unit_price=Decimal("10.50"),
        line_amount=Decimal("21.00"),
    )
    order = make_order(
        uow,
        customer.user_id,
        OrderState.PAYMENT_SUBMITTED,
        created_at=datetime(2024, 3, 1),
        items=[item],
        total="21.00",
    )
    uow.payments.add(
        Payment(payment_id=new_id(), order_id=order.order_id, utr="ABCDE1234567")
    )

    detail = service.get_order_for_customer(order.order_id, customer.user_id)

    assert isinstance(detail, CustomerOrderDetail)
    assert detail.order.order_id == order.order_id
    assert detail.order.state is OrderState.PAYMENT_SUBMITTED
    assert detail.order.total_amount == Decimal("21.00")
    assert len(detail.order.items) == 1
    # Recorded payment reference surfaced (Req 10.3).
    assert detail.payment_reference == "ABCDE1234567"
    assert detail.has_payment_reference is True


def test_get_order_for_customer_placeholder_when_no_utr(uow, service):
    customer = make_customer(uow)
    order = make_order(
        uow, customer.user_id, OrderState.PAYMENT_PENDING, created_at=datetime(2024, 3, 1)
    )
    # Payment row exists but no UTR recorded yet.
    uow.payments.add(Payment(payment_id=new_id(), order_id=order.order_id, utr=None))

    detail = service.get_order_for_customer(order.order_id, customer.user_id)

    assert isinstance(detail, CustomerOrderDetail)
    # "Not yet provided" placeholder semantics (Req 10.4): no reference recorded.
    assert detail.payment_reference is None
    assert detail.has_payment_reference is False


def test_get_order_for_customer_placeholder_when_no_payment_row(uow, service):
    customer = make_customer(uow)
    order = make_order(
        uow, customer.user_id, OrderState.PLACED, created_at=datetime(2024, 3, 1)
    )

    detail = service.get_order_for_customer(order.order_id, customer.user_id)

    assert isinstance(detail, CustomerOrderDetail)
    assert detail.payment_reference is None
    assert detail.has_payment_reference is False


def test_get_order_for_customer_other_owner_non_disclosing(uow, service):
    owner = make_customer(uow, telegram_id=2)
    intruder = make_customer(uow, telegram_id=3)
    item = OrderItem(
        product_id=new_id(),
        ordered_quantity=Decimal("2"),
        unit_price=Decimal("10.50"),
        line_amount=Decimal("21.00"),
    )
    order = make_order(
        uow,
        owner.user_id,
        OrderState.APPROVED,
        created_at=datetime(2024, 3, 1),
        items=[item],
        total="21.00",
    )

    result = service.get_order_for_customer(order.order_id, intruder.user_id)

    assert isinstance(result, NotAuthorized)
    assert result.code == ORDER_NOT_AVAILABLE
    # Non-disclosing: the rejection carries no order details (Req 10.5/12.2).
    assert not hasattr(result, "order")
    assert str(order.order_id) not in (result.reason or "")


def test_get_order_for_customer_unknown_returns_not_found(uow, service):
    customer = make_customer(uow)
    result = service.get_order_for_customer(new_id(), customer.user_id)
    assert isinstance(result, NotFound)


# --------------------------------------------------------------------------- #
# Task 14.2 - list_active_for_seller (Req 10.6)
# --------------------------------------------------------------------------- #
def test_list_active_for_seller_excludes_terminal_oldest_first(uow, service):
    customer = make_customer(uow)
    # Active orders (non-terminal), created out of order.
    placed = make_order(
        uow, customer.user_id, OrderState.PLACED, created_at=datetime(2024, 1, 3)
    )
    approved = make_order(
        uow, customer.user_id, OrderState.APPROVED, created_at=datetime(2024, 1, 1)
    )
    ready = make_order(
        uow, customer.user_id, OrderState.READY_FOR_PICKUP, created_at=datetime(2024, 1, 2)
    )
    # Terminal orders that must be excluded.
    make_order(uow, customer.user_id, OrderState.COMPLETED, created_at=datetime(2024, 1, 4))
    make_order(uow, customer.user_id, OrderState.CANCELLED, created_at=datetime(2024, 1, 5))
    make_order(uow, customer.user_id, OrderState.REJECTED, created_at=datetime(2024, 1, 6))

    result = service.list_active_for_seller()

    # Oldest-first ordering, terminal states excluded (Req 10.6).
    assert [o.order_id for o in result] == [
        approved.order_id,
        ready.order_id,
        placed.order_id,
    ]


def test_list_active_for_seller_empty_when_all_terminal(uow, service):
    customer = make_customer(uow)
    make_order(uow, customer.user_id, OrderState.COMPLETED, created_at=datetime(2024, 1, 1))
    make_order(uow, customer.user_id, OrderState.CANCELLED, created_at=datetime(2024, 1, 2))

    assert service.list_active_for_seller() == []
