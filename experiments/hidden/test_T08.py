from decimal import Decimal

import pytest

from shoplib.models import CartItem, Product
from shoplib.tax import calculate_tax

FOOD = Product("RICE", "Rice", Decimal("333"), "food")
BOOK = Product("BOOK", "Book", Decimal("1099"), "book")
PEN = Product("PEN", "Pen", Decimal("105"), "stationery")
RATES = {"food": Decimal("0.08"), "book": Decimal("0.10")}


def test_per_line_round_down_and_default():
    items = [CartItem(FOOD, 1), CartItem(BOOK, 1), CartItem(PEN, 1)]
    result = calculate_tax(items, RATES)
    # food: 26.64 -> 26, book: 109.9 -> 109, pen(default 10%): 10.5 -> 10
    assert result.subtotal == Decimal("1537")
    assert result.tax == Decimal("145")
    assert result.total == Decimal("1682")


def test_line_level_not_total_level():
    items = [CartItem(PEN, 1), CartItem(Product("PEN2", "Pen2", Decimal("105"), "stationery"), 1)]
    assert calculate_tax(items, {}).tax == Decimal("20")  # 10 + 10, not 21


def test_quantity_in_line():
    assert calculate_tax([CartItem(FOOD, 3)], RATES).tax == Decimal("79")  # 999 * 0.08 = 79.92


@pytest.mark.parametrize("rates, default", [({"food": Decimal("-0.1")}, Decimal("0.1")),
                                            ({"food": Decimal("1.5")}, Decimal("0.1")),
                                            ({}, Decimal("-0.01"))])
def test_invalid_rates(rates, default):
    with pytest.raises(ValueError):
        calculate_tax([CartItem(FOOD, 1)], rates, default)


def test_empty():
    result = calculate_tax([], RATES)
    assert (result.subtotal, result.tax, result.total) == (0, 0, 0)
