from decimal import Decimal

import pytest

from shoplib.formatting import format_money


@pytest.mark.parametrize("amount, expected", [
    (Decimal("1234.5"), "¥1,235"),
    (Decimal("0"), "¥0"),
    (Decimal("1000000"), "¥1,000,000"),
    (Decimal("0.4"), "¥0"),
    (1500, "¥1,500"),
])
def test_jpy(amount, expected):
    assert format_money(amount) == expected
    assert format_money(amount, "JPY") == expected


@pytest.mark.parametrize("amount, expected", [
    (Decimal("1234.5"), "$1,234.50"),
    (Decimal("0.005"), "$0.01"),
    (Decimal("999999.999"), "$1,000,000.00"),
])
def test_usd(amount, expected):
    assert format_money(amount, "USD") == expected


def test_negative():
    assert format_money(Decimal("-1234"), "JPY") == "-¥1,234"
    assert format_money(Decimal("-0.5"), "USD") == "-$0.50"


def test_unknown_currency():
    with pytest.raises(ValueError):
        format_money(Decimal("1"), "EUR")


def test_float_rejected():
    with pytest.raises(TypeError):
        format_money(1.5)
