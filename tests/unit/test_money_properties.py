"""Scoped Hypothesis properties for the Monetary_Rounding utility (Task 4.4).

These exercise universal invariants of the rounding utility itself across many
inputs. They are intentionally **scoped to the utility** -- the formal numbered
correctness properties (Property 10: cart/order totals, Property 25:
modification recomputation) are implemented by separate later tasks (10.5,
16.6) against the Cart/Order services and are not duplicated here.

Requirements: 4.9, 5.2, 15.11.
"""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marketplace.domain.money import line_amount, order_total, round_money

# Money-ish and quantity-ish generators kept inside sane spec ranges
# (design: price in [0, 9_999_999.99], quantity fractional to 3 dp).
prices = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("9999999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)
quantities = st.decimals(
    min_value=Decimal("0.001"),
    max_value=Decimal("9999999"),
    allow_nan=False,
    allow_infinity=False,
    places=3,
)
money_values = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("9999999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)


@pytest.mark.property
@given(
    value=st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("9999999.99"),
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_round_money_is_idempotent(value):
    """Rounding an already-rounded amount changes nothing.

    Generated within the spec money range (NUMERIC(12,2): non-negative, at most
    9_999_999.99), matching the other money property generators in this file.
    ``places`` is intentionally left unconstrained here so that inputs with a
    third (and beyond) decimal digit are exercised -- that is exactly what the
    half-up rounding primitive must collapse to two places idempotently.
    """
    once = round_money(value)
    assert round_money(once) == once
    # And the result always has exactly two decimal places.
    assert once.as_tuple().exponent == -2


@pytest.mark.property
@given(quantity=quantities, unit_price=prices)
def test_line_amount_always_two_places_and_nonnegative(quantity, unit_price):
    """Any non-negative quantity x price yields a two-place, non-negative amount."""
    amount = line_amount(quantity, unit_price)
    assert amount >= Decimal("0.00")
    assert amount.as_tuple().exponent == -2


@pytest.mark.property
@given(lines=st.lists(money_values, max_size=20))
def test_order_total_equals_sum_of_rounded_lines(lines):
    """order_total equals the plain sum of the already-rounded line amounts.

    This is the round-each-line-first-then-add rule at the utility level: the
    displayed (rounded) line amounts always add up exactly to the total.
    """
    rounded = [round_money(x) for x in lines]
    assert order_total(lines) == sum(rounded, Decimal("0.00"))


@pytest.mark.property
@given(lines=st.lists(money_values, max_size=20))
def test_order_total_two_places(lines):
    """The total is always normalized to exactly two decimal places."""
    assert order_total(lines).as_tuple().exponent == -2
