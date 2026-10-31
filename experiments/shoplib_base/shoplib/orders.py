from datetime import datetime
from itertools import count

from shoplib.cart import Cart
from shoplib.inventory import Inventory
from shoplib.models import CartItem, Order, User


class OrderRepository:
    def __init__(self) -> None:
        self._orders: dict[int, Order] = {}
        self._ids = count(1)

    def next_id(self) -> int:
        return next(self._ids)

    def save(self, order: Order) -> None:
        self._orders[order.id] = order

    def get(self, order_id: int) -> Order | None:
        return self._orders.get(order_id)

    def all(self) -> list[Order]:
        return list(self._orders.values())


class OrderService:
    def __init__(self, inventory: Inventory, repository: OrderRepository, clock=datetime.now) -> None:
        self.inventory = inventory
        self.repository = repository
        self.clock = clock

    def place_order(self, user: User, cart: Cart) -> Order:
        if not cart.items:
            raise ValueError("cart is empty")
        for item in cart.items:
            self.inventory.reserve(item.product.sku, item.quantity)
        order = Order(
            id=self.repository.next_id(),
            user_id=user.id,
            items=[CartItem(i.product, i.quantity) for i in cart.items],
            total=cart.subtotal(),
            created_at=self.clock(),
        )
        self.repository.save(order)
        cart.clear()
        return order

    def list_orders(self, user: User) -> list[Order]:
        return [o for o in self.repository.all() if o.user_id == user.id]
