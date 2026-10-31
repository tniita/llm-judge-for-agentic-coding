from decimal import Decimal

import pytest

from shoplib.cart import Cart
from shoplib.models import Product

PEN = Product("PEN", "Pen", Decimal("120"))


@pytest.mark.parametrize("q", [0, -1])
def test_add_non_positive(q):
    with pytest.raises(ValueError):
        Cart().add(PEN, q)


@pytest.mark.parametrize("q", [0, -2])
def test_remove_non_positive(q):
    cart = Cart()
    cart.add(PEN, 2)
    with pytest.raises(ValueError):
        cart.remove("PEN", q)
    assert cart.items[0].quantity == 2


def test_remove_too_many_keeps_cart():
    cart = Cart()
    cart.add(PEN, 2)
    with pytest.raises(ValueError):
        cart.remove("PEN", 3)
    assert cart.items[0].quantity == 2


def test_remove_to_zero_deletes():
    cart = Cart()
    cart.add(PEN, 2)
    cart.remove("PEN", 2)
    assert cart.items == []


@pytest.mark.parametrize("q", [1.5, True])
def test_non_int(q):
    with pytest.raises(TypeError):
        Cart().add(PEN, q)
    cart = Cart()
    cart.add(PEN, 2)
    with pytest.raises(TypeError):
        cart.remove("PEN", q)


def test_remove_unknown():
    with pytest.raises(KeyError):
        Cart().remove("NOPE")
