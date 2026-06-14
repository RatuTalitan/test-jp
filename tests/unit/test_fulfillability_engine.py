"""Unit / example tests for the Fulfillability Engine (task 13.1).

Covers the pure ``is_fulfillable`` / ``shortfalls`` functions (Req 16.2) and the
on-read ``recompute_for_product_change`` helper (Req 16.1):

* fully Fulfillable orders, including the ``ordered == stock`` equality boundary;
* not-Fulfillable orders with correct shortfall reporting;
* multi-line orders mixing fulfillable and short lines;
* the recompute helper selecting only not-yet-Approved orders that contain the
  changed product, computed against the live in-memory catalog.

Pure functions are exercised with plain stock-snapshot dicts; the recompute
helper is exercised through the in-memory ``UnitOfWork`` (no DB, no I/O).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from marketplace.domain.entities import (
    Order,
    OrderItem,
    OrderState,
    Product,
    Unit,
    new_id,
)
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.fulfillability.engine import (
    NOT_YET_APPROVED_STATES,
    OrderFulfillability,
    Shortfall,
    is_fulfillable,
    recompute_for_product_change,
    shortfalls,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _item(product_id: uuid.UUID, qty: str) -> OrderItem:
    """An order line with snapshot price/amount irrelevant to fulfillability."""
    quantity = Decimal(qty)
    return OrderItem(
        product_id=product_id,
        ordered_quantity=quantity,
        unit_price=Decimal("10.00"),
        line_amount=(quantity * Decimal("10.00")).quantize(Decimal("0.01")),
    )


def _order(items: list[OrderItem], state: OrderState = OrderState.PLACED) -> Order:
    return Order(
        order_id=new_id(),
        customer_id=new_id(),
        state=state,
        total_amount=sum((i.line_amount for i in items), Decimal("0.00")),
        items=items,
    )


def _product(stock: str, category_id: uuid.UUID | None = None) -> Product:
    return Product(
        product_id=new_id(),
        name="Cotton Seed Oil Cake",
        category_id=category_id or new_id(),
        unit=Unit.KILOGRAM,
        price_per_unit=Decimal("10.00"),
        min_order_quantity=Decimal("1"),
        stock_quantity=Decimal(stock),
    )


# --------------------------------------------------------------------------- #
# is_fulfillable / shortfalls : fully fulfillable (Req 16.2)
# --------------------------------------------------------------------------- #
def test_fulfillable_when_all_lines_below_stock():
    p1, p2 = new_id(), new_id()
    order = _order([_item(p1, "3"), _item(p2, "5")])
    snapshot = {p1: Decimal("10"), p2: Decimal("8")}

    assert is_fulfillable(order, snapshot) is True
    assert shortfalls(order, snapshot) == []


def test_fulfillable_at_equality_boundary():
    """ordered_quantity == current_stock is Fulfillable (Req 16.2: '<=')."""
    p1, p2 = new_id(), new_id()
    order = _order([_item(p1, "10"), _item(p2, "4")])
    snapshot = {p1: Decimal("10"), p2: Decimal("4")}

    assert is_fulfillable(order, snapshot) is True
    assert shortfalls(order, snapshot) == []


def test_empty_order_is_vacuously_fulfillable():
    order = _order([])
    assert is_fulfillable(order, {}) is True
    assert shortfalls(order, {}) == []


# --------------------------------------------------------------------------- #
# is_fulfillable / shortfalls : not fulfillable (Req 16.2)
# --------------------------------------------------------------------------- #
def test_not_fulfillable_when_a_line_exceeds_stock():
    p1 = new_id()
    order = _order([_item(p1, "12")])
    snapshot = {p1: Decimal("10")}

    assert is_fulfillable(order, snapshot) is False
    result = shortfalls(order, snapshot)
    assert result == [
        Shortfall(product_id=p1, ordered_quantity=Decimal("12"), current_stock=Decimal("10"))
    ]


def test_shortfall_reports_exact_ordered_and_current_stock():
    p1 = new_id()
    order = _order([_item(p1, "7")])
    snapshot = {p1: Decimal("2")}

    [sf] = shortfalls(order, snapshot)
    assert sf.product_id == p1
    assert sf.ordered_quantity == Decimal("7")
    assert sf.current_stock == Decimal("2")
    # Implied shortfall amount is ordered - current.
    assert sf.ordered_quantity - sf.current_stock == Decimal("5")


def test_missing_product_in_snapshot_treated_as_zero_stock():
    p1 = new_id()
    order = _order([_item(p1, "1")])

    assert is_fulfillable(order, {}) is False
    [sf] = shortfalls(order, {})
    assert sf.current_stock == Decimal("0")


# --------------------------------------------------------------------------- #
# Multi-line orders (Req 16.2)
# --------------------------------------------------------------------------- #
def test_multiline_order_reports_only_short_lines():
    p1, p2, p3 = new_id(), new_id(), new_id()
    order = _order([_item(p1, "5"), _item(p2, "20"), _item(p3, "3")])
    snapshot = {p1: Decimal("5"), p2: Decimal("10"), p3: Decimal("100")}

    assert is_fulfillable(order, snapshot) is False
    result = shortfalls(order, snapshot)
    # Only the p2 line is short; p1 is at the equality boundary, p3 is plentiful.
    assert result == [
        Shortfall(product_id=p2, ordered_quantity=Decimal("20"), current_stock=Decimal("10"))
    ]


def test_multiline_order_all_short():
    p1, p2 = new_id(), new_id()
    order = _order([_item(p1, "9"), _item(p2, "9")])
    snapshot = {p1: Decimal("1"), p2: Decimal("2")}

    assert is_fulfillable(order, snapshot) is False
    assert {sf.product_id for sf in shortfalls(order, snapshot)} == {p1, p2}


def test_is_fulfillable_consistent_with_shortfalls():
    """is_fulfillable(...) is True exactly when shortfalls(...) is empty."""
    p1, p2 = new_id(), new_id()
    order = _order([_item(p1, "4"), _item(p2, "6")])
    for snapshot in (
        {p1: Decimal("4"), p2: Decimal("6")},  # both met
        {p1: Decimal("3"), p2: Decimal("6")},  # one short
        {p1: Decimal("0"), p2: Decimal("0")},  # both short
    ):
        assert is_fulfillable(order, snapshot) == (shortfalls(order, snapshot) == [])


# --------------------------------------------------------------------------- #
# recompute_for_product_change (Req 16.1)
# --------------------------------------------------------------------------- #
def _seed_product(uow: InMemoryUnitOfWork, product: Product) -> None:
    uow.catalog.add_product(product)


def test_recompute_selects_only_not_yet_approved_orders_with_product():
    db = InMemoryDatabase()
    uow = InMemoryUnitOfWork(db)

    changed = _product(stock="5")
    other = _product(stock="100")
    _seed_product(uow, changed)
    _seed_product(uow, other)

    # An order in each state, all containing the changed product, ordering 8
    # (which exceeds the current stock of 5 -> not fulfillable).
    orders_by_state = {}
    for state in OrderState:
        order = _order([_item(changed.product_id, "8")], state=state)
        uow.orders.add(order)
        orders_by_state[state] = order

    # An unrelated not-yet-approved order that does NOT contain the changed
    # product must be ignored even though it is in a pending state.
    unrelated = _order([_item(other.product_id, "1")], state=OrderState.PLACED)
    uow.orders.add(unrelated)

    results = recompute_for_product_change(uow, changed.product_id)

    selected_ids = {r.order_id for r in results}
    expected_ids = {
        orders_by_state[s].order_id for s in NOT_YET_APPROVED_STATES
    }
    assert selected_ids == expected_ids
    # Unrelated order and APPROVED/terminal-state orders are excluded.
    assert unrelated.order_id not in selected_ids
    assert orders_by_state[OrderState.APPROVED].order_id not in selected_ids
    assert orders_by_state[OrderState.COMPLETED].order_id not in selected_ids
    assert orders_by_state[OrderState.CANCELLED].order_id not in selected_ids
    assert orders_by_state[OrderState.REJECTED].order_id not in selected_ids


def test_recompute_uses_current_catalog_stock_and_flags_shortfall():
    db = InMemoryDatabase()
    uow = InMemoryUnitOfWork(db)

    product = _product(stock="10")
    _seed_product(uow, product)

    fulfillable_order = _order([_item(product.product_id, "10")], OrderState.PLACED)
    short_order = _order([_item(product.product_id, "11")], OrderState.PAYMENT_PENDING)
    uow.orders.add(fulfillable_order)
    uow.orders.add(short_order)

    results = {r.order_id: r for r in recompute_for_product_change(uow, product.product_id)}

    assert results[fulfillable_order.order_id].is_fulfillable is True
    assert results[fulfillable_order.order_id].shortfalls == ()

    short = results[short_order.order_id]
    assert short.is_fulfillable is False
    assert short.shortfalls == (
        Shortfall(
            product_id=product.product_id,
            ordered_quantity=Decimal("11"),
            current_stock=Decimal("10"),
        ),
    )


def test_recompute_reflects_stock_change_on_read():
    """Lowering catalog stock flips a previously-fulfillable order to short."""
    db = InMemoryDatabase()
    uow = InMemoryUnitOfWork(db)

    product = _product(stock="20")
    _seed_product(uow, product)
    order = _order([_item(product.product_id, "15")], OrderState.PAYMENT_VERIFIED)
    uow.orders.add(order)

    # Initially fulfillable (15 <= 20).
    [before] = recompute_for_product_change(uow, product.product_id)
    assert before.is_fulfillable is True

    # Seller stock edit drops stock to 10 -> now short by 5, recomputed on read.
    product.stock_quantity = Decimal("10")
    uow.catalog.update_product(product)

    [after] = recompute_for_product_change(uow, product.product_id)
    assert after.is_fulfillable is False
    assert after.shortfalls[0].current_stock == Decimal("10")
    assert after.shortfalls[0].ordered_quantity == Decimal("15")


def test_recompute_evaluates_all_products_in_a_multiline_order():
    """An order short only on a *sibling* product is still flagged not-fulfillable."""
    db = InMemoryDatabase()
    uow = InMemoryUnitOfWork(db)

    changed = _product(stock="50")   # plenty of the changed product
    sibling = _product(stock="2")    # but the sibling is short
    _seed_product(uow, changed)
    _seed_product(uow, sibling)

    order = _order(
        [_item(changed.product_id, "5"), _item(sibling.product_id, "9")],
        OrderState.PLACED,
    )
    uow.orders.add(order)

    [result] = recompute_for_product_change(uow, changed.product_id)
    assert result.is_fulfillable is False
    assert result.shortfalls == (
        Shortfall(
            product_id=sibling.product_id,
            ordered_quantity=Decimal("9"),
            current_stock=Decimal("2"),
        ),
    )


def test_recompute_returns_empty_when_no_pending_orders_contain_product():
    db = InMemoryDatabase()
    uow = InMemoryUnitOfWork(db)
    product = _product(stock="5")
    _seed_product(uow, product)
    # Only an APPROVED order contains it -> excluded by Req 16.1 state filter.
    uow.orders.add(_order([_item(product.product_id, "3")], OrderState.APPROVED))

    assert recompute_for_product_change(uow, product.product_id) == []
