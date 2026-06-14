"""Unit tests for the Monetary_Rounding utility (Task 4.4).

Covers the design's "Domain Rules: Monetary Rounding" section and the
``Monetary_Rounding`` glossary term:

* half-up behaviour at the third decimal (x.xx5 rounds up);
* a third decimal below 5 rounds down;
* fractional quantities (kg / quintal / bag) producing sub-paisa products;
* ``order_total`` equals the sum of the **already-rounded** line amounts
  (round each line first, then add) -- not a single end rounding.

Requirements: 4.9, 5.2, 15.11.
"""

from decimal import Decimal

import pytest

from marketplace.domain.money import (
    MONEY_QUANTUM,
    line_amount,
    order_total,
    round_money,
    to_decimal,
)


# ---------------------------------------------------------------------------
# round_money: half-up behaviour at the third decimal place
# ---------------------------------------------------------------------------
class TestRoundMoneyHalfUp:
    @pytest.mark.parametrize(
        "value,expected",
        [
            # Exactly .005 at the third decimal rounds the second decimal UP.
            ("0.005", "0.01"),
            ("1.005", "1.01"),
            ("2.345", "2.35"),
            ("10.675", "10.68"),
            ("0.015", "0.02"),
            # Greater than 5 at the third decimal also rounds up.
            ("0.006", "0.01"),
            ("2.999", "3.00"),
        ],
    )
    def test_third_decimal_five_or_more_rounds_up(self, value, expected):
        assert round_money(value) == Decimal(expected)

    @pytest.mark.parametrize(
        "value,expected",
        [
            # Below 5 at the third decimal rounds DOWN (truncates the paisa up-tick).
            ("0.004", "0.00"),
            ("1.004", "1.00"),
            ("2.344", "2.34"),
            ("10.671", "10.67"),
            ("0.0149", "0.01"),
            ("99.994", "99.99"),
        ],
    )
    def test_third_decimal_below_five_rounds_down(self, value, expected):
        assert round_money(value) == Decimal(expected)

    def test_result_always_quantized_to_two_places(self):
        result = round_money("5")
        assert result == Decimal("5.00")
        # exponent of -2 means exactly two decimal places.
        assert result.as_tuple().exponent == -2

    def test_already_two_places_is_unchanged(self):
        assert round_money(Decimal("12.34")) == Decimal("12.34")


# ---------------------------------------------------------------------------
# to_decimal: float inputs must not inherit binary representation error
# ---------------------------------------------------------------------------
class TestToDecimal:
    def test_float_is_converted_via_string_form(self):
        # If we did Decimal(0.1) directly we'd get 0.1000000000000000055...;
        # routing through str() yields the literal the caller wrote.
        assert to_decimal(0.1) == Decimal("0.1")

    def test_int_is_exact(self):
        assert to_decimal(7) == Decimal("7")

    def test_decimal_passthrough(self):
        d = Decimal("3.14159")
        assert to_decimal(d) is d

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            to_decimal(True)

    def test_bad_string_rejected(self):
        with pytest.raises(ValueError):
            to_decimal("not-a-number")


# ---------------------------------------------------------------------------
# line_amount: round(quantity x unit_price, 2) half-up, incl. fractional qty
# ---------------------------------------------------------------------------
class TestLineAmount:
    def test_simple_whole_quantity(self):
        assert line_amount(3, "10.00") == Decimal("30.00")

    def test_half_up_at_third_decimal_of_product(self):
        # 1 x 2.345 = 2.345 -> 2.35 (third decimal is 5 -> up).
        assert line_amount(1, "2.345") == Decimal("2.35")

    def test_fractional_kg_quantity_sub_paisa_product_rounds_up(self):
        # 2.5 kg x 10.01 = 25.025 -> 25.03 (third decimal 5 -> up).
        assert line_amount("2.5", "10.01") == Decimal("25.03")

    def test_fractional_quintal_quantity_sub_paisa_product_rounds_down(self):
        # 1.5 quintal x 100.001 = 150.0015 -> 150.00 (fourth decimal irrelevant,
        # third decimal is 1 -> down).
        assert line_amount("1.5", "100.001") == Decimal("150.00")

    def test_fractional_bag_quantity_with_exact_half_paisa(self):
        # 0.5 bag x 0.01 = 0.005 -> 0.01 (exact half-paisa rounds up).
        assert line_amount("0.5", "0.01") == Decimal("0.01")

    def test_three_decimal_quantity_precision_preserved_before_rounding(self):
        # 0.333 x 3.00 = 0.999 -> 1.00 (product computed exactly, then rounded).
        assert line_amount("0.333", "3.00") == Decimal("1.00")

    def test_float_inputs_do_not_leak_binary_error(self):
        # 0.1 x 3 should be 0.30, not 0.30000000000000004.
        assert line_amount(0.1, 3) == Decimal("0.30")

    def test_zero_quantity(self):
        assert line_amount(0, "999.99") == Decimal("0.00")


# ---------------------------------------------------------------------------
# order_total: sum of ALREADY-ROUNDED line amounts (round each, then add)
# ---------------------------------------------------------------------------
class TestOrderTotal:
    def test_empty_is_zero(self):
        assert order_total([]) == Decimal("0.00")

    def test_sum_of_rounded_lines(self):
        lines = [Decimal("10.00"), Decimal("5.55"), Decimal("0.45")]
        assert order_total(lines) == Decimal("16.00")

    def test_rounds_each_line_before_summing(self):
        # Two lines each at .005 -> each rounds to 0.01 BEFORE summing => 0.02.
        # A single end-rounding of 0.005 + 0.005 = 0.010 would give 0.01, which
        # would be WRONG per the round-each-line-first rule.
        assert order_total(["0.005", "0.005"]) == Decimal("0.02")

    def test_total_equals_sum_of_rounded_line_amounts_not_end_rounding(self):
        # Build lines from quantity x price; the documented behaviour is that the
        # total equals the sum of the per-line rounded amounts.
        specs = [("2.5", "10.01"), ("1.5", "100.001"), ("0.333", "3.00")]
        rounded_lines = [line_amount(q, p) for q, p in specs]
        # rounded lines: 25.03, 150.00, 1.00
        assert rounded_lines == [Decimal("25.03"), Decimal("150.00"), Decimal("1.00")]
        assert order_total(rounded_lines) == Decimal("176.03")
        # And the total is exactly the arithmetic sum of the displayed lines.
        assert order_total(rounded_lines) == sum(rounded_lines)

    def test_displayed_lines_sum_exactly_to_total(self):
        # Regression guard: end-rounding an unrounded product would diverge.
        specs = [("0.1", "0.1"), ("0.1", "0.1"), ("0.1", "0.1")]
        # each exact product = 0.01 -> rounds to 0.01; total = 0.03
        rounded_lines = [line_amount(q, p) for q, p in specs]
        total = order_total(rounded_lines)
        assert total == Decimal("0.03")
        assert sum(rounded_lines) == total

    def test_result_is_quantized_to_two_places(self):
        total = order_total([Decimal("1"), Decimal("2")])
        assert total == Decimal("3.00")
        assert total.as_tuple().exponent == -2


def test_money_quantum_is_one_paisa():
    assert MONEY_QUANTUM == Decimal("0.01")
