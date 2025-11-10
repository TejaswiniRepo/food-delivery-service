from pydantic import BaseModel
from typing import List, Optional


class OrderItemRequest(BaseModel):
    item_id: int
    quantity: int


class CreateOrderRequest(BaseModel):
    customer_id: int
    restaurant_id: int
    address_id: Optional[int] = None
    items: List[OrderItemRequest]
    payment_method: str = "CARD"
    customer_email: Optional[str] = None   # for notifications


class OrderItemRead(BaseModel):
    order_item_id: int
    item_id: int
    quantity: int
    price: float

    class Config:
        from_attributes = True


class OrderRead(BaseModel):
    order_id: int
    customer_id: int
    restaurant_id: int
    address_id: Optional[int]
    order_status: str
    order_total: float
    payment_status: str
    items: List[OrderItemRead]

    class Config:
        from_attributes = True
