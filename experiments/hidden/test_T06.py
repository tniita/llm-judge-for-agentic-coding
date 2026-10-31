from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from shoplib.cart import Cart
from shoplib.inventory import Inventory
from shoplib.models import Product, Role, User
from shoplib.orders import OrderRepository, OrderService

ALICE = User(1, "alice")
BOB = User(2, "bob")
ADMIN = User(9, "admin", Role.ADMIN)
BASE = datetime(2026, 1, 1)


@pytest.fixture
def service():
    times = iter([BASE + timedelta(minutes=m) for m in [0, 5, 5, 10, 20, 30, 40]])
    inv = Inventory()
    inv.set_stock("A", 100)
    svc = OrderService(inv, OrderRepository(), clock=lambda: next(times))
    for user in [ALICE, ALICE, ALICE, BOB, ALICE, BOB, ALICE]:
        cart = Cart()
        cart.add(Product("A", "a", Decimal("1")))
        svc.place_order(user, cart)
    return svc


def test_order_and_tiebreak(service):
    ids = [o.id for o in service.list_orders(ALICE)]
    assert ids == [7, 5, 3, 2, 1]


def test_paging(service):
    assert [o.id for o in service.list_orders(ALICE, page=1, page_size=2)] == [7, 5]
    assert [o.id for o in service.list_orders(ALICE, page=3, page_size=2)] == [1]
    assert service.list_orders(ALICE, page=4, page_size=2) == []


@pytest.mark.parametrize("page, size", [(0, 10), (-1, 10), (1, 0), (1, 101)])
def test_invalid(service, page, size):
    with pytest.raises(ValueError):
        service.list_orders(ALICE, page=page, page_size=size)


def test_admin_sees_all(service):
    assert [o.id for o in service.list_orders(ADMIN)] == [7, 6, 5, 4, 3, 2, 1]
    assert [o.id for o in service.list_orders(BOB)] == [6, 4]
