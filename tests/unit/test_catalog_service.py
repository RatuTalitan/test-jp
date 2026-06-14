"""Focused unit tests for the Catalog_Service (tasks 7.1-7.3).

These exercise concrete examples and edge cases against the in-memory
UnitOfWork (no database):

  * field validation ranges (price / MOQ / stock / name / description / unit),
  * required-field rejection that identifies every missing field,
  * zero-stock products are allowed to exist,
  * case-insensitive category-name duplicate rejection,
  * availability filtering in customer browsing,
  * the in-stock / out-of-stock availability indicator,
  * empty-catalog and empty/nonexistent-category responses.

The numbered property tests (7.4-7.8) and example tests (7.9) are separate
optional tasks and are intentionally not implemented here.
"""

from decimal import Decimal

import pytest

from marketplace.catalog import (
    IN_STOCK,
    MOQ_MAX,
    OUT_OF_STOCK,
    PRICE_MAX,
    STOCK_MAX,
    CatalogService,
    EmptyCatalog,
    EmptyCategory,
    ProductDetail,
)
from marketplace.domain import InMemoryUnitOfWork
from marketplace.domain.entities import Category, Product, Unit
from marketplace.domain.results import NotFound, Rejected


@pytest.fixture
def uow():
    return InMemoryUnitOfWork()


@pytest.fixture
def service(uow):
    return CatalogService(uow)


def _make_category(service, name="Premium Grade"):
    category = service.create_category(name)
    assert isinstance(category, Category)
    return category


def _valid_product_fields(category_id, **overrides):
    fields = dict(
        name="Cotton Seed Oil Cake",
        category_id=category_id,
        unit=Unit.KILOGRAM,
        price_per_unit=Decimal("50.00"),
        min_order_quantity=Decimal("10"),
        stock_quantity=Decimal("100"),
        description="High quality cattle feed.",
    )
    fields.update(overrides)
    return fields


# --------------------------------------------------------------------------- categories
def test_create_category_succeeds_and_persists(service, uow):
    category = service.create_category("Grade A")
    assert isinstance(category, Category)
    assert category.name == "Grade A"
    assert uow.db.categories[category.category_id].name == "Grade A"


def test_create_category_rejects_blank_name(service):
    result = service.create_category("   ")
    assert isinstance(result, Rejected)
    assert result.code == "CATEGORY_NAME_REQUIRED"


def test_create_category_rejects_too_long_name(service):
    result = service.create_category("x" * 51)
    assert isinstance(result, Rejected)
    assert result.code == "CATEGORY_NAME_LENGTH_INVALID"


def test_create_category_accepts_boundary_lengths(service):
    assert isinstance(service.create_category("x"), Category)
    assert isinstance(service.create_category("y" * 50), Category)


def test_create_category_rejects_case_insensitive_duplicate(service, uow):
    first = service.create_category("Premium Grade")
    assert isinstance(first, Category)

    dup = service.create_category("  premium GRADE  ")
    assert isinstance(dup, Rejected)
    assert dup.code == "CATEGORY_NAME_EXISTS"
    # No second category was created.
    assert len(uow.db.categories) == 1


# --------------------------------------------------------------------------- create_product validation
def test_create_product_succeeds(service, uow):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id))
    assert isinstance(product, Product)
    assert product.name == "Cotton Seed Oil Cake"
    assert product.unit is Unit.KILOGRAM
    assert uow.db.products[product.product_id].name == "Cotton Seed Oil Cake"


def test_create_product_missing_required_fields_lists_each(service):
    result = service.create_product()
    assert isinstance(result, Rejected)
    assert result.code == "MISSING_REQUIRED_FIELDS"
    assert set(result.details["fields"]) == {
        "name",
        "category_id",
        "unit",
        "price_per_unit",
        "min_order_quantity",
    }


def test_create_product_missing_subset_of_fields(service):
    category = _make_category(service)
    result = service.create_product(
        name="Feed", category_id=category.category_id, unit=Unit.BAG
    )
    assert isinstance(result, Rejected)
    assert result.code == "MISSING_REQUIRED_FIELDS"
    assert set(result.details["fields"]) == {"price_per_unit", "min_order_quantity"}


def _error_fields(rejected):
    return {e["field"] for e in rejected.details["errors"]}


