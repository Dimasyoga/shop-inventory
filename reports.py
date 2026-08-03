"""Monthly audit report: collect a calendar month's records and render them to PDF.

Page 1 summarizes the month's sales performance; the sections after it list every
completed order, restock batch and self-use batch line by line, so the figures on
page 1 can be reconciled against the underlying rows.

Like services.py this module takes an open sqlite3 connection and must not import
app.py -- both the web routes and the Telegram bot render reports.
"""
import csv
import hashlib
import io
import json
import logging
import os
from datetime import datetime, timezone

import i18n
import services
from services import (build_date_filter, format_percent, format_rupiah,
                      get_date_range)

log = logging.getLogger('reports')

# Defaults keep local dev writing beside the source; a deployment points this at
# the mounted volume (see Dockerfile) so the source tree stays read-only. Read at
# call time like database.DB_PATH, so tests can monkeypatch it.
REPORT_DIR = (os.environ.get('SHOP_REPORT_DIR')
              or os.path.join(os.path.dirname(__file__), 'reports'))

FONT_DIR = os.path.join(os.path.dirname(__file__), 'static', 'fonts')
FONT_NAME = 'DejaVu'


# --- Month identity ---

def period_key(dt):
    """'2026-06' for a date or datetime. The archive filename and the
    last-report-sent marker are both keyed on this."""
    return f'{dt.year:04d}-{dt.month:02d}'


def report_filename(period, ext='pdf'):
    return f'shop-report-{period}.{ext}'


def month_offset(period, tz, now=None):
    """Whole months from `period` back to the current month, for get_date_range.

    Returns None when `period` is unparseable or in the future.
    """
    try:
        year, month = (int(x) for x in period.split('-'))
    except (ValueError, AttributeError):
        return None
    now = now or datetime.now(tz)
    offset = (now.year - year) * 12 + (now.month - month)
    return offset if offset >= 0 else None


# --- Data collection ---

def _group(rows, header_keys, item_keys):
    """Collapse a header-joined-to-items result set into nested dicts.

    One query with a LEFT JOIN replaces the per-batch follow-up query the history
    endpoints use, and the LEFT keeps a header with no line items visible instead
    of dropping it from an audit record. Rows must be ordered by header id.
    """
    groups = []
    for row in rows:
        if not groups or groups[-1]['id'] != row['id']:
            groups.append({k: row[k] for k in header_keys} | {'items': []})
        if row['product_name'] is not None or row[item_keys[0]] is not None:
            groups[-1]['items'].append({k: row[k] for k in item_keys}
                                       | {'product_name': row['product_name'],
                                          'product_sku': row['product_sku']})
    return groups


def completed_orders(db, start, end):
    """Completed orders in the window, each with its line items.

    order_items has no created_at of its own, so the window is applied to the
    parent order -- the same rule sales_summary and the top-sellers query follow.
    """
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    rows = db.execute("""
        SELECT o.id, o.created_at, o.total_amount, o.buyer_name, o.payment_method,
               oi.quantity, oi.unit_price, oi.unit_cost, oi.subtotal,
               p.name AS product_name, p.sku AS product_sku
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE o.status = 'completed'
    """ + date_filter + " ORDER BY o.created_at, o.id, oi.id", params).fetchall()
    # unit_cost rides along for the CSV export; the PDF builds its rows from the
    # keys it names, so carrying one more costs it nothing. Same for payment_method,
    # which the CSV exports and the PDF leaves out to keep its row on one line.
    return _group(rows, ('id', 'created_at', 'total_amount', 'buyer_name', 'payment_method'),
                  ('quantity', 'unit_price', 'unit_cost', 'subtotal'))


def restock_batches(db, start, end):
    """Restock batches in the window, each with its line items."""
    date_filter, params = build_date_filter(start, end, 'rb.created_at')
    rows = db.execute("""
        SELECT rb.id, rb.created_at, rb.subtotal_cost, rb.discount, rb.shipping_cost,
               rb.admin_fee, rb.total_cost, rb.voids_batch_id,
               ri.qty_added, ri.unit_price, ri.unit_cost, ri.allocated_cost,
               p.name AS product_name, p.sku AS product_sku
        FROM restock_batches rb
        LEFT JOIN restock_items ri ON ri.batch_id = rb.id
        LEFT JOIN products p ON p.id = ri.product_id
        WHERE 1=1
    """ + date_filter + " ORDER BY rb.created_at, rb.id, ri.id", params).fetchall()
    return _group(rows, ('id', 'created_at', 'subtotal_cost', 'discount', 'shipping_cost',
                         'admin_fee', 'total_cost', 'voids_batch_id'),
                  ('qty_added', 'unit_price', 'unit_cost', 'allocated_cost'))


