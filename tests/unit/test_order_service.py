"""Unit tests for OrderService.place_order (task 10.2).

Exercise placement against the in-memory Unit-of-Work: success (cart copied with
snapshot prices, totals via Monetary_Rounding, ends in PAYMENT_PENDING, cart
cleared, seller notification enqueued), empty-cart rejection (Req 5.3),
over-stock rejection identifying affected lines + available stock (Req 5.4), and
unauthenticated rejection (Req 12.6).

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 12.6.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from marketplace.cart.service import CartService
from marketplace.domain import money
from marketplace.domain.entities import (
    Cart,
    CartItem,
    NotificationKind,
    Product,
    Role,
    Unit,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import Conflict, Rejected, Unauthenticated
from marketplace.domain.entities import OrderState
from marketplace.order.service import (
    EMPTY_CART,
    PLACEMENT_TRANSITION_SEQ,
    STOCK_EXCEEDED_AT_PLACEMENT,
    OrderService,
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


def make_customer(uow: InMemoryUnitOfWork, *, authenticated: bool = True) -> User:
    customer = User(
        user_id=new_id(),
        telegram_user_id=2,
        role=Role.CUSTOMER,
        verified_contact="+919999999999" if authenticated else None,
        contact_verified_at=datetime(2024, 1, 2) if authenticated else None,
    )
    return uow.users.add(customer)


def make_product(
    uow: InMemoryUnitOfWork,
    *,
    name: str = "Cotton Seed Oil Cake",
    price: str = "10.50",
    stock: str = "100",
    moq: str = "1",
) -> Product:
    product = Product(
        product_id=new_id(),
        name=name,
        category_id=new_id(),
        unit=Unit.KILOGRAM,
        price_per_unit=Decimal(price),
        min_order_quantity=Decimal(moq),
        stock_quantity=Decimal(stock),
    )
    return uow.catalog.add_product(product)


def add_cart(uow: InMemoryUnitOfWork, customer_id, lines: list[tuple]) -> Cart:
    cart = Cart(
        cart_id=new_id(),
        customer_id=customer_id,
        items=[CartItem(product_id=pid, quantity=Decimal(q)) for pid, q in lines],
    )
    return uow.carts.add(cart)


# --------------------------------------------------------------------------- #
# Success path (Req 5.1, 5.2, 5.5, 5.6, 5.7)
# --------------------------------------------------------------------------- #
def test_place_order_success_copies_cart_and_snapshots_prices(uow, service):
    seller = make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, name="A", price="10.50", stock="100")
    p2 = make_product(uow, name="B", price="3.333", stock="50")
    add_cart(uow, customer.user_id, [(p1.product_id, "2"), (p2.product_id, "3")])

    order = service.place_order(customer.user_id)

    assert order.customer_id == customer.user_id
    assert len(order.items) == 2
    # Snapshot unit prices match the products' catalog prices at placement.
    by_pid = {it.product_id: it for it in order.items}
    assert by_pid[p1.product_id].unit_price == Decimal("10.50")
    assert by_pid[p2.product_id].unit_price == Decimal("3.333")
    # Quantities copied from the cart.
    assert by_pid[p1.product_id].ordered_quantity == Decimal("2")
    assert by_pid[p2.product_id].ordered_quantity == Decimal("3")


def test_place_order_totals_use_monetary_rounding(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    # 3 x 3.333 = 9.999 -> line rounds half-up to 10.00; 2 x 10.50 = 21.00.
    p1 = make_product(uow, name="A", price="10.50", stock="100")
    p2 = make_product(uow, name="B", price="3.333", stock="50")
    add_cart(uow, customer.user_id, [(p1.product_id, "2"), (p2.product_id, "3")])

    order = service.place_order(customer.user_id)

    by_pid = {it.product_id: it for it in order.items}
    assert by_pid[p1.product_id].line_amount == Decimal("21.00")
    assert by_pid[p2.product_id].line_amount == Decimal("10.00")
    # Total == sum of already-rounded line amounts.
    expected = money.order_total(it.line_amount for it in order.items)
    assert order.total_amount == expected == Decimal("31.00")


def test_place_order_ends_in_payment_pending_and_clears_cart(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    add_cart(uow, customer.user_id, [(p1.product_id, "5")])

    order = service.place_order(customer.user_id)

    # Auto-transitioned PLACED -> PAYMENT_PENDING via the state machine.
    assert order.state is OrderState.PAYMENT_PENDING
    # Cart cleared (Req 5.6).
    assert uow.carts.get_by_customer(customer.user_id) is None
    # Persisted and assigned a unique id.
    assert uow.orders.get(order.order_id) is not None
    assert order.created_at is not None


def test_place_order_enqueues_seller_notification(uow, service):
    seller = make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    add_cart(uow, customer.user_id, [(p1.product_id, "5")])

    order = service.place_order(customer.user_id)

    notif = uow.notifications.find_by_idempotency_key(
        order.order_id, NotificationKind.NEW_ORDER, PLACEMENT_TRANSITION_SEQ
    )
    assert notif is not None
    assert notif.recipient_id == seller.user_id
    assert notif.kind is NotificationKind.NEW_ORDER
    assert notif.payload["new_state"] == OrderState.PAYMENT_PENDING.value


def test_place_order_assigns_unique_ids(uow, service):
    make_seller(uow)
    c1 = make_customer(uow)
    c2 = User(
        user_id=new_id(),
        telegram_user_id=3,
        role=Role.CUSTOMER,
        verified_contact="+918888888888",
        contact_verified_at=datetime(2024, 1, 3),
    )
    uow.users.add(c2)
    p1 = make_product(uow)
    add_cart(uow, c1.user_id, [(p1.product_id, "5")])
    add_cart(uow, c2.user_id, [(p1.product_id, "5")])

    o1 = service.place_order(c1.user_id)
    o2 = service.place_order(c2.user_id)
    assert o1.order_id != o2.order_id


# --------------------------------------------------------------------------- #
# Empty-cart rejection (Req 5.3)
# --------------------------------------------------------------------------- #
def test_place_order_rejects_empty_cart_no_cart(uow, service):
    make_seller(uow)
    customer = make_customer(uow)

    result = service.place_order(customer.user_id)

    assert isinstance(result, Rejected)
    assert result.code == EMPTY_CART
    # No order created.
    assert uow.orders.list_by_customer(customer.user_id) == []


def test_place_order_rejects_empty_cart_zero_lines(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    add_cart(uow, customer.user_id, [])  # cart exists but has no lines

    result = service.place_order(customer.user_id)

    assert isinstance(result, Rejected)
    assert result.code == EMPTY_CART


# --------------------------------------------------------------------------- #
# Over-stock rejection (Req 5.4)
# --------------------------------------------------------------------------- #
def test_place_order_rejects_over_stock_lines_with_details(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, name="A", stock="5")
    p2 = make_product(uow, name="B", stock="100")
    p3 = make_product(uow, name="C", stock="2")
    add_cart(
        uow,
        customer.user_id,
        [(p1.product_id, "10"), (p2.product_id, "1"), (p3.product_id, "9")],
    )

    result = service.place_order(customer.user_id)

    assert isinstance(result, Conflict)
    assert result.code == STOCK_EXCEEDED_AT_PLACEMENT
    affected = {line["product_id"]: line for line in result.details["lines"]}
    # Both over-stock lines are reported, the in-stock line is not.
    assert set(affected) == {p1.product_id, p3.product_id}
    assert affected[p1.product_id]["available_stock"] == Decimal("5")
    assert affected[p1.product_id]["ordered_quantity"] == Decimal("10")
    assert affected[p3.product_id]["available_stock"] == Decimal("2")
    # Cart left unchanged, no order created.
    assert uow.carts.get_by_customer(customer.user_id) is not None
    assert uow.orders.list_by_customer(customer.user_id) == []


def test_place_order_allows_qty_equal_to_stock(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow, stock="5")
    add_cart(uow, customer.user_id, [(p1.product_id, "5")])

    order = service.place_order(customer.user_id)
    assert order.state is OrderState.PAYMENT_PENDING


# --------------------------------------------------------------------------- #
# Unauthenticated rejection (Req 12.6)
# --------------------------------------------------------------------------- #
def test_place_order_rejects_unauthenticated_customer(uow, service):
    make_seller(uow)
    customer = make_customer(uow, authenticated=False)
    p1 = make_product(uow)
    add_cart(uow, customer.user_id, [(p1.product_id, "5")])

    result = service.place_order(customer.user_id)

    assert isinstance(result, Unauthenticated)
    # No order created and the cart is untouched (still present).
    assert uow.orders.list_by_customer(customer.user_id) == []
    assert uow.carts.get_by_customer(customer.user_id) is not None


def test_place_order_rejects_unknown_customer(uow, service):
    make_seller(uow)
    result = service.place_order(new_id())
    assert isinstance(result, Unauthenticated)


# --------------------------------------------------------------------------- #
# Integration with CartService.clear semantics (cart fully emptied)
# --------------------------------------------------------------------------- #
def test_place_order_cart_view_empty_after_placement(uow, service):
    make_seller(uow)
    customer = make_customer(uow)
    p1 = make_product(uow)
    add_cart(uow, customer.user_id, [(p1.product_id, "5")])

    service.place_order(customer.user_id)

    view = CartService(uow).view(customer.user_id)
    assert view.line_items == []
    assert view.total == Decimal("0.00")
