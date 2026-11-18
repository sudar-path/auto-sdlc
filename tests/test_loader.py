import json
import sqlite3
from pathlib import Path

import pytest

from pipeline import run
from tests.factories import order, write_order_file


def rows(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    return conn.execute("SELECT order_id, order_total FROM orders ORDER BY order_id").fetchall()


def test_connect_creates_orders_table_with_contract_columns(conn: sqlite3.Connection) -> None:
    columns = [(name, col_type, pk) for _, name, col_type, _, _, pk in conn.execute("PRAGMA table_info(orders)")]

    assert columns == [
        ("order_id", "TEXT", 1),
        ("customer_id", "TEXT", 0),
        ("order_total", "REAL", 0),
        ("currency", "TEXT", 0),
        ("created_at", "TEXT", 0),
    ]


def test_connect_reuses_existing_database(tmp_path: Path, landing: Path, processed: Path) -> None:
    db = str(tmp_path / "orders.db")
    first = run.connect(db)
    run.load_file(first, str(write_order_file(landing, "orders_a_0001.json", [order("ord_1", 5)])), str(processed))
    first.close()

    second = run.connect(db)
    try:
        assert rows(second) == [("ord_1", 5.0)]
    finally:
        second.close()


def test_load_file_inserts_every_field_and_moves_file(
    conn: sqlite3.Connection, landing: Path, processed: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    orders = [order("ord_1", 12.5, "cust_0042"), order("ord_2", 7)]
    path = write_order_file(landing, "orders_20261118T000000000000Z_0001.json", orders)

    result = run.load_file(conn, str(path), str(processed))

    assert result == 2
    stored = conn.execute(
        "SELECT order_id, customer_id, order_total, currency, created_at FROM orders ORDER BY order_id"
    ).fetchall()
    assert stored == [tuple(o.values()) for o in orders]
    assert not path.exists()
    assert json.loads((processed / path.name).read_text()) == orders
    assert capsys.readouterr().out == "loaded orders_20261118T000000000000Z_0001.json orders=2\n"


def test_zero_and_negative_totals_load_like_any_other_order(
    conn: sqlite3.Connection, landing: Path, processed: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_order_file(
        landing,
        "orders_a_0001.json",
        [
            order("ord_1", 10.5),
            order("ord_zero", 0),
            order("ord_2", 0.01),
            order("ord_neg", -3.25),
            order("ord_3", 299.99),
        ],
    )

    assert run.load_file(conn, str(path), str(processed)) == 5

    assert rows(conn) == [
        ("ord_1", 10.5),
        ("ord_2", 0.01),
        ("ord_3", 299.99),
        ("ord_neg", -3.25),
        ("ord_zero", 0.0),
    ]
    assert capsys.readouterr().out == "loaded orders_a_0001.json orders=5\n"
    assert not path.exists()
    assert (processed / "orders_a_0001.json").exists()


def test_empty_file_is_processed(
    conn: sqlite3.Connection, landing: Path, processed: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_order_file(landing, "orders_d_0001.json", [])

    assert run.load_file(conn, str(path), str(processed)) == 0
    assert (processed / path.name).exists()
    assert capsys.readouterr().out == "loaded orders_d_0001.json orders=0\n"


def test_null_customer_id_is_loaded_as_null(conn: sqlite3.Connection, landing: Path, processed: Path) -> None:
    path = write_order_file(landing, "orders_e_0001.json", [order("ord_1", 9.99, customer_id=None)])

    run.load_file(conn, str(path), str(processed))

    assert conn.execute("SELECT customer_id FROM orders").fetchall() == [(None,)]


@pytest.mark.parametrize(
    ("bad_order", "error"),
    [
        pytest.param({"order_id": "ord_bad", "customer_id": "c", "total_amount": 5}, KeyError, id="schema_drift"),
        pytest.param(order("ord_1", 3), sqlite3.IntegrityError, id="duplicate_order_id"),
    ],
)
def test_bad_order_rolls_back_whole_file_and_leaves_it_in_landing(
    conn: sqlite3.Connection,
    landing: Path,
    processed: Path,
    bad_order: dict[str, object],
    error: type[Exception],
) -> None:
    path = write_order_file(landing, "orders_f_0001.json", [order("ord_1", 1), order("ord_2", 2), bad_order])

    with pytest.raises(error):
        run.load_file(conn, str(path), str(processed))

    assert rows(conn) == []
    assert path.exists()
    assert list(processed.iterdir()) == []


def test_process_landing_loads_only_order_files_in_name_order(
    conn: sqlite3.Connection, landing: Path, processed: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_order_file(landing, "orders_b_0002.json", [order("ord_b", 2)])
    write_order_file(landing, "orders_a_0001.json", [order("ord_a", 1)])
    # In-progress upstream writes and unrelated files must be ignored.
    write_order_file(landing, ".orders_c_0003.json.tmp", [order("ord_tmp", 3)])
    write_order_file(landing, "notes.json", [order("ord_notes", 4)])

    assert run.process_landing(conn, str(landing), str(processed)) == 2

    assert rows(conn) == [("ord_a", 1.0), ("ord_b", 2.0)]
    assert capsys.readouterr().out.splitlines() == [
        "loaded orders_a_0001.json orders=1",
        "loaded orders_b_0002.json orders=1",
    ]
    assert sorted(p.name for p in landing.iterdir()) == [".orders_c_0003.json.tmp", "notes.json"]


def test_process_landing_with_no_files_does_nothing(conn: sqlite3.Connection, landing: Path, processed: Path) -> None:
    assert run.process_landing(conn, str(landing), str(processed)) == 0
    assert rows(conn) == []


class StopLoopError(Exception):
    pass


def test_main_creates_dirs_and_polls_landing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    landing = tmp_path / "data" / "landing"
    processed = tmp_path / "data" / "processed"
    db = tmp_path / "data" / "orders.db"
    monkeypatch.setattr(run, "LANDING", str(landing))
    monkeypatch.setattr(run, "PROCESSED", str(processed))
    monkeypatch.setattr(run, "DB", str(db))
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            write_order_file(landing, "orders_a_0001.json", [order("ord_1", 4)])
        else:
            raise StopLoopError

    monkeypatch.setattr("pipeline.run.time.sleep", fake_sleep)

    with pytest.raises(StopLoopError):
        run.main()

    assert sleeps == [run.POLL_INTERVAL, run.POLL_INTERVAL]
    assert (processed / "orders_a_0001.json").exists()
    check = sqlite3.connect(db)
    try:
        assert rows(check) == [("ord_1", 4.0)]
    finally:
        check.close()
