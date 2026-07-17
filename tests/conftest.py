import pytest

import app as app_module
import database


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point the app at a throwaway database.

    get_db() reads the module-level DB_PATH at call time, so patching it here is picked
    up by app.py even though it imported get_db by name. A temp file (not ':memory:') is
    required: before_request opens a new connection per request, and each would otherwise
    get its own empty in-memory database.
    """
    path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    database.init_db()
    return str(path)


@pytest.fixture
def client(db_path):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = 1
        yield c


@pytest.fixture
def insert():
    """Insert rows with explicit UTC created_at, mirroring CURRENT_TIMESTAMP storage."""
    def _insert(table, created_at, **cols):
        conn = database.get_db()
        cols["created_at"] = created_at
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        cur = conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(cols.values()))
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id
    return _insert
