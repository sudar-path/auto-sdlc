import glob
import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANDING = os.path.join(ROOT, "data", "landing")
PROCESSED = os.path.join(ROOT, "data", "processed")
DB = os.path.join(ROOT, "data", "orders.db")
POLL_INTERVAL = 2
ORDER_FILE_PATTERN = "orders_*.json"


def connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        "order_id TEXT PRIMARY KEY, customer_id TEXT, order_total REAL, currency TEXT, created_at TEXT)"
    )
    conn.commit()
    return conn


def load_file(conn: sqlite3.Connection, path: str, processed: str) -> int:
    """Load one order file in a single transaction, then move it into ``processed``.

    Any error (malformed JSON, missing field, duplicate order_id) rolls back the whole
    file and leaves it in landing. Returns the number of orders loaded.
    """
    with open(path) as f:
        orders = json.load(f)
    with conn:
        for o in orders:
            conn.execute(
                "INSERT INTO orders (order_id, customer_id, order_total, currency, created_at) VALUES (?, ?, ?, ?, ?)",
                (o["order_id"], o["customer_id"], o["order_total"], o["currency"], o["created_at"]),
            )
    os.replace(path, os.path.join(processed, os.path.basename(path)))
    print("loaded %s orders=%d" % (os.path.basename(path), len(orders)), flush=True)
    return len(orders)


def process_landing(conn: sqlite3.Connection, landing: str, processed: str) -> int:
    """Load every order file currently in ``landing``, oldest name first. Returns the file count."""
    paths = sorted(glob.glob(os.path.join(landing, ORDER_FILE_PATTERN)))
    for path in paths:
        load_file(conn, path, processed)
    return len(paths)


def main() -> None:
    os.makedirs(LANDING, exist_ok=True)
    os.makedirs(PROCESSED, exist_ok=True)
    conn = connect(DB)
    while True:
        process_landing(conn, LANDING, PROCESSED)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
