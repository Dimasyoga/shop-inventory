"""The restock and self-use histories are paged, and a page costs a fixed two queries.

Both pages used to read their whole table -- no LIMIT anywhere -- and then fan a second
query out per batch for its lines. Opening /restock therefore cost one query per batch
the shop had ever recorded and shipped every one of them to the browser in a single
JSON array, which the page rendered into one innerHTML. Measured against two years at a
thousand orders and three hundred restocks a month, that was 7,201 queries and 5.8 MB
for a page showing ten rows.

Parametrized over both histories because they are one implementation
(services._list_batches) reached through two endpoints: a fix that only lands on the
restock side is the failure this is here to catch.
"""
import pytest

import database
import services

PAGE = 10  # app.HISTORY_PAGE_SIZE

# (endpoint, batch table, line table, batch amount column, line-insert SQL)
RESTOCK = (
    '/api/restock/history', 'restock_batches', 'restock_items', 'total_cost',
    "INSERT INTO restock_items (batch_id, product_id, qty_added, unit_price, unit_cost,"
    " allocated_cost, cost_before) VALUES (?, ?, 2, 1000, 1000, 2000, 0)",
)
SELF_USE = (
    '/api/self-use/history', 'self_use_batches', 'self_use_items', 'total_value',
    "INSERT INTO self_use_items (batch_id, product_id, quantity, unit_price, subtotal)"
    " VALUES (?, ?, 2, 1000, 2000)",
)
BOTH = pytest.mark.parametrize('history', [RESTOCK, SELF_USE], ids=['restock', 'self_use'])


@pytest.fixture
def db(db_path):
    """An open connection for calling services directly.

    Depends on db_path: get_db() reads DB_PATH at call time, so a fixture that opens a
    connection without waiting for the monkeypatch connects to the source tree's own
    database.
    """
    conn = database.get_db()
    yield conn
    conn.close()


def product(name='Kopi'):
    conn = database.get_db()
    cur = conn.execute("INSERT INTO products (name, price, cost_price, stock_qty)"
                       " VALUES (?, 1000, 500, 10000)", (name,))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def make_batches(history, pid, count, same_second=False):
    """`count` batches, oldest first, each with one line. Returns their ids."""
    _, table, _, amount_col, line_sql = history
    conn = database.get_db()
    ids = []
    for n in range(count):
        when = '2026-07-05 09:00:00' if same_second else f'2026-07-{n + 1:02d} 08:00:00'
        cur = conn.execute(
            f"INSERT INTO {table} ({amount_col}, created_at) VALUES (?, ?)", (1000 + n, when))
        ids.append(cur.lastrowid)
        conn.execute(line_sql, (cur.lastrowid, pid))
    conn.commit()
    conn.close()
    return ids


def page(client, history, n=0, period='all'):
    return client.get(f'{history[0]}?page={n}&period={period}').get_json()


# --- Paging ---

@BOTH
def test_a_page_holds_one_screenful(client, db_path, history):
    make_batches(history, product(), 25)
    body = page(client, history)
    assert len(body['batches']) == PAGE
    assert body['has_more'] is True
    assert body['page'] == 0


@BOTH
def test_the_last_page_says_there_is_no_more(client, db_path, history):
    make_batches(history, product(), 25)
    body = page(client, history, 2)
    assert len(body['batches']) == 5
    assert body['has_more'] is False


@BOTH
def test_every_batch_appears_exactly_once_across_the_pages(client, db_path, history):
    created = make_batches(history, product(), 25)
    seen = []
    for n in range(3):
        seen += [b['id'] for b in page(client, history, n)['batches']]
    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen))


@BOTH
def test_batches_entered_in_the_same_second_still_page_cleanly(client, db_path, history):
    """created_at is second-resolution and an invoice is entered in one sitting.

    Several batches sharing a timestamp is ordinary here, so without a stable tiebreak
    their relative order is up to the query planner and a row can drift across the page
    boundary between two requests -- appearing twice, or not at all.
    """
    created = make_batches(history, product(), 25, same_second=True)
    seen = []
    for n in range(3):
        seen += [b['id'] for b in page(client, history, n)['batches']]
    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen))


