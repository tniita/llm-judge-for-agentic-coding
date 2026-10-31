class OutOfStockError(Exception):
    pass


class Inventory:
    def __init__(self) -> None:
        self._stock: dict[str, int] = {}

    def set_stock(self, sku: str, quantity: int) -> None:
        self._stock[sku] = quantity

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserve(self, sku: str, quantity: int) -> None:
        if self.available(sku) < quantity:
            raise OutOfStockError(sku)
        self._stock[sku] = self.available(sku) - quantity

    def release(self, sku: str, quantity: int) -> None:
        self._stock[sku] = self.available(sku) + quantity
