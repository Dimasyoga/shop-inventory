"""Who an order was for, how they paid, and the receipt that proves it.

All three are optional and all three are recorded after the fact as often as with
the order itself -- a transfer receipt arrives on a phone minutes or days later --
so the rule these lean on is that payment details are editable in every status but
cancelled, while the *lines* stay draft-only. Nothing here moves stock or money.

The proof is stored as bytes in the database rather than a file on disk, because
backup.sh copies shop.db and nothing else; a proof kept outside it would be outside
every backup the shop takes. test_the_proof_survives_a_backup pins that.
"""
import io
import sqlite3

import pytest

import database
import services
from services import NotFoundError, ServiceError

# Smallest byte strings that each sniffed type accepts. Real files, not valid ones:
# the point is that the magic bytes decide the stored type, and nothing else does.
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 32
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
WEBP = b'RIFF' + b'\x24\x00\x00\x00' + b'WEBP' + b'\x00' * 32
PDF = b'%PDF-1.7\n' + b'\x00' * 32


def product(name='Kopi', stock=10, price=25000, cost=15000):
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO products (name, price, cost_price, stock_qty) VALUES (?, ?, ?, ?)",
        (name, price, cost, stock))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def an_order(status='draft'):
    """An order in the requested status, taken through the real service calls so the
    holds and the stock ledger are whatever that status genuinely implies."""
    conn = database.get_db()
    pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()
    pid = pid[0] if pid else None
    conn.close()
    if pid is None:
        pid = product()
    conn = database.get_db()
    order_id = services.create_order(conn, [{'product_id': pid, 'quantity': 1}])['order_id']
    if status in ('confirmed', 'completed'):
        services.confirm_order(conn, order_id)
    if status == 'completed':
        services.complete_order(conn, order_id)
    if status == 'cancelled':
        services.cancel_order(conn, order_id)
    conn.close()
    return order_id


def stored(order_id):
    conn = database.get_db()
    row = conn.execute("SELECT buyer_name, payment_method FROM orders WHERE id = ?",
                       (order_id,)).fetchone()
    conn.close()
    return row['buyer_name'], row['payment_method']


def post_payment(client, order_id, **fields):
    """The endpoint as the browser calls it: multipart, one field per input shown."""
    return client.post(f'/api/orders/{order_id}/payment', data=fields,
                       content_type='multipart/form-data')


# --- The service ---

def test_a_buyer_and_a_method_are_recorded(db_path):
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='Bu Rina',
                               payment_method='bank_transfer')
    conn.close()
    assert stored(order_id) == ('Bu Rina', 'bank_transfer')


def test_an_order_with_nothing_recorded_reads_as_null(db_path):
    """The columns are optional, and "not recorded" has to be distinguishable from
    a buyer who is genuinely called nothing."""
    order_id = an_order()
    assert stored(order_id) == (None, None)


def test_a_blank_buyer_clears_the_column(db_path):
    # The editor submits every field it shows, so an emptied box means the seller
    # just deleted the name -- not that they left the field alone.
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='Typo')
    services.set_order_payment(conn, order_id, buyer_name='   ')
    conn.close()
    assert stored(order_id) == (None, None)


def test_a_buyer_name_is_trimmed_and_capped(db_path):
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='  Pak Budi  ')
    conn.close()
    assert stored(order_id)[0] == 'Pak Budi'
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='x' * 500)
    conn.close()
    assert len(stored(order_id)[0]) == services.MAX_BUYER_NAME


def test_an_invented_payment_method_is_refused(db_path):
    """The column is a closed set: a stray value would be a category nothing counts."""
    order_id = an_order()
    conn = database.get_db()
    with pytest.raises(ServiceError):
        services.set_order_payment(conn, order_id, payment_method='crypto')
    conn.close()
    assert stored(order_id) == (None, None)


@pytest.mark.parametrize('status', ['draft', 'confirmed', 'completed'])
def test_payment_details_can_be_recorded_in_any_open_status(db_path, status):
    """Unlike the lines, which are draft-only. A receipt that arrives after the sale
    is completed is the normal case, not the exception."""
    order_id = an_order(status)
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='Bu Rina', payment_method='cash')
    conn.close()
    assert stored(order_id) == ('Bu Rina', 'cash')