@BOTH
def test_pages_run_newest_first(client, db_path, history):
    created = make_batches(history, product(), 15)
    first = [b['id'] for b in page(client, history)['batches']]
    assert first == list(reversed(created))[:PAGE]


@BOTH
def test_a_page_past_the_end_is_empty_rather_than_an_error(client, db_path, history):
    make_batches(history, product(), 5)
    body = page(client, history, 9)
    assert body['batches'] == []
    assert body['has_more'] is False


@BOTH
def test_a_nonsense_page_is_refused(client, db_path, history):
    assert client.get(f'{history[0]}?page=-1').status_code == 400
    assert client.get(f'{history[0]}?page=abc').status_code == 400


@BOTH
def test_a_period_pages_within_its_own_window(client, db_path, history):
    """Paging and the date filter have to compose: the window narrows the rows, and the
    page number then walks what is left rather than the whole table."""
    _, table, _, amount_col, _ = history
    make_batches(history, product(), 15)  # July 2026
    conn = database.get_db()
    conn.execute(f"INSERT INTO {table} ({amount_col}, created_at)"
                 " VALUES (999, '2020-01-01 08:00:00')")
    conn.commit()
    conn.close()

    body = page(client, history, 0, period='year')
    assert len(body['batches']) == PAGE
    assert 999 not in [b[amount_col] for b in body['batches']]


# --- Lines come with the page ---

@BOTH
def test_each_batch_carries_its_lines(client, db_path, history):
    make_batches(history, product(), 3)
    for batch in page(client, history)['batches']:
        assert len(batch['items']) == 1
        assert batch['items'][0]['product_name'] == 'Kopi'


@BOTH
def test_the_lines_of_a_page_are_fetched_in_one_query(db, db_path, history):
    """A query per row is what made the old page cost grow with the whole history."""
    _, _, item_table, _, _ = history
    lister = (services.list_restock_batches if history is RESTOCK
              else services.list_self_use_batches)
    make_batches(history, product(), 25)
    seen = []
    db.set_trace_callback(lambda sql: seen.append(' '.join(sql.split())))
    batches, has_more = lister(db, page_size=PAGE)
    db.set_trace_callback(None)
    assert len(batches) == PAGE and has_more is True
    assert len([q for q in seen if f'FROM {item_table}' in q]) == 1
    # Two queries for a page, whatever the table holds: the page, then its lines.
    assert len(seen) == 2


@BOTH
def test_a_batch_with_no_lines_still_lists(client, db_path, history):
    # Defensive: nothing writes one, but a header with no lines must not drop out of
    # the page or arrive without the key the table reads.
    _, table, _, amount_col, _ = history
    conn = database.get_db()
    conn.execute(f"INSERT INTO {table} ({amount_col}) VALUES (0)")
    conn.commit()
    conn.close()
    batches = page(client, history)['batches']
    assert len(batches) == 1
    assert batches[0]['items'] == []


# --- The void back-link survives paging ---

@BOTH
def test_a_void_and_its_original_both_carry_the_link(client, db_path, history):
    """voids_batch_id and voided_by are the two halves the history table renders from,
    and the correlated subquery that supplies voided_by now runs per page rather than
    per batch -- it still has to produce the same answer."""
    pid = product()
    if history is RESTOCK:
        client.post('/api/restock', json={
            'items': [{'product_id': pid, 'qty': 5, 'unit_price': 1000}]})
        original = page(client, history)['batches'][0]['id']
        assert client.post(f'/api/restock/{original}/void').status_code == 200
    else:
        client.post('/api/self-use', json={'items': [{'product_id': pid, 'qty': 3}]})
        original = page(client, history)['batches'][0]['id']
        assert client.post(f'/api/self-use/{original}/void').status_code == 200

    batches = page(client, history)['batches']
    void = next(b for b in batches if b['voids_batch_id'])
    assert void['voids_batch_id'] == original
    assert next(b for b in batches if b['id'] == original)['voided_by'] == void['id']
