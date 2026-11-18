import re
import sqlite3
from pathlib import Path

import pytest

from pipeline import run
from tools import totals


def test_read_totals_without_database_returns_none_and_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "orders.db"

    assert totals.read_totals(str(db)) is None
    assert not db.exists()


def test_read_totals_before_table_exists_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "orders.db"
    sqlite3.connect(db).close()

    assert totals.read_totals(str(db)) is None


def test_read_totals_counts_orders_and_sums_revenue(tmp_path: Path) -> None:
    db = str(tmp_path / "orders.db")
    conn = run.connect(db)
    with conn:
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
            [("o1", "c1", 10.25, "USD", "t"), ("o2", "c2", 4.75, "USD", "t")],
        )
    conn.close()

    assert totals.read_totals(db) == (2, 15.0)


def test_read_totals_on_empty_table(tmp_path: Path) -> None:
    db = str(tmp_path / "orders.db")
    run.connect(db).close()

    assert totals.read_totals(db) == (0, 0)


@pytest.mark.parametrize("with_db", [False, True])
def test_main_prints_status_line_and_exits_on_ctrl_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], with_db: bool
) -> None:
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "orders_a_0001.json").write_text("[]")
    db = tmp_path / "orders.db"
    if with_db:
        run.connect(str(db)).close()
    monkeypatch.setattr(totals, "LANDING", str(landing))
    monkeypatch.setattr(totals, "DB", str(db))

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("tools.totals.time.sleep", interrupt)

    totals.main()

    out = capsys.readouterr().out
    expected = r"orders=0 revenue=0\.00 waiting=1" if with_db else r"orders=- revenue=- waiting=1 \(no database yet\)"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ " + expected + "\n", out)
