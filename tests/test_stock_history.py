"""The stock movement history: what stock_logs looks like once something reads it.

The table has been written by every sale, restock, self use and adjustment since long
before anything displayed it, so these tests care as much about what the history *omits*
as what it shows. Reservations are the notable omission -- an open order moves
reserved_qty and never touches stock_qty, so there is no movement to report until it
completes.
"""
import database
import services


def product(name='Kopi', sku='KOPI-1', stock=100):
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO products (name, sku, price, cost_price, stock_qty)"
        " VALUES (?, ?, 20000, 12000, ?)", (name, sku, stock))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def movements(**kwargs):
    conn = database.get_db()
    try:
        return services.list_stock_movements(conn, **kwargs)
    finally:
        conn.close()


def sell(product_id, qty, actor='web:admin'):
    conn = database.get_db()
    res = services.create_order(conn, [{'product_id': product_id, 'quantity': qty}])
    services.confirm_order(conn, res['order_id'])
    services.complete_order(conn, res['order_id'], actor=actor)
    conn.close()
    return res['order_id']


def test_a_sale_is_recorded_as_stock_leaving(db_path):
    pid = product()
    order_id = sell(pid, 3)

    rows, has_more = movements()
    assert has_more is False
    assert len(rows) == 1
    assert rows[0]['change_qty'] == -3
    assert rows[0]['reason'] == f'sale order #{order_id}'
    assert rows[0]['actor'] == 'web:admin'
    assert rows[0]['product_name'] == 'Kopi'
    assert rows[0]['product_sku'] == 'KOPI-1'


def test_a_restock_is_recorded_as_stock_arriving(db_path):
    pid = product(stock=0)
    conn = database.get_db()
    services.create_restock(conn, [{'product_id': pid, 'qty': 10, 'unit_price': 9000}],
                            actor='telegram:42')
    conn.close()

    rows, _ = movements()
    assert rows[0]['change_qty'] == 10
    assert rows[0]['actor'] == 'telegram:42'
    assert 'restock batch' in rows[0]['reason']


def test_an_open_order_records_no_movement(db_path):
    """The distinction the page's help text promises: holding stock is not moving it."""
    pid = product()
    conn = database.get_db()
    res = services.create_order(conn, [{'product_id': pid, 'quantity': 4}])
    services.confirm_order(conn, res['order_id'])
    conn.close()

    assert movements() == ([], False)

    # ...and the movement appears the moment the order is completed.
    conn = database.get_db()
    services.complete_order(conn, res['order_id'], actor='web:admin')
    conn.close()
    rows, _ = movements()
    assert [r['change_qty'] for r in rows] == [-4]


def test_a_cancelled_order_never_records_a_movement(db_path):
    pid = product()
    conn = database.get_db()
    res = services.create_order(conn, [{'product_id': pid, 'quantity': 4}])
    services.cancel_order(conn, res['order_id'])
    conn.close()
    assert movements() == ([], False)


def test_a_void_shows_as_its_own_reversing_movement(db_path):
    """Corrections are reversing entries, so the history shows both halves rather than
    the original disappearing."""
    pid = product(stock=0)
    conn = database.get_db()
    batch = services.create_restock(conn, [{'product_id': pid, 'qty': 10, 'unit_price': 9000}])
    services.void_restock(conn, batch['batch_id'])
    conn.close()

    rows, _ = movements()
    assert [r['change_qty'] for r in rows] == [-10, 10]
    assert 'void of restock batch' in rows[0]['reason']


def test_self_use_is_recorded(db_path):
    pid = product()
    conn = database.get_db()
    services.create_self_use(conn, [{'product_id': pid, 'qty': 2}], actor='web:admin')
    conn.close()
    rows, _ = movements()
    assert rows[0]['change_qty'] == -2
    assert 'self use batch' in rows[0]['reason']


def test_newest_first(db_path):
    pid = product()
    first = sell(pid, 1)
    second = sell(pid, 2)
    rows, _ = movements()
    assert [r['reason'] for r in rows] == [
        f'sale order #{second}', f'sale order #{first}']


def test_filtering_by_product(db_path):
    kopi, teh = product('Kopi', 'K-1'), product('Teh', 'T-1')
    sell(kopi, 1)
    sell(teh, 2)

    rows, _ = movements(product_id=teh)
    assert [r['product_name'] for r in rows] == ['Teh']
    assert [r['change_qty'] for r in rows] == [-2]


