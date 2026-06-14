"""Cart_Service: per-Customer cart line items, quantity validation, totals.

This is the channel- and DB-agnostic Cart_Service described in design.md ->
``Cart_Service``. It reaches persistence **only** through the repository
protocols exposed by a :class:`~marketplace.domain.repositories.UnitOfWork`
(``uow.catalog`` and ``uow.carts``); it never imports the ORM or opens a
database connection (design.md -> Architectural Principles: "clear interfaces,
no cross-module DB poking"). The caller (the Bot_Interface) owns the
transaction boundary -- it opens the ``UnitOfWork`` for the inbound update and
commits/rolls back; this service performs the work inside that transaction and
never commits on its own.

Following the "stable status codes, never localized prose" rule (design.md ->
Bot_Interface), every business-rule refusal is returned as a typed
:class:`~marketplace.domain.results.Rejected` carrying a **stable code** plus
structured ``details`` (the offending MOQ / available stock) that the
presentation layer maps to a localized Message_Catalog template. The concrete
codes are:

* ``QTY_NOT_POSITIVE``    -- quantity must be greater than zero (Req 4.4/4.7).
* ``QTY_BELOW_MOQ``       -- quantity below the Product's MOQ (Req 4.2/4.7).
* ``QTY_EXCEEDS_STOCK``   -- quantity exceeds current stock (Req 4.3/4.7).
* ``PRODUCT_OUT_OF_STOCK``-- the Product's stock is exactly zero, so it cannot
  be added regardless of quantity or MOQ (Req 3.3).

Totals are computed with the shared ``Monetary_Rounding`` utility
(:mod:`marketplace.domain.money`): each line total is ``round(qty x price, 2)``
half-up and the cart total is the sum of the already-rounded line totals
(Req 4.9).

Requirements: 3.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Union

from marketplace.domain import money
from marketplace.domain.entities import Cart, CartItem, Product, Unit, new_id
from marketplace.domain.repositories import UnitOfWork
from marketplace.domain.results import NotFound, Rejected

__all__ = [
    "CartService",
    "CartLineView",
    "CartView",
    "QTY_NOT_POSITIVE",
    "QTY_BELOW_MOQ",
    "QTY_EXCEEDS_STOCK",
    "PRODUCT_OUT_OF_STOCK",
]

# --- Stable status codes (language-agnostic; localized by the Bot_Interface) --
QTY_NOT_POSITIVE = "QTY_NOT_POSITIVE"
QTY_BELOW_MOQ = "QTY_BELOW_MOQ"
QTY_EXCEEDS_STOCK = "QTY_EXCEEDS_STOCK"
PRODUCT_OUT_OF_STOCK = "PRODUCT_OUT_OF_STOCK"

_ZERO = Decimal("0")


@dataclass(frozen=True)
class CartLineView:
    """A single rendered cart line (Req 4.9).

    Holds the data the presentation layer needs to display one line: the
    Product's name and Unit, the line quantity, the snapshot ``unit_price`` used
    for the math, and the ``line_total`` = ``round(quantity x unit_price, 2)``
    half-up (computed via the Monetary_Rounding utility).
    """

    product_id: uuid.UUID
    name: str
    quantity: Decimal
    unit: Unit
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class CartView:
    """A rendered cart: its line views plus the rounded cart total (Req 4.9).

    ``total`` is the sum of the **already-rounded** line totals, so the displayed
    line totals always add up exactly to the total (Monetary_Rounding).
    """

    line_items: list[CartLineView] = field(default_factory=list)
    total: Decimal = field(default_factory=lambda: Decimal("0.00"))


# A cart operation returns the updated cart, a typed rejection, or NotFound.
CartResult = Union[Cart, Rejected, NotFound]


class CartService:
    """Cart operations bound to a single :class:`UnitOfWork` (one per request).

    The service is constructed with the active ``UnitOfWork`` and uses only its
    ``catalog`` and ``carts`` repositories. Mutating operations leave the
    transaction open for the caller to commit; a rejected operation makes no
    persistent change so the existing cart/line is retained verbatim.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # ------------------------------------------------------------------ add
    def add_item(self, customer_id, product_id, qty) -> CartResult:
        """Add ``qty`` of ``product_id`` to ``customer_id``'s cart (Req 4.1-4.5).

        Creates the customer's cart on first use. The **input** quantity must be
        greater than zero (Req 4.4). If the product already has a line, the
        added quantity is combined with the existing quantity and the
        **combined** value is re-validated against the Product's MOQ and current
        stock (Req 4.5). A zero-stock product is never addable (Req 3.3). On any
        rejection nothing is persisted.
        """
        product = self._uow.catalog.get_product(product_id)
        if product is None:
            return NotFound("product", product_id)

        qty_d = money.to_decimal(qty)
        if qty_d <= _ZERO:
            return Rejected(
                QTY_NOT_POSITIVE,
                reason="quantity must be greater than zero",
                details={"product_id": product_id},
            )

        cart = self._uow.carts.get_by_customer(customer_id)
        existing = self._find_line(cart, product_id) if cart is not None else None
        existing_qty = existing.quantity if existing is not None else _ZERO
        combined = existing_qty + qty_d

        # Combine-on-add THEN re-validate the combined quantity (Req 4.5).
        rejection = self._validate_quantity(product, combined)
        if rejection is not None:
            return rejection

        # Success: create the cart if needed and store the combined quantity.
        is_new = cart is None
        if cart is None:
            cart = Cart(cart_id=new_id(), customer_id=customer_id, items=[])

        line = self._find_line(cart, product_id)
        if line is not None:
            line.quantity = combined
        else:
            cart.items.append(CartItem(product_id=product_id, quantity=combined))

        return self._uow.carts.add(cart) if is_new else self._uow.carts.update(cart)

    # --------------------------------------------------------------- change
    def change_qty(self, customer_id, product_id, new_qty) -> CartResult:
        """Set an existing line's quantity to ``new_qty`` (Req 4.6/4.7).

        ``new_qty`` is validated exactly like an add (``> 0``, ``>= MOQ``,
        ``<= current stock``). On any rejection the line's existing quantity is
        retained unchanged (nothing is persisted). Returns ``NotFound`` if the
        product or the cart line does not exist.
        """
        product = self._uow.catalog.get_product(product_id)
        if product is None:
            return NotFound("product", product_id)

        cart = self._uow.carts.get_by_customer(customer_id)
        line = self._find_line(cart, product_id) if cart is not None else None
        if line is None:
            return NotFound("cart_item", product_id)

        new_qty_d = money.to_decimal(new_qty)
        if new_qty_d <= _ZERO:
            return Rejected(
                QTY_NOT_POSITIVE,
                reason="quantity must be greater than zero",
                details={"product_id": product_id},
            )

        rejection = self._validate_quantity(product, new_qty_d)
        if rejection is not None:
            # Retain the existing line unchanged (Req 4.7) -- persist nothing.
            return rejection

        line.quantity = new_qty_d
        return self._uow.carts.update(cart)

    # --------------------------------------------------------------- remove
    def remove_item(self, customer_id, product_id) -> Cart:
        """Delete ``product_id``'s line from the customer's cart (Req 4.8).

        Idempotent: removing a product that is not in the cart (or when the
        customer has no cart yet) is a no-op that returns the current/empty cart.
        """
        cart = self._uow.carts.get_by_customer(customer_id)
        if cart is None:
            return Cart(cart_id=new_id(), customer_id=customer_id, items=[])

        before = len(cart.items)
        cart.items = [item for item in cart.items if item.product_id != product_id]
        if len(cart.items) != before:
            return self._uow.carts.update(cart)
        return cart

    # ----------------------------------------------------------------- view
    def view(self, customer_id) -> CartView:
        """Render the customer's cart with per-line and cart totals (Req 4.9).

        Each line total is ``round(quantity x unit_price, 2)`` half-up and the
        cart total is the sum of the rounded line totals, both computed via the
        Monetary_Rounding utility. Lines whose product no longer exists are
        skipped defensively.
        """
        cart = self._uow.carts.get_by_customer(customer_id)
        if cart is None:
            return CartView(line_items=[], total=Decimal("0.00"))

        line_views: list[CartLineView] = []
        for item in cart.items:
            product = self._uow.catalog.get_product(item.product_id)
            if product is None:
                continue
            line_total = money.line_amount(item.quantity, product.price_per_unit)
            line_views.append(
                CartLineView(
                    product_id=item.product_id,
                    name=product.name,
                    quantity=item.quantity,
                    unit=product.unit,
                    unit_price=product.price_per_unit,
                    line_total=line_total,
                )
            )

        total = money.order_total(lv.line_total for lv in line_views)
        return CartView(line_items=line_views, total=total)

    # ---------------------------------------------------------------- clear
    def clear(self, customer_id) -> None:
        """Empty the customer's cart (used by order placement).

        Deletes the cart and its line items if one exists; a no-op otherwise.
        """
        cart = self._uow.carts.get_by_customer(customer_id)
        if cart is not None:
            self._uow.carts.delete(cart.cart_id)
        return None

    # ------------------------------------------------------------- internals
    @staticmethod
    def _find_line(cart: Optional[Cart], product_id) -> Optional[CartItem]:
        """Return the cart's line item for ``product_id``, or ``None``."""
        if cart is None:
            return None
        for item in cart.items:
            if item.product_id == product_id:
                return item
        return None

    @staticmethod
    def _validate_quantity(product: Product, qty: Decimal) -> Optional[Rejected]:
        """Validate ``qty`` (assumed ``> 0``) against MOQ and stock.

        Order of checks honours Req 3.3: a zero-stock product is rejected with
        ``PRODUCT_OUT_OF_STOCK`` regardless of quantity or MOQ, before the
        MOQ/stock comparisons. Returns ``None`` when the quantity is acceptable.
        """
        if product.stock_quantity == _ZERO:
            return Rejected(
                PRODUCT_OUT_OF_STOCK,
                reason="product is out of stock",
                details={"product_id": product.product_id, "stock": _ZERO},
            )
        if qty < product.min_order_quantity:
            return Rejected(
                QTY_BELOW_MOQ,
                reason="quantity below minimum order quantity",
                details={
                    "product_id": product.product_id,
                    "moq": product.min_order_quantity,
                },
            )
        if qty > product.stock_quantity:
            return Rejected(
                QTY_EXCEEDS_STOCK,
                reason="quantity exceeds available stock",
                details={
                    "product_id": product.product_id,
                    "stock": product.stock_quantity,
                },
            )
        return None
