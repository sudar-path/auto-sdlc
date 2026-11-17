import glob
import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANDING = os.path.join(ROOT, "data", "landing")
PROCESSED = os.path.join(ROOT, "data", "processed")
DB = os.path.join(ROOT, "data", "orders.db")

os.makedirs(LANDING, exist_ok=True)
os.makedirs(PROCESSED, exist_ok=True)

conn = sqlite3.connect(DB)
conn.execute(
    "CREATE TABLE IF NOT EXISTS orders ("
    "order_id TEXT PRIMARY KEY, customer_id TEXT, order_total REAL, currency TEXT, created_at TEXT)"
)
conn.commit()

while True:
    for path in sorted(glob.glob(os.path.join(LANDING, "orders_*.json"))):
        with open(path) as f:
            orders = json.load(f)
        with conn:
            for o in orders:
                conn.execute(
                    "INSERT INTO orders (order_id, customer_id, order_total, currency, created_at) VALUES (?, ?, ?, ?, ?)",
                    (o["order_id"], o["customer_id"], o["order_total"], o["currency"], o["created_at"]),
                )
        os.replace(path, os.path.join(PROCESSED, os.path.basename(path)))
        print("loaded %s orders=%d" % (os.path.basename(path), len(orders)), flush=True)
    time.sleep(2)
