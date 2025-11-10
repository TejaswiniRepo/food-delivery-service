from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
import logging
import os
import httpx

from . import db, models, schemas
from .metrics import MetricsMiddleware, metrics_endpoint, ORDERS_CREATED
from .deps import get_correlation_id

# ----- Logging -----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [order-service] [cid=%(correlation_id)s] %(message)s",
)
logger = logging.getLogger("order-service")

# ----- Init -----
db.init_db()
app = FastAPI(title="order-service", version="v1")
app.add_middleware(MetricsMiddleware, service_name="order-service")

# ----- Config (URLs from env, default to docker-compose service names) -----
CUSTOMER_SERVICE_URL = os.getenv("CUSTOMER_SERVICE_URL", "http://customer-service:8000")
RESTAURANT_SERVICE_URL = os.getenv("RESTAURANT_SERVICE_URL", "http://restaurant-service:8001")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8002")
DELIVERY_SERVICE_URL = os.getenv("DELIVERY_SERVICE_URL", "http://delivery-service:8003")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8004")


# ----- DB Dependency -----
def get_db():
    s = db.SessionLocal()
    try:
        yield s
    finally:
        s.close()


def get_http_client():
    return httpx.Client(timeout=5.0)


# ----- Infra Endpoints -----
@app.get("/health")
def health():
    return {"status": "ok", "service": "order-service"}


@app.get("/metrics")
def metrics():
    return metrics_endpoint()


# ----- Helper: Customer + Address Validation -----

def validate_customer_and_address(client, customer_id: int, address_id: int | None, cid: str):
    # Use public customer endpoint (we already built it)
    r = client.get(f"{CUSTOMER_SERVICE_URL}/v1/customers/{customer_id}",
                   headers={"X-Correlation-Id": cid})
    if r.status_code != 200:
        raise HTTPException(
            400,
            {"code": "INVALID_CUSTOMER", "correlationId": cid},
        )

    data = r.json()
    if address_id is not None:
        if not any(a["address_id"] == address_id for a in data.get("addresses", [])):
            raise HTTPException(
                400,
                {
                    "code": "INVALID_ADDRESS_FOR_CUSTOMER",
                    "correlationId": cid,
                },
            )

    # Extract email (for payment notifications)
    return data.get("email")


# ----- Helper: Restaurant + Items Validation -----

def validate_restaurant_and_items(client, restaurant_id: int, items: List[schemas.OrderItemRequest], cid: str):
    if not items:
        raise HTTPException(400, {"code": "EMPTY_ORDER", "correlationId": cid})

    # Use internal validate-items endpoint we defined in restaurant-service
    body = {
        "restaurant_id": restaurant_id,
        "items": [{"item_id": it.item_id, "quantity": it.quantity} for it in items],
    }
    r = client.post(f"{RESTAURANT_SERVICE_URL}/internal/v1/validate-items",
                    json=body,
                    headers={"X-Correlation-Id": cid})
    if r.status_code != 200:
        raise HTTPException(
            502,
            {"code": "MENU_VALIDATION_FAILED", "correlationId": cid},
        )

    v = r.json()
    if not v.get("valid"):
        raise HTTPException(
            400,
            {
                "code": "INVALID_MENU_SELECTION",
                "reason": v.get("reason"),
                "correlationId": cid,
            },
        )

    # v contains total + item details
    return v


# ----- Helper: Call Payment-Service -----

def call_payment_service(client, order: models.Order, method: str, email: str | None, cid: str):
    payload = {
        "order_id": order.order_id,
        "amount": order.order_total,
        "method": method,
        "reference": f"ORDER-{order.order_id}",
        "force_fail": False,
    }

    headers = {
        "X-Correlation-Id": cid,
    }
    if email:
        headers["X-Customer-Email"] = email
    # idempotency: one key per order
    headers["Idempotency-Key"] = f"order-{order.order_id}-payment"

    r = client.post(f"{PAYMENT_SERVICE_URL}/v1/payments/charge",
                    json=payload,
                    headers=headers)

    if r.status_code == 201:
        data = r.json()
        if data.get("status") == "SUCCESS":
            return True
        return False

    # If failed with structured error
    return False


# ----- Helper: Call Delivery-Service -----

def call_delivery_assign(client, order_id: int, cid: str):
    payload = {"order_id": order_id}
    r = client.post(f"{DELIVERY_SERVICE_URL}/v1/deliveries/assign",
                    json=payload,
                    headers={"X-Correlation-Id": cid})
    return r.status_code == 201