@pytest.mark.parametrize(
    "price",
    [Decimal("-0.01"), Decimal("10000000.00"), Decimal("-1")],
)
def test_create_product_rejects_price_out_of_range(service, price):
    category = _make_category(service)
    result = service.create_product(**_valid_product_fields(category.category_id, price_per_unit=price))
    assert isinstance(result, Rejected)
    assert result.code == "VALIDATION_FAILED"
    err = next(e for e in result.details["errors"] if e["field"] == "price_per_unit")
    assert err["issue"] == "OUT_OF_RANGE"
    assert err["max"] == str(PRICE_MAX)


def test_create_product_accepts_price_boundaries(service):
    category = _make_category(service)
    low = service.create_product(**_valid_product_fields(category.category_id, price_per_unit=Decimal("0")))
    high = service.create_product(**_valid_product_fields(category.category_id, price_per_unit=PRICE_MAX))
    assert isinstance(low, Product)
    assert isinstance(high, Product)


@pytest.mark.parametrize("moq", [Decimal("0"), Decimal("-5"), Decimal("10000000")])
def test_create_product_rejects_moq_out_of_range(service, moq):
    category = _make_category(service)
    result = service.create_product(**_valid_product_fields(category.category_id, min_order_quantity=moq))
    assert isinstance(result, Rejected)
    err = next(e for e in result.details["errors"] if e["field"] == "min_order_quantity")
    assert err["issue"] == "OUT_OF_RANGE"
    assert err["min_inclusive"] is False  # MOQ must be strictly > 0


def test_create_product_accepts_moq_boundaries(service):
    category = _make_category(service)
    smallest = service.create_product(
        **_valid_product_fields(category.category_id, min_order_quantity=Decimal("0.001"))
    )
    largest = service.create_product(
        **_valid_product_fields(category.category_id, min_order_quantity=MOQ_MAX)
    )
    assert isinstance(smallest, Product)
    assert isinstance(largest, Product)


@pytest.mark.parametrize("stock", [Decimal("-1"), Decimal("10000000")])
def test_create_product_rejects_stock_out_of_range(service, stock):
    category = _make_category(service)
    result = service.create_product(**_valid_product_fields(category.category_id, stock_quantity=stock))
    assert isinstance(result, Rejected)
    err = next(e for e in result.details["errors"] if e["field"] == "stock_quantity")
    assert err["issue"] == "OUT_OF_RANGE"
    assert err["max"] == str(STOCK_MAX)


def test_create_product_allows_zero_stock(service, uow):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id, stock_quantity=Decimal("0")))
    assert isinstance(product, Product)
    assert product.stock_quantity == Decimal("0")
    assert product.product_id in uow.db.products


def test_create_product_defaults_stock_to_zero_when_omitted(service):
    category = _make_category(service)
    fields = _valid_product_fields(category.category_id)
    del fields["stock_quantity"]
    product = service.create_product(**fields)
    assert isinstance(product, Product)
    assert product.stock_quantity == Decimal("0")


def test_create_product_rejects_name_too_long(service):
    category = _make_category(service)
    result = service.create_product(**_valid_product_fields(category.category_id, name="n" * 101))
    assert isinstance(result, Rejected)
    assert "name" in _error_fields(result)


def test_create_product_rejects_description_too_long(service):
    category = _make_category(service)
    result = service.create_product(**_valid_product_fields(category.category_id, description="d" * 1001))
    assert isinstance(result, Rejected)
    assert "description" in _error_fields(result)


def test_create_product_rejects_invalid_unit(service):
    category = _make_category(service)
    result = service.create_product(**_valid_product_fields(category.category_id, unit="TONNE"))
    assert isinstance(result, Rejected)
    err = next(e for e in result.details["errors"] if e["field"] == "unit")
    assert set(err["accepted"]) == {"KILOGRAM", "QUINTAL", "BAG"}


def test_create_product_accepts_unit_string_case_insensitive(service):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id, unit="quintal"))
    assert isinstance(product, Product)
    assert product.unit is Unit.QUINTAL


def test_create_product_rejects_unknown_category(service):
    result = service.create_product(**_valid_product_fields(category_id="does-not-exist"))
    assert isinstance(result, Rejected)
    assert result.code == "VALIDATION_FAILED"
    assert "category_id" in _error_fields(result)


def test_create_product_collects_multiple_errors(service):
    category = _make_category(service)
    result = service.create_product(
        **_valid_product_fields(
            category.category_id,
            price_per_unit=Decimal("-1"),
            min_order_quantity=Decimal("0"),
            stock_quantity=Decimal("-3"),
        )
    )
    assert isinstance(result, Rejected)
    assert {"price_per_unit", "min_order_quantity", "stock_quantity"} <= _error_fields(result)


