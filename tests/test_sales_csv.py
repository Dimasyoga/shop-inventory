"""The month's sold lines as CSV.

The monthly PDF is a document to file away; this is the same month in a shape a
spreadsheet or a tax return can take. What counts follows the PDF exactly --
completed orders only -- and the two share a month window on purpose, because two
exports of "July" that disagreed about where July starts would be worse than one.

The distinction these lean on hardest is that a cost of 0 means "never recorded"
everywhere in this app. A spreadsheet cannot tell that from stock that genuinely
cost nothing, so the cell is left blank rather than filled with a zero that would
read as pure margin.
"""
import csv
import io
from datetime import datetime, timezone

import database
import reports


def read(text):
    return list(csv.reader(io.StringIO(text)))


def product(name='Kopi', price=25000, cost=15000, sku='KP-1'):
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO products (name, sku, price, cost_price, stock_qty) VALUES (?, ?, ?, ?, 999)",
        (name, sku, price, cost))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def order(lines, status='completed', created_at='2026-07-15 08:00:00'):
    """lines = [(product_id, qty, unit_price, unit_cost)]"""
    conn = database.get_db()
    total = sum(q * p for _, q, p, _ in lines)
    cur = conn.execute("INSERT INTO orders (status, total_amount, created_at) VALUES (?, ?, ?)",
                       (status, total, created_at))
    oid = cur.lastrowid
    for pid, qty, price, cost in lines:
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " unit_cost, subtotal) VALUES (?, ?, ?, ?, ?, ?)",
                     (oid, pid, qty, price, cost, qty * price))
    conn.commit()
    conn.close()
    return oid


def csv_for(client, offset=0):
    res = client.get(f'/api/reports/monthly.csv?offset={offset}')
    assert res.status_code == 200, res.get_data(as_text=True)
    return res


def this_month():
    """A timestamp inside the current month, so offset=0 covers it. Clamped to the
    28th so the substitution is valid in February."""
    now = datetime.now(timezone.utc)
    return now.replace(day=min(now.day, 28)).strftime('%Y-%m-%d %H:%M:%S')


# --- Shape ---

def test_the_header_names_every_column(client, db_path):
    rows = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))
    assert rows[0] == ['Order', 'Date', 'Product', 'SKU', 'Qty',
                       'Unit Price', 'Unit Cost', 'Subtotal', 'Profit']


def test_an_empty_month_is_headers_only(client, db_path):
    rows = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))
    assert len(rows) == 1


def test_one_row_per_line_not_per_order(client, db_path):
    kopi = product(name='Kopi', sku='KP-1')
    teh = product(name='Teh', sku='TM-1', price=2000, cost=1200)
    order([(kopi, 2, 25000, 15000), (teh, 3, 2000, 1200)], created_at=this_month())
    rows = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))
    assert len(rows) == 3  # header + two lines
    assert [r[2] for r in rows[1:]] == ['Kopi', 'Teh']
    # Both lines carry the same order number, which is what makes them one sale.
    assert rows[1][0] == rows[2][0]


# --- What counts ---

def test_only_completed_orders_appear(client, db_path):
    pid = product()
    when = this_month()
    order([(pid, 1, 25000, 15000)], status='completed', created_at=when)
    order([(pid, 9, 25000, 15000)], status='draft', created_at=when)
    order([(pid, 9, 25000, 15000)], status='confirmed', created_at=when)
    order([(pid, 9, 25000, 15000)], status='cancelled', created_at=when)
    rows = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))
    assert len(rows) == 2
    assert rows[1][4] == '1'  # the completed one, not the nines


def test_only_the_month_asked_for(client, db_path):
    pid = product()
    order([(pid, 1, 25000, 15000)], created_at=this_month())
    order([(pid, 7, 25000, 15000)], created_at='2020-01-05 08:00:00')
    rows = read(csv_for(client, offset=0).get_data(as_text=True).lstrip('﻿'))
    assert [r[4] for r in rows[1:]] == ['1']


# --- Values ---

