"""Monthly audit report: collect a calendar month's records and render them to PDF.

Page 1 summarizes the month's sales performance; the sections after it list every
completed order, restock batch and self-use batch line by line, so the figures on
page 1 can be reconciled against the underlying rows.

Like services.py this module takes an open sqlite3 connection and must not import
app.py -- both the web routes and the Telegram bot render reports.
"""
import logging
import os
from datetime import datetime, timezone

import i18n
import services
from services import build_date_filter, format_rupiah, get_date_range

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


def report_filename(period):
    return f'shop-report-{period}.pdf'


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
        SELECT o.id, o.created_at, o.total_amount,
               oi.quantity, oi.unit_price, oi.subtotal,
               p.name AS product_name, p.sku AS product_sku
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE o.status = 'completed'
    """ + date_filter + " ORDER BY o.created_at, o.id, oi.id", params).fetchall()
    return _group(rows, ('id', 'created_at', 'total_amount'),
                  ('quantity', 'unit_price', 'subtotal'))


def restock_batches(db, start, end):
    """Restock batches in the window, each with its line items."""
    date_filter, params = build_date_filter(start, end, 'rb.created_at')
    rows = db.execute("""
        SELECT rb.id, rb.created_at, rb.total_cost,
               ri.qty_added, ri.allocated_cost,
               p.name AS product_name, p.sku AS product_sku
        FROM restock_batches rb
        LEFT JOIN restock_items ri ON ri.batch_id = rb.id
        LEFT JOIN products p ON p.id = ri.product_id
        WHERE 1=1
    """ + date_filter + " ORDER BY rb.created_at, rb.id, ri.id", params).fetchall()
    return _group(rows, ('id', 'created_at', 'total_cost'),
                  ('qty_added', 'allocated_cost'))


def self_use_batches(db, start, end):
    """Self-use batches in the window, each with its line items."""
    date_filter, params = build_date_filter(start, end, 'sb.created_at')
    rows = db.execute("""
        SELECT sb.id, sb.created_at, sb.total_value,
               su.quantity, su.unit_price, su.subtotal,
               p.name AS product_name, p.sku AS product_sku
        FROM self_use_batches sb
        LEFT JOIN self_use_items su ON su.batch_id = sb.id
        LEFT JOIN products p ON p.id = su.product_id
        WHERE 1=1
    """ + date_filter + " ORDER BY sb.created_at, sb.id, su.id", params).fetchall()
    return _group(rows, ('id', 'created_at', 'total_value'),
                  ('quantity', 'unit_price', 'subtotal'))


def _sellers(db, start, end, direction):
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    return db.execute("""
        SELECT p.name, p.sku, SUM(oi.quantity) AS total_sold,
               SUM(oi.subtotal) AS total_revenue
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        JOIN products p ON oi.product_id = p.id
        WHERE o.status = 'completed'
    """ + date_filter + f" GROUP BY p.id ORDER BY total_sold {direction} LIMIT 3",
        params).fetchall()


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
        'top': _sellers(db, start, end, 'DESC'),
        'bottom': _sellers(db, start, end, 'ASC'),
        'orders': completed_orders(db, start, end),
        'restocks': restock_batches(db, start, end),
        'self_uses': self_use_batches(db, start, end),
    }


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
        (t('Self Use'), format_rupiah(s['self_use_value'])),
        (t('Stock Value (today)'), format_rupiah(data['stock_value'])),
    ], widths=(58, 32), align=('LEFT', 'RIGHT'))

    pdf.set_font(FONT_NAME, '', 7.5)
    pdf.set_text_color(120)
    # Single-line literal: tests/test_i18n_coverage.py scans line by line, so an
    # implicitly concatenated string would register a fragment as the key.
    pdf.multi_cell(0, 4.2, t('Net profit is revenue minus restock cost. Self use is reported separately and never subtracted: those goods were already paid for as restock spend.'))
    pdf.ln(6)
    pdf.set_text_color(0)

    seller_cols = [t('Product'), t('SKU'), t('Qty Sold'), t('Revenue')]
    seller_widths = (68, 34, 22, 34)
    seller_align = ('LEFT', 'LEFT', 'RIGHT', 'RIGHT')
    _heading(pdf, t('Top 3 Sellers'), size=11)
    _table(pdf, seller_cols, [
        (r['name'], r['sku'] or '—', r['total_sold'], format_rupiah(r['total_revenue']))
        for r in data['top']
    ], widths=seller_widths, align=seller_align, empty_text=t('No data yet'))

    _heading(pdf, t('Bottom 3 Sellers'), size=11)
    _table(pdf, seller_cols, [
        (r['name'], r['sku'] or '—', r['total_sold'], format_rupiah(r['total_revenue']))
        for r in data['bottom']
    ], widths=seller_widths, align=seller_align, empty_text=t('No data yet'))


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
    rows = [(o['id'], _local(o['created_at'], tz), it['product_name'] or '—',
             it['product_sku'] or '—', it['quantity'],
             format_rupiah(it['unit_price']), format_rupiah(it['subtotal']))
            for o in data['orders'] for it in o['items']]
    revenue = sum(o['total_amount'] for o in data['orders'])
    _record_section(
        pdf, t('Sales Records'),
        t('One row per product sold. Only completed orders are included: drafts, confirmed-but-unpaid and cancelled orders never move stock or revenue.'),
        [t('Order'), t('Date'), t('Product'), t('SKU'), t('Qty'), t('Unit Price'), t('Subtotal')],
        widths=(13, 26, 51, 26, 11, 26, 28),
        align=('RIGHT', 'LEFT', 'LEFT', 'LEFT', 'RIGHT', 'RIGHT', 'RIGHT'),
        rows=rows,
        total_row=t('Orders: {n} — total {amount}',
                    n=len(data['orders']), amount=format_rupiah(revenue)),
        empty_text=t('No records for this month'))


def _restock_section(pdf, data, t):
    tz = data['tz']
    rows = [(b['id'], _local(b['created_at'], tz), it['product_name'] or '—',
             it['product_sku'] or '—', it['qty_added'],
             format_rupiah(it['allocated_cost']))
            for b in data['restocks'] for it in b['items']]
    cost = sum(b['total_cost'] for b in data['restocks'])
    _record_section(
        pdf, t('Restock Records'),
        t('One row per product restocked. Cost is allocated across a batch in proportion to quantity, so a line cost is a share of the batch total, not a supplier price.'),
        [t('Batch'), t('Date'), t('Product'), t('SKU'), t('Qty Added'), t('Allocated Cost')],
        widths=(14, 27, 57, 30, 22, 31),
        align=('RIGHT', 'LEFT', 'LEFT', 'LEFT', 'RIGHT', 'RIGHT'),
        rows=rows,
        total_row=t('Batches: {n} — total {amount}',
                    n=len(data['restocks']), amount=format_rupiah(cost)),
        empty_text=t('No records for this month'))


def _self_use_section(pdf, data, t):
    tz = data['tz']
    rows = [(b['id'], _local(b['created_at'], tz), it['product_name'] or '—',
             it['product_sku'] or '—', it['quantity'],
             format_rupiah(it['unit_price']), format_rupiah(it['subtotal']))
            for b in data['self_uses'] for it in b['items']]
    value = sum(b['total_value'] for b in data['self_uses'])
    _record_section(
        pdf, t('Self Use Records'),
        t('One row per product taken by the seller, valued at the retail price at the time of entry. No revenue, and not deducted from net profit.'),
        [t('Batch'), t('Date'), t('Product'), t('SKU'), t('Qty'), t('Unit Price'), t('Subtotal')],
        widths=(14, 26, 51, 26, 11, 26, 27),
        align=('RIGHT', 'LEFT', 'LEFT', 'LEFT', 'RIGHT', 'RIGHT', 'RIGHT'),
        rows=rows,
        total_row=t('Batches: {n} — total {amount}',
                    n=len(data['self_uses']), amount=format_rupiah(value)),
        empty_text=t('No records for this month'))


def render(data, t):
    """Render collected report data to PDF bytes."""
    pdf = _pdf_class()(t('Monthly Report'), data['label'])
    pdf.alias_nb_pages()
    pdf.add_page()
    _summary_page(pdf, data, t)
    _sales_section(pdf, data, t)
    _restock_section(pdf, data, t)
    _self_use_section(pdf, data, t)
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


def build(db, offset, tz, lang, now=None):
    """Collect, render and archive one month. Returns (path, content, data)."""
    data = collect(db, offset, tz, lang, now=now)
    content = render(data, i18n.make_t(lang))
    path = save(content, data['period'])
    log.info('monthly report %s written to %s (%d bytes)',
             data['period'], path, len(content))
    return path, content, data
