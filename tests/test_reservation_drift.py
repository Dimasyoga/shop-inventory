"""reserved_qty is a counter; the open order lines are what justify it.

Every path that moves the counter does so in one statement inside a transaction that
rolls back whole, so drift is not supposed to happen. These tests exist because when
it does, nothing else would say so: _release_stock clamps at zero, which turns an
over-release into a silently wrong figure rather than a visible one, and the symptom
reaches the shop owner as stock the app will not sell.

Drift is forced here by writing reserved_qty directly -- there is deliberately no
service call that can produce it, and a test that could reach this state through the
public API would be reporting a bug in the holding code instead.
"""
import sqlite3

import pytest

import app as app_module
import database
import services


def product(name='Kopi', stock=10, reserved=0):
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO products (name, price, cost_price, stock_qty, reserved_qty)"
        " VALUES (?, 20000, 12000, ?, ?)", (name, stock, reserved))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def set_reserved(product_id, qty):
    """Corrupt the counter behind the services' back."""
    conn = database.get_db()
    conn.execute("UPDATE products SET reserved_qty = ? WHERE id = ?", (qty, product_id))
    conn.commit()
    conn.close()


def reserved_of(product_id):
    conn = database.get_db()
    row = conn.execute("SELECT reserved_qty FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return row['reserved_qty']


def test_no_drift_reported_for_a_consistent_database(db_path):
    pid = product()
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 3}])
    assert services.reservation_drift(conn) == []
    conn.close()


def test_holds_from_draft_and_confirmed_orders_both_count_as_justified(db_path):
    pid = product(stock=20)
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 2}])
    res = services.create_order(conn, [{'product_id': pid, 'quantity': 5}])
    services.confirm_order(conn, res['order_id'])
    assert reserved_of(pid) == 7
    assert services.reservation_drift(conn) == []
    conn.close()


def test_completed_and_cancelled_orders_justify_nothing(db_path):
    """The statuses that stop holding stock must stop justifying a hold too, or a
    repair would restore reservations the order already gave back."""
    pid = product(stock=20)
    conn = database.get_db()
    done = services.create_order(conn, [{'product_id': pid, 'quantity': 4}])
    services.confirm_order(conn, done['order_id'])
    services.complete_order(conn, done['order_id'])
    gone = services.create_order(conn, [{'product_id': pid, 'quantity': 3}])
    services.cancel_order(conn, gone['order_id'])
    assert reserved_of(pid) == 0
    assert services.reservation_drift(conn) == []
    conn.close()


def test_the_check_reads_the_open_orders_not_the_whole_history(db_path):
    """What the check costs must depend on how many orders are *open*, not on how many
    the shop has ever taken -- open orders do not accumulate, closed ones do.

    The query recomputed the held total per product with a correlated subquery, so
    finding the handful of lines still open meant walking every line that product had
    ever sold: 194 ms at two years of a thousand orders a month, the slowest endpoint
    in the app, and the same query bootstrap() runs at startup. Asserted on the plan
    rather than a timing, like tests/test_indexes.py and for the same reason: the
    shop's own database is far too small for the difference to show up as one.
    """
    conn = database.get_db()
    plan = ' '.join(r[3] for r in conn.execute(
        'EXPLAIN QUERY PLAN ' + services._DRIFT_SQL, services.OPEN_STATUSES))
    conn.close()
    # The open orders are aggregated once, up front...
    assert 'MATERIALIZE' in plan
    # ...and reached through the status index rather than by reading every order.
    assert 'idx_orders_status_created' in plan
    # A correlated subquery here is the regression: it re-runs per product.
    assert 'CORRELATED' not in plan


def test_a_long_closed_history_does_not_change_the_answer(db_path):
    """The behavioural half of the above: whatever the plan does, piling up completed
    orders against a product must leave the drift figures exactly where they were."""
    pid = product(stock=500)
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 3}])
    set_reserved(pid, 8)
    before = [tuple(r) for r in services.reservation_drift(conn)]

    for _ in range(20):
        done = services.create_order(conn, [{'product_id': pid, 'quantity': 2}])
        services.confirm_order(conn, done['order_id'])
        services.complete_order(conn, done['order_id'])
    set_reserved(pid, 8)  # the completions moved the counter; put the drift back

    assert [tuple(r) for r in services.reservation_drift(conn)] == before
    conn.close()


def test_over_holding_is_reported_with_both_figures(db_path):
    pid = product()
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 3}])
    set_reserved(pid, 8)

    drift = services.reservation_drift(conn)
    assert len(drift) == 1
    assert drift[0]['id'] == pid
    assert drift[0]['reserved_qty'] == 8
    assert drift[0]['expected'] == 3
    conn.close()