def self_use_batches(db, start, end):
    """Self-use batches in the window, each with its line items."""
    date_filter, params = build_date_filter(start, end, 'sb.created_at')
    rows = db.execute("""
        SELECT sb.id, sb.created_at, sb.total_value, sb.voids_batch_id,
               su.quantity, su.unit_price, su.subtotal,
               p.name AS product_name, p.sku AS product_sku
        FROM self_use_batches sb
        LEFT JOIN self_use_items su ON su.batch_id = sb.id
        LEFT JOIN products p ON p.id = su.product_id
        WHERE 1=1
    """ + date_filter + " ORDER BY sb.created_at, sb.id, su.id", params).fetchall()
    return _group(rows, ('id', 'created_at', 'total_value', 'voids_batch_id'),
                  ('quantity', 'unit_price', 'subtotal'))


def collect(db, offset, tz, lang, now=None):
    """Everything the report renders, for the month `offset` months back.

    offset=0 is the current (incomplete) month, 1 the month that just closed.
    """
    start, end = get_date_range('month', offset, tz, now=now)
    summary = services.sales_summary(db, 'month', offset, tz, now=now)
    stock_value = db.execute(
        "SELECT COALESCE(SUM(price * stock_qty), 0) AS total FROM products "
        "WHERE is_archived = 0").fetchone()['total']
    return {
        'period': period_key(start),
        'label': i18n.month_label(start, lang),
        'start': start,
        'end': end,
        'tz': tz,
        'generated_at': (now or datetime.now(tz)).astimezone(tz),
        'summary': summary,
        # Stock value is a point-in-time figure, not a windowed one: it describes
        # the shelves right now, not at month end, and is labelled as such.
        'stock_value': stock_value,
        'by_quantity': services.top_products_by_quantity(db, start, end),
        'by_profit': services.top_products_by_profit(db, start, end),
        'uncosted_sales': services.sales_missing_cost(db, start, end),
        # Unsliced: an audit document should account for every idle product, and the
        # section paginates on its own if the list is long.
        'unsold': services.products_without_sales(db, start, end),
        'orders': completed_orders(db, start, end),
        'restocks': restock_batches(db, start, end),
        'self_uses': self_use_batches(db, start, end),
    }


# --- CSV export ---

def _num(value):
    """A number a spreadsheet can sum.

    Rupiah amounts are whole in practice and SQLite hands them back as REAL, so a
    column of '25000.0' would be noise; anything with a genuine fraction keeps it.
    """
    if value is None:
        return ''
    return str(int(value)) if float(value).is_integer() else f'{float(value):.2f}'


def _payment_label(method, t):
    """Translated label for a stored payment slug; '' when none was recorded.

    Blank rather than a placeholder for the same reason a missing unit cost exports
    blank: a spreadsheet can filter an empty cell out, and any word put there would
    be one more value to explain to whoever sorts the column.
    """
    label = services.PAYMENT_METHOD_LABELS.get(method)
    return t(label) if label else ''


def sales_csv(db, offset, tz, lang, now=None):
    """Every sold line of a month, as CSV text.

    A sibling of the PDF rather than a replacement: the report is a document to file
    away, this is the same month in a shape a spreadsheet or a tax return can take.
    What counts follows the same rule -- completed orders only, so drafts, confirmed
    but unpaid, and cancelled orders never appear.

    Deliberately not built on collect(): that also computes the summary, the top
    sellers and the unsold list, none of which belongs in a row-per-line export.
    """
    t = i18n.make_t(lang)
    start, end = get_date_range('month', offset, tz, now=now)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([t('Order'), t('Date'), t('Buyer'), t('Payment Method'),
                     t('Product'), t('SKU'), t('Qty'),
                     t('Unit Price'), t('Unit Cost'), t('Subtotal'), t('Profit')])
    for order in completed_orders(db, start, end):
        # Repeated on every line of an order, the same way the order number and date
        # already are: a spreadsheet filters and pivots on a column, and a value that
        # appears only on an order's first row disappears the moment one is sorted.
        buyer = order['buyer_name'] or ''
        method = _payment_label(order['payment_method'], t)
        for item in order['items']:
            # A cost of 0 means "never recorded" everywhere in this app, and a
            # spreadsheet cannot tell that from stock that genuinely cost nothing.
            # Blank says unknown, and the profit that would have been derived from it
            # stays blank too rather than reading as pure margin.
            costed = item['unit_cost'] > 0
            profit = item['subtotal'] - item['unit_cost'] * item['quantity']
            writer.writerow([
                order['id'],
                _local(order['created_at'], tz),
                buyer,
                method,
                item['product_name'] or '',
                item['product_sku'] or '',
                item['quantity'],
                _num(item['unit_price']),
                _num(item['unit_cost']) if costed else '',
                _num(item['subtotal']),
                _num(profit) if costed else '',
            ])
    return out.getvalue()


