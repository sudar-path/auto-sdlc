import glob
import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANDING = os.path.join(ROOT, "data", "landing")
DB = os.path.join(ROOT, "data", "orders.db")
INTERVAL = 2


def read_totals():
    if not os.path.exists(DB):
        return None
    # mode=ro never creates the file or takes a write lock, so this can't interfere with the pipeline.
    conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(DB), uri=True)
    try:
        return conn.execute("SELECT COUNT(*), COALESCE(SUM(order_total), 0) FROM orders").fetchone()
    except sqlite3.OperationalError:
        # The pipeline creates the table just after creating the file.
        return None
    finally:
        conn.close()


def main():
    try:
        while True:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            waiting = len(glob.glob(os.path.join(LANDING, "orders_*.json")))
            totals = read_totals()
            if totals is None:
                print("%s orders=- revenue=- waiting=%d (no database yet)" % (ts, waiting), flush=True)
            else:
                print("%s orders=%d revenue=%.2f waiting=%d" % (ts, totals[0], totals[1], waiting), flush=True)
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
