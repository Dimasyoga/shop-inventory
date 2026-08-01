"""Voiding a restock or self-use batch that should not have been entered.

A void is a reversing batch, not an edit: the original stays, its negated twin sits
beside it, and the two cancel. That keeps restock spend append-only, so a month already
reported keeps the figure it printed and the correction lands in the month it was made.

The hard part is cost_price, which create_restock rolls forward as a weighted average.
Reversing an average needs the stock level it was computed against, and sales since have
moved that -- so a snapshot taken at the time is restored when it is still exact, and the
product is flagged for a human when it is not. These tests pin which case is which.
"""
from datetime import datetime, timedelta, timezone

import pytest

import database
import services
from services import ServiceError, NotFoundError


def utc_now():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def product(insert):
    def _make(name="Kopi", price=20000, cost=0, stock=0, sku=None):
        return insert("products", stamp(utc_now()), name=name, sku=sku, price=price,
                      cost_price=cost, stock_qty=stock)
    return _make


def call(fn, *args, **kwargs):
    db = database.get_db()
    try:
        return fn(db, *args, **kwargs)
    finally:
        db.close()


def one(sql, params=()):
    conn = database.get_db()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def product_row(pid):
    return one("SELECT stock_qty, cost_price, cost_review_needed FROM products WHERE id = ?",
               (pid,))


# --- Refusals ---

def test_voiding_a_missing_batch_raises_not_found(db_path):
    with pytest.raises(NotFoundError):
        call(services.void_restock, 999)


def test_a_batch_cannot_be_voided_twice(db_path, product):
    pid = product()
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 1000}])
    call(services.void_restock, batch['batch_id'])
    with pytest.raises(ServiceError, match='already voided'):
        call(services.void_restock, batch['batch_id'])


def test_a_void_cannot_itself_be_voided(db_path, product):
    """Reversing a reversal is just the original again -- re-enter it by hand instead."""
    pid = product()
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 1000}])
    void = call(services.void_restock, batch['batch_id'])
    with pytest.raises(ServiceError, match='itself a void'):
        call(services.void_restock, void['void_batch_id'])


def test_the_void_is_refused_when_the_stock_has_been_sold(db_path, product):
    """All-or-nothing, like create_self_use: never drive stock negative to force it."""
    pid = product(stock=0)
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 1000}])
    order = call(services.create_order, [{'product_id': pid, 'quantity': 8}])
    call(services.confirm_order, order['order_id'])
    call(services.complete_order, order['order_id'])

    with pytest.raises(ServiceError, match='no longer has'):
        call(services.void_restock, batch['batch_id'])
    # And nothing moved: the failed void rolled back whole.
    assert product_row(pid)['stock_qty'] == 2
    assert one("SELECT COUNT(*) AS n FROM restock_batches")['n'] == 1


# --- Stock and money ---

def test_the_void_reverses_stock_and_the_invoice(db_path, product):
    kopi = product(name='Kopi')
    gula = product(name='Gula', sku='G1')
    batch = call(services.create_restock,
                 [{'product_id': kopi, 'qty': 10, 'unit_price': 12000},
                  {'product_id': gula, 'qty': 5, 'unit_price': 8000}],
                 discount=20000, shipping_cost=15000, admin_fee=2500)
    assert product_row(kopi)['stock_qty'] == 10

    result = call(services.void_restock, batch['batch_id'])

    assert product_row(kopi)['stock_qty'] == 0
    assert product_row(gula)['stock_qty'] == 0
    void = one("SELECT * FROM restock_batches WHERE id = ?", (result['void_batch_id'],))
    assert void['voids_batch_id'] == batch['batch_id']
    assert void['total_cost'] == -batch['total_cost']
    assert void['discount'] == -20000
    assert void['shipping_cost'] == -15000
    assert void['admin_fee'] == -2500
    # The pair sums to nothing, which is what makes restock spend net out.
    assert one("SELECT SUM(total_cost) AS t FROM restock_batches")['t'] == 0


