from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Role(Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"


@dataclass(frozen=True)
class User:
    id: int
    name: str
    role: Role = Role.CUSTOMER

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: Decimal
    category: str = "general"


@dataclass
class CartItem:
    product: Product
    quantity: int

    @property
    def subtotal(self) -> Decimal:
        return self.product.price * self.quantity


class OrderStatus(Enum):
    PLACED = "placed"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


@dataclass
class Order:
    id: int
    user_id: int
    items: list[CartItem]
    total: Decimal
    status: OrderStatus = OrderStatus.PLACED
    created_at: datetime = field(default_factory=datetime.now)