def test_numbers_are_raw_not_rupiah_formatted(client, db_path):
    """A spreadsheet has to sum these; 'Rp 25.000' is a string to it."""
    pid = product()
    order([(pid, 2, 25000, 15000)], created_at=this_month())
    row = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))[1]
    assert row[5] == '25000'   # unit price, not 'Rp 25.000' and not '25000.0'
    assert row[6] == '15000'   # unit cost
    assert row[7] == '50000'   # subtotal
    assert row[8] == '20000'   # profit: 50000 - 15000 * 2


def test_an_unknown_cost_is_blank_not_zero(client, db_path):
    # 0 means "never recorded" here, and a zero in the file would read as free stock
    # and a 100% margin -- the one thing the export must not assert.
    pid = product(cost=0)
    order([(pid, 2, 25000, 0)], created_at=this_month())
    row = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))[1]
    assert row[6] == ''
    assert row[8] == ''
    assert row[7] == '50000'  # revenue is still known and still stated


def test_a_fractional_amount_keeps_its_decimals(client, db_path):
    pid = product()
    order([(pid, 1, 1500.5, 700.25)], created_at=this_month())
    row = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))[1]
    assert row[5] == '1500.50'


def test_a_product_with_no_sku_leaves_the_cell_empty(client, db_path):
    pid = product(sku=None)
    order([(pid, 1, 25000, 15000)], created_at=this_month())
    row = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))[1]
    assert row[3] == ''


# --- The file as a file ---

def test_a_comma_and_a_quote_in_a_name_survive(client, db_path):
    """Product names are user data. Naive joining would split this row in two."""
    tricky = 'Kopi "Susu", besar'
    pid = product(name=tricky)
    order([(pid, 1, 25000, 15000)], created_at=this_month())
    rows = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))
    assert len(rows) == 2
    assert rows[1][2] == tricky


def test_the_file_starts_with_a_bom(client, db_path):
    # Without it Excel reads the file as the local codepage and mangles every
    # non-ASCII product name.
    assert csv_for(client).get_data().startswith(b'\xef\xbb\xbf')


def test_it_downloads_as_a_named_csv(client, db_path):
    res = csv_for(client)
    assert res.mimetype == 'text/csv'
    assert 'attachment' in res.headers['Content-Disposition']
    assert res.headers['Content-Disposition'].endswith('.csv"')


def test_a_nonsense_month_is_refused(client, db_path):
    assert client.get('/api/reports/monthly.csv?offset=abc').status_code == 400
    assert client.get('/api/reports/monthly.csv?offset=-1').status_code == 400
    assert client.get('/api/reports/monthly.csv?offset=999').status_code == 400


def test_it_needs_a_login(db_path):
    import app as app_module
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as anon:
        assert anon.get('/api/reports/monthly.csv').status_code == 401


# --- Shared ground with the PDF ---

def test_the_headers_follow_the_shop_language(client, db_path):
    conn = database.get_db()
    database.set_setting(conn, 'language', 'id')
    conn.commit()
    conn.close()
    rows = read(csv_for(client).get_data(as_text=True).lstrip('﻿'))
    assert rows[0][0] == 'Pesanan'
    assert rows[0][8] == 'Laba'


def test_the_pdf_still_renders_with_the_extra_column(client, db_path):
    """sales_csv needed unit_cost on the shared query; the PDF must not notice."""
    pid = product()
    order([(pid, 2, 25000, 15000)], created_at=this_month())
    res = client.get('/api/reports/monthly?offset=0')
    assert res.status_code == 200
    assert res.get_data().startswith(b'%PDF')


def test_both_exports_describe_the_same_month(db_path):
    """The CSV and the PDF take their window from the same helper. If they ever
    diverge, one of them is lying about which sales belong to the month."""
    conn = database.get_db()
    tz = timezone.utc
    now = datetime(2026, 7, 15, tzinfo=tz)
    pid = product()
    order([(pid, 1, 25000, 15000)], created_at='2026-07-02 08:00:00')
    order([(pid, 5, 25000, 15000)], created_at='2026-06-30 23:00:00')  # previous month
    text = reports.sales_csv(conn, 0, tz, 'en', now=now)
    data = reports.collect(conn, 0, tz, 'en', now=now)
    conn.close()
    rows = read(text)[1:]
    assert [r[4] for r in rows] == ['1']
    assert len(data['orders']) == 1
