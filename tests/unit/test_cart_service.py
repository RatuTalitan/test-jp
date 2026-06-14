"""Unit tests for the Cart_Service (task 8.1).

These exercise the channel-/DB-agnostic ``CartService`` against the in-memory
Unit-of-Work, covering: valid add, below-MOQ, above-stock, non-positive
quantity, zero-stock products (Req 3.3), combine-on-add re-validation (Req 4.5),
change_qty retaining the existing line on rejection (Req 4.6/4.7), remove
(Req 4.8), and totals via the Monetary_Rounding utility (Req 4.9).

Requirements: 3.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from marketplace.cart.service import (
    PRODUCT_OUT_OF_STOCK,
    QTY_BELOW_MOQ,
    QTY_EXCEEDS_STOCK,
    QTY_NOT_POSITIVE,
    CartService,
)
from marketplace.domain import money
from marketplace.domain.entities import Cart, Product, Unit, new_id
from marketplace.domain.memory import InMemoryDatabase, InMemoryUnitOfWork
from marketplace.domain.results import NotFound, Rejected


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
def service(uow: InMemoryUnitOfWork) -> CartService:
    return CartService(uow)


@pytest.fixture
def customer_id() -> uuid.UUID:
    return new_id()


def make_product(
    uow: InMemoryUnitOfWork,
    *,
    name: str = "Cotton Seed Oil Cake",
    unit: Unit = Unit.KILOGRAM,
    price: str = "10.00",
    moq: str = "1",
    stock: str = "100",
) -> Product:
    """Persist and return a product directly via the catalog repository."""
    product = Product(
        product_id=new_id(),
        name=name,
        category_id=new_id(),
        unit=unit,
        price_per_unit=Decimal(price),
        min_order_quantity=Decimal(moq),
        stock_quantity=Decimal(stock),
    )
    return uow.catalog.add_product(product)


# --------------------------------------------------------------------------- #
# add_item: valid path (Req 4.1)
# --------------------------------------------------------------------------- #
def test_add_valid_item_creates_cart_and_line(service, uow, customer_id):
    product = make_product(uow, moq="2", stock="50")

    with uow:
        result = service.add_item(customer_id, product.product_id, Decimal("5"))
        uow.commit()

    assert isinstance(result, Cart)
    assert len(result.items) == 1
    assert result.items[0].product_id == product.product_id
    assert result.items[0].quantity == Decimal("5")

    # Persisted under the customer.
    stored = uow.carts.get_by_customer(customer_id)
    assert stored is not None
    assert stored.items[0].quantity == Decimal("5")


# --------------------------------------------------------------------------- #
# add_item: below MOQ (Req 4.2)
# --------------------------------------------------------------------------- #
def test_add_below_moq_is_rejected_with_required_moq(service, uow, customer_id):
    product = make_product(uow, moq="5", stock="50")

    with uow:
        result = service.add_item(customer_id, product.product_id, Decimal("3"))
        uow.commit()

    assert isinstance(result, Rejected)
    assert result.code == QTY_BELOW_MOQ
    assert result.details["moq"] == Decimal("5")
    # Nothing was stored.
    assert uow.carts.get_by_customer(customer_id) is None


# --------------------------------------------------------------------------- #
# add_item: above stock (Req 4.3)
# --------------------------------------------------------------------------- #
def test_add_above_stock_is_rejected_with_available_stock(service, uow, customer_id):
    product = make_product(uow, moq="1", stock="4")

    with uow:
        result = service.add_item(customer_id, product.product_id, Decimal("5"))
        uow.commit()

    assert isinstance(result, Rejected)
    assert result.code == QTY_EXCEEDS_STOCK
    assert result.details["stock"] == Decimal("4")
    assert uow.carts.get_by_customer(customer_id) is None


# --------------------------------------------------------------------------- #
# add_item: non-positive quantity (Req 4.4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("qty", [Decimal("0"), Decimal("-1"), Decimal("-2.5")])
def test_add_non_positive_quantity_is_rejected(service, uow, customer_id, qty):
    product = make_product(uow, moq="1", stock="50")

    with uow:
        result = service.add_item(customer_id, product.product_id, qty)
        uow.commit()

    assert isinstance(result, Rejected)
    assert result.code == QTY_NOT_POSITIVE
    assert uow.carts.get_by_customer(customer_id) is None


# --------------------------------------------------------------------------- #
# add_item: zero-stock product never addable, regardless of qty/MOQ (Req 3.3)
# --------------------------------------------------------------------------- #
def test_zero_stock_product_not_addable(service, uow, customer_id):
    # MOQ would otherwise be satisfied by qty, but stock == 0 blocks the add.
    product = make_product(uow, moq="1", stock="0")

    with uow:
        result = service.add_item(customer_id, product.product_id, Decimal("5"))
        uow.commit()

    assert isinstance(result, Rejected)
    assert result.code == PRODUCT_OUT_OF_STOCK
    assert uow.carts.get_by_customer(customer_id) is None


# --------------------------------------------------------------------------- #
# add_item: combine-on-add then re-validate combined (Req 4.5)
# --------------------------------------------------------------------------- #
def test_combine_on_add_sums_quantities_when_valid(service, uow, customer_id):
    product = make_product(uow, moq="1", stock="50")

    with uow:
        service.add_item(customer_id, product.product_id, Decimal("6"))
        result = service.add_item(customer_id, product.product_id, Decimal("4"))
        uow.commit()

    assert isinstance(result, Cart)
    assert len(result.items) == 1  # combined into a single line
    assert result.items[0].quantity == Decimal("10")


def test_combine_on_add_revalidates_combined_against_stock(service, uow, customer_id):
    # Each add alone is valid, but the COMBINED quantity exceeds stock (Req 4.5).
    product = make_product(uow, moq="1", stock="10")

    with uow:
        first = service.add_item(customer_id, product.product_id, Decimal("6"))
        second = service.add_item(customer_id, product.product_id, Decimal("6"))
        uow.commit()

    assert isinstance(first, Cart)
    assert isinstance(second, Rejected)
    assert second.code == QTY_EXCEEDS_STOCK
    # The existing line is retained at the first (valid) quantity.
    stored = uow.carts.get_by_customer(customer_id)
    assert stored.items[0].quantity == Decimal("6")


# --------------------------------------------------------------------------- #
# change_qty: valid update and rejection-retains-existing (Req 4.6/4.7)
# --------------------------------------------------------------------------- #
def test_change_qty_updates_line_when_valid(service, uow, customer_id):
    product = make_product(uow, moq="1", stock="50")

    with uow:
        service.add_item(customer_id, product.product_id, Decimal("5"))
        result = service.change_qty(customer_id, product.product_id, Decimal("12"))
        uow.commit()

    assert isinstance(result, Cart)
    assert result.items[0].quantity == Decimal("12")


def test_change_qty_rejection_retains_existing_line(service, uow, customer_id):
    product = make_product(uow, moq="2", stock="10")

    with uow:
        service.add_item(customer_id, product.product_id, Decimal("4"))
        # Change to a value above stock -> rejected, line must stay at 4.
        result = service.change_qty(customer_id, product.product_id, Decimal("20"))
        uow.commit()

    assert isinstance(result, Rejected)
    assert result.code == QTY_EXCEEDS_STOCK
    stored = uow.carts.get_by_customer(customer_id)
    assert stored.items[0].quantity == Decimal("4")


def test_change_qty_below_moq_retains_existing_line(service, uow, customer_id):
    product = make_product(uow, moq="5", stock="50")

    with uow:
        service.add_item(customer_id, product.product_id, Decimal("10"))
        result = service.change_qty(customer_id, product.product_id, Decimal("3"))
        uow.commit()

    assert isinstance(result, Rejected)
    assert result.code == QTY_BELOW_MOQ
    stored = uow.carts.get_by_customer(customer_id)
    assert stored.items[0].quantity == Decimal("10")


def test_change_qty_non_positive_retains_existing_line(service, uow, customer_id):
    product = make_product(uow, moq="1", stock="50")

    with uow:
        service.add_item(customer_id, product.product_id, Decimal("7"))
        result = service.change_qty(customer_id, product.product_id, Decimal("0"))
        uow.commit()

    assert isinstance(result, Rejected)
    assert result.code == QTY_NOT_POSITIVE
    stored = uow.carts.get_by_customer(customer_id)
    assert stored.items[0].quantity == Decimal("7")


def test_change_qty_missing_line_returns_not_found(service, uow, customer_id):
    product = make_product(uow, moq="1", stock="50")

    with uow:
        result = service.change_qty(customer_id, product.product_id, Decimal("5"))
        uow.commit()

    assert isinstance(result, NotFound)
    assert result.entity == "cart_item"


# --------------------------------------------------------------------------- #
# remove_item (Req 4.8)
# --------------------------------------------------------------------------- #
def test_remove_item_deletes_line(service, uow, customer_id):
    product_a = make_product(uow, name="A", moq="1", stock="50")
    product_b = make_product(uow, name="B", moq="1", stock="50")

    with uow:
        service.add_item(customer_id, product_a.product_id, Decimal("5"))
        service.add_item(customer_id, product_b.product_id, Decimal("3"))
        result = service.remove_item(customer_id, product_a.product_id)
        uow.commit()

    assert isinstance(result, Cart)
    remaining_ids = [item.product_id for item in result.items]
    assert product_a.product_id not in remaining_ids
    assert product_b.product_id in remaining_ids


def test_remove_item_on_empty_cart_is_noop(service, uow, customer_id):
    product = make_product(uow)

    with uow:
        result = service.remove_item(customer_id, product.product_id)
        uow.commit()

    assert isinstance(result, Cart)
    assert result.items == []


# --------------------------------------------------------------------------- #
# view + totals via Monetary_Rounding (Req 4.9)
# --------------------------------------------------------------------------- #
def test_view_reports_line_and_cart_totals_via_rounding(service, uow, customer_id):
    # Prices chosen so half-up rounding is exercised on a line total.
    # 3 x 10.005 = 30.015 -> rounds half-up to 30.02 (Monetary_Rounding).
    product_a = make_product(
        uow, name="Oil Cake", unit=Unit.KILOGRAM, price="10.005", moq="1", stock="50"
    )
    # 2.5 x 4.20 = 10.50 exactly.
    product_b = make_product(
        uow, name="Bran", unit=Unit.BAG, price="4.20", moq="1", stock="50"
    )

    with uow:
        service.add_item(customer_id, product_a.product_id, Decimal("3"))
        service.add_item(customer_id, product_b.product_id, Decimal("2.5"))
        uow.commit()

    view = service.view(customer_id)

    assert len(view.line_items) == 2
    lines = {lv.product_id: lv for lv in view.line_items}

    line_a = lines[product_a.product_id]
    assert line_a.name == "Oil Cake"
    assert line_a.unit == Unit.KILOGRAM
    assert line_a.quantity == Decimal("3")
    assert line_a.line_total == money.line_amount(Decimal("3"), Decimal("10.005"))
    assert line_a.line_total == Decimal("30.02")

    line_b = lines[product_b.product_id]
    assert line_b.line_total == money.line_amount(Decimal("2.5"), Decimal("4.20"))
    assert line_b.line_total == Decimal("10.50")

    # Cart total = sum of the already-rounded line totals (Req 4.9).
    expected_total = money.order_total([line_a.line_total, line_b.line_total])
    assert view.total == expected_total
    assert view.total == Decimal("40.52")


def test_view_empty_cart_has_zero_total(service, uow, customer_id):
    view = service.view(customer_id)
    assert view.line_items == []
    assert view.total == Decimal("0.00")


# --------------------------------------------------------------------------- #
# clear
# --------------------------------------------------------------------------- #
def test_clear_empties_cart(service, uow, customer_id):
    product = make_product(uow, moq="1", stock="50")

    with uow:
        service.add_item(customer_id, product.product_id, Decimal("5"))
        uow.commit()
    assert uow.carts.get_by_customer(customer_id) is not None

    with uow:
        service.clear(customer_id)
        uow.commit()

    assert uow.carts.get_by_customer(customer_id) is None
    assert service.view(customer_id).line_items == []
