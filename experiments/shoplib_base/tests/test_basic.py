from decimal import Decimal

import pytest

from shoplib.cart import Cart
from shoplib.inventory import Inventory, OutOfStockError
from shoplib.models import Product, User
from shoplib.orders import OrderRepository, OrderService

PEN = Product("PEN", "Pen", Decimal("120"))
NOTE = Product("NOTE", "Notebook", Decimal("300"))


@pytest.fixture
def service():
    inventory = Inventory()
    inventory.set_stock("PEN", 10)
    inventory.set_stock("NOTE", 5)
    return OrderService(inventory, OrderRepository())


def test_cart_subtotal():
    cart = Cart()
    cart.add(PEN, 2)
    cart.add(NOTE)
    assert cart.subtotal() == Decimal("540")


def test_place_order_reserves_stock(service):
    cart = Cart()
    cart.add(PEN, 3)
    order = service.place_order(User(1, "alice"), cart)
    assert order.total == Decimal("360")
    assert service.inventory.available("PEN") == 7
    assert cart.items == []


def test_place_order_out_of_stock(service):
    cart = Cart()
    cart.add(NOTE, 6)
    with pytest.raises(OutOfStockError):
        service.place_order(User(1, "alice"), cart)
