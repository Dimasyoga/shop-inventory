"""Every stock movement records who caused it.

The web login is shared, but the Telegram whitelist can hold several people, so
"who took this stock" already had more than one answer and stock_logs could not
say. The column is forensic -- nothing in the UI reads it, which is also why
tests/test_indexes.py leaves the table unindexed on purpose.

The default matters as much as the values: a call with no request behind it lands
as 'system' rather than being credited to the admin, because a column whose history
is a guess is worse than one that admits what it does not know.
"""
import database
import services


def product(name='Kopi', stock=100, price=5000, cost=3000):
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO products (name, price, cost_price, stock_qty) VALUES (?, ?, ?, ?)",
        (name, price, cost, stock))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def actors(product_id=None):
    conn = database.get_db()
    sql = "SELECT reason, actor FROM stock_logs"
    params = ()
    if product_id:
        sql += " WHERE product_id = ?"
        params = (product_id,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    conn.close()
    return [(r['reason'], r['actor']) for r in rows]


# --- From the web UI ---

def test_completing_an_order_records_the_signed_in_user(client, db_path):
    pid = product()
    created = client.post('/api/orders', json={'items': [{'product_id': pid, 'quantity': 2}]})
    oid = created.get_json()['order_id']
    client.post(f'/api/orders/{oid}/confirm')
    client.post(f'/api/orders/{oid}/complete')
    assert actors(pid) == [(f'sale order #{oid}', 'web:admin')]


def test_a_restock_records_the_signed_in_user(client, db_path):
    pid = product()
    client.post('/api/restock', json={
        'items': [{'product_id': pid, 'qty': 5, 'unit_price': 1000}]})
    assert [a for _, a in actors(pid)] == ['web:admin']


def test_self_use_records_the_signed_in_user(client, db_path):
    pid = product()
    client.post('/api/self-use', json={'items': [{'product_id': pid, 'qty': 3}]})
    assert [a for _, a in actors(pid)] == ['web:admin']


def test_a_stock_adjustment_records_the_signed_in_user(client, db_path):
    pid = product()
    client.post('/api/stock/adjust',
                json={'product_id': pid, 'change_qty': -2, 'reason': 'breakage'})
    assert actors(pid) == [('breakage', 'web:admin')]


def test_a_void_records_who_voided_it(client, db_path):
    """The reversal is a movement of its own and gets its own attribution -- the
    person undoing a batch is not necessarily the one who entered it."""
    pid = product()
    res = client.post('/api/restock', json={
        'items': [{'product_id': pid, 'qty': 5, 'unit_price': 1000}]})
    assert res.status_code == 200
    conn = database.get_db()
    batch_id = conn.execute("SELECT id FROM restock_batches ORDER BY id DESC LIMIT 1").fetchone()['id']
    conn.close()
    client.post(f'/api/restock/{batch_id}/void')
    reasons = actors(pid)
    assert len(reasons) == 2
    assert reasons[1][1] == 'web:admin'
    assert 'void' in reasons[1][0]


def test_the_username_is_whatever_is_signed_in(client, db_path):
    # Not hardcoded to 'admin': the account name is changeable in Settings.
    conn = database.get_db()
    conn.execute("UPDATE users SET username = 'bu-sari' WHERE id = 1")
    conn.commit()
    conn.close()
    with client.session_transaction() as sess:
        sess['username'] = 'bu-sari'
    pid = product()
    client.post('/api/self-use', json={'items': [{'product_id': pid, 'qty': 1}]})
    assert [a for _, a in actors(pid)] == ['web:bu-sari']


# --- From the bot ---

def test_the_bot_records_the_telegram_id(db_path):
    """The chat id, not a display name: it is what the whitelist is written in, so a
    log row can be matched back to a Settings entry."""
    import telegram_bot
    pid = product()
    conn = database.get_db()
    telegram_bot._actor(88412339)  # the format under test
    services.create_self_use(conn, [{'product_id': pid, 'qty': 2}],
                             actor=telegram_bot._actor(88412339))
    conn.close()
    assert [a for _, a in actors(pid)] == ['telegram:88412339']


def test_the_bot_helper_formats_the_id(db_path):
    import telegram_bot
    assert telegram_bot._actor(123) == 'telegram:123'


# --- Everything else ---

def test_a_call_with_no_caller_is_system_not_admin(db_path):
    """A script, a migration or a test is not the shop owner. Recording a guess would
    make the column worse than useless the one time somebody reads it."""
    pid = product()
    conn = database.get_db()
    services.create_self_use(conn, [{'product_id': pid, 'qty': 1}])
    conn.close()
    assert [a for _, a in actors(pid)] == ['system']


def test_every_movement_carries_an_actor(client, db_path):
    """No path may leave it NULL: a trail with holes is one you cannot rely on."""
    pid = product()
    client.post('/api/restock', json={
        'items': [{'product_id': pid, 'qty': 5, 'unit_price': 1000}]})
    client.post('/api/self-use', json={'items': [{'product_id': pid, 'qty': 1}]})
    client.post('/api/stock/adjust',
                json={'product_id': pid, 'change_qty': 1, 'reason': 'recount'})
    created = client.post('/api/orders', json={'items': [{'product_id': pid, 'quantity': 1}]})
    oid = created.get_json()['order_id']
    client.post(f'/api/orders/{oid}/confirm')
    client.post(f'/api/orders/{oid}/complete')

    recorded = actors(pid)
    assert len(recorded) == 4
    assert all(a for _, a in recorded), recorded


def test_the_actor_survives_a_rollback_free_path(client, db_path):
    """A failed movement writes no log at all, so there is no actor to get wrong."""
    pid = product(stock=1)
    res = client.post('/api/self-use', json={'items': [{'product_id': pid, 'qty': 9}]})
    assert res.status_code == 400
    assert actors(pid) == []