def test_under_holding_is_reported_too(db_path):
    """The direction that hands out stock twice. Less visible than over-holding --
    nothing refuses a sale -- so it matters more that the check catches it."""
    pid = product()
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 4}])
    set_reserved(pid, 1)

    drift = services.reservation_drift(conn)
    assert [(r['reserved_qty'], r['expected']) for r in drift] == [(1, 4)]
    conn.close()


def test_repair_sets_the_counter_to_what_the_orders_justify(db_path):
    pid = product()
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 3}])
    set_reserved(pid, 9)

    repaired = services.repair_reservations(conn)
    assert [(r['reserved_qty'], r['expected']) for r in repaired] == [(9, 3)]
    assert reserved_of(pid) == 3
    assert services.reservation_drift(conn) == []
    conn.close()


def test_repair_reports_the_figures_it_replaced_not_the_new_ones(db_path):
    """The caller logs and shows what changed, which is only possible if the return
    value is the before state."""
    pid = product()
    conn = database.get_db()
    set_reserved(pid, 5)
    repaired = services.repair_reservations(conn)
    assert repaired[0]['reserved_qty'] == 5
    assert repaired[0]['expected'] == 0
    conn.close()


def test_repair_is_a_no_op_on_a_healthy_database(db_path):
    pid = product()
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 2}])
    assert services.repair_reservations(conn) == []
    assert reserved_of(pid) == 2
    conn.close()


def test_repair_leaves_other_products_alone(db_path):
    kopi, teh = product('Kopi'), product('Teh')
    conn = database.get_db()
    services.create_order(conn, [{'product_id': kopi, 'quantity': 2}])
    services.create_order(conn, [{'product_id': teh, 'quantity': 3}])
    set_reserved(kopi, 7)

    repaired = services.repair_reservations(conn)
    assert [r['id'] for r in repaired] == [kopi]
    assert reserved_of(kopi) == 2
    assert reserved_of(teh) == 3
    conn.close()


def test_repaired_stock_becomes_sellable_again(db_path):
    """The point of the whole exercise: units stuck behind a phantom hold come back."""
    pid = product(stock=5)
    conn = database.get_db()
    set_reserved(pid, 5)
    with pytest.raises(services.ServiceError):
        services.create_order(conn, [{'product_id': pid, 'quantity': 4}])

    services.repair_reservations(conn)
    services.create_order(conn, [{'product_id': pid, 'quantity': 4}])
    assert reserved_of(pid) == 4
    conn.close()


def test_archived_products_are_checked(db_path):
    """Held stock on an archived product is exactly what nobody would go looking for."""
    pid = product()
    conn = database.get_db()
    conn.execute("UPDATE products SET is_archived = 1 WHERE id = ?", (pid,))
    conn.commit()
    set_reserved(pid, 4)
    assert [r['id'] for r in services.reservation_drift(conn)] == [pid]
    conn.close()


# --- the routes ---

def test_check_endpoint_reports_drift(client, db_path):
    pid = product()
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 2}])
    conn.close()
    set_reserved(pid, 6)

    body = client.get('/api/stock/reservations/check').get_json()
    assert body['drift'] == [
        {'id': pid, 'name': 'Kopi', 'reserved': 6, 'expected': 2, 'difference': 4}]


def test_check_endpoint_does_not_repair(client, db_path):
    """Looking must not change anything -- the two actions are separate on purpose."""
    pid = product()
    set_reserved(pid, 6)
    client.get('/api/stock/reservations/check')
    assert reserved_of(pid) == 6


def test_repair_endpoint_fixes_and_reports(client, db_path):
    pid = product()
    conn = database.get_db()
    services.create_order(conn, [{'product_id': pid, 'quantity': 2}])
    conn.close()
    set_reserved(pid, 6)

    body = client.post('/api/stock/reservations/repair').get_json()
    assert body['success'] is True
    assert body['repaired'] == [
        {'id': pid, 'name': 'Kopi', 'reserved': 6, 'expected': 2, 'difference': 4}]
    assert reserved_of(pid) == 2
    assert client.get('/api/stock/reservations/check').get_json()['drift'] == []


def test_reservation_routes_require_login(db_path):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as anon:
        assert anon.get('/api/stock/reservations/check').status_code == 401
        assert anon.post('/api/stock/reservations/repair').status_code == 401


def test_startup_warning_does_not_raise_when_the_database_is_unreachable(monkeypatch):
    """A diagnostic must never be why the shop cannot boot."""
    def boom():
        raise sqlite3.OperationalError('unable to open database file')

    monkeypatch.setattr(app_module, 'get_db', boom)
    app_module._warn_on_reservation_drift()  # must not raise


def test_startup_warning_logs_each_drifted_product(db_path, caplog):
    pid = product()
    set_reserved(pid, 3)
    with caplog.at_level('WARNING'):
        app_module._warn_on_reservation_drift()
    assert 'Kopi' in caplog.text
    assert 'holding 3' in caplog.text
    # Reporting only: startup must not rewrite what customers are owed.
    assert reserved_of(pid) == 3