# --------------------------------------------------------------------------- update_product
def test_update_product_round_trip(service):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id))
    updated = service.update_product(
        product.product_id, price_per_unit=Decimal("75.50"), stock_quantity=Decimal("5")
    )
    assert isinstance(updated, Product)
    assert updated.price_per_unit == Decimal("75.50")
    assert updated.stock_quantity == Decimal("5")
    # Untouched fields are preserved.
    assert updated.name == product.name


def test_update_product_not_found(service):
    assert isinstance(service.update_product("missing", price_per_unit=Decimal("1")), NotFound)


def test_update_product_rejects_invalid_and_persists_nothing(service):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id, price_per_unit=Decimal("50.00")))
    result = service.update_product(product.product_id, price_per_unit=Decimal("-1"))
    assert isinstance(result, Rejected)
    # Original value is unchanged.
    detail = service.get_product(product.product_id)
    assert detail.price_per_unit == Decimal("50.00")


# --------------------------------------------------------------------------- availability + browsing
def test_set_availability_excludes_from_browsing(service):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id))

    # Initially available -> appears in category browsing.
    listing = service.list_available_in_category(category.category_id)
    assert isinstance(listing, list)
    assert [p.product_id for p in listing] == [product.product_id]

    # Mark unavailable -> excluded.
    updated = service.set_availability(product.product_id, False)
    assert isinstance(updated, Product)
    assert updated.available is False
    assert isinstance(service.list_available_in_category(category.category_id), EmptyCategory)


def test_set_availability_not_found(service):
    assert isinstance(service.set_availability("missing", True), NotFound)


def test_list_available_grouped_by_category_excludes_unavailable(service):
    cat_a = _make_category(service, "Grade A")
    cat_b = _make_category(service, "Grade B")
    p1 = service.create_product(**_valid_product_fields(cat_a.category_id, name="A1"))
    p2 = service.create_product(**_valid_product_fields(cat_a.category_id, name="A2"))
    p3 = service.create_product(**_valid_product_fields(cat_b.category_id, name="B1"))
    service.set_availability(p2.product_id, False)

    grouped = service.list_available_grouped_by_category()
    assert isinstance(grouped, list)
    by_cat = {cat.category_id: {p.product_id for p in products} for cat, products in grouped}
    assert by_cat[cat_a.category_id] == {p1.product_id}
    assert by_cat[cat_b.category_id] == {p3.product_id}


def test_empty_catalog_when_no_available_products(service):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id))
    service.set_availability(product.product_id, False)
    result = service.list_available_grouped_by_category()
    assert isinstance(result, EmptyCatalog)
    assert result.code == "NO_PRODUCTS_AVAILABLE"


def test_empty_catalog_when_no_products_at_all(service):
    assert isinstance(service.list_available_grouped_by_category(), EmptyCatalog)


def test_list_available_in_nonexistent_category(service):
    result = service.list_available_in_category("nope")
    assert isinstance(result, EmptyCategory)
    assert result.code == "NO_PRODUCTS_IN_CATEGORY"


def test_list_available_in_category_with_no_available_products(service):
    category = _make_category(service)
    product = service.create_product(**_valid_product_fields(category.category_id))
    service.set_availability(product.product_id, False)
    result = service.list_available_in_category(category.category_id)
    assert isinstance(result, EmptyCategory)


# --------------------------------------------------------------------------- product detail + indicator
def test_get_product_detail_in_stock_indicator(service):
    category = _make_category(service)
    product = service.create_product(
        **_valid_product_fields(category.category_id, stock_quantity=Decimal("3"))
    )
    detail = service.get_product(product.product_id)
    assert isinstance(detail, ProductDetail)
    assert detail.stock_status == IN_STOCK
    assert detail.category_name == category.name
    assert detail.unit is Unit.KILOGRAM
    assert detail.price_per_unit == Decimal("50.00")
    assert detail.min_order_quantity == Decimal("10")


def test_get_product_detail_out_of_stock_indicator(service):
    category = _make_category(service)
    product = service.create_product(
        **_valid_product_fields(category.category_id, stock_quantity=Decimal("0"))
    )
    detail = service.get_product(product.product_id)
    assert detail.stock_status == OUT_OF_STOCK


def test_get_product_not_found(service):
    assert isinstance(service.get_product("missing"), NotFound)
