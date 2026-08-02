"""The products page: the two filtered views and the archive round trip.

*Needs cost* is the other half of the profit figures, which say how many sales they
dropped for want of a cost but never which products to fix. Two conditions qualify, and
they are deliberately not the same thing: nothing was ever recorded (cost_price 0, which
reads as "unknown" everywhere else), or a void left a figure standing that is merely
suspect.

*Archived* exists because archiving used to be one-way: the product vanished from every
list with no route to bring it back, so a misclick needed SQLite by hand.
"""
import database
import services


def product(**cols):
    """Insert an active product, defaulting the columns this module does not care about."""
    conn = database.get_db()
    cols.setdefault('name', 'Kopi')
    cols.setdefault('price', 20000)
    cols.setdefault('cost_price', 12000)
    cols.setdefault('stock_qty', 5)
    cur = conn.execute(
        "INSERT INTO products (name, sku, price, cost_price, stock_qty, cost_review_needed)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (cols['name'], cols.get('sku'), cols['price'], cols['cost_price'],
         cols['stock_qty'], cols.get('cost_review_needed', 0)))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def names(client, **params):
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    return {p['name'] for p in client.get(f'/api/products?{query}').get_json()}


def test_no_cost_with_stock_needs_a_cost(client, db_path):
    product(name='Uncosted', cost_price=0, stock_qty=4)
    product(name='Costed', cost_price=9000, stock_qty=4)
    assert names(client, needs_cost=1) == {'Uncosted'}


def test_no_cost_but_no_stock_is_not_flagged(client, db_path):
    """Nothing is owed on stock that is not there -- the next restock records the cost."""
    product(name='Empty', cost_price=0, stock_qty=0)
    assert names(client, needs_cost=1) == set()


def test_a_suspect_cost_is_flagged_even_with_no_stock(client, db_path):
    """Unlike an absent cost: sales already snapshotted this figure, so it still matters."""
    product(name='Suspect', cost_price=11000, stock_qty=0, cost_review_needed=1)
    assert names(client, needs_cost=1) == {'Suspect'}


def test_archived_products_are_never_listed(client, db_path):
    pid = product(name='Gone', cost_price=0, stock_qty=3)
    client.delete(f'/api/products/{pid}')
    assert names(client, needs_cost=1) == set()


def test_the_filter_composes_with_search(client, db_path):
    product(name='Kopi Susu', cost_price=0, stock_qty=2)
    product(name='Gula Aren', cost_price=0, stock_qty=2)
    assert names(client, needs_cost=1, search='Kopi') == {'Kopi Susu'}


def test_without_the_filter_everything_active_is_listed(client, db_path):
    product(name='Uncosted', cost_price=0, stock_qty=4)
    product(name='Costed', cost_price=9000, stock_qty=4)
    assert names(client) == {'Uncosted', 'Costed'}


def test_setting_a_cost_clears_the_review_flag(client, db_path):
    pid = product(name='Suspect', cost_price=11000, cost_review_needed=1)
    resp = client.put(f'/api/products/{pid}',
                      json={'name': 'Suspect', 'price': 20000, 'cost_price': 12500})
    assert resp.status_code == 200
    conn = database.get_db()
    row = conn.execute("SELECT cost_price, cost_review_needed FROM products WHERE id = ?",
                       (pid,)).fetchone()
    conn.close()
    assert row['cost_price'] == 12500
    assert row['cost_review_needed'] == 0


def test_editing_without_a_cost_leaves_the_flag_alone(client, db_path):
    """Saving the form with the cost field blank is not a statement about the cost."""
    pid = product(name='Suspect', cost_price=11000, cost_review_needed=1)
    client.put(f'/api/products/{pid}', json={'name': 'Renamed', 'price': 21000, 'cost_price': 0})
    conn = database.get_db()
    row = conn.execute("SELECT cost_review_needed FROM products WHERE id = ?", (pid,)).fetchone()
    conn.close()
    assert row['cost_review_needed'] == 1


# --- Archive and restore ---

def test_archiving_hides_a_product_and_restoring_brings_it_back(client, db_path):
    pid = product(name='Gone')
    client.delete(f'/api/products/{pid}')
    assert names(client) == set()
    assert names(client, archived=1) == {'Gone'}

    assert client.post(f'/api/products/{pid}/restore').status_code == 200
    assert names(client) == {'Gone'}
    assert names(client, archived=1) == set()


def test_restoring_keeps_the_stock_and_cost_it_was_archived_with(client, db_path):
    """Archiving is a soft delete -- nothing about the product changed while it was away."""
    pid = product(name='Gone', cost_price=7500, stock_qty=9)
    client.delete(f'/api/products/{pid}')
    client.post(f'/api/products/{pid}/restore')
    row = client.get('/api/products').get_json()[0]
    assert (row['cost_price'], row['stock_qty']) == (7500, 9)


def test_restoring_a_product_that_is_not_archived_is_404(client, db_path):
    pid = product(name='Active')
    res = client.post(f'/api/products/{pid}/restore')
    assert res.status_code == 404
    assert 'error' in res.get_json()


def test_restoring_a_missing_product_is_404(client, db_path):
    assert client.post('/api/products/999/restore').status_code == 404


def test_the_archived_view_composes_with_search(client, db_path):
    a = product(name='Kopi Lama')
    b = product(name='Gula Lama')
    client.delete(f'/api/products/{a}')
    client.delete(f'/api/products/{b}')
    assert names(client, archived=1, search='Kopi') == {'Kopi Lama'}


def test_the_products_page_offers_the_archived_chip_only_when_there_is_one(client, db_path):
    pid = product(name='Gone')
    assert 'archivedChip' not in client.get('/products').get_data(as_text=True)
    client.delete(f'/api/products/{pid}')
    assert 'Archived (1)' in client.get('/products').get_data(as_text=True)


def test_the_page_does_not_select_the_catalogue_it_never_renders(client, db_path, monkeypatch):
    """/products renders no product rows -- loadProducts() fills the table from
    /api/products, which is also what the search box and the filter chips re-fetch.

    The route selected every active product into a template variable products.html
    ignores, so each page load built the whole catalogue to throw it away. The orders
    page carried the same dead query once; this pins it shut here.
    """
    import app as app_module
    product(name='Kopi')
    seen = []
    real = database.get_db

    def traced():
        conn = real()
        conn.set_trace_callback(lambda sql: seen.append(' '.join(sql.split())))
        return conn

    # app.py imported get_db by name, so its own reference is the one that matters.
    monkeypatch.setattr(app_module, 'get_db', traced)
    assert client.get('/products').status_code == 200

    selects = [q for q in seen if 'FROM products' in q and q.lstrip().upper().startswith('SELECT')]
    # Only the two counts behind the chips: archived, and needs-cost. Neither returns rows.
    assert selects, 'the trace hook caught nothing -- the test is not watching the route'
    assert all('COUNT(' in q.upper() for q in selects), selects


def test_count_needs_cost_matches_the_filter(client, db_path):
    product(name='Uncosted', cost_price=0, stock_qty=4)
    product(name='Suspect', cost_price=11000, cost_review_needed=1)
    product(name='Fine', cost_price=9000, stock_qty=4)
    conn = database.get_db()
    count = services.count_needs_cost(conn)
    conn.close()
    assert count == len(names(client, needs_cost=1)) == 2
