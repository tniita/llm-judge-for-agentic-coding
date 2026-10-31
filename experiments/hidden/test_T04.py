from decimal import Decimal

import pytest

from shoplib.cart import Cart
from shoplib.inventory import Inventory
from shoplib.models import OrderStatus, Product, Role, User
from shoplib.orders import OrderRepository, OrderService

ALICE = User(1, "alice")
BOB = User(2, "bob")
ADMIN = User(9, "admin", Role.ADMIN)


@pytest.fixture
def ctx():
    inv = Inventory()
    inv.set_stock("A", 10)
    inv.set_stock("B", 10)
    service = OrderService(inv, OrderRepository())
    cart = Cart()
    cart.add(Product("A", "a", Decimal("100")), 3)
    cart.add(Product("B", "b", Decimal("50")), 2)
    order = service.place_order(ALICE, cart)
    return service, inv, order


def test_owner_cancel_releases_stock(ctx):
    service, inv, order = ctx
    result = service.cancel_order(ALICE, order.id)
    assert result.status is OrderStatus.CANCELLED
    assert inv.available("A") == 10 and inv.available("B") == 10


def test_admin_can_cancel(ctx):
    service, _, order = ctx
    assert service.cancel_order(ADMIN, order.id).status is OrderStatus.CANCELLED


def test_other_user_forbidden(ctx):
    service, inv, order = ctx
    with pytest.raises(PermissionError):
        service.cancel_order(BOB, order.id)
    assert inv.available("A") == 7


def test_not_found(ctx):
    service, _, _ = ctx
    with pytest.raises(LookupError):
        service.cancel_order(ALICE, 999)


def test_shipped(ctx):
    service, inv, order = ctx
    order.status = OrderStatus.SHIPPED
    with pytest.raises(ValueError):
        service.cancel_order(ALICE, order.id)
    assert inv.available("A") == 7


def test_double_cancel(ctx):
    service, inv, order = ctx
    service.cancel_order(ALICE, order.id)
    with pytest.raises(ValueError):
        service.cancel_order(ALICE, order.id)
    assert inv.available("A") == 10
