"""init_db upgrading a database written by an older version.

The categories removal is the first destructive migration in this app: it rebuilds
products to shed a foreign-keyed column. Other tables reference products(id), so the
risk is not the dropped column but the rows and references around it.
"""
import sqlite3

import pytest

import database

# products as it stood before categories were removed, plus the tables that hang
# foreign keys off it.
PRE_CATEGORIES_SCHEMA = '''
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sku TEXT UNIQUE,
    category_id INTEGER, price REAL NOT NULL DEFAULT 0, stock_qty INTEGER NOT NULL DEFAULT 0,
    reorder_threshold INTEGER NOT NULL DEFAULT 0, is_archived INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id));
CREATE TABLE stock_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
    change_qty INTEGER NOT NULL, reason TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id));
CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL DEFAULT 'draft',
    total_amount REAL NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE order_items (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, unit_price REAL NOT NULL,
    subtotal REAL NOT NULL, FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id));
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Restock as it stood before per-product cost: one total per batch, split across the
-- lines by quantity, with no invoice price, discount, shipping or fee recorded.
CREATE TABLE restock_batches (id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_cost REAL NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE restock_items (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL, qty_added INTEGER NOT NULL,
    allocated_cost REAL NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES restock_batches(id),
    FOREIGN KEY (product_id) REFERENCES products(id));

INSERT INTO categories (name) VALUES ('sabun'), ('susu');
-- Non-contiguous ids on purpose: other tables reference these values, so the
-- rebuild must carry them across rather than renumbering.
INSERT INTO products (id, name, sku, category_id, price, stock_qty, reorder_threshold, is_archived)
    VALUES (7, 'Kopi', 'KP-1', 1, 25000, 100, 5, 0),
           (9, 'Teh', 'TM-1', NULL, 2000, 50, 3, 0),
           (11, 'Arsip', 'AR-1', 2, 500, 0, 0, 1);
INSERT INTO orders (id, status, total_amount) VALUES (3, 'completed', 50000);
INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)
    VALUES (3, 7, 2, 25000, 50000);
INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (7, -2, 'sale order #3');
-- Batch 1 mixes two products, so its per-unit split (40000/4 and 20000/2 -- identical,
-- which is the flaw) says nothing about either one's real cost. Batch 2 restocked Kopi
-- alone, so its total genuinely is Kopi's cost.
INSERT INTO restock_batches (id, total_cost) VALUES (1, 60000), (2, 30000);
INSERT INTO restock_items (batch_id, product_id, qty_added, allocated_cost)
    VALUES (1, 7, 4, 40000), (1, 9, 2, 20000), (2, 7, 2, 30000);
'''


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A pre-categories-removal database, migrated by init_db()."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(PRE_CATEGORIES_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.delenv("SHOP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(database, "ENCRYPTION_KEY_PATH", str(tmp_path / "legacy.key"))
    database.init_db()
    return str(path)