# --- Rendering ---

def _local(utc_str, tz):
    """Stored UTC timestamp -> 'YYYY-MM-DD HH:MM' in the shop's timezone.

    Numeric on purpose: a localized month name inside a dense audit table costs
    width without helping anyone reconcile a row.
    """
    if not utc_str:
        return ''
    try:
        dt = datetime.fromisoformat(str(utc_str)).replace(tzinfo=timezone.utc)
    except ValueError:
        return str(utc_str)
    return dt.astimezone(tz).strftime('%Y-%m-%d %H:%M')


def _pdf_class():
    """Import fpdf lazily and build the document class.

    Deferred so that importing this module -- which app.py does at startup, and
    which the i18n coverage test does to scan it -- never depends on fpdf2 being
    installed. Only actually rendering a report does.
    """
    from fpdf import FPDF

    class ReportPDF(FPDF):
        """A4 portrait with a running title and 'page N of M' footer."""

        def __init__(self, title, subtitle):
            super().__init__(orientation='P', unit='mm', format='A4')
            self.report_title = title
            self.report_subtitle = subtitle
            self.set_auto_page_break(auto=True, margin=18)
            self.set_margin(12)
            self.add_font(FONT_NAME, '', os.path.join(FONT_DIR, 'DejaVuSansCondensed.ttf'))
            self.add_font(FONT_NAME, 'B', os.path.join(FONT_DIR, 'DejaVuSansCondensed-Bold.ttf'))
            self.set_font(FONT_NAME, '', 9)

        def header(self):
            if self.page_no() == 1:  # page 1 carries the full title block instead
                return
            self.set_font(FONT_NAME, '', 7.5)
            self.set_text_color(120)
            self.cell(0, 6, f'{self.report_title} — {self.report_subtitle}', align='L')
            self.ln(8)
            self.set_text_color(0)

        def footer(self):
            self.set_y(-14)
            self.set_font(FONT_NAME, '', 7.5)
            self.set_text_color(120)
            self.cell(0, 6, f'{self.page_no()} / {{nb}}', align='C')
            self.set_text_color(0)

    return ReportPDF


def _heading(pdf, text, size=12):
    pdf.set_font(FONT_NAME, 'B', size)
    pdf.cell(0, 7, text, align='L')
    pdf.ln(9)
    pdf.set_font(FONT_NAME, '', 9)


def _table(pdf, headings, rows, widths, align=None, empty_text=None):
    """One data table, or `empty_text` when there is nothing to show."""
    if not rows:
        pdf.set_font(FONT_NAME, '', 9)
        pdf.set_text_color(120)
        pdf.cell(0, 6, empty_text or '', align='L')
        pdf.ln(9)
        pdf.set_text_color(0)
        return
    pdf.set_font(FONT_NAME, '', 8)
    with pdf.table(col_widths=widths, text_align=align or 'LEFT',
                   line_height=5.2, padding=1.2,
                   headings_style=_headings_style()) as table:
        table.row(headings)
        for row in rows:
            table.row([str(c) for c in row])
    pdf.ln(4)


def _headings_style():
    from fpdf.fonts import FontFace
    return FontFace(emphasis='BOLD', color=(255, 255, 255), fill_color=(70, 80, 95))


