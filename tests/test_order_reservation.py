"""Open orders hold their stock, and drafts can be edited.

Stock used to move only on completion, so two drafts for the last unit both passed
their check and the second failed at completion -- after the customer had been told
yes. An order now takes its units the moment it is written and gives them back if it
is cancelled, and stock_qty goes on meaning physical stock throughout.

The editing half is what makes holding bearable: a wrong quantity used to mean
cancelling and retyping the order, which under the new rules would also mean
surrendering the stock to whoever asked next.
"""
import pytest

import database
import services
from services import NotFoundError, ServiceError


def product(name='Kopi', stock=5, price=20000, cost=12000):
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO products (name, price, cost_price, stock_qty) VALUES (?, ?, ?, ?)",
        (name, price, cost, stock))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def state(product_id):
    """(physical stock, reserved) for a product."""
    conn = database.get_db()
    row = conn.execute("SELECT stock_qty, reserved_qty FROM products WHERE id = ?",
                       (product_id,)).fetchone()
    conn.close()
    return row['stock_qty'], row['reserved_qty']


def order_status(order_id):
    conn = database.get_db()
    row = conn.execute("SELECT status, total_amount FROM orders WHERE id = ?",
                       (order_id,)).fetchone()
    conn.close()
    return row['status'], row['total_amount']


def lines(order_id):
    conn = database.get_db()
    rows = conn.execute("SELECT product_id, quantity, unit_price FROM order_items"
                        " WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


@pytest.fixture
def db(db_path):
    """An open connection for calling services directly.

    Depends on db_path rather than merely being used alongside it: get_db() reads
    DB_PATH at call time, so a fixture that opens a connection without waiting for
    the monkeypatch connects to whatever database the source tree happens to hold.
    """
    conn = database.get_db()
    yield conn
    conn.close()


# --- Holding stock ---

def test_a_new_order_holds_its_stock_without_moving_it(db, db_path):
    pid = product(stock=5)
    services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    # Nothing has left the shelf: a stock count would still find five.
    assert state(pid) == (5, 2)


def test_a_second_order_cannot_take_held_units(db, db_path):
    """The oversell this whole feature exists to stop."""
    pid = product(stock=5)
    services.create_order(db, [{'product_id': pid, 'quantity': 4}])
    with pytest.raises(ServiceError):
        services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    assert state(pid) == (5, 4)  # the failed order took nothing


def test_the_refusal_says_how_many_are_held(db, db_path):
    # "Insufficient stock" next to a products page reading 5 is a contradiction the
    # seller cannot act on; naming the holder points at the order to chase.
    pid = product(name='Kopi', stock=5)
    services.create_order(db, [{'product_id': pid, 'quantity': 4}])
    with pytest.raises(ServiceError) as exc:
        services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    assert exc.value.params == {'n': 1, 'name': 'Kopi', 'held': 4}


def test_plain_out_of_stock_still_reads_as_out_of_stock(db, db_path):
    # Nothing is held, so there is no other order to talk about.
    pid = product(name='Kopi', stock=1)
    with pytest.raises(ServiceError) as exc:
        services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    assert exc.value.template == 'Insufficient stock for {name}'


def test_taking_exactly_what_is_available_is_allowed(db, db_path):
    pid = product(stock=5)
    services.create_order(db, [{'product_id': pid, 'quantity': 5}])
    assert state(pid) == (5, 5)


def test_two_lines_of_the_same_product_both_count(db, db_path):
    # Nothing stops a seller adding the same product twice; the holds have to add up
    # or the order promises more than it took.
    pid = product(stock=5)
    services.create_order(db, [{'product_id': pid, 'quantity': 2},
                               {'product_id': pid, 'quantity': 3}])
    assert state(pid) == (5, 5)


def test_a_failed_line_releases_what_earlier_lines_took(db, db_path):
    # Otherwise the first product stays held against an order that was never written.
    kopi = product(name='Kopi', stock=5)
    teh = product(name='Teh', stock=1)
    with pytest.raises(ServiceError):
        services.create_order(db, [{'product_id': kopi, 'quantity': 2},
                                   {'product_id': teh, 'quantity': 9}])
    assert state(kopi) == (5, 0)
    assert state(teh) == (1, 0)


def test_an_unknown_product_releases_earlier_lines_too(db, db_path):
    kopi = product(name='Kopi', stock=5)
    with pytest.raises(NotFoundError):
        services.create_order(db, [{'product_id': kopi, 'quantity': 2},
                                   {'product_id': 9999, 'quantity': 1}])
    assert state(kopi) == (5, 0)


# --- Releasing it again ---

def test_cancelling_gives_the_units_back(db, db_path):
    pid = product(stock=5)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 4}])
    services.cancel_order(db, result['order_id'])
    assert state(pid) == (5, 0)
    # And the units are genuinely spendable again, not merely uncounted.
    services.create_order(db, [{'product_id': pid, 'quantity': 5}])


