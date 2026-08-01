"""The orders list is paged, and a single order is fetchable on its own.

The page used to read every order the shop had ever taken on each load -- twice,
once server-side into a template variable nothing rendered and once through the API
-- and then fan a second query out per row for its lines. It also found an order to
view or edit by pulling the whole list and searching it in the browser, which paging
would have quietly broken: an order on page 3 is not in the array the page is holding.
"""
import pytest

import database
import services

PAGE = 10  # app.ORDERS_PAGE_SIZE


@pytest.fixture
def db(db_path):
    """An open connection for calling services directly.

    Depends on db_path: get_db() reads DB_PATH at call time, so a fixture that opens
    a connection without waiting for the monkeypatch connects to the source tree's
    own database.
    """
    conn = database.get_db()
    yield conn
    conn.close()


@pytest.fixture
def sql_log(monkeypatch):
    """Every statement the request path runs, via sqlite3's own trace hook.

    Wrapping Connection.execute is not an option -- it is a C type and rejects
    attribute assignment -- and counting queries is the only way to assert that a
    page costs a fixed number of them rather than one per row.
    """
    import app as app_module
    seen = []
    real = database.get_db

    def traced():
        conn = real()
        conn.set_trace_callback(lambda sql: seen.append(' '.join(sql.split())))
        return conn

    # app.py imported get_db by name, so its own reference is the one that matters.
    monkeypatch.setattr(app_module, 'get_db', traced)
    return seen


def product(name='Kopi', stock=10000, price=1000):
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO products (name, price, cost_price, stock_qty) VALUES (?, ?, ?, ?)",
        (name, price, 500, stock))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def make_orders(pid, count, status=None):
    """Create `count` orders, oldest first, with distinct created_at values."""
    conn = database.get_db()
    ids = []
    for n in range(count):
        cur = conn.execute(
            "INSERT INTO orders (status, total_amount, created_at) VALUES (?, ?, ?)",
            (status or 'draft', 1000, f'2026-07-{n + 1:02d} 08:00:00'))
        oid = cur.lastrowid
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " unit_cost, subtotal) VALUES (?, ?, 1, 1000, 500, 1000)", (oid, pid))
        ids.append(oid)
    conn.commit()
    conn.close()
    return ids


def page(client, n=0, **params):
    query = '&'.join([f'page={n}'] + [f'{k}={v}' for k, v in params.items()])
    return client.get(f'/api/orders?{query}').get_json()


# --- Paging ---

def test_a_page_holds_one_screenful(client, db_path):
    pid = product()
    make_orders(pid, 25)
    body = page(client)
    assert len(body['orders']) == PAGE
    assert body['has_more'] is True
    assert body['page'] == 0


def test_the_last_page_says_there_is_no_more(client, db_path):
    pid = product()
    make_orders(pid, 25)
    body = page(client, 2)
    assert len(body['orders']) == 5
    assert body['has_more'] is False


def test_every_order_appears_exactly_once_across_the_pages(client, db_path):
    # The point of paging: nothing may be skipped or shown twice at a boundary.
    pid = product()
    created = make_orders(pid, 25)
    seen = []
    for n in range(3):
        seen += [o['id'] for o in page(client, n)['orders']]
    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen))


def test_orders_taken_in_the_same_second_still_page_cleanly(client, db_path):
    """created_at is second-resolution, so ties need a stable tiebreak.

    Without one their relative order is up to the query planner, and a row can drift
    across the page boundary between two requests -- appearing twice, or not at all.
    """
    pid = product()
    conn = database.get_db()
    ids = []
    for _ in range(25):
        cur = conn.execute("INSERT INTO orders (status, total_amount, created_at)"
                           " VALUES ('draft', 1000, '2026-07-05 09:00:00')")
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()

    seen = []
    for n in range(3):
        seen += [o['id'] for o in page(client, n)['orders']]
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))