def _summary_page(pdf, data, t):
    s = data['summary']
    pdf.set_font(FONT_NAME, 'B', 17)
    pdf.cell(0, 10, t('Monthly Report'), align='L')
    pdf.ln(11)
    pdf.set_font(FONT_NAME, 'B', 12)
    pdf.set_text_color(70, 80, 95)
    pdf.cell(0, 7, data['label'], align='L')
    pdf.ln(8)
    pdf.set_font(FONT_NAME, '', 7.5)
    pdf.set_text_color(120)
    pdf.cell(0, 5, t('Generated {timestamp}', timestamp=data['generated_at'].strftime('%Y-%m-%d %H:%M %Z')),
             align='L')
    pdf.ln(10)
    pdf.set_text_color(0)

    _heading(pdf, t('Sales Performance'))
    _table(pdf, [t('Metric'), t('Value')], [
        (t('Total Revenue'), format_rupiah(s['total_revenue'])),
        (t('Completed Orders'), s['total_orders']),
        (t('Unique SKUs Sold'), s['unique_skus']),
        (t('Total Items Sold'), s['total_items_sold']),
        (t('Restock Cost'), format_rupiah(s['restock_cost'])),
        (t('Net Profit'), format_rupiah(s['net_profit'])),
        (t('Cost of Goods Sold'), format_rupiah(s['cogs'])),
        (t('Gross Profit'), format_rupiah(s['gross_profit'])),
        (t('Self Use'), format_rupiah(s['self_use_value'])),
        (t('Stock Value (today)'), format_rupiah(data['stock_value'])),
    ], widths=(58, 32), align=('LEFT', 'RIGHT'))

    pdf.set_font(FONT_NAME, '', 7.5)
    pdf.set_text_color(120)
    # Single-line literal: tests/test_i18n_coverage.py scans line by line, so an
    # implicitly concatenated string would register a fragment as the key.
    pdf.multi_cell(0, 4.2, t('Net profit is revenue minus restock cost. Self use is reported separately and never subtracted: those goods were already paid for as restock spend.'))
    # multi_cell leaves the cursor at the right margin, where a second full-width cell
    # would have no room to render.
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 4.2, t('Gross profit is revenue minus what the goods sold this month cost, so it ignores stock bought but not yet sold. A month of heavy restocking shows a thin net profit and a healthy gross one. Sales whose cost was never recorded are left out of both it and cost of goods sold.'))
    pdf.ln(6)
    pdf.set_text_color(0)

    lang = t.lang
    seller_widths = (68, 34, 22, 34)
    seller_align = ('LEFT', 'LEFT', 'RIGHT', 'RIGHT')

    _heading(pdf, t('Top 3 by Quantity'), size=11)
    _table(pdf, [t('Product'), t('SKU'), t('Qty Sold'), t('Revenue')], [
        (r['name'], r['sku'] or '—', r['total_sold'], format_rupiah(r['total_revenue']))
        for r in data['by_quantity']
    ], widths=seller_widths, align=seller_align, empty_text=t('No data yet'))

    # Ranked by money kept rather than money taken, so a cheap high-volume line cannot
    # hide which products actually carried the month.
    _heading(pdf, t('Top 3 by Profit'), size=11)
    _table(pdf, [t('Product'), t('SKU'), t('Profit'), t('Margin'), t('Share')], [
        (r['name'], r['sku'] or '—', format_rupiah(r['total_profit']),
         format_percent(r['margin'], lang), format_percent(r['share'], lang))
        for r in data['by_profit']
    ], widths=(62, 30, 32, 20, 20),
       align=('LEFT', 'LEFT', 'RIGHT', 'RIGHT', 'RIGHT'), empty_text=t('No data yet'))
    if data['uncosted_sales']:
        # An audit reader must not read the omission as the product having sold nothing.
        pdf.set_font(FONT_NAME, '', 7.5)
        pdf.set_text_color(120)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 4.2, t('{n} sale(s) are excluded from Gross Profit and from this ranking because no cost was recorded for the product when the order was created.', n=data['uncosted_sales']))
        pdf.set_text_color(0)


def _record_section(pdf, title, note, headings, widths, align, rows, total_row, empty_text):
    pdf.add_page()
    _heading(pdf, title)
    if note:
        pdf.set_font(FONT_NAME, '', 7.5)
        pdf.set_text_color(120)
        pdf.multi_cell(0, 4.2, note)
        pdf.ln(4)
        pdf.set_text_color(0)
    _table(pdf, headings, rows, widths=widths, align=align, empty_text=empty_text)
    if rows and total_row:
        pdf.set_font(FONT_NAME, 'B', 8.5)
        pdf.cell(0, 6, total_row, align='R')
        pdf.ln(7)
        pdf.set_font(FONT_NAME, '', 9)


