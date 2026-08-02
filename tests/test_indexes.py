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
    'idx_restock_batches_created',
    'idx_self_use_batches_created',
    'idx_products_active_name',
    'idx_stock_logs_product',
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


BATCH_PAGE = ("SELECT b.id FROM {table} b {where}"
              " ORDER BY b.created_at DESC, b.id DESC LIMIT 11 OFFSET 0")


def test_a_batch_history_page_is_a_backwards_walk_and_needs_no_sort(db_path):
    """services._list_batches, behind the restock and self-use history tables.

    The sort is (created_at DESC, id DESC) and an index entry carries the rowid, which
    id is -- so the index already holds the rows in exactly that order and a page is a
    backwards scan that stops at the page size. Sorting instead would mean ordering
    every batch the shop has ever recorded to render ten of them.
    """
    for table in ('restock_batches', 'self_use_batches'):
        p = plan(db_path, BATCH_PAGE.format(table=table, where=''))
        assert f'idx_{table}_created' in p
        assert 'TEMP B-TREE' not in p


def test_a_months_batches_are_a_range_seek(db_path):
    """The same index serves the windowed form: the history page's period filter, and
    the sums behind sales_summary's restock cost and self-use value, which the dashboard
    runs on every load. Without it, totalling one month reads every batch ever written.
    """
    for table in ('restock_batches', 'self_use_batches'):
        p = plan(db_path, BATCH_PAGE.format(
            table=table,
            where="WHERE b.created_at >= '2026-07-01' AND b.created_at < '2026-08-01'"))
        assert f'idx_{table}_created' in p
        assert 'TEMP B-TREE' not in p


def test_the_active_catalogue_comes_back_in_name_order(db_path):
    p = plan(db_path, "SELECT * FROM products WHERE is_archived = 0 ORDER BY name")
    assert 'idx_products_active_name' in p
    assert 'TEMP B-TREE' not in p


HISTORY = ("SELECT s.*, p.name FROM stock_logs s JOIN products p ON p.id = s.product_id"
           " {where} ORDER BY s.id DESC LIMIT 26 OFFSET 0")


def test_one_products_stock_history_is_a_seek_and_needs_no_sort(db_path):
    """stock_logs was unindexed for as long as nothing read it; the history page does.

    Both halves matter. The index turns the filter from a scan of every movement the
    shop has ever recorded into a seek, and the ORDER BY comes out free: an index entry
    carries the rowid, and stock_logs.id *is* the rowid, so the index already orders
    each product's rows the way the page wants them.
    """
    p = plan(db_path, HISTORY.format(where="WHERE s.product_id = 1"))
    assert 'idx_stock_logs_product' in p
    assert 'TEMP B-TREE' not in p


def test_the_unfiltered_history_sorts_nothing_and_wants_no_index(db_path):
    """Descending rowid walks the table backwards and stops at the page size, so the
    all-products view needs no index of its own. A second one on created_at would widen
    every sale, restock and self-use write to serve a plan that already does not sort.
    """
    p = plan(db_path, HISTORY.format(where=""))
    assert 'TEMP B-TREE' not in p


def test_stock_logs_has_exactly_one_index(db_path):
    """The table the app writes to most. One index is justified above; a second needs
    its own plan assertion here, or it is costing every movement a write for nothing.
    """
    assert [i for i in indexes(db_path) if 'stock_log' in i] == ['idx_stock_logs_product']
