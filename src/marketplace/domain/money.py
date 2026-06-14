"""Monetary_Rounding utility (single source of truth for money math).

This module implements the ``Monetary_Rounding`` rule defined in the
requirements glossary and the design's "Domain Rules: Monetary Rounding"
section. It is the **single** shared utility used by Cart totals (Task 8.1),
Order placement amounts (Task 10.2), and modification recalculation
(Task 16.x), so that all three stay consistent.

Rules implemented here
-----------------------
* All monetary amounts are Indian rupees expressed to two decimal places
  (paise).
* **Line amount** = ``round(quantity x unit_price, 2)`` using **half-up**
  rounding: a third-decimal digit of 5 or greater rounds the second decimal
  up; a digit below 5 rounds it down.
* **Cart / order total** = the **sum of the already-rounded line amounts**
  (round each line first, *then* add). Totals are never computed from an
  unrounded product and rounded once at the end, so the displayed line
  amounts always add up exactly to the total.
* All arithmetic uses :class:`decimal.Decimal` with ``ROUND_HALF_UP`` --
  **never** binary ``float`` -- to avoid floating-point representation error.
  Inputs (``unit_price``, ``quantity``) are converted to ``Decimal`` *before*
  multiplication and the result is quantized to two places.

Requirements: 4.9, 5.2, 15.11.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Iterable, Union

__all__ = [
    "MONEY_QUANTUM",
    "TWO_PLACES",
    "Numeric",
    "to_decimal",
    "round_money",
    "line_amount",
    "order_total",
]

# A value acceptable as a monetary/quantity input. ``float`` is accepted only
# as a convenience and is converted through its string form so we never inherit
# binary floating-point representation error (e.g. ``0.1 + 0.2``).
Numeric = Union[Decimal, int, str, float]

# The smallest representable money unit: two decimal places (one paisa).
MONEY_QUANTUM = Decimal("0.01")

# Backwards-friendly alias used in some call sites / docs.
TWO_PLACES = MONEY_QUANTUM


def to_decimal(value: Numeric) -> Decimal:
    """Convert a numeric input to an exact :class:`~decimal.Decimal`.

    ``int``, ``str`` and ``Decimal`` values are converted exactly. A ``float``
    is converted via its ``repr`` (``str(value)``) rather than directly, so the
    result reflects the literal the caller wrote (``0.1`` -> ``Decimal("0.1")``)
    instead of the binary-float expansion. This keeps the whole pipeline free of
    binary ``float`` representation error, per the design.

    Raises:
        ValueError: if ``value`` cannot be parsed as a decimal number.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool is an int subclass; reject it explicitly to avoid silly inputs.
        raise ValueError(f"bool is not a valid monetary/quantity value: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Route through str() so 0.1 -> Decimal("0.1"), not the binary expansion.
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception as exc:  # noqa: BLE001 - re-raise as a clear ValueError
            raise ValueError(f"not a valid decimal string: {value!r}") from exc
    raise ValueError(f"unsupported numeric type: {type(value).__name__}")


def round_money(value: Numeric) -> Decimal:
    """Round ``value`` to two decimal places (paise) using half-up.

    This is the lowest-level primitive of the Monetary_Rounding rule: a digit of
    5 or greater at the third decimal place rounds the second decimal place up;
    a digit below 5 rounds it down. The result is always quantized to exactly
    two decimal places (e.g. ``Decimal("5.00")``).
    """
    amount = to_decimal(value)
    # Use a local context so the global decimal context is never mutated and the
    # rounding mode is unambiguous regardless of caller configuration.
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        # quantize() raises decimal.InvalidOperation when the quantized result
        # would need more significant digits than the context allows (the
        # default is 28). A large-but-finite in-range amount -- or a large
        # intermediate product handed down from line_amount -- can exceed that.
        # The quantized value spans from the most-significant digit (position
        # ``adjusted()``) down to the 1e-2 paisa place, i.e. ``adjusted() + 3``
        # significant digits. Widen the local precision to comfortably cover
        # that so quantize() never raises for a reasonable Decimal. The
        # ROUND_HALF_UP semantics are unchanged.
        ctx.prec = max(ctx.prec, amount.adjusted() + 4)
        return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def line_amount(quantity: Numeric, unit_price: Numeric) -> Decimal:
    """Compute one line's amount: ``round(quantity x unit_price, 2)`` half-up.

    ``quantity`` (which may be fractional for kg / quintal / bag units) and
    ``unit_price`` are both converted to :class:`~decimal.Decimal` *before*
    multiplication; the exact product is then rounded to two places half-up.
    Computing the product exactly first and rounding once captures sub-paisa
    products correctly (e.g. ``2.5 x 10.001`` rounds the exact ``25.0025`` to
    ``25.00``).
    """
    qty = to_decimal(quantity)
    price = to_decimal(unit_price)
    with localcontext() as ctx:
        # Widen precision so the exact product is preserved before quantizing.
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_UP
        product = qty * price
    return round_money(product)


def order_total(line_amounts: Iterable[Numeric]) -> Decimal:
    """Sum **already-rounded** line amounts into a cart / order total.

    Each element is rounded to two places (half-up) before being added, so the
    total equals the sum of the displayed line amounts exactly -- never a single
    end-rounding of an unrounded sum. An empty iterable yields ``Decimal("0.00")``.
    """
    total = Decimal("0.00")
    for amount in line_amounts:
        total += round_money(amount)
    # The sum of two-place decimals is already two-place, but quantize defensively
    # so the return value is always normalized to exactly two decimal places.
    return round_money(total)
