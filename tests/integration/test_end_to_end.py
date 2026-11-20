"""End-to-end: run the three real scripts as subprocesses against a temporary copy of the repo.

Each script derives its data/ paths from its own location, so copying them into tmp_path keeps
these tests away from the developer's real data/ directory.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = ("upstream/generate.py", "pipeline/run.py", "tools/totals.py")
TIMEOUT = 20


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    for rel in SCRIPTS:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dest)
    return tmp_path


def env_with_fault(fault: str) -> dict[str, str]:
    return {**os.environ, "FAULT": fault}


def generate_batch(sandbox: Path, fault: str) -> Path:
    subprocess.run(
        [sys.executable, str(sandbox / "upstream/generate.py"), "--once"],
        env=env_with_fault(fault),
        check=True,
        capture_output=True,
        timeout=TIMEOUT,
    )
    (batch,) = (sandbox / "data/landing").glob("orders_*.json")
    return batch


def start(sandbox: Path, script: str, fault: str = "none") -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(sandbox / script)],
        env=env_with_fault(fault),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop(proc: subprocess.Popen[str]) -> tuple[str, str]:
    proc.terminate()
    return proc.communicate(timeout=TIMEOUT)


def test_clean_batch_flows_from_upstream_to_totals(sandbox: Path) -> None:
    batch = generate_batch(sandbox, "none")
    orders = json.loads(batch.read_text())

    # The pipeline must ignore FAULT entirely; only upstream reads it.
    pipeline = start(sandbox, "pipeline/run.py", fault="schema_drift")
    processed_file = sandbox / "data/processed" / batch.name
    deadline = time.monotonic() + TIMEOUT
    while not processed_file.exists() and time.monotonic() < deadline and pipeline.poll() is None:
        time.sleep(0.1)
    out, err = stop(pipeline)

    assert processed_file.exists(), err
    assert out == f"loaded {batch.name} orders={len(orders)}\n"
    conn = sqlite3.connect(sandbox / "data/orders.db")
    try:
        stored = conn.execute(
            "SELECT order_id, customer_id, order_total, currency, created_at FROM orders ORDER BY order_id"
        ).fetchall()
    finally:
        conn.close()
    assert stored == sorted(tuple(o.values()) for o in orders)

    totals = start(sandbox, "tools/totals.py")
    assert totals.stdout is not None
    first_line = totals.stdout.readline()
    stop(totals)
    revenue = sum(o["order_total"] for o in orders)
    assert re.search(rf"orders={len(orders)} revenue={revenue:.2f} waiting=0$", first_line.rstrip())


def test_schema_drift_batch_loads_total_amount_as_order_total(sandbox: Path) -> None:
    batch = generate_batch(sandbox, "schema_drift")
    orders = json.loads(batch.read_text())
    assert any("total_amount" in o and "order_total" not in o for o in orders)

    pipeline = start(sandbox, "pipeline/run.py")
    processed_file = sandbox / "data/processed" / batch.name
    deadline = time.monotonic() + TIMEOUT
    while not processed_file.exists() and time.monotonic() < deadline and pipeline.poll() is None:
        time.sleep(0.1)
    out, err = stop(pipeline)

    assert processed_file.exists(), err
    assert not batch.exists()
    assert out == f"loaded {batch.name} orders={len(orders)}\n"
    conn = sqlite3.connect(sandbox / "data/orders.db")
    try:
        stored = conn.execute("SELECT order_id, order_total FROM orders ORDER BY order_id").fetchall()
    finally:
        conn.close()
    assert stored == sorted((o["order_id"], o.get("order_total", o.get("total_amount"))) for o in orders)

    totals = start(sandbox, "tools/totals.py")
    assert totals.stdout is not None
    first_line = totals.stdout.readline()
    stop(totals)
    revenue = sum(amount for _, amount in stored)
    assert re.search(rf"orders={len(orders)} revenue={revenue:.2f} waiting=0$", first_line.rstrip())
