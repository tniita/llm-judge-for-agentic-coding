from datetime import datetime
from decimal import Decimal

import pytest

from shoplib.pricing import Coupon, CouponError, apply_coupon

NOW = datetime(2026, 10, 1, 12, 0)


def test_percent():
    c = Coupon("P10", "percent", Decimal("10"))
    assert apply_coupon(Decimal("1000"), c, NOW) == Decimal("900.00")


def test_percent_rounding_half_up():
    c = Coupon("P15", "percent", Decimal("15"))
    assert apply_coupon(Decimal("0.10"), c, NOW) == Decimal("0.09")  # 0.085 -> 0.09


def test_fixed_not_below_zero():
    c = Coupon("F500", "fixed", Decimal("500"))
    assert apply_coupon(Decimal("1200"), c, NOW) == Decimal("700")
    assert apply_coupon(Decimal("300"), c, NOW) == Decimal("0")


def test_min_subtotal():
    c = Coupon("MIN", "fixed", Decimal("100"), min_subtotal=Decimal("1000"))
    with pytest.raises(CouponError):
        apply_coupon(Decimal("999"), c, NOW)
    assert apply_coupon(Decimal("1000"), c, NOW) == Decimal("900")


def test_expiry_boundary():
    c = Coupon("EXP", "percent", Decimal("10"), expires_at=NOW)
    with pytest.raises(CouponError):
        apply_coupon(Decimal("100"), c, NOW)
    ok = Coupon("EXP2", "percent", Decimal("10"), expires_at=datetime(2026, 10, 1, 12, 1))
    assert apply_coupon(Decimal("100"), ok, NOW) == Decimal("90.00")


@pytest.mark.parametrize("kind, value", [("percent", "101"), ("percent", "-1"), ("fixed", "-5"), ("bogus", "1")])
def test_invalid_coupon(kind, value):
    with pytest.raises(ValueError):
        Coupon("X", kind, Decimal(value))