# ----- Helper: Notify via notification-service -----

def notify(event_type: str, recipient: str | None, subject: str, message: str, cid: str):
    if not (NOTIFICATION_SERVICE_URL and recipient):
        return
    try:
        with get_http_client() as client:
            client.post(
                f"{NOTIFICATION_SERVICE_URL}/v1/notifications/email",
                json={
                    "event_type": event_type,
                    "recipient": recipient,
                    "subject": subject,
                    "message": message,
                    "correlation_id": cid,
                },
                headers={"X-Correlation-Id": cid},
            )
    except Exception as e:
        logger.warning(f"Failed to send order notification: {e}",
                       extra={"correlation_id": cid})


# ----- API: Create Order (Main Orchestration) -----


@app.post("/v1/orders", response_model=schemas.OrderRead, status_code=201)
def create_order(
    payload: schemas.CreateOrderRequest,
    db_sess: Session = Depends(get_db),
    cid: str = Depends(get_correlation_id),
):
    """
    Orchestration flow:
    1. Validate customer + address via customer-service.
    2. Validate restaurant + items via restaurant-service.
    3. Create order + order_items locally.
    4. Call payment-service.
    5. On success → call delivery-service.
    6. Send notifications.
    """
    client = get_http_client()

    # 1. Validate customer & address
    customer_email = payload.customer_email or validate_customer_and_address(
        client,
        payload.customer_id,
        payload.address_id,
        cid,
    )

    # 2. Validate restaurant & items + compute total from authoritative prices
    valid = validate_restaurant_and_items(
        client,
        payload.restaurant_id,
        payload.items,
        cid,
    )

    total = float(valid["total"])
    items_details = valid["items"]

    # 3. Create order & order_items in DB (initially PENDING_PAYMENT)
    order = models.Order(
        customer_id=payload.customer_id,
        restaurant_id=payload.restaurant_id,
        address_id=payload.address_id,
        order_status="PENDING_PAYMENT",
        payment_status="PENDING",
        order_total=total,
        created_at=datetime.utcnow(),
    )
    db_sess.add(order)
    db_sess.flush()  # get order_id

    for d in items_details:
        oi = models.OrderItem(
            order_id=order.order_id,
            item_id=d["item_id"],
            quantity=d["quantity"],
            price=d["unit_price"],
        )
        db_sess.add(oi)

    db_sess.commit()
    db_sess.refresh(order)

    logger.info(
        f"Order {order.order_id} created pending payment",
        extra={"correlation_id": cid},
    )

    # 4. Call payment-service
    payment_ok = call_payment_service(
        client,
        order,
        payload.payment_method,
        customer_email,
        cid,
    )

    if not payment_ok:
        order.order_status = "PAYMENT_FAILED"
        order.payment_status = "FAILED"
        db_sess.commit()
        ORDERS_CREATED.labels("PAYMENT_FAILED").inc()
        raise HTTPException(
            402,
            {
                "code": "PAYMENT_FAILED",
                "order_id": order.order_id,
                "correlationId": cid,
            },
        )

    # Update status after successful payment
    order.payment_status = "SUCCESS"
    order.order_status = "CONFIRMED"
    db_sess.commit()
    db_sess.refresh(order)

    ORDERS_CREATED.labels("CONFIRMED").inc()
    logger.info(
        f"Order {order.order_id} payment success, confirming",
        extra={"correlation_id": cid},
    )

    # 5. Call delivery-service
    delivery_ok = call_delivery_assign(client, order.order_id, cid)
    if delivery_ok:
        order.order_status = "OUT_FOR_DELIVERY"
        db_sess.commit()
        db_sess.refresh(order)
        logger.info(
            f"Order {order.order_id} assigned for delivery",
            extra={"correlation_id": cid},
        )

    # 6. Notification to customer
    if customer_email:
        notify(
            "ORDER_CREATED",
            customer_email,
            f"Order #{order.order_id} placed successfully",
            f"Your order total is {order.order_total}. Status: {order.order_status}",
            cid,
        )

    # reload items for response
    db_sess.refresh(order)
    return order


# ----- API: Get Order by ID -----


@app.get("/v1/orders/{order_id}", response_model=schemas.OrderRead)
def get_order(
    order_id: int,
    db_sess: Session = Depends(get_db),
    cid: str = Depends(get_correlation_id),
):
    order = (
        db_sess.query(models.Order)
        .filter(models.Order.order_id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(
            404,
            {"code": "ORDER_NOT_FOUND", "correlationId": cid},
        )
    return order