def test_confirming_changes_nothing_about_the_hold(db, db_path):
    # Confirmation is about the money; the units were already spoken for.
    pid = product(stock=5)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    services.confirm_order(db, result['order_id'])
    assert state(pid) == (5, 2)


def test_a_confirmed_order_still_releases_on_cancel(db, db_path):
    pid = product(stock=5)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    services.confirm_order(db, result['order_id'])
    services.cancel_order(db, result['order_id'])
    assert state(pid) == (5, 0)


def test_completing_turns_the_hold_into_a_withdrawal(db, db_path):
    pid = product(stock=5)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    services.confirm_order(db, result['order_id'])
    services.complete_order(db, result['order_id'])
    # Down by two and holding nothing -- not double-counted as both gone and reserved.
    assert state(pid) == (3, 0)


def test_completing_leaves_other_orders_holds_alone(db, db_path):
    pid = product(stock=5)
    first = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    services.create_order(db, [{'product_id': pid, 'quantity': 3}])
    services.confirm_order(db, first['order_id'])
    services.complete_order(db, first['order_id'])
    assert state(pid) == (3, 3)


def test_stock_physically_removed_can_still_strand_a_hold(db, db_path):
    """The documented edge: a hold is a claim on stock, not a lock on the shelf.

    Self use records something that already happened, so it is not refused; the order
    that was counting on those units fails at completion, exactly as it did before.
    """
    pid = product(stock=5)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 5}])
    services.create_self_use(db, [{'product_id': pid, 'qty': 5}])
    services.confirm_order(db, result['order_id'])
    with pytest.raises(ServiceError):
        services.complete_order(db, result['order_id'])


# --- Editing a draft ---

def test_raising_a_quantity_takes_more(db, db_path):
    pid = product(stock=10)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    services.update_order(db, result['order_id'], [{'product_id': pid, 'quantity': 5}])
    assert state(pid) == (10, 5)
    assert lines(result['order_id']) == [(pid, 5, 20000)]


def test_lowering_a_quantity_gives_the_difference_back(db, db_path):
    pid = product(stock=10)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 8}])
    services.update_order(db, result['order_id'], [{'product_id': pid, 'quantity': 3}])
    assert state(pid) == (10, 3)


def test_an_unchanged_quantity_survives_a_sold_out_product(db, db_path):
    """Releasing before re-taking is what makes this work.

    Delta arithmetic would get here too, but only by special-casing zero; releasing
    first means the order is always bidding against a shelf that includes its own
    units, so any edit that does not ask for more can never be refused.
    """
    pid = product(stock=5)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 5}])
    services.update_order(db, result['order_id'], [{'product_id': pid, 'quantity': 5}])
    assert state(pid) == (5, 5)


def test_dropping_a_line_frees_it_for_another_in_the_same_edit(db, db_path):
    # Swapping one product for another when the shop is at its limit is the ordinary
    # case for a draft, and it only works if the release lands before the new hold.
    kopi = product(name='Kopi', stock=3)
    teh = product(name='Teh', stock=3)
    result = services.create_order(db, [{'product_id': kopi, 'quantity': 3}])
    services.update_order(db, result['order_id'], [{'product_id': teh, 'quantity': 3}])
    assert state(kopi) == (3, 0)
    assert state(teh) == (3, 3)
    assert lines(result['order_id']) == [(teh, 3, 20000)]


