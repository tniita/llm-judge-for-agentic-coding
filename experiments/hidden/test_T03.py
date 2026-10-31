import threading
from decimal import Decimal

import pytest

from shoplib.cart import Cart
from shoplib.inventory import Inventory, OutOfStockError
from shoplib.models import Product, User
from shoplib.orders import OrderRepository, OrderService


def make_inventory():
    inv = Inventory()
    inv.set_stock("A", 5)
    inv.set_stock("B", 1)
    return inv


def test_reserve_many_all_or_nothing():
    inv = make_inventory()
    with pytest.raises(OutOfStockError):
        inv.reserve_many({"A": 2, "B": 2})
    assert inv.available("A") == 5 and inv.available("B") == 1
    inv.reserve_many({"A": 2, "B": 1})
    assert inv.available("A") == 3 and inv.available("B") == 0


def test_place_order_no_partial_reservation():
    inv = make_inventory()
    service = OrderService(inv, OrderRepository())
    cart = Cart()
    cart.add(Product("A", "a", Decimal("1")), 2)
    cart.add(Product("B", "b", Decimal("1")), 2)
    with pytest.raises(OutOfStockError):
        service.place_order(User(1, "u"), cart)
    assert inv.available("A") == 5 and inv.available("B") == 1


@pytest.mark.parametrize("q", [0, -1])
def test_non_positive(q):
    inv = make_inventory()
    with pytest.raises(ValueError):
        inv.reserve("A", q)
    with pytest.raises(ValueError):
        inv.reserve_many({"A": q})
    with pytest.raises(ValueError):
        inv.release("A", q)


def test_concurrent_reserve_never_oversells():
    inv = Inventory()
    inv.set_stock("A", 20)
    successes = []
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()
        try:
            inv.reserve("A", 1)
            successes.append(1)
        except OutOfStockError:
            pass

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(successes) == 20
    assert inv.available("A") == 0
