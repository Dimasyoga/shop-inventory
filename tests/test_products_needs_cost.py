"""Surfacing products whose cost cannot be relied on.

The profit figures already say how many sales they dropped for want of a cost; this is
the other half -- which products to go and fix. Two conditions qualify, and they are
deliberately not the same thing: nothing was ever recorded (cost_price 0, which reads as
"unknown" everywhere else), or a void left a figure standing that is merely suspect.
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


def test_count_needs_cost_matches_the_filter(client, db_path):
    product(name='Uncosted', cost_price=0, stock_qty=4)
    product(name='Suspect', cost_price=11000, cost_review_needed=1)
    product(name='Fine', cost_price=9000, stock_qty=4)
    conn = database.get_db()
    count = services.count_needs_cost(conn)
    conn.close()
    assert count == len(names(client, needs_cost=1)) == 2