def test_the_void_writes_a_reversing_stock_log(db_path, product):
    """The audit trail records the correction as a movement, never by deleting one."""
    pid = product()
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 1000}])
    call(services.void_restock, batch['batch_id'])
    logs = [dict(r) for r in _all(
        "SELECT change_qty, reason FROM stock_logs WHERE product_id = ? ORDER BY id", (pid,))]
    assert logs == [
        {'change_qty': 10, 'reason': f"restock batch #{batch['batch_id']}"},
        {'change_qty': -10, 'reason': f"void of restock batch #{batch['batch_id']}"},
    ]


def _all(sql, params=()):
    conn = database.get_db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_a_voided_batch_drops_out_of_the_restock_cost(db_path, product):
    pid = product()
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 1000}])
    call(services.void_restock, batch['batch_id'])
    summary = call(services.sales_summary, 'month', 0, timezone.utc)
    assert summary['restock_cost'] == 0
    assert summary['net_profit'] == 0


# --- Cost repair ---

def test_the_previous_cost_comes_back_when_nothing_blended_onto_it(db_path, product):
    """The exact case: one restock on top of a known cost, voided before any other."""
    pid = product(cost=10000, stock=4)
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 6, 'unit_price': 15000}])
    assert product_row(pid)['cost_price'] == 13000  # (4x10.000 + 6x15.000) / 10

    result = call(services.void_restock, batch['batch_id'])

    assert product_row(pid)['cost_price'] == 10000
    assert product_row(pid)['cost_review_needed'] == 0
    assert result['restored'] == ['Kopi']
    assert result['flagged'] == []


def test_a_first_restock_voids_back_to_no_cost_at_all(db_path, product):
    """cost_before is 0 and there is nothing behind it, so 0 is the truth, not a gap."""
    pid = product(cost=0, stock=0)
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 12000}])
    assert product_row(pid)['cost_price'] == 12000

    result = call(services.void_restock, batch['batch_id'])

    assert product_row(pid)['cost_price'] == 0
    assert product_row(pid)['cost_review_needed'] == 0
    assert result['restored'] == ['Kopi']


def test_a_later_restock_makes_the_cost_unrecoverable_and_flags_it(db_path, product):
    """The average has moved on; inventing the counterfactual would be worse than saying so."""
    pid = product(cost=10000, stock=4)
    first = call(services.create_restock, [{'product_id': pid, 'qty': 6, 'unit_price': 15000}])
    call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 20000}])
    blended = product_row(pid)['cost_price']

    result = call(services.void_restock, first['batch_id'])

    assert product_row(pid)['cost_price'] == blended  # untouched, not guessed at
    assert product_row(pid)['cost_review_needed'] == 1
    assert result['flagged'] == ['Kopi']
    assert result['restored'] == []


def test_a_later_batch_that_was_itself_voided_does_not_block_the_restore(db_path, product):
    """It describes a movement that no longer applies, so it cannot have blended anything."""
    pid = product(cost=10000, stock=4)
    first = call(services.create_restock, [{'product_id': pid, 'qty': 6, 'unit_price': 15000}])
    second = call(services.create_restock, [{'product_id': pid, 'qty': 10, 'unit_price': 20000}])
    call(services.void_restock, second['batch_id'])

    result = call(services.void_restock, first['batch_id'])

    assert result['restored'] == ['Kopi']
    assert product_row(pid)['cost_price'] == 10000


def test_a_product_listed_twice_restores_from_the_first_lines_snapshot(db_path, product):
    """The second line averaged onto what the first produced, not onto the pre-batch cost."""
    pid = product(cost=10000, stock=4)
    batch = call(services.create_restock,
                 [{'product_id': pid, 'qty': 6, 'unit_price': 15000},
                  {'product_id': pid, 'qty': 5, 'unit_price': 18000}])

    result = call(services.void_restock, batch['batch_id'])

    assert product_row(pid)['cost_price'] == 10000
    assert result['restored'] == ['Kopi']
    assert product_row(pid)['stock_qty'] == 4


def test_a_legacy_line_with_no_snapshot_is_flagged_rather_than_zeroed(db_path, product):
    """cost_before 0 behind an earlier batch is a pre-upgrade row, not a recorded zero."""
    pid = product(cost=0, stock=0)
    call(services.create_restock, [{'product_id': pid, 'qty': 4, 'unit_price': 9000}])
    second = call(services.create_restock, [{'product_id': pid, 'qty': 6, 'unit_price': 15000}])
    conn = database.get_db()
    conn.execute("UPDATE restock_items SET cost_before = 0 WHERE batch_id = ?",
                 (second['batch_id'],))
    conn.commit()
    conn.close()
    blended = product_row(pid)['cost_price']

    result = call(services.void_restock, second['batch_id'])

    assert result['flagged'] == ['Kopi']
    assert product_row(pid)['cost_price'] == blended
    assert product_row(pid)['cost_review_needed'] == 1