def test_pages_run_newest_first(client, db_path):
    pid = product()
    created = make_orders(pid, 15)
    first = [o['id'] for o in page(client, 0)['orders']]
    assert first == list(reversed(created))[:PAGE]


def test_a_page_past_the_end_is_empty_rather_than_an_error(client, db_path):
    pid = product()
    make_orders(pid, 5)
    body = page(client, 9)
    assert body['orders'] == []
    assert body['has_more'] is False


def test_a_nonsense_page_is_refused(client, db_path):
    assert client.get('/api/orders?page=-1').status_code == 400
    assert client.get('/api/orders?page=abc').status_code == 400


def test_a_filter_pages_within_its_own_results(client, db_path):
    pid = product()
    make_orders(pid, 12, status='draft')
    make_orders(pid, 3, status='completed')
    body = page(client, 0, status='completed')
    assert len(body['orders']) == 3
    assert body['has_more'] is False
    assert {o['status'] for o in body['orders']} == {'completed'}


def test_search_still_matches_the_order_number(client, db_path):
    pid = product()
    ids = make_orders(pid, 12)
    target = ids[0]
    found = [o['id'] for o in page(client, 0, search=target)['orders']]
    assert target in found


# --- Lines come with the page ---

def test_each_order_carries_its_lines(client, db_path):
    pid = product()
    make_orders(pid, 3)
    for order in page(client)['orders']:
        assert len(order['items']) == 1
        assert order['items'][0]['product_name'] == 'Kopi'


def test_the_lines_of_a_page_are_fetched_in_one_query(db, db_path):
    """A query per row is what made the old page cost grow with the whole history."""
    pid = product()
    make_orders(pid, 10)
    seen = []
    db.set_trace_callback(lambda sql: seen.append(' '.join(sql.split())))
    orders, _ = services.list_orders(db, page_size=PAGE)
    db.set_trace_callback(None)
    assert len(orders) == PAGE
    assert len([q for q in seen if 'FROM order_items' in q]) == 1


def test_an_order_with_no_lines_still_lists(client, db_path):
    # Defensive: nothing writes one, but a header with no lines must not drop out of
    # the page or arrive without the key the table reads.
    conn = database.get_db()
    conn.execute("INSERT INTO orders (status, total_amount) VALUES ('draft', 0)")
    conn.commit()
    conn.close()
    orders = page(client)['orders']
    assert len(orders) == 1
    assert orders[0]['items'] == []


# --- One order on its own ---

def test_a_single_order_is_fetchable_by_id(client, db_path):
    pid = product()
    ids = make_orders(pid, 25)
    # Deliberately one that no longer fits on the first page.
    target = ids[0]
    body = client.get(f'/api/orders/{target}').get_json()
    assert body['id'] == target
    assert len(body['items']) == 1
    assert body['items'][0]['product_name'] == 'Kopi'


def test_fetching_an_order_that_does_not_exist(client, db_path):
    res = client.get('/api/orders/999')
    assert res.status_code == 404
    assert 'error' in res.get_json()


# --- The page itself ---

def test_the_orders_page_does_not_read_the_orders_table(client, db_path, sql_log):
    # It renders an empty table that loadOrders() fills from the API. Rendering the
    # orders server-side as well meant reading every one of them to throw them away.
    pid = product()
    make_orders(pid, 30)
    assert client.get('/orders').status_code == 200
    assert not [q for q in sql_log if 'FROM orders' in q]


def test_a_page_costs_a_fixed_number_of_queries(client, db_path, sql_log):
    """Whatever the shop's history, one page is one query for the orders and one for
    their lines. This is the regression that matters: reintroducing a per-row lookup
    would pass every other test in this file."""
    pid = product()
    make_orders(pid, 40)
    sql_log.clear()
    client.get('/api/orders?page=0')
    assert len([q for q in sql_log if 'FROM orders' in q]) == 1
    assert len([q for q in sql_log if 'FROM order_items' in q]) == 1
