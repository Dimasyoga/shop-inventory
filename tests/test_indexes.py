"""The indexes exist, and the queries that motivated them actually use them.

SQLite indexes primary keys and UNIQUE columns by itself but not foreign keys, so
every join from a batch or an order to its lines used to scan the whole table. On the
shop's own database that is invisible -- a few hundred rows -- which is exactly why
this asserts on query plans rather than on timings: an index the planner declines to
use costs writes and buys nothing, and nothing else in the suite would notice.
"""
import sqlite3

import database

EXPECTED = {
    'idx_order_items_order',
    'idx_order_items_product',
    'idx_orders_status_created',
    'idx_orders_created',
    'idx_restock_items_batch',
    'idx_self_use_items_batch',
    'idx_restock_items_product',
    'idx_restock_batches_voids',
    'idx_self_use_batches_voids',
    'idx_products_active_name',
}


def indexes(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    finally:
        conn.close()


def plan(path, sql):
    conn = sqlite3.connect(path)
    try:
        return ' '.join(r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + sql))
    finally:
        conn.close()


def test_a_fresh_database_has_every_index(db_path):
    assert indexes(db_path) == EXPECTED


def test_creating_them_twice_is_harmless(db_path):
    database.init_db()
    assert indexes(db_path) == EXPECTED


# --- The plans that justify each one ---

def test_an_orders_lines_are_looked_up_not_scanned(db_path):
    # Every order detail view, in the web UI and the bot.
    assert 'idx_order_items_order' in plan(
        db_path, "SELECT * FROM order_items WHERE order_id = 1")


def test_a_sales_window_seeks_completed_orders_by_date(db_path):
    # The shape under nearly every figure on the sales page and in the monthly report.
    p = plan(db_path, "SELECT oi.* FROM order_items oi JOIN orders o ON oi.order_id = o.id"
                      " WHERE o.status = 'completed' AND o.created_at >= '2026-07-01'"
                      " AND o.created_at < '2026-08-01'")
    assert 'idx_orders_status_created' in p
    # ...and reaches the lines of each order it finds through the other index, rather
    # than scanning order_items once per order.
    assert 'idx_order_items_order' in p


def test_the_orders_list_reads_in_date_order(db_path):
    # No status filter, so the composite index cannot serve this: its leading column
    # is not the sort. Without a plain created_at index the page sorts every order.
    p = plan(db_path, "SELECT * FROM orders ORDER BY created_at DESC LIMIT 10")
    assert 'idx_orders_created' in p
    assert 'TEMP B-TREE' not in p


def test_the_product_ranking_finds_a_products_sales(db_path):
    assert 'idx_order_items_product' in plan(
        db_path, "SELECT * FROM order_items WHERE product_id = 1")


def test_batch_lines_are_looked_up_by_batch(db_path):
    assert 'idx_restock_items_batch' in plan(
        db_path, "SELECT * FROM restock_items WHERE batch_id = 1")
    assert 'idx_self_use_items_batch' in plan(
        db_path, "SELECT * FROM self_use_items WHERE batch_id = 1")


def test_the_void_cost_check_walks_one_products_restocks(db_path):
    # services._surviving_restock_lines, which decides whether a voided restock can
    # have its cost snapshot restored.
    assert 'idx_restock_items_product' in plan(
        db_path, "SELECT * FROM restock_items WHERE product_id = 1 AND batch_id > 5")


def test_the_void_back_link_is_not_a_scan_per_row(db_path):
    # Each history page asks "was this batch voided?" once per row it renders. A scan
    # here makes the page quadratic in the number of batches ever recorded.
    assert 'idx_restock_batches_voids' in plan(
        db_path, "SELECT id FROM restock_batches WHERE voids_batch_id = 1")
    assert 'idx_self_use_batches_voids' in plan(
        db_path, "SELECT id FROM self_use_batches WHERE voids_batch_id = 1")


def test_the_active_catalogue_comes_back_in_name_order(db_path):
    p = plan(db_path, "SELECT * FROM products WHERE is_archived = 0 ORDER BY name")
    assert 'idx_products_active_name' in p
    assert 'TEMP B-TREE' not in p


def test_stock_logs_is_deliberately_unindexed(db_path):
    """It is written by every sale, restock and self use, and read by nothing.

    If a feature ever reads it back, this test should fail and be replaced by an
    index -- until then the writes should not be paying for one.
    """
    assert not [i for i in indexes(db_path) if 'stock_log' in i]