def query(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def test_categories_table_is_gone(legacy_db):
    tables = {r["name"] for r in query(
        legacy_db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "categories" not in tables


def test_category_id_column_is_gone(legacy_db):
    cols = {r["name"] for r in query(legacy_db, "PRAGMA table_info(products)")}
    assert "category_id" not in cols
    assert cols == {"id", "name", "sku", "price", "cost_price", "stock_qty",
                    "reserved_qty", "reorder_threshold", "is_archived",
                    "cost_review_needed", "created_at", "updated_at"}


def test_the_rebuild_leaves_no_scratch_table_behind(legacy_db):
    tables = {r["name"] for r in query(
        legacy_db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not [t for t in tables if "pre_categories" in t]


def test_every_product_survives_with_its_id_and_values(legacy_db):
    rows = query(legacy_db, "SELECT id, name, sku, price, stock_qty, reorder_threshold,"
                            " is_archived FROM products ORDER BY id")
    assert rows == [
        {"id": 7, "name": "Kopi", "sku": "KP-1", "price": 25000.0, "stock_qty": 100,
         "reorder_threshold": 5, "is_archived": 0},
        {"id": 9, "name": "Teh", "sku": "TM-1", "price": 2000.0, "stock_qty": 50,
         "reorder_threshold": 3, "is_archived": 0},
        # The archived product must come across too -- it is still referenced by history.
        {"id": 11, "name": "Arsip", "sku": "AR-1", "price": 500.0, "stock_qty": 0,
         "reorder_threshold": 0, "is_archived": 1},
    ]


def test_no_reference_is_left_dangling(legacy_db):
    assert query(legacy_db, "PRAGMA foreign_key_check") == []


def test_history_still_joins_to_its_product(legacy_db):
    # The rebuild drops and recreates products; if other tables' foreign keys had
    # been rewritten to follow the renamed-aside table, these joins would come back
    # empty and the shop would silently lose its history.
    assert query(legacy_db, "SELECT p.name FROM order_items oi"
                            " JOIN products p ON p.id = oi.product_id") == [{"name": "Kopi"}]
    assert query(legacy_db, "SELECT p.name FROM stock_logs sl"
                            " JOIN products p ON p.id = sl.product_id") == [{"name": "Kopi"}]


def test_foreign_keys_are_enforced_again_afterwards(legacy_db):
    conn = sqlite3.connect(legacy_db)
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " subtotal) VALUES (3, 999, 1, 1, 1)")
    conn.close()


def test_new_products_continue_the_id_sequence(legacy_db):
    # AUTOINCREMENT state lives in sqlite_sequence; losing it would hand a new
    # product an id that old history already refers to.
    conn = sqlite3.connect(legacy_db)
    new_id = conn.execute(
        "INSERT INTO products (name, price) VALUES ('Baru', 1) RETURNING id").fetchone()[0]
    conn.commit()
    conn.close()
    assert new_id > 11


def test_migrating_twice_changes_nothing(legacy_db):
    before = query(legacy_db, "SELECT * FROM products ORDER BY id")
    database.init_db()
    assert query(legacy_db, "SELECT * FROM products ORDER BY id") == before
    assert query(legacy_db, "PRAGMA foreign_key_check") == []


def test_restock_lines_get_a_unit_cost_from_the_old_quantity_split(legacy_db):
    # The split is all the cost history there is, so it seeds both the landed cost and
    # the invoice price that was never recorded.
    rows = query(legacy_db, "SELECT batch_id, product_id, unit_price, unit_cost,"
                            " allocated_cost FROM restock_items ORDER BY id")
    assert rows == [
        {"batch_id": 1, "product_id": 7, "unit_price": 10000.0, "unit_cost": 10000.0,
         "allocated_cost": 40000.0},
        {"batch_id": 1, "product_id": 9, "unit_price": 10000.0, "unit_cost": 10000.0,
         "allocated_cost": 20000.0},
        {"batch_id": 2, "product_id": 7, "unit_price": 15000.0, "unit_cost": 15000.0,
         "allocated_cost": 30000.0},
    ]


def test_batch_totals_are_untouched_and_count_wholly_as_goods(legacy_db):
    # total_cost still means money paid, which is what keeps net_profit unchanged by the
    # upgrade; with no charge history, all of it was goods.
    assert query(legacy_db, "SELECT id, subtotal_cost, discount, shipping_cost, admin_fee,"
                            " total_cost FROM restock_batches ORDER BY id") == [
        {"id": 1, "subtotal_cost": 60000.0, "discount": 0.0, "shipping_cost": 0.0,
         "admin_fee": 0.0, "total_cost": 60000.0},
        {"id": 2, "subtotal_cost": 30000.0, "discount": 0.0, "shipping_cost": 0.0,
         "admin_fee": 0.0, "total_cost": 30000.0},
    ]


def test_only_single_product_batches_are_trusted_to_seed_a_cost(legacy_db):
    # The old split divides a batch total evenly per unit, so a mixed batch would hand a
    # cheap product the same cost as an expensive one -- margins in the hundreds of
    # percent, and a poisoned weighted average on the next restock. Teh only ever appeared
    # in the mixed batch, so it stays unknown.
    assert query(legacy_db, "SELECT id, cost_price FROM products ORDER BY id") == [
        {"id": 7, "cost_price": 15000.0},   # batch 2, Kopi alone: 30000 over 2 units
        {"id": 9, "cost_price": 0.0},       # mixed batch only
        {"id": 11, "cost_price": 0.0},      # never restocked
    ]


def test_historical_order_lines_inherit_only_a_defensible_cost(legacy_db):
    # Knowingly an estimate -- what Kopi cost at the time of this sale was never recorded
    # -- but it comes from the single-product batch, so it is at least the right order of
    # magnitude. A line whose product has no trusted cost stays at 0 and is excluded.
    assert query(legacy_db, "SELECT quantity, unit_price, unit_cost FROM order_items") == [
        {"quantity": 2, "unit_price": 25000.0, "unit_cost": 15000.0},
    ]


def test_a_second_migration_does_not_overwrite_captured_costs(legacy_db):
    # The backfills are one-shot: they run in the pass that adds the columns. A re-run
    # that recomputed them would throw away every cost captured since.
    conn = sqlite3.connect(legacy_db)
    conn.execute("UPDATE products SET cost_price = 99 WHERE id = 7")
    conn.execute("UPDATE order_items SET unit_cost = 42")
    conn.commit()
    conn.close()
    database.init_db()
    assert query(legacy_db, "SELECT cost_price FROM products WHERE id = 7") == [
        {"cost_price": 99.0}]
    assert query(legacy_db, "SELECT unit_cost FROM order_items") == [{"unit_cost": 42.0}]


def test_a_fresh_database_never_creates_categories(db_path):
    tables = {r["name"] for r in query(
        db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "categories" not in tables
    assert "products" in tables


# --- Voiding, on a database that predates it ---

def test_the_void_columns_are_added_to_an_old_database(legacy_db):
    batch_cols = {r["name"] for r in query(legacy_db, "PRAGMA table_info(restock_batches)")}
    item_cols = {r["name"] for r in query(legacy_db, "PRAGMA table_info(restock_items)")}
    self_use_cols = {r["name"] for r in query(legacy_db, "PRAGMA table_info(self_use_batches)")}
    assert "voids_batch_id" in batch_cols
    assert "voids_batch_id" in self_use_cols
    assert "cost_before" in item_cols


def test_old_restock_lines_have_no_cost_snapshot(legacy_db):
    """0 across the board -- these rows were written before the column existed."""
    assert {r["cost_before"] for r in query(legacy_db, "SELECT cost_before FROM restock_items")} \
        == {0.0}


def test_an_old_batch_is_still_voidable_and_takes_the_flag_path(legacy_db, monkeypatch):
    """No snapshot to restore from, and an earlier batch behind it, so the cost is doubted
    rather than zeroed -- and the reversal of stock and money still happens in full."""
    monkeypatch.setattr(database, "DB_PATH", legacy_db)
    import services
    conn = database.get_db()
    try:
        # Batch 2 restocked Kopi (id 7) alone; batch 1 is earlier and also holds Kopi.
        result = services.void_restock(conn, 2)
    finally:
        conn.close()

    assert result["flagged"] == ["Kopi"]
    assert result["restored"] == []
    assert query(legacy_db, "SELECT stock_qty, cost_review_needed FROM products WHERE id = 7") == [
        {"stock_qty": 98, "cost_review_needed": 1}]
    assert query(legacy_db, "SELECT SUM(total_cost) AS t FROM restock_batches") == [{"t": 60000.0}]


# --- Reserved stock, on a database whose orders never held any ---

# products as it stood before open orders held stock, plus the order tables the
# backfill reads. init_db() creates everything else from scratch.
PRE_RESERVATION_SCHEMA = '''
CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sku TEXT UNIQUE,
    price REAL NOT NULL DEFAULT 0, cost_price REAL NOT NULL DEFAULT 0,
    stock_qty INTEGER NOT NULL DEFAULT 0, reorder_threshold INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0, cost_review_needed INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL DEFAULT 'draft',
    total_amount REAL NOT NULL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE order_items (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, unit_price REAL NOT NULL,
    subtotal REAL NOT NULL, FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id));

INSERT INTO products (id, name, price, stock_qty) VALUES
    (1, 'Kopi', 25000, 10), (2, 'Teh', 2000, 4), (3, 'Gula', 5000, 7);
-- One order per status. Only draft and confirmed are still waiting on their units:
-- completed already took them out of stock_qty, cancelled gave them up.
INSERT INTO orders (id, status) VALUES (1, 'draft'), (2, 'confirmed'),
    (3, 'completed'), (4, 'cancelled');
INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal) VALUES
    (1, 1, 2, 25000, 50000),
    (2, 1, 3, 25000, 75000),
    (3, 1, 4, 25000, 100000),
    (4, 1, 5, 25000, 125000),
    -- Teh is already oversold: an open order promises 6 of the 4 in stock. Nothing
    -- checked that under the old rules, and the upgrade inherits the situation.
    (2, 2, 6, 2000, 12000);
'''


@pytest.fixture
def pre_reservation_db(tmp_path, monkeypatch):
    """A database from before orders reserved stock, migrated by init_db()."""
    path = tmp_path / "pre-reservation.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(PRE_RESERVATION_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.delenv("SHOP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(database, "ENCRYPTION_KEY_PATH", str(tmp_path / "pre-reservation.key"))
    database.init_db()
    return str(path)


def test_open_orders_start_holding_their_stock(pre_reservation_db):
    # Kopi: 2 from the draft and 3 from the confirmed order. The completed order's 4
    # already left stock_qty and the cancelled order's 5 were given up, so counting
    # either would hold units nothing is waiting on.
    assert query(pre_reservation_db,
                 "SELECT id, stock_qty, reserved_qty FROM products ORDER BY id") == [
        {"id": 1, "stock_qty": 10, "reserved_qty": 5},
        {"id": 2, "stock_qty": 4, "reserved_qty": 6},
        {"id": 3, "stock_qty": 7, "reserved_qty": 0},
    ]


def test_an_already_oversold_product_keeps_its_honest_figure(pre_reservation_db):
    # Teh's open order promises 6 of 4. Clamping the reservation to stock would make
    # the shortfall vanish from the page that has to surface it, and would quietly
    # free units the order is still counting on.
    rows = query(pre_reservation_db,
                 "SELECT stock_qty - reserved_qty AS available FROM products WHERE id = 2")
    assert rows == [{"available": -2}]


def test_stock_itself_is_untouched_by_the_upgrade(pre_reservation_db):
    # Reserving is bookkeeping about orders, not a stock movement: nothing physically
    # left the shelf, so the dashboard, the low-stock alerts and the monthly report
    # must all read exactly what they read before.
    assert query(pre_reservation_db, "SELECT SUM(stock_qty) AS total FROM products") == [
        {"total": 21}]
    assert query(pre_reservation_db, "SELECT COUNT(*) AS n FROM stock_logs") == [{"n": 0}]


def test_migrating_twice_does_not_double_count_the_holds(pre_reservation_db):
    # The backfill runs in the pass that adds the column. A re-run that recomputed it
    # would be harmless here but would wipe every hold taken since the upgrade.
    database.init_db()
    assert query(pre_reservation_db, "SELECT reserved_qty FROM products WHERE id = 1") == [
        {"reserved_qty": 5}]


def test_a_later_run_leaves_live_reservations_alone(pre_reservation_db):
    conn = sqlite3.connect(pre_reservation_db)
    conn.execute("UPDATE products SET reserved_qty = 9 WHERE id = 3")
    conn.commit()
    conn.close()
    database.init_db()
    assert query(pre_reservation_db, "SELECT reserved_qty FROM products WHERE id = 3") == [
        {"reserved_qty": 9}]


# --- Indexes, on a database that predates them ---

def test_an_upgraded_database_gets_the_indexes(legacy_db):
    names = {r["name"] for r in query(
        legacy_db, "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    # Ordinary ones, on columns the old schema already had.
    assert "idx_order_items_order" in names
    assert "idx_orders_created" in names
    # And the ones whose columns this very run adds: voids_batch_id arrives by ALTER
    # TABLE above, so building the index before the migrations would fail outright on
    # an old database. This is what pins the index block to the end of init_db.
    assert "idx_restock_batches_voids" in names
    assert "idx_self_use_batches_voids" in names


def test_indexes_reach_a_database_that_had_no_self_use_tables(pre_reservation_db):
    # self_use_batches did not exist in this schema at all; init_db creates it, and the
    # index has to land on the freshly created table in the same pass.
    names = {r["name"] for r in query(
        pre_reservation_db,
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    assert "idx_self_use_items_batch" in names
    assert "idx_products_active_name" in names
