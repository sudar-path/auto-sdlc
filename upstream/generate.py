import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANDING = os.path.join(ROOT, "data", "landing")
INTERVAL = 3
FAULTS = ("none", "schema_drift", "null_spike")


def make_order(fault, run_id, n):
    now = datetime.now(timezone.utc)
    order = {
        "order_id": "ord_%s_%05d" % (run_id, n),
        "customer_id": "cust_%04d" % random.randint(1, 500),
        "order_total": round(random.uniform(5, 300), 2),
        "currency": "USD",
        "created_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    if fault == "schema_drift":
        order["total_amount"] = order.pop("order_total")
    elif fault == "null_spike" and random.random() < 0.3:
        order["customer_id"] = None
    return order


def write_batch(fault, run_id, seq, counter):
    orders = []
    for _ in range(random.randint(3, 5)):
        counter += 1
        orders.append(make_order(fault, run_id, counter))
    # Microseconds keep names unique across restarts; seq alone resets to 1 each run.
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = "orders_%s_%04d.json" % (utc, seq)
    final = os.path.join(LANDING, name)
    # Dot-prefixed .tmp name so readers globbing orders_*.json never see a partial file.
    tmp = os.path.join(LANDING, ".%s.tmp" % name)
    with open(tmp, "w") as f:
        json.dump(orders, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    print("wrote %s orders=%d fault=%s" % (os.path.relpath(final, ROOT), len(orders), fault), flush=True)
    return counter


def main():
    parser = argparse.ArgumentParser(description="Fake Brightcart orders system")
    parser.add_argument("--once", action="store_true", help="write one batch and exit")
    args = parser.parse_args()

    fault = os.environ.get("FAULT", "none").strip() or "none"
    if fault not in FAULTS:
        sys.exit("unknown FAULT=%r (expected one of %s)" % (fault, ", ".join(FAULTS)))

    os.makedirs(LANDING, exist_ok=True)
    # Run id is the startup time to the microsecond, so order ids stay unique across restarts.
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    seq = 0
    counter = 0
    try:
        while True:
            seq += 1
            counter = write_batch(fault, run_id, seq, counter)
            if args.once:
                break
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
