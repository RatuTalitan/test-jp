"""Catalog_Service: categories, products, validation, availability, browsing.

This module implements tasks 7.1-7.3 of the cottonseed-oilcake-marketplace
spec (design.md -> Catalog_Service). It is **language-agnostic**: every method
returns either a domain entity / view object or one of the channel-neutral
typed result objects from :mod:`marketplace.domain.results` (plus the two small
catalog-specific "empty browsing" results defined below). It never returns
localized prose -- the Bot_Interface maps the stable ``code`` values here to the
Message_Catalog for the active language.

Persistence is reached **only** through the repository protocols exposed by the
injected :class:`~marketplace.domain.repositories.UnitOfWork`
(``uow.catalog`` -> ``CatalogRepository``). The service never opens a database
connection or imports the ORM directly (design.md -> Architectural Principles:
"clear interfaces, no cross-module DB poking"). Each mutating method runs inside
the unit-of-work transaction and commits atomically, so a rejected submission
creates no record (Req 2.5/2.6/2.9/2.10).

Validation ranges (design.md -> Catalog_Service; Req 2.5/2.6/2.9):

  * product name        : 1-100 characters
  * description         : <= 1000 characters (optional)
  * unit                : one of KILOGRAM / QUINTAL / BAG (Req 2.7)
  * price_per_unit      : [0, 9,999,999.99]
  * min_order_quantity  : (0, 9,999,999]
  * stock_quantity      : [0, 9,999,999]   (0 is allowed -> "out of stock")
  * category name       : 1-50 characters, case-insensitively unique
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Union

from marketplace.domain.entities import Category, Product, Unit, new_id
from marketplace.domain.money import to_decimal
from marketplace.domain.repositories import UnitOfWork
from marketplace.domain.results import NotFound, Rejected

__all__ = [
    # Validation bounds (exported so tests / callers share one source of truth).
    "NAME_MIN_LEN",
    "NAME_MAX_LEN",
    "DESCRIPTION_MAX_LEN",
    "CATEGORY_NAME_MIN_LEN",
    "CATEGORY_NAME_MAX_LEN",
    "PRICE_MIN",
    "PRICE_MAX",
    "MOQ_MAX",
    "STOCK_MIN",
    "STOCK_MAX",
    # Stable availability-indicator codes (Req 3.2).
    "IN_STOCK",
    "OUT_OF_STOCK",
    # View / result types.
    "ProductDetail",
    "EmptyCatalog",
    "EmptyCategory",
    "CatalogService",
]

# --- validation bounds -----------------------------------------------------
NAME_MIN_LEN = 1
NAME_MAX_LEN = 100
DESCRIPTION_MAX_LEN = 1000
CATEGORY_NAME_MIN_LEN = 1
CATEGORY_NAME_MAX_LEN = 50

PRICE_MIN = Decimal("0")
PRICE_MAX = Decimal("9999999.99")
# MOQ is strictly greater than zero (Req 2.6) up to and including the max.
MOQ_MAX = Decimal("9999999")
STOCK_MIN = Decimal("0")
STOCK_MAX = Decimal("9999999")

# --- stable availability-indicator codes (Req 3.2) -------------------------
# These are *codes*, not user copy. "in stock" / "out of stock" wording is
# produced by the presentation layer; the domain only emits the stable code.
IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"

# Sentinel marking "this field was not supplied" for partial updates, so we can
# distinguish "leave unchanged" from an explicit ``None``.
_UNSET: Any = object()


@dataclass(frozen=True)
class ProductDetail:
    """A Customer-facing product-detail view (Req 3.2).

    Exposes the product's name, category (id + name), description, unit, price
    per unit, minimum order quantity, and a stable availability indicator
    (``IN_STOCK`` when stock > 0, ``OUT_OF_STOCK`` when stock == 0). The
    presentation layer renders ``stock_status`` as localized "in stock" /
    "out of stock" copy.
    """

    product_id: Any
    name: str
    category_id: Any
    category_name: Optional[str]
    description: Optional[str]
    unit: Unit
    price_per_unit: Decimal
    min_order_quantity: Decimal
    stock_quantity: Decimal
    available: bool
    stock_status: str


@dataclass(frozen=True)
class EmptyCatalog:
    """No available products exist anywhere (Req 3.5)."""

    code: str = "NO_PRODUCTS_AVAILABLE"


@dataclass(frozen=True)
class EmptyCategory:
    """A category does not exist, or has no available products (Req 3.6)."""

    category_id: Any = None
    code: str = "NO_PRODUCTS_IN_CATEGORY"


def _stock_status(stock_quantity: Decimal) -> str:
    """Map a stock quantity to the stable availability code (Req 3.2)."""
    return IN_STOCK if stock_quantity > 0 else OUT_OF_STOCK


def _coerce_unit(value: Any) -> Optional[Unit]:
    """Coerce ``value`` to a :class:`Unit`, or ``None`` if it is not valid.

    Accepts a :class:`Unit` directly or a (case-insensitive) string naming one
    of the members (``"KILOGRAM"``, ``"quintal"``, ...).
    """
    if isinstance(value, Unit):
        return value
    if isinstance(value, str):
        try:
            return Unit[value.strip().upper()]
        except KeyError:
            return None
    return None


def _coerce_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a numeric input to :class:`~decimal.Decimal`, else ``None``.

    Booleans and unparseable values yield ``None`` (treated as invalid by the
    caller). Uses the shared money converter so ``float`` inputs route through
    their string form and never inherit binary representation error.
    """
    try:
        return to_decimal(value)
    except (ValueError, ArithmeticError):
        return None