def test_a_cancelled_order_refuses_payment_details(db_path):
    order_id = an_order('cancelled')
    conn = database.get_db()
    with pytest.raises(ServiceError):
        services.set_order_payment(conn, order_id, buyer_name='Bu Rina')
    conn.close()
    assert stored(order_id) == (None, None)


def test_an_order_that_does_not_exist_is_a_404(db_path):
    conn = database.get_db()
    with pytest.raises(NotFoundError):
        services.set_order_payment(conn, 9999, buyer_name='Bu Rina')
    with pytest.raises(NotFoundError):
        services.get_payment_proof(conn, 9999)
    conn.close()


def test_recording_payment_moves_no_stock(db_path):
    """It touches neither stock_qty nor reserved_qty. The lines were paid for as
    they stand; this is a note about who paid."""
    pid = product()
    order_id = an_order('confirmed')
    conn = database.get_db()
    before = conn.execute("SELECT stock_qty, reserved_qty FROM products WHERE id = ?",
                          (pid,)).fetchone()
    before = (before['stock_qty'], before['reserved_qty'])
    log_count = conn.execute("SELECT COUNT(*) FROM stock_logs").fetchone()[0]
    services.set_order_payment(conn, order_id, buyer_name='Bu Rina',
                               payment_method='cash', proof=JPEG)
    after = conn.execute("SELECT stock_qty, reserved_qty FROM products WHERE id = ?",
                         (pid,)).fetchone()
    assert (after['stock_qty'], after['reserved_qty']) == before
    assert conn.execute("SELECT COUNT(*) FROM stock_logs").fetchone()[0] == log_count
    conn.close()


# --- The proof ---

@pytest.mark.parametrize('data, mime', [
    (JPEG, 'image/jpeg'),
    (PNG, 'image/png'),
    (WEBP, 'image/webp'),
    (PDF, 'application/pdf'),
])
def test_each_accepted_type_is_recognised_by_its_bytes(db_path, data, mime):
    assert services.sniff_proof_type(data) == mime
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, proof=data)
    proof = services.get_payment_proof(conn, order_id)
    conn.close()
    assert proof['mime_type'] == mime
    assert bytes(proof['data']) == data
    assert proof['byte_size'] == len(data)


def test_a_file_that_is_not_an_accepted_type_is_refused(db_path):
    """Sniffed, not trusted: the browser's declared Content-Type is chosen by the
    client, and the type we store is the one we later serve the bytes back as."""
    order_id = an_order()
    conn = database.get_db()
    with pytest.raises(ServiceError):
        services.set_order_payment(conn, order_id, proof=b'<svg onload="alert(1)"/>')
    assert services.get_payment_proof(conn, order_id) is None
    conn.close()


def test_an_oversized_proof_is_refused(db_path):
    order_id = an_order()
    conn = database.get_db()
    with pytest.raises(ServiceError):
        services.set_order_payment(
            conn, order_id, proof=JPEG + b'\x00' * services.MAX_PROOF_BYTES)
    assert services.get_payment_proof(conn, order_id) is None
    conn.close()


def test_a_second_proof_replaces_the_first(db_path):
    """A corrected receipt overwrites the wrong one. Two rows with no way to say
    which is current is the state this must never reach."""
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, proof=JPEG)
    services.set_order_payment(conn, order_id, proof=PNG)
    proof = services.get_payment_proof(conn, order_id)
    count = conn.execute("SELECT COUNT(*) FROM order_payment_proofs WHERE order_id = ?",
                         (order_id,)).fetchone()[0]
    conn.close()
    assert count == 1
    assert proof['mime_type'] == 'image/png'


def test_no_file_leaves_the_stored_proof_alone(db_path):
    """A file input cannot be pre-filled with what is already stored, so an empty one
    has to mean "leave it" -- otherwise editing a buyer's name would silently drop
    the receipt attached to the same order."""
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, proof=JPEG)
    services.set_order_payment(conn, order_id, buyer_name='Bu Rina')
    proof = services.get_payment_proof(conn, order_id)
    conn.close()
    assert proof is not None
    assert stored(order_id)[0] == 'Bu Rina'


