import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pipeline import run


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = run.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def landing(tmp_path: Path) -> Path:
    path = tmp_path / "landing"
    path.mkdir()
    return path


@pytest.fixture
def processed(tmp_path: Path) -> Path:
    path = tmp_path / "processed"
    path.mkdir()
    return path