def _range_error(field: str, low: Any, high: Any, *, low_inclusive: bool) -> dict:
    """Build a structured out-of-range error describing the accepted range."""
    return {
        "field": field,
        "issue": "OUT_OF_RANGE",
        "min": str(low),
        "min_inclusive": low_inclusive,
        "max": str(high),
        "max_inclusive": True,
    }


class CatalogService:
    """Catalog domain service operating through a :class:`UnitOfWork`.

    The service is stateless apart from the injected unit of work; all reads and
    writes go through ``uow.catalog``. Mutating methods open the unit-of-work
    transaction and commit on success so a rejected request persists nothing.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # ------------------------------------------------------------------ categories
    def create_category(self, name: Any) -> Union[Category, Rejected]:
        """Create a category with a 1-50 char, case-insensitively unique name.

        Returns the created :class:`Category`, or :class:`Rejected` with:
          * ``CATEGORY_NAME_REQUIRED`` if the name is missing/blank,
          * ``CATEGORY_NAME_LENGTH_INVALID`` if it is outside 1-50 chars,
          * ``CATEGORY_NAME_EXISTS`` if it case-insensitively matches an
            existing category (Req 2.8/2.11).
        """
        if name is None or not isinstance(name, str) or name.strip() == "":
            return Rejected(
                code="CATEGORY_NAME_REQUIRED",
                reason="category name is required",
                details={"field": "name"},
            )

        cleaned = name.strip()
        if not (CATEGORY_NAME_MIN_LEN <= len(cleaned) <= CATEGORY_NAME_MAX_LEN):
            return Rejected(
                code="CATEGORY_NAME_LENGTH_INVALID",
                reason="category name length out of range",
                details={
                    "field": "name",
                    "min": CATEGORY_NAME_MIN_LEN,
                    "max": CATEGORY_NAME_MAX_LEN,
                },
            )

        with self._uow as uow:
            existing = uow.catalog.get_category_by_name(cleaned)
            if existing is not None:
                return Rejected(
                    code="CATEGORY_NAME_EXISTS",
                    reason="a category with this name already exists",
                    details={"field": "name", "existing_name": existing.name},
                )
            category = Category(category_id=new_id(), name=cleaned)
            stored = uow.catalog.add_category(category)
            uow.commit()
            return stored

    # ------------------------------------------------------------------ products
    def create_product(
        self,
        *,
        name: Any = None,
        category_id: Any = None,
        unit: Any = None,
        price_per_unit: Any = None,
        min_order_quantity: Any = None,
        stock_quantity: Any = None,
        description: Any = None,
        available: bool = True,
    ) -> Union[Product, Rejected]:
        """Create a product after validating all fields (Req 2.1/2.2/2.5/2.6/2.9/2.10).

        Required fields are ``name``, ``category_id``, ``unit``,
        ``price_per_unit`` and ``min_order_quantity``; a missing one yields
        ``MISSING_REQUIRED_FIELDS`` listing every absent field. Out-of-range or
        otherwise invalid values yield ``VALIDATION_FAILED`` whose
        ``details["errors"]`` lists each offending field with its accepted
        range. ``stock_quantity`` is optional and defaults to 0 (a zero-stock
        product is allowed to exist, Req 2.2).
        """
        # 1) Required-field presence (Req 2.10): identify *every* missing field.
        missing: list[str] = []
        if name is None or (isinstance(name, str) and name.strip() == ""):
            missing.append("name")
        if category_id is None:
            missing.append("category_id")
        if unit is None:
            missing.append("unit")
        if price_per_unit is None:
            missing.append("price_per_unit")
        if min_order_quantity is None:
            missing.append("min_order_quantity")
        if missing:
            return Rejected(
                code="MISSING_REQUIRED_FIELDS",
                reason="one or more required fields are missing",
                details={"fields": missing},
            )

        # 2) Field validation (collect *all* errors so the caller can surface
        #    every offending field with its accepted range).
        errors: list[dict] = []

        cleaned_name = name.strip() if isinstance(name, str) else name
        if not isinstance(cleaned_name, str) or not (
            NAME_MIN_LEN <= len(cleaned_name) <= NAME_MAX_LEN
        ):
            errors.append(
                {
                    "field": "name",
                    "issue": "LENGTH_INVALID",
                    "min": NAME_MIN_LEN,
                    "max": NAME_MAX_LEN,
                }
            )

        cleaned_description = description
        if description is not None:
            if not isinstance(description, str) or len(description) > DESCRIPTION_MAX_LEN:
                errors.append(
                    {
                        "field": "description",
                        "issue": "TOO_LONG",
                        "max": DESCRIPTION_MAX_LEN,
                    }
                )

        unit_value = _coerce_unit(unit)
        if unit_value is None:
            errors.append(
                {
                    "field": "unit",
                    "issue": "INVALID",
                    "accepted": [u.value for u in Unit],
                }
            )

        price_value = _coerce_decimal(price_per_unit)
        if price_value is None or not (PRICE_MIN <= price_value <= PRICE_MAX):
            errors.append(_range_error("price_per_unit", PRICE_MIN, PRICE_MAX, low_inclusive=True))

        moq_value = _coerce_decimal(min_order_quantity)
        # MOQ is strictly greater than zero (Req 2.6).
        if moq_value is None or not (moq_value > 0 and moq_value <= MOQ_MAX):
            errors.append(_range_error("min_order_quantity", 0, MOQ_MAX, low_inclusive=False))

        # Stock is optional; default to 0 when not supplied (Req 2.2).
        if stock_quantity is None:
            stock_value: Optional[Decimal] = Decimal("0")
        else:
            stock_value = _coerce_decimal(stock_quantity)
            if stock_value is None or not (STOCK_MIN <= stock_value <= STOCK_MAX):
                errors.append(_range_error("stock_quantity", STOCK_MIN, STOCK_MAX, low_inclusive=True))

        if errors:
            return Rejected(
                code="VALIDATION_FAILED",
                reason="one or more fields failed validation",
                details={"errors": errors},
            )

        with self._uow as uow:
            # Referential integrity: the category must exist (Req 2.1 requires a
            # Category). Reported as a validation rejection so nothing is created.
            category = uow.catalog.get_category(category_id)
            if category is None:
                return Rejected(
                    code="VALIDATION_FAILED",
                    reason="category does not exist",
                    details={"errors": [{"field": "category_id", "issue": "NOT_FOUND"}]},
                )

            product = Product(
                product_id=new_id(),
                name=cleaned_name,
                category_id=category_id,
                unit=unit_value,
                price_per_unit=price_value,
                min_order_quantity=moq_value,
                stock_quantity=stock_value,
                description=cleaned_description,
                available=available,
            )
            stored = uow.catalog.add_product(product)
            uow.commit()
            return stored

    def update_product(
        self,
        product_id: Any,
        *,
        name: Any = _UNSET,
        category_id: Any = _UNSET,
        unit: Any = _UNSET,
        price_per_unit: Any = _UNSET,
        min_order_quantity: Any = _UNSET,
        stock_quantity: Any = _UNSET,
        description: Any = _UNSET,
        available: Any = _UNSET,
    ) -> Union[Product, Rejected, NotFound]:
        """Update the supplied fields of an existing product (Req 2.3).

        Only fields actually passed are changed; each supplied value is
        validated against the same ranges as :meth:`create_product`. Returns the
        updated :class:`Product`, :class:`NotFound` if the product does not
        exist, or :class:`Rejected` (``VALIDATION_FAILED``) listing every
        offending field. A rejected update persists no change.
        """
        with self._uow as uow:
            product = uow.catalog.get_product(product_id)
            if product is None:
                return NotFound(entity="product", identifier=product_id)

            errors: list[dict] = []

            if name is not _UNSET:
                cleaned_name = name.strip() if isinstance(name, str) else name
                if not isinstance(cleaned_name, str) or not (
                    NAME_MIN_LEN <= len(cleaned_name) <= NAME_MAX_LEN
                ):
                    errors.append(
                        {"field": "name", "issue": "LENGTH_INVALID", "min": NAME_MIN_LEN, "max": NAME_MAX_LEN}
                    )
                else:
                    product.name = cleaned_name

            if description is not _UNSET:
                if description is not None and (
                    not isinstance(description, str) or len(description) > DESCRIPTION_MAX_LEN
                ):
                    errors.append({"field": "description", "issue": "TOO_LONG", "max": DESCRIPTION_MAX_LEN})
                else:
                    product.description = description

            if category_id is not _UNSET:
                category = uow.catalog.get_category(category_id)
                if category is None:
                    errors.append({"field": "category_id", "issue": "NOT_FOUND"})
                else:
                    product.category_id = category_id

            if unit is not _UNSET:
                unit_value = _coerce_unit(unit)
                if unit_value is None:
                    errors.append(
                        {"field": "unit", "issue": "INVALID", "accepted": [u.value for u in Unit]}
                    )
                else:
                    product.unit = unit_value

            if price_per_unit is not _UNSET:
                price_value = _coerce_decimal(price_per_unit)
                if price_value is None or not (PRICE_MIN <= price_value <= PRICE_MAX):
                    errors.append(_range_error("price_per_unit", PRICE_MIN, PRICE_MAX, low_inclusive=True))
                else:
                    product.price_per_unit = price_value

            if min_order_quantity is not _UNSET:
                moq_value = _coerce_decimal(min_order_quantity)
                if moq_value is None or not (moq_value > 0 and moq_value <= MOQ_MAX):
                    errors.append(_range_error("min_order_quantity", 0, MOQ_MAX, low_inclusive=False))
                else:
                    product.min_order_quantity = moq_value

            if stock_quantity is not _UNSET:
                stock_value = _coerce_decimal(stock_quantity)
                if stock_value is None or not (STOCK_MIN <= stock_value <= STOCK_MAX):
                    errors.append(_range_error("stock_quantity", STOCK_MIN, STOCK_MAX, low_inclusive=True))
                else:
                    product.stock_quantity = stock_value

            if available is not _UNSET:
                if not isinstance(available, bool):
                    errors.append({"field": "available", "issue": "INVALID"})
                else:
                    product.available = available

            if errors:
                # Roll back (leave the with-block without commit): no change persists.
                return Rejected(
                    code="VALIDATION_FAILED",
                    reason="one or more fields failed validation",
                    details={"errors": errors},
                )

            stored = uow.catalog.update_product(product)
            uow.commit()
            return stored

    # ------------------------------------------------------------------ availability
    def set_availability(
        self, product_id: Any, available: bool
    ) -> Union[Product, NotFound]:
        """Mark a product available/unavailable (Req 2.4).

        An unavailable product is excluded from customer browsing. Returns the
        updated product, or :class:`NotFound` if it does not exist.
        """
        with self._uow as uow:
            product = uow.catalog.get_product(product_id)
            if product is None:
                return NotFound(entity="product", identifier=product_id)
            product.available = bool(available)
            stored = uow.catalog.update_product(product)
            uow.commit()
            return stored

    # ------------------------------------------------------------------ browsing
    def list_available_grouped_by_category(
        self,
    ) -> Union[list[tuple[Category, list[Product]]], EmptyCatalog]:
        """Return available products grouped by category (Req 3.1).

        Only available products are included, and only categories that have at
        least one available product appear. When no available products exist
        anywhere, returns :class:`EmptyCatalog` (Req 3.5).
        """
        with self._uow as uow:
            available = [p for p in uow.catalog.list_products() if p.available]
            if not available:
                return EmptyCatalog()

            categories = {c.category_id: c for c in uow.catalog.list_categories()}
            grouped: dict[Any, list[Product]] = {}
            for product in available:
                grouped.setdefault(product.category_id, []).append(product)

            result: list[tuple[Category, list[Product]]] = []
            for category_id, products in grouped.items():
                category = categories.get(category_id)
                if category is None:
                    # Defensive: a product referencing a missing category still
                    # surfaces under a synthetic placeholder rather than vanishing.
                    category = Category(category_id=category_id, name="")
                result.append((category, products))
            return result

    def list_available_in_category(
        self, category_id: Any
    ) -> Union[list[Product], EmptyCategory]:
        """Return available products in one category (Req 3.4).

        Returns :class:`EmptyCategory` when the category does not exist or has no
        available products (Req 3.6).
        """
        with self._uow as uow:
            category = uow.catalog.get_category(category_id)
            if category is None:
                return EmptyCategory(category_id=category_id)
            available = [
                p
                for p in uow.catalog.list_products_in_category(category_id)
                if p.available
            ]
            if not available:
                return EmptyCategory(category_id=category_id)
            return available

    def get_product(self, product_id: Any) -> Union[ProductDetail, NotFound]:
        """Return a product-detail view (Req 3.2).

        Exposes name, category (id + name), description, unit, price, MOQ and a
        stable availability indicator (``IN_STOCK`` / ``OUT_OF_STOCK``). Returns
        :class:`NotFound` if the product does not exist.
        """
        with self._uow as uow:
            product = uow.catalog.get_product(product_id)
            if product is None:
                return NotFound(entity="product", identifier=product_id)
            category = uow.catalog.get_category(product.category_id)
            return ProductDetail(
                product_id=product.product_id,
                name=product.name,
                category_id=product.category_id,
                category_name=category.name if category is not None else None,
                description=product.description,
                unit=product.unit,
                price_per_unit=product.price_per_unit,
                min_order_quantity=product.min_order_quantity,
                stock_quantity=product.stock_quantity,
                available=product.available,
                stock_status=_stock_status(product.stock_quantity),
            )