# --- Sales already made ---

def test_sales_that_snapshotted_the_voided_cost_are_reported_not_rewritten(db_path, product):
    """A snapshot is never rewritten (see create_order); the void says how many there are."""
    pid = product(cost=10000, stock=4)
    batch = call(services.create_restock, [{'product_id': pid, 'qty': 6, 'unit_price': 15000}])
    order = call(services.create_order, [{'product_id': pid, 'quantity': 2}])
    call(services.confirm_order, order['order_id'])
    call(services.complete_order, order['order_id'])
    # Put back what the sale took, so the void has the stock to reverse.
    call(services.create_restock, [{'product_id': pid, 'qty': 2, 'unit_price': 15000}])

    result = call(services.void_restock, batch['batch_id'])

    assert result['affected_sales'] == 1
    line = one("SELECT unit_cost FROM order_items WHERE order_id = ?", (order['order_id'],))
    assert line['unit_cost'] == 13000  # the cost that was live when the order was created


def test_sales_from_before_the_batch_are_not_counted(db_path, insert, product):
    pid = product(cost=10000, stock=10)
    old = stamp(utc_now() - timedelta(days=3))
    # order_items carries no created_at of its own -- the window is the order's.
    order_id = insert("orders", old, status='completed', total_amount=20000)
    conn = database.get_db()
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                 " unit_cost, subtotal) VALUES (?, ?, ?, ?, ?, ?)",
                 (order_id, pid, 1, 20000, 10000, 20000))
    conn.commit()
    conn.close()

    batch = call(services.create_restock, [{'product_id': pid, 'qty': 6, 'unit_price': 15000}])
    result = call(services.void_restock, batch['batch_id'])
    assert result['affected_sales'] == 0


# --- Self use ---

def test_voiding_self_use_puts_the_stock_back(db_path, product):
    pid = product(price=20000, cost=10000, stock=10)
    batch = call(services.create_self_use, [{'product_id': pid, 'qty': 3}])
    assert product_row(pid)['stock_qty'] == 7

    result = call(services.void_self_use, batch['batch_id'])

    assert product_row(pid)['stock_qty'] == 10
    void = one("SELECT * FROM self_use_batches WHERE id = ?", (result['void_batch_id'],))
    assert void['voids_batch_id'] == batch['batch_id']
    assert void['total_value'] == -60000
    assert one("SELECT SUM(total_value) AS t FROM self_use_batches")['t'] == 0


def test_voiding_self_use_leaves_the_cost_alone(db_path, product):
    """Self use never touched cost_price, so there is nothing to restore or doubt."""
    pid = product(price=20000, cost=10000, stock=10)
    batch = call(services.create_self_use, [{'product_id': pid, 'qty': 3}])
    call(services.void_self_use, batch['batch_id'])
    assert product_row(pid)['cost_price'] == 10000
    assert product_row(pid)['cost_review_needed'] == 0


def test_a_voided_self_use_drops_out_of_the_summary(db_path, product):
    pid = product(price=20000, cost=10000, stock=10)
    batch = call(services.create_self_use, [{'product_id': pid, 'qty': 3}])
    call(services.void_self_use, batch['batch_id'])
    assert call(services.sales_summary, 'month', 0, timezone.utc)['self_use_value'] == 0


def test_self_use_void_refusals_match_restock(db_path, product):
    pid = product(price=20000, cost=10000, stock=10)
    batch = call(services.create_self_use, [{'product_id': pid, 'qty': 3}])
    void = call(services.void_self_use, batch['batch_id'])
    with pytest.raises(ServiceError, match='already voided'):
        call(services.void_self_use, batch['batch_id'])
    with pytest.raises(ServiceError, match='itself a void'):
        call(services.void_self_use, void['void_batch_id'])
    with pytest.raises(NotFoundError):
        call(services.void_self_use, 999)
