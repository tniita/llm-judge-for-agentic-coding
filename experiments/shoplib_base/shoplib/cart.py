from decimal import Decimal

from shoplib.models import CartItem, Product


class Cart:
    def __init__(self) -> None:
        self._items: dict[str, CartItem] = {}

    def add(self, product: Product, quantity: int = 1) -> None:
        if product.sku in self._items:
            self._items[product.sku].quantity += quantity
        else:
            self._items[product.sku] = CartItem(product, quantity)

    def remove(self, sku: str, quantity: int = 1) -> None:
        item = self._items[sku]
        item.quantity -= quantity
        if item.quantity == 0:
            del self._items[sku]

    @property
    def items(self) -> list[CartItem]:
        return list(self._items.values())

    def subtotal(self) -> Decimal:
        return sum((item.subtotal for item in self._items.values()), Decimal("0"))

    def clear(self) -> None:
        self._items.clear()
