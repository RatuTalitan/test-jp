"""Fulfillability Engine (task 13.1, design.md -> Fulfillability Engine).

This module implements the **pure** fulfillability computation described in
design.md and the on-read recompute helper required by Requirement 16:

* :func:`is_fulfillable` -- an Order is **fully Fulfillable** WHEN every line
  item's ordered quantity is ``<=`` the corresponding Product's current stock
  quantity (Req 16.2).
* :func:`shortfalls` -- the list of lines whose ordered quantity **exceeds**
  current stock, each reported as ``(product_id, ordered_quantity,
  current_stock)`` (Req 16.2/16.3).
* :func:`recompute_for_product_change` -- given a Product whose stock changed,
  recompute fulfillability for **every not-yet-Approved Order** (states
  ``PLACED``, ``PAYMENT_PENDING``, ``PAYMENT_SUBMITTED``, ``PAYMENT_VERIFIED``)
  that contains that Product, reading current stock through the repository
  protocols (Req 16.1).

### Design alignment

* **Pure & language-agnostic.** :func:`is_fulfillable` and :func:`shortfalls`
  take an explicit ``stock_snapshot`` mapping (``product_id -> current stock``)
  rather than touching persistence, so they are deterministic and trivially
  testable. They return data / stable structures, never localized prose, so the
  Admin_Console (task 20.1) can render flags and shortfalls via the
  Message_Catalog (design.md -> Bot_Interface).
* **Computed on read, never stored.** :func:`recompute_for_product_change`
  derives stock snapshots from the **current** catalog on every call and
  returns the result; nothing is persisted, so fulfillability can never become
  stale relative to stock (design.md -> Fulfillability Engine; Req 16.1).
* **No cross-module DB poking.** The recompute helper reaches persistence only
  through the :class:`~marketplace.domain.repositories.UnitOfWork` repository
  protocols (``uow.orders.list_by_product`` and ``uow.catalog.get_product``);
  it never imports the ORM or opens a connection (design.md -> Architectural
  Principles). The caller owns the transaction boundary.

Requirements: 16.1, 16.2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from marketplace.domain.entities import Order, OrderState
from marketplace.domain.repositories import UnitOfWork

__all__ = [
    "Shortfall",
    "OrderFulfillability",
    "NOT_YET_APPROVED_STATES",
    "is_fulfillable",
    "shortfalls",
    "recompute_for_product_change",
]

_ZERO = Decimal("0")

# The not-yet-Approved Order_States for which fulfillability is recomputed when
# a Product's stock changes (Req 16.1). Note this is the set explicitly named in
# Req 16.1 -- it deliberately EXCLUDES APPROVED (an Approved Order has already
# had its stock decremented, so it is not a competing pending Order).
NOT_YET_APPROVED_STATES = frozenset(
    {
        OrderState.PLACED,
        OrderState.PAYMENT_PENDING,
        OrderState.PAYMENT_SUBMITTED,
        OrderState.PAYMENT_VERIFIED,
    }
)


@dataclass(frozen=True)
class Shortfall:
    """A single line item that cannot be met from current stock (Req 16.2/16.3).

    ``ordered_quantity`` is the line's ordered quantity and ``current_stock`` is
    the affected Product's current stock quantity; the implied shortfall amount
    is ``ordered_quantity - current_stock`` (always ``> 0`` for a reported
    line, matching the Fulfillable glossary term).
    """

    product_id: uuid.UUID
    ordered_quantity: Decimal
    current_stock: Decimal


@dataclass(frozen=True)
class OrderFulfillability:
    """The recomputed fulfillability of one Order (Req 16.1/16.2).

    Returned by :func:`recompute_for_product_change` for each affected Order.
    ``is_fulfillable`` is ``True`` iff ``shortfalls`` is empty.
    """

    order_id: uuid.UUID
    state: OrderState
    is_fulfillable: bool
    shortfalls: tuple[Shortfall, ...]


def _stock_of(stock_snapshot: Mapping, product_id) -> Decimal:
    """Current stock for ``product_id`` from the snapshot (missing => zero).

    Treating an absent Product as zero stock is the safe default: a line whose
    Product is unknown to the snapshot cannot be fulfilled, so it surfaces as a
    shortfall rather than being silently considered fulfillable.
    """

    value = stock_snapshot.get(product_id, _ZERO)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def is_fulfillable(order: Order, stock_snapshot: Mapping) -> bool:
    """Return ``True`` iff every line is met from current stock (Req 16.2).

    An Order is fully Fulfillable WHEN, for every line item, the line item's
    ``ordered_quantity`` is less than or equal to the corresponding Product's
    current stock quantity in ``stock_snapshot`` (a ``product_id -> stock``
    mapping). The equality boundary (``ordered_quantity == current_stock``) is
    Fulfillable. An Order with no line items is vacuously Fulfillable.
    """

    return all(
        item.ordered_quantity <= _stock_of(stock_snapshot, item.product_id)
        for item in order.items
    )


def shortfalls(order: Order, stock_snapshot: Mapping) -> list[Shortfall]:
    """Return one :class:`Shortfall` per line that exceeds stock (Req 16.2/16.3).

    For each line item whose ``ordered_quantity`` is strictly greater than the
    corresponding Product's current stock in ``stock_snapshot``, emit a
    :class:`Shortfall` carrying the ``product_id``, the ``ordered_quantity`` and
    the ``current_stock``. The result is empty exactly when the Order
    :func:`is_fulfillable`.
    """

    result: list[Shortfall] = []
    for item in order.items:
        current = _stock_of(stock_snapshot, item.product_id)
        if item.ordered_quantity > current:
            result.append(
                Shortfall(
                    product_id=item.product_id,
                    ordered_quantity=item.ordered_quantity,
                    current_stock=current,
                )
            )
    return result


def _stock_snapshot_for_order(uow: UnitOfWork, order: Order) -> dict:
    """Build a ``product_id -> current stock`` snapshot for one Order.

    Reads the **current** stock of every Product referenced by the Order's lines
    through the catalog repository, so the fulfillability computed from it
    reflects the live catalog at call time (computed on read; Req 16.1). A
    Product that no longer exists contributes zero stock.
    """

    snapshot: dict = {}
    for item in order.items:
        if item.product_id in snapshot:
            continue
        product = uow.catalog.get_product(item.product_id)
        snapshot[item.product_id] = product.stock_quantity if product is not None else _ZERO
    return snapshot


def recompute_for_product_change(
    uow: UnitOfWork, product_id
) -> list[OrderFulfillability]:
    """Recompute fulfillability for every competing pending Order (Req 16.1).

    When a Product's stock changes (an approval-time decrement, a stock return
    from a modification/cancellation, or a Seller stock edit), this recomputes
    -- **on read, never stored** -- whether each not-yet-Approved Order
    (``PLACED``/``PAYMENT_PENDING``/``PAYMENT_SUBMITTED``/``PAYMENT_VERIFIED``)
    that contains the Product is fully Fulfillable from current stock.

    It selects candidate Orders via ``uow.orders.list_by_product`` and filters
    to :data:`NOT_YET_APPROVED_STATES`, then computes each Order's
    fulfillability against a fresh snapshot of its products' current stock.
    Returns one :class:`OrderFulfillability` per affected Order (Orders not in a
    not-yet-Approved state are skipped).
    """

    results: list[OrderFulfillability] = []
    for order in uow.orders.list_by_product(product_id):
        if order.state not in NOT_YET_APPROVED_STATES:
            continue
        snapshot = _stock_snapshot_for_order(uow, order)
        order_shortfalls = tuple(shortfalls(order, snapshot))
        results.append(
            OrderFulfillability(
                order_id=order.order_id,
                state=order.state,
                is_fulfillable=not order_shortfalls,
                shortfalls=order_shortfalls,
            )
        )
    return results