def test_remove_proof_deletes_it(db_path):
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, proof=JPEG)
    services.set_order_payment(conn, order_id, remove_proof=True)
    assert services.get_payment_proof(conn, order_id) is None
    conn.close()


def test_a_new_file_wins_over_remove(db_path):
    """Both arriving together means the seller ticked remove and then picked a
    replacement. Taking the replacement is the reading that loses nothing."""
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, proof=JPEG)
    services.set_order_payment(conn, order_id, proof=PNG, remove_proof=True)
    proof = services.get_payment_proof(conn, order_id)
    conn.close()
    assert proof is not None and proof['mime_type'] == 'image/png'


# --- Reading orders back ---

def test_an_order_carries_whether_it_has_a_proof_but_never_the_bytes(db_path):
    """Every orders page would otherwise ship its screenshots to draw a table that
    shows none of them."""
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, proof=JPEG)
    order, _ = services.get_order(conn, order_id)
    orders, _ = services.list_orders(conn)
    conn.close()
    assert order['has_payment_proof'] == 1
    assert 'data' not in order.keys()
    assert orders[0]['has_payment_proof'] == 1
    assert 'data' not in orders[0]


def test_the_orders_list_finds_an_order_by_its_buyer(db_path):
    """The reason the name is recorded at all: "which order was Bu Rina's" is asked
    by someone who does not have the order number."""
    first = an_order()
    second = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, first, buyer_name='Bu Rina')
    services.set_order_payment(conn, second, buyer_name='Pak Budi')
    found, _ = services.list_orders(conn, search='rina')
    conn.close()
    assert [o['id'] for o in found] == [first]


def test_searching_by_order_id_still_works(db_path):
    an_order()
    second = an_order()
    conn = database.get_db()
    found, _ = services.list_orders(conn, search=str(second))
    conn.close()
    assert [o['id'] for o in found] == [second]


# --- The endpoints ---

def test_the_endpoint_records_a_buyer_a_method_and_a_proof(client, db_path):
    order_id = an_order()
    res = post_payment(client, order_id, buyer_name='Bu Rina',
                       payment_method='bank_transfer',
                       proof=(io.BytesIO(JPEG), 'receipt.jpg'))
    assert res.status_code == 200, res.get_data(as_text=True)
    assert stored(order_id) == ('Bu Rina', 'bank_transfer')
    detail = client.get(f'/api/orders/{order_id}').get_json()
    assert detail['buyer_name'] == 'Bu Rina'
    assert detail['has_payment_proof'] == 1


def test_a_form_with_no_file_chosen_keeps_the_stored_proof(client, db_path):
    """A file input submits its field even when nothing was picked, with an empty
    filename. Reading it would write zero bytes over a perfectly good receipt."""
    order_id = an_order()
    post_payment(client, order_id, proof=(io.BytesIO(JPEG), 'receipt.jpg'))
    res = post_payment(client, order_id, buyer_name='Bu Rina',
                       proof=(io.BytesIO(b''), ''))
    assert res.status_code == 200
    assert client.get(f'/api/orders/{order_id}').get_json()['has_payment_proof'] == 1


def test_the_endpoint_refuses_a_bad_method_with_a_translated_message(client, db_path):
    order_id = an_order()
    conn = database.get_db()
    database.set_setting(conn, 'language', 'id')
    conn.commit()
    conn.close()
    res = post_payment(client, order_id, payment_method='crypto')
    assert res.status_code == 400
    assert res.get_json()['error'] == 'Metode pembayaran tidak dikenal'


def test_the_proof_downloads_as_the_type_it_was_found_to_be(client, db_path):
    order_id = an_order()
    # An uploader claiming HTML for a JPEG: what comes back is what the bytes are.
    post_payment(client, order_id,
                 proof=(io.BytesIO(JPEG), 'receipt.jpg', 'text/html'))
    res = client.get(f'/api/orders/{order_id}/proof')
    assert res.status_code == 200
    assert res.mimetype == 'image/jpeg'
    assert res.headers['X-Content-Type-Options'] == 'nosniff'
    assert res.get_data() == JPEG