def test_an_edit_that_asks_too_much_keeps_the_old_order_intact(db, db_path):
    # The rollback has to restore the holds it released on the way in, or a rejected
    # edit quietly disarms the order it failed to change.
    pid = product(stock=5)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 3}])
    with pytest.raises(ServiceError):
        services.update_order(db, result['order_id'], [{'product_id': pid, 'quantity': 9}])
    assert state(pid) == (5, 3)
    assert lines(result['order_id']) == [(pid, 3, 20000)]
    assert order_status(result['order_id'])[1] == 60000


def test_an_edit_is_priced_at_todays_price(db, db_path):
    # Nothing has been sold yet, so an edited draft quotes what a new order would.
    pid = product(stock=10, price=20000)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    conn = database.get_db()
    conn.execute("UPDATE products SET price = 25000 WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    services.update_order(db, result['order_id'], [{'product_id': pid, 'quantity': 2}])
    assert lines(result['order_id']) == [(pid, 2, 25000)]
    assert order_status(result['order_id'])[1] == 50000


def test_a_confirmed_order_cannot_be_edited(db, db_path):
    # The money has been taken; the lines are what the customer paid for.
    pid = product(stock=10)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    services.confirm_order(db, result['order_id'])
    with pytest.raises(ServiceError) as exc:
        services.update_order(db, result['order_id'], [{'product_id': pid, 'quantity': 3}])
    assert exc.value.template == 'Only draft orders can be edited'
    assert state(pid) == (10, 2)


def test_a_cancelled_order_cannot_be_edited_back_to_life(db, db_path):
    pid = product(stock=10)
    result = services.create_order(db, [{'product_id': pid, 'quantity': 2}])
    services.cancel_order(db, result['order_id'])
    with pytest.raises(ServiceError):
        services.update_order(db, result['order_id'], [{'product_id': pid, 'quantity': 2}])
    assert state(pid) == (10, 0)


def test_editing_an_order_that_does_not_exist(db, db_path):
    pid = product()
    with pytest.raises(NotFoundError):
        services.update_order(db, 999, [{'product_id': pid, 'quantity': 1}])


# --- Through the API ---

def test_the_endpoint_edits_a_draft(client, db_path):
    pid = product(stock=10)
    created = client.post('/api/orders', json={'items': [{'product_id': pid, 'quantity': 2}]})
    order_id = created.get_json()['order_id']

    res = client.put(f'/api/orders/{order_id}',
                     json={'items': [{'product_id': pid, 'quantity': 4}]})
    assert res.status_code == 200
    assert res.get_json()['total'] == 80000
    assert state(pid) == (10, 4)


def test_the_endpoint_rejects_an_edit_it_cannot_meet(client, db_path):
    pid = product(stock=3)
    created = client.post('/api/orders', json={'items': [{'product_id': pid, 'quantity': 3}]})
    order_id = created.get_json()['order_id']

    res = client.put(f'/api/orders/{order_id}',
                     json={'items': [{'product_id': pid, 'quantity': 4}]})
    assert res.status_code == 400
    assert 'error' in res.get_json()
    assert state(pid) == (3, 3)


def test_the_endpoint_validates_lines_the_same_way_create_does(client, db_path):
    pid = product(stock=10)
    created = client.post('/api/orders', json={'items': [{'product_id': pid, 'quantity': 1}]})
    order_id = created.get_json()['order_id']

    assert client.put(f'/api/orders/{order_id}', json={'items': []}).status_code == 400
    assert client.put(f'/api/orders/{order_id}',
                      json={'items': [{'product_id': pid, 'quantity': 0}]}).status_code == 400
    assert client.put(f'/api/orders/{order_id}',
                      json={'items': [{'product_id': pid, 'quantity': True}]}).status_code == 400
    # None of the rejected shapes touched the order.
    assert state(pid) == (10, 1)


def test_editing_a_missing_order_is_a_404(client, db_path):
    pid = product()
    res = client.put('/api/orders/999', json={'items': [{'product_id': pid, 'quantity': 1}]})
    assert res.status_code == 404


def test_the_products_api_reports_what_is_available(client, db_path):
    pid = product(stock=5)
    client.post('/api/orders', json={'items': [{'product_id': pid, 'quantity': 2}]})
    row = next(p for p in client.get('/api/products').get_json() if p['id'] == pid)
    assert (row['stock_qty'], row['reserved_qty'], row['available']) == (5, 2, 3)