def test_paging_reports_has_more_and_does_not_overlap(db_path):
    pid = product()
    for _ in range(7):
        sell(pid, 1)

    first, has_more = movements(page=0, page_size=5)
    assert has_more is True
    assert len(first) == 5

    second, has_more = movements(page=1, page_size=5)
    assert has_more is False
    assert len(second) == 2
    assert not {r['id'] for r in first} & {r['id'] for r in second}


def test_paging_is_stable_when_movements_share_a_timestamp(db_path):
    """created_at is second-resolution, so a page of movements written in one second
    would have no inherent order if id were not the sort. Every row here lands in the
    same second, and the two pages must still partition the set exactly."""
    pid = product()
    conn = database.get_db()
    for _ in range(12):
        conn.execute(services.STOCK_LOG_INSERT, (pid, -1, 'same second', 'web:admin'))
    conn.commit()
    conn.close()

    first, _ = movements(page=0, page_size=6)
    second, _ = movements(page=1, page_size=6)
    ids = [r['id'] for r in first] + [r['id'] for r in second]
    assert len(set(ids)) == 12
    assert ids == sorted(ids, reverse=True)


def test_archived_products_keep_their_history(db_path):
    """A product is usually archived after its last movement; hiding those rows would
    lose exactly the stock the history is most useful for accounting for."""
    pid = product()
    sell(pid, 3)
    conn = database.get_db()
    conn.execute("UPDATE products SET is_archived = 1 WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

    rows, _ = movements()
    assert [r['change_qty'] for r in rows] == [-3]


# --- the route and the page ---

def test_the_api_returns_a_page_envelope(client, db_path):
    pid = product()
    sell(pid, 2)
    body = client.get('/api/stock/movements?page=0').get_json()
    assert body['page'] == 0
    assert body['has_more'] is False
    assert [m['change_qty'] for m in body['movements']] == [-2]


def test_the_api_filters_by_product(client, db_path):
    kopi, teh = product('Kopi', 'K-1'), product('Teh', 'T-1')
    sell(kopi, 1)
    sell(teh, 5)
    body = client.get(f'/api/stock/movements?product_id={teh}').get_json()
    assert [m['product_name'] for m in body['movements']] == ['Teh']


def test_the_api_rejects_a_bad_page_or_product(client, db_path):
    assert client.get('/api/stock/movements?page=-1').status_code == 400
    assert client.get('/api/stock/movements?page=abc').status_code == 400
    assert client.get('/api/stock/movements?product_id=abc').status_code == 400


def test_an_unknown_product_filter_is_empty_not_an_error(client, db_path):
    """A stale link to an since-deleted product should show nothing, not a 500."""
    body = client.get('/api/stock/movements?product_id=99999')
    assert body.status_code == 200
    assert body.get_json()['movements'] == []


def test_the_page_renders_and_lists_products_for_the_filter(client, db_path):
    product('Kopi Bubuk "Gayo", 200g', 'K-1')
    html = client.get('/stock-history').get_data(as_text=True)
    assert 'movementsBody' in html
    # The name carries a quote; the template must escape it rather than break the option.
    assert 'Kopi Bubuk &#34;Gayo&#34;, 200g' in html or 'Kopi Bubuk &quot;Gayo&quot;, 200g' in html


def test_stock_history_requires_login(db_path):
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as anon:
        assert anon.get('/stock-history').status_code == 302
        assert anon.get('/api/stock/movements').status_code == 302


def test_a_manual_adjustment_records_its_typed_reason_and_actor(client, db_path):
    pid = product()
    client.post('/api/stock/adjust', json={
        'product_id': pid, 'change_qty': -2, 'reason': 'broken in transit'})

    body = client.get('/api/stock/movements').get_json()
    assert body['movements'][0]['reason'] == 'broken in transit'
    assert body['movements'][0]['actor'] == 'web:admin'
    assert body['movements'][0]['change_qty'] == -2


def test_the_list_costs_one_query_per_page(db_path):
    """Fixed query count, like the orders list: the history joins the product name in
    rather than looking it up per row."""
    pid = product()
    for _ in range(5):
        sell(pid, 1)

    conn = database.get_db()
    seen = []
    conn.set_trace_callback(seen.append)
    services.list_stock_movements(conn, page=0, page_size=25)
    conn.set_trace_callback(None)
    conn.close()
    assert len(seen) == 1
