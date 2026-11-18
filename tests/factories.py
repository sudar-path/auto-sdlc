"""Builders for order records that follow the order-file contract (see README.md)."""

import json
from pathlib import Path
from typing import Any

Order = dict[str, Any]


def order(order_id: str, total: float, customer_id: str | None = "cust_0001") -> Order:
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "order_total": total,
        "currency": "USD",
        "created_at": "2026-11-18T00:00:00Z",
    }


def write_order_file(directory: Path, name: str, orders: list[Order]) -> Path:
    path = directory / name
    path.write_text(json.dumps(orders))
    return path