def test_a_pdf_proof_downloads_rather_than_opening_inline(client, db_path):
    order_id = an_order()
    post_payment(client, order_id, proof=(io.BytesIO(PDF), 'invoice.pdf'))
    res = client.get(f'/api/orders/{order_id}/proof')
    assert res.mimetype == 'application/pdf'
    assert res.headers['Content-Disposition'].startswith('attachment')


def test_the_download_name_is_derived_not_the_uploaders(client, db_path):
    """The uploader's filename ends up in a response header if it is kept, so it
    is not kept at all."""
    order_id = an_order()
    post_payment(client, order_id,
                 proof=(io.BytesIO(JPEG), 'receipt"; drop=1; x=".jpg'))
    disposition = client.get(f'/api/orders/{order_id}/proof').headers['Content-Disposition']
    assert disposition == f'inline; filename="order-{order_id}-payment-proof.jpg"'


def test_an_order_with_no_proof_is_a_404(client, db_path):
    order_id = an_order()
    assert client.get(f'/api/orders/{order_id}/proof').status_code == 404


def test_a_body_over_the_request_ceiling_is_refused_as_json(client, db_path):
    """Flask's own 413 is an HTML page, and api() in app.js parses every response as
    JSON -- it would report a syntax error to a seller whose photo is simply too big."""
    order_id = an_order()
    res = post_payment(client, order_id,
                       proof=(io.BytesIO(JPEG + b'\x00' * (6 * 1024 * 1024)), 'big.jpg'))
    assert res.status_code == 413
    assert res.is_json
    assert 'error' in res.get_json()


def test_the_proof_endpoints_need_a_login(db_path):
    import app as app_module
    order_id = an_order()
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as anon:
        assert anon.get(f'/api/orders/{order_id}/proof').status_code == 401
        assert anon.post(f'/api/orders/{order_id}/payment').status_code == 401


# --- The bot's order screen ---

def bot_detail(order_id, lang='en'):
    import i18n
    import telegram_bot
    conn = database.get_db()
    text, _ = telegram_bot.screen_order_detail(conn, order_id, i18n.make_t(lang))
    conn.close()
    return text


def test_the_bot_shows_the_buyer_and_method_when_there_are_any(db_path):
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='Bu Rina',
                               payment_method='bank_transfer')
    conn.close()
    text = bot_detail(order_id)
    assert 'Buyer: Bu Rina' in text
    assert 'Paid by: Bank Transfer' in text


def test_the_bot_stays_silent_about_details_nobody_recorded(db_path):
    """A chat screen is narrow. Two lines reading "not recorded" on every order
    would push the items off it."""
    text = bot_detail(an_order())
    assert 'Buyer' not in text
    assert 'Paid by' not in text


def test_the_bot_escapes_a_buyer_name(db_path):
    """The bot sends HTML. A name is user data and goes through esc() like every
    product name already does."""
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='Toko <b>Maju</b>')
    conn.close()
    text = bot_detail(order_id)
    assert 'Toko &lt;b&gt;Maju&lt;/b&gt;' in text


def test_the_bot_translates_the_method_label(db_path):
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, payment_method='cash')
    conn.close()
    assert 'Dibayar dengan: Tunai' in bot_detail(order_id, lang='id')


# --- Storage ---

def test_the_proof_survives_a_backup(db_path, tmp_path):
    """backup.sh copies shop.db and nothing else. That is the whole reason the bytes
    live in the database: a proof written beside it would be outside every backup
    the shop takes, which is the one thing a record kept for evidence must not be."""
    order_id = an_order()
    conn = database.get_db()
    services.set_order_payment(conn, order_id, buyer_name='Bu Rina', proof=PNG)
    copy_path = tmp_path / 'backup.db'
    dst = sqlite3.connect(copy_path)
    with dst:
        conn.backup(dst)
    conn.close()
    dst.row_factory = sqlite3.Row
    row = dst.execute("SELECT o.buyer_name, pp.data FROM orders o"
                      " JOIN order_payment_proofs pp ON pp.order_id = o.id"
                      " WHERE o.id = ?", (order_id,)).fetchone()
    dst.close()
    assert row['buyer_name'] == 'Bu Rina'
    assert bytes(row['data']) == PNG
