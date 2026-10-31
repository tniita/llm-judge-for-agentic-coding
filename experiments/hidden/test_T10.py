import dataclasses
from datetime import datetime
from decimal import Decimal

import pytest

from shoplib.audit import AuditEvent, AuditLog
from shoplib.cart import Cart
from shoplib.inventory import Inventory, OutOfStockError
from shoplib.models import Product, User
from shoplib.orders import OrderRepository, OrderService

T0 = datetime(2026, 5, 1, 9, 0)


def make(audit):
    inv = Inventory()
    inv.set_stock("A", 2)
    return OrderService(inv, OrderRepository(), clock=lambda: T0, audit=audit)


def cart(q=1):
    c = Cart()
    c.add(Product("A", "a", Decimal("1")), q)
    return c


def test_records_on_success():
    log = AuditLog()
    order = make(log).place_order(User(1, "u"), cart())
    assert log.all() == [AuditEvent(T0, 1, "order_placed", order.id)]


def test_no_record_on_failure():
    log = AuditLog()
    service = make(log)
    with pytest.raises(OutOfStockError):
        service.place_order(User(1, "u"), cart(5))
    with pytest.raises(ValueError):
        service.place_order(User(1, "u"), Cart())
    assert log.all() == []


def test_filter_and_order():
    log = AuditLog()
    e1 = AuditEvent(T0, 1, "order_placed", 1)
    e2 = AuditEvent(T0, 2, "order_placed", 2)
    e3 = AuditEvent(T0, 1, "order_placed", 3)
    for e in (e1, e2, e3):
        log.record(e)
    assert log.events_for_user(1) == [e1, e3]
    assert log.all() == [e1, e2, e3]


def test_immutability():
    log = AuditLog()
    e = AuditEvent(T0, 1, "order_placed", 1)
    log.record(e)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.action = "x"
    log.all().clear()
    log.events_for_user(1).append(e)
    assert log.all() == [e]


def test_without_audit():
    inv = Inventory()
    inv.set_stock("A", 2)
    OrderService(inv, OrderRepository()).place_order(User(1, "u"), cart())
