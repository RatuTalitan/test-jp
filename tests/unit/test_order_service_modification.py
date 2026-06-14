"""Unit tests for OrderService.modify (tasks 16.1 / 16.2, Req 15.1-15.16).

Exercise the Seller order-modification path against the in-memory Unit-of-Work:

* Guards: Seller-only (Req 15.3), modifiable-state (Req 15.1/15.2), unknown
  order (Req 15.4), and the never-empty-order rule (Req 15.10).
* Pre-decrement states (PLACED/PAYMENT_PENDING/PAYMENT_SUBMITTED/
  PAYMENT_VERIFIED): add/change validation against MOQ (Req 15.5), > 0, and
  current stock **without** decrementing it (Req 15.6).
* Edit-time pricing: a newly added line snapshots the product's current catalog
  price (Req 15.15); an untouched line keeps its original snapshot unit_price;
  a changed line reprices to the current catalog price; totals are recomputed
  as the sum of rounded line amounts (Req 15.11/15.16).
* Approved-state stock delta (Req 15.7/15.8/15.9): reduce/remove returns stock,
  increase deducts, an increase/add beyond available stock is rejected with
  nothing changed.
* Exactly one Audit_Trail entry per modification (Req 15.12), state unchanged
  (Req 15.14), and the customer is notified (Req 15.13).

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8, 15.9, 15.10,
15.11, 15.12, 15.13, 15.14, 15.15, 15.16.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from marketplace.domain.entities import (
    AuditAction,
    Category,
    NotificationKind,
    Order,
    OrderItem,
    OrderState,
    Product,
    Role,
    Unit,
    User,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import Conflict, NotAuthorized, NotFound, Rejected
from marketplace.order.service import (
    LINE_ALREADY_EXISTS,
    MODIFICATION_STOCK_EXCEEDED,
    MODIFY_REQUIRES_SELLER,
    ORDER_MUST_RETAIN_LINE,
    ORDER_NOT_MODIFIABLE,
    QTY_BELOW_MOQ,
    ModificationOp,
    OrderModification,
    OrderService,
)
from marketplace.order.state_machine import Actor


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


def make_customer(uow: InMemoryUnitOfWork) -> User:
    customer = User(
        user_id=new_id(),
        telegram_user_id=2,
        role=Role.CUSTOMER,
        verified_contact="+919999999999",
        contact_verified_at=datetime(2024, 1, 2),
    )
    return uow.users.add(customer)


def make_product(
    uow: InMemoryUnitOfWork,
    *,
    name: str = "Cottonseed Oilcake",
    price: str = "10.00",
    moq: str = "1",
    stock: str = "100",
) -> Product:
    category = uow.catalog.add_category(
        Category(category_id=new_id(), name=f"cat-{name}")
    )
    product = Product(
        product_id=new_id(),
        name=name,
        category_id=category.category_id,
        unit=Unit.KILOGRAM,
        price_per_unit=Decimal(price),
        min_order_quantity=Decimal(moq),
        stock_quantity=Decimal(stock),
    )
    return uow.catalog.add_product(product)


def make_order(
    uow: InMemoryUnitOfWork,
    customer_id,
    state: OrderState,
    items: list[OrderItem],
) -> Order:
    from marketplace.domain import money

    total = money.order_total(it.line_amount for it in items)
    order = Order(
        order_id=new_id(),
        customer_id=customer_id,
        state=state,
        total_amount=total,
        items=items,
        created_at=datetime(2024, 1, 3),
    )
    return uow.orders.add(order)


def line(product: Product, qty: str, *, unit_price: str | None = None) -> OrderItem:
    from marketplace.domain import money

    price = Decimal(unit_price) if unit_price is not None else product.price_per_unit
    q = Decimal(qty)
    return OrderItem(
        product_id=product.product_id,
        ordered_quantity=q,
        unit_price=price,
        line_amount=money.line_amount(q, price),
    )


def seller_actor(uow: InMemoryUnitOfWork) -> Actor:
    seller = make_seller(uow)
    return Actor.seller(seller.user_id)


PRE_DECREMENT = [
    OrderState.PLACED,
    OrderState.PAYMENT_PENDING,
    OrderState.PAYMENT_SUBMITTED,
    OrderState.PAYMENT_VERIFIED,
]
NON_MODIFIABLE = [
    OrderState.READY_FOR_PICKUP,
    OrderState.COMPLETED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
]


# --------------------------------------------------------------------------- #
# Guards: seller-only, state, not-found, zero-line (Req 15.2/15.3/15.4/15.10)
# --------------------------------------------------------------------------- #
def test_non_seller_is_rejected_and_order_unchanged(service, uow):
    customer = make_customer(uow)
    product = make_product(uow)
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("9")),
        Actor.customer(customer.user_id),
    )

    assert isinstance(result, NotAuthorized)
    assert result.code == MODIFY_REQUIRES_SELLER
    stored = uow.orders.get(order.order_id)
    assert stored.items[0].ordered_quantity == Decimal("5")
    assert uow.audit.list_by_order(order.order_id) == []


def test_unknown_order_returns_not_found(service, uow):
    actor = seller_actor(uow)
    result = service.modify(
        new_id(),
        OrderModification(ModificationOp.REMOVE_LINE, new_id()),
        actor,
    )
    assert isinstance(result, NotFound)
    assert result.entity == "order"


@pytest.mark.parametrize("state", NON_MODIFIABLE)
def test_non_modifiable_state_is_rejected(service, uow, state):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow)
    order = make_order(uow, customer.user_id, state, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("9")),
        actor,
    )

    assert isinstance(result, Rejected)
    assert result.code == ORDER_NOT_MODIFIABLE
    assert uow.orders.get(order.order_id).items[0].ordered_quantity == Decimal("5")


def test_removing_last_line_is_rejected(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow)
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.REMOVE_LINE, product.product_id),
        actor,
    )

    assert isinstance(result, Rejected)
    assert result.code == ORDER_MUST_RETAIN_LINE
    assert len(uow.orders.get(order.order_id).items) == 1


# --------------------------------------------------------------------------- #
# Pre-decrement validation: MOQ, > 0, stock without decrement (Req 15.5/15.6)
# --------------------------------------------------------------------------- #
def test_change_below_moq_is_rejected_with_moq(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, moq="3", stock="100")
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("2")),
        actor,
    )

    assert isinstance(result, Rejected)
    assert result.code == QTY_BELOW_MOQ
    assert result.details["min_order_quantity"] == "3"
    assert uow.orders.get(order.order_id).items[0].ordered_quantity == Decimal("5")


def test_change_to_zero_is_rejected(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, moq="1")
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("0")),
        actor,
    )

    assert isinstance(result, Rejected)
    assert result.code == QTY_BELOW_MOQ


def test_predecrement_increase_beyond_stock_rejected_no_decrement(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="8")
    order = make_order(uow, customer.user_id, OrderState.PAYMENT_PENDING, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("9")),
        actor,
    )

    assert isinstance(result, Conflict)
    assert result.code == MODIFICATION_STOCK_EXCEEDED
    assert result.details["available_stock"] == "8"
    # Stock is untouched in pre-decrement states (Req 15.6).
    assert uow.catalog.get_product(product.product_id).stock_quantity == Decimal("8")
    assert uow.orders.get(order.order_id).items[0].ordered_quantity == Decimal("5")


def test_predecrement_valid_change_does_not_decrement_stock(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="50")
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("12")),
        actor,
    )

    assert isinstance(result, Order)
    assert result.items[0].ordered_quantity == Decimal("12")
    assert result.state is OrderState.PLACED  # state unchanged (Req 15.14)
    # No decrement in pre-decrement states (Req 15.6).
    assert uow.catalog.get_product(product.product_id).stock_quantity == Decimal("50")


# --------------------------------------------------------------------------- #
# Edit-time pricing (Req 15.11/15.15/15.16)
# --------------------------------------------------------------------------- #
def test_added_line_uses_current_catalog_price(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    existing_product = make_product(uow, name="A", price="10.00", stock="100")
    # The added product's catalog price changed since the order was placed.
    added_product = make_product(uow, name="B", price="7.50", moq="2", stock="100")
    order = make_order(
        uow,
        customer.user_id,
        OrderState.PLACED,
        [line(existing_product, "4", unit_price="9.00")],
    )

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.ADD_LINE, added_product.product_id, Decimal("3")),
        actor,
    )

    assert isinstance(result, Order)
    added = next(i for i in result.items if i.product_id == added_product.product_id)
    untouched = next(
        i for i in result.items if i.product_id == existing_product.product_id
    )
    # New line snapshots the CURRENT catalog price (Req 15.15).
    assert added.unit_price == Decimal("7.50")
    assert added.line_amount == Decimal("22.50")
    # Untouched line keeps its ORIGINAL snapshot price (Req 15.16).
    assert untouched.unit_price == Decimal("9.00")
    assert untouched.line_amount == Decimal("36.00")
    # Total = sum of rounded line amounts (Req 15.11).
    assert result.total_amount == Decimal("58.50")


def test_changed_line_reprices_untouched_line_keeps_snapshot(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    # Product whose catalog price is now higher than the order's snapshot.
    changed = make_product(uow, name="C", price="12.00", stock="100")
    other = make_product(uow, name="D", price="5.00", stock="100")
    order = make_order(
        uow,
        customer.user_id,
        OrderState.PAYMENT_VERIFIED,
        [
            line(changed, "2", unit_price="8.00"),
            line(other, "3", unit_price="4.00"),
        ],
    )

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, changed.product_id, Decimal("5")),
        actor,
    )

    assert isinstance(result, Order)
    changed_line = next(i for i in result.items if i.product_id == changed.product_id)
    other_line = next(i for i in result.items if i.product_id == other.product_id)
    # The touched line reprices to the current catalog price (Req 15.16).
    assert changed_line.unit_price == Decimal("12.00")
    assert changed_line.line_amount == Decimal("60.00")
    # The untouched line keeps its original snapshot price.
    assert other_line.unit_price == Decimal("4.00")
    assert other_line.line_amount == Decimal("12.00")
    assert result.total_amount == Decimal("72.00")


# --------------------------------------------------------------------------- #
# Approved-state stock delta (Req 15.7/15.8/15.9)
# --------------------------------------------------------------------------- #
def test_approved_reduce_returns_stock(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="20")  # 20 left after a prior decrement
    order = make_order(uow, customer.user_id, OrderState.APPROVED, [line(product, "10")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("4")),
        actor,
    )

    assert isinstance(result, Order)
    # Reduced by 6 -> 6 returned to stock (Req 15.9): 20 + (10 - 4) = 26.
    assert uow.catalog.get_product(product.product_id).stock_quantity == Decimal("26")
    assert result.items[0].ordered_quantity == Decimal("4")
    assert result.state is OrderState.APPROVED


def test_approved_increase_deducts_stock(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="20")
    order = make_order(uow, customer.user_id, OrderState.APPROVED, [line(product, "10")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("15")),
        actor,
    )

    assert isinstance(result, Order)
    # Increase of 5 deducts 5 (Req 15.7): 20 + (10 - 15) = 15.
    assert uow.catalog.get_product(product.product_id).stock_quantity == Decimal("15")


def test_approved_increase_beyond_stock_rejected_nothing_changed(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="3")
    order = make_order(uow, customer.user_id, OrderState.APPROVED, [line(product, "10")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("14")),
        actor,
    )

    assert isinstance(result, Conflict)
    assert result.code == MODIFICATION_STOCK_EXCEEDED
    # An increase of 4 needs 4 but only 3 available -> nothing changed (Req 15.8).
    assert uow.catalog.get_product(product.product_id).stock_quantity == Decimal("3")
    assert uow.orders.get(order.order_id).items[0].ordered_quantity == Decimal("10")
    assert uow.audit.list_by_order(order.order_id) == []


def test_approved_remove_returns_stock(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    keep = make_product(uow, name="keep", stock="5")
    drop = make_product(uow, name="drop", stock="5")
    order = make_order(
        uow,
        customer.user_id,
        OrderState.APPROVED,
        [line(keep, "2"), line(drop, "8")],
    )

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.REMOVE_LINE, drop.product_id),
        actor,
    )

    assert isinstance(result, Order)
    assert len(result.items) == 1
    # Removing the approved line returns its 8 to stock (Req 15.9): 5 + 8 = 13.
    assert uow.catalog.get_product(drop.product_id).stock_quantity == Decimal("13")
    # The kept product's stock is untouched.
    assert uow.catalog.get_product(keep.product_id).stock_quantity == Decimal("5")


def test_approved_add_line_beyond_stock_rejected(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    existing = make_product(uow, name="existing", stock="50")
    fresh = make_product(uow, name="fresh", stock="2")
    order = make_order(
        uow, customer.user_id, OrderState.APPROVED, [line(existing, "5")]
    )

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.ADD_LINE, fresh.product_id, Decimal("4")),
        actor,
    )

    assert isinstance(result, Conflict)
    assert result.code == MODIFICATION_STOCK_EXCEEDED
    assert uow.catalog.get_product(fresh.product_id).stock_quantity == Decimal("2")
    assert len(uow.orders.get(order.order_id).items) == 1


# --------------------------------------------------------------------------- #
# Audit + notification + duplicate-add (Req 15.12/15.13)
# --------------------------------------------------------------------------- #
def test_exactly_one_audit_entry_per_modification(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="100")
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("7")),
        actor,
    )
    entries = uow.audit.list_by_order(order.order_id)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.action is AuditAction.MODIFY_LINE
    assert entry.acting_user_id == actor.user_id
    assert entry.detail["old_value"] == "5"
    assert entry.detail["new_value"] == "7"

    # A second modification appends exactly one more entry.
    service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("9")),
        actor,
    )
    assert len(uow.audit.list_by_order(order.order_id)) == 2


def test_customer_is_notified_of_modification(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="100")
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.CHANGE_QTY, product.product_id, Decimal("8")),
        actor,
    )

    notes = [
        n
        for n in uow.db.notifications.values()
        if n.order_id == order.order_id and n.kind is NotificationKind.MODIFIED
    ]
    assert len(notes) == 1
    note = notes[0]
    assert note.recipient_id == customer.user_id
    assert note.payload["total_amount"] == str(result.total_amount)


def test_duplicate_add_line_is_rejected(service, uow):
    actor = seller_actor(uow)
    customer = make_customer(uow)
    product = make_product(uow, stock="100")
    order = make_order(uow, customer.user_id, OrderState.PLACED, [line(product, "5")])

    result = service.modify(
        order.order_id,
        OrderModification(ModificationOp.ADD_LINE, product.product_id, Decimal("3")),
        actor,
    )

    assert isinstance(result, Rejected)
    assert result.code == LINE_ALREADY_EXISTS
    assert len(uow.orders.get(order.order_id).items) == 1