def _sales_section(pdf, data, t):
    tz = data['tz']
    # An em dash where no buyer was recorded, matching how a missing product or SKU
    # already reads here. This is a document to file away, so a blank cell would look
    # like the renderer dropped something.
    rows = [(o['id'], _local(o['created_at'], tz), o['buyer_name'] or '—',
             it['product_name'] or '—',
             it['product_sku'] or '—', it['quantity'],
             format_rupiah(it['unit_price']), format_rupiah(it['subtotal']))
            for o in data['orders'] for it in o['items']]
    revenue = sum(o['total_amount'] for o in data['orders'])
    _record_section(
        pdf, t('Sales Records'),
        t('One row per product sold. Only completed orders are included: drafts, confirmed-but-unpaid and cancelled orders never move stock or revenue.'),
        [t('Order'), t('Date'), t('Buyer'), t('Product'), t('SKU'), t('Qty'),
         t('Unit Price'), t('Subtotal')],
        widths=(12, 24, 28, 40, 19, 9, 24, 25),
        align=('RIGHT', 'LEFT', 'LEFT', 'LEFT', 'LEFT', 'RIGHT', 'RIGHT', 'RIGHT'),
        rows=rows,
        total_row=t('Orders: {n} — total {amount}',
                    n=len(data['orders']), amount=format_rupiah(revenue)),
        empty_text=t('No records for this month'))


def _batch_label(batch):
    """`45/42` for a void of batch 42, otherwise the plain batch number.

    A void carries negated figures, so it already reads as a credit; the notation is what
    says which entry it takes back. Explained in each section's note.
    """
    if batch['voids_batch_id']:
        return f"{batch['id']}/{batch['voids_batch_id']}"
    return str(batch['id'])


def _restock_section(pdf, data, t):
    tz = data['tz']
    rows = [(_batch_label(b), _local(b['created_at'], tz), it['product_name'] or '—',
             it['product_sku'] or '—', it['qty_added'],
             format_rupiah(it['unit_price']), format_rupiah(it['unit_cost']),
             format_rupiah(it['allocated_cost']))
            for b in data['restocks'] for it in b['items']]
    cost = sum(b['total_cost'] for b in data['restocks'])
    _record_section(
        pdf, t('Restock Records'),
        t('One row per product restocked. Unit price is what the supplier invoice listed; unit cost adds that line’s share of the invoice discount, shipping and bank fee, split in proportion to line value. Landed cost is unit cost times quantity, and the lines of a batch sum to what was paid. A batch numbered 45/42 is a void: it reverses batch 42, and its negative figures cancel that entry out.'),
        [t('Batch'), t('Date'), t('Product'), t('SKU'), t('Qty Added'), t('Unit Price'),
         t('Unit Cost'), t('Landed Cost')],
        widths=(18, 24, 44, 18, 18, 24, 24, 26),
        align=('RIGHT', 'LEFT', 'LEFT', 'LEFT', 'RIGHT', 'RIGHT', 'RIGHT', 'RIGHT'),
        rows=rows,
        total_row=t('Batches: {n} — total {amount}',
                    n=len(data['restocks']), amount=format_rupiah(cost)),
        empty_text=t('No records for this month'))


def _self_use_section(pdf, data, t):
    tz = data['tz']
    rows = [(_batch_label(b), _local(b['created_at'], tz), it['product_name'] or '—',
             it['product_sku'] or '—', it['quantity'],
             format_rupiah(it['unit_price']), format_rupiah(it['subtotal']))
            for b in data['self_uses'] for it in b['items']]
    value = sum(b['total_value'] for b in data['self_uses'])
    _record_section(
        pdf, t('Self Use Records'),
        t('One row per product taken by the seller, valued at the retail price at the time of entry. No revenue, and not deducted from net profit. A batch numbered 45/42 is a void: it reverses batch 42, putting that stock back.'),
        [t('Batch'), t('Date'), t('Product'), t('SKU'), t('Qty'), t('Unit Price'), t('Subtotal')],
        widths=(20, 26, 51, 20, 11, 26, 27),
        align=('RIGHT', 'LEFT', 'LEFT', 'LEFT', 'RIGHT', 'RIGHT', 'RIGHT'),
        rows=rows,
        total_row=t('Batches: {n} — total {amount}',
                    n=len(data['self_uses']), amount=format_rupiah(value)),
        empty_text=t('No records for this month'))


def _no_sales_section(pdf, data, t):
    """Appendix: active products that sold nothing in the month.

    An appendix rather than part of page 1, because the list is unbounded -- it can
    run to more rows than every transaction in a quiet month.
    """
    rows = [(p['name'], p['sku'] or '—', p['stock_qty'], format_rupiah(p['stock_value']))
            for p in data['unsold']]
    at_risk = sum(p['stock_value'] for p in data['unsold'])
    _record_section(
        pdf, t('Products With No Sales'),
        t('Active products with no completed sale this month, most valuable idle stock first. Stock value is the current price times the quantity on hand.'),
        [t('Product'), t('SKU'), t('Stock'), t('Stock Value')],
        widths=(78, 40, 22, 41),
        align=('LEFT', 'LEFT', 'RIGHT', 'RIGHT'),
        rows=rows,
        total_row=t('Products: {n} — stock value {amount}',
                    n=len(rows), amount=format_rupiah(at_risk)),
        empty_text=t('All products sold at least once'))


def render(data, t):
    """Render collected report data to PDF bytes."""
    pdf = _pdf_class()(t('Monthly Report'), data['label'])
    pdf.alias_nb_pages()
    pdf.add_page()
    _summary_page(pdf, data, t)
    _sales_section(pdf, data, t)
    _restock_section(pdf, data, t)
    _self_use_section(pdf, data, t)
    _no_sales_section(pdf, data, t)
    return bytes(pdf.output())


# --- Archive ---

def save(content, period):
    """Write the PDF into the report directory, returning its path.

    Named after the month and overwritten on regeneration, so re-running a month
    corrects the archive instead of littering it with near-duplicates.
    """
    directory = REPORT_DIR
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, report_filename(period))
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(content)
    os.replace(tmp, path)  # never leave a half-written PDF where a reader looks
    return path


# --- Reusing an archived render ---

# Collecting a month costs milliseconds; rendering it costs seconds -- fpdf2 lays out
# and measures every cell, so the bill grows with the month's sale lines and reached
# roughly six seconds at a thousand orders. That is six seconds of CPU inside a server
# pinned to one worker (see AGENTS.md), which blocks every other request and, through
# the GIL, the bot poller with it. build() used to pay it again on every single
# download of the same closed month.
#
# The fingerprint is over the collected data, so anything that would change a figure --
# a late completion landing in the window, a void, a renamed product, a different
# language -- misses and re-renders. Only an identical report is served from disk.

def fingerprint(data, lang):
    """Stable hash of everything render() draws, for reusing an archived PDF.

    ``generated_at`` is deliberately excluded: it changes on every collect() and would
    make the hash miss every time. Serving the archive keeps the timestamp of the
    render that actually produced it, which is what the line claims to say.

    ``default=str`` covers the datetimes and the ZoneInfo; the timezone is in the hash
    because it decides where the month starts.
    """
    payload = {k: v for k, v in data.items() if k != 'generated_at'}
    return hashlib.sha256(json.dumps(
        {'lang': lang, 'data': payload}, sort_keys=True, default=str).encode()).hexdigest()


def _stamp_path(period):
    return os.path.join(REPORT_DIR, report_filename(period, 'sha256'))


def _archived(period, stamp):
    """The archived PDF for `period` if it was rendered from exactly `stamp`, else None.

    Any unreadable or mismatched sidecar simply misses, so a corrupt archive costs a
    re-render rather than serving the wrong month's figures.
    """
    try:
        with open(_stamp_path(period)) as f:
            if f.read().strip() != stamp:
                return None
        with open(os.path.join(REPORT_DIR, report_filename(period)), 'rb') as f:
            return f.read()
    except OSError:
        return None


def build(db, offset, tz, lang, now=None):
    """Collect, render and archive one month. Returns (path, content, data).

    Serves the archived PDF when the month's records still hash to what produced it,
    which is the common case for a closed month: it is downloaded repeatedly and cannot
    change on its own.
    """
    data = collect(db, offset, tz, lang, now=now)
    stamp = fingerprint(data, lang)
    path = os.path.join(REPORT_DIR, report_filename(data['period']))
    cached = _archived(data['period'], stamp)
    if cached is not None:
        log.info('monthly report %s served from the archive at %s (%d bytes)',
                 data['period'], path, len(cached))
        return path, cached, data
    content = render(data, i18n.make_t(lang))
    path = save(content, data['period'])
    # After the PDF, never before: a crash between the two leaves no stamp, which
    # misses and re-renders. The reverse would serve a stamp for a file that is not
    # there yet, or worse, for the previous render of the month.
    with open(_stamp_path(data['period']), 'w') as f:
        f.write(stamp)
    log.info('monthly report %s written to %s (%d bytes)',
             data['period'], path, len(content))
    return path, content, data
