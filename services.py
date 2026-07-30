"""Business logic shared by the web routes and the Telegram bot.

Each function takes an open sqlite3 connection, owns its transaction
(commit on success, rollback on business failure), and raises ServiceError /
NotFoundError for business rule violations. Callers translate: routes to
jsonify + status, the bot to a Telegram message.
"""
from datetime import datetime, timedelta, timezone, date, time as dtime


class ServiceError(Exception):
    """A business-rule violation surfaced to the user.

    The message is stored as an English template plus its ``str.format`` params
    so display sites can translate it (see ``i18n.translate_error``). ``str(e)``
    still renders the English message, keeping logging and non-UI callers simple.
    """
    status = 400

    def __init__(self, template, **params):
        self.template = template
        self.params = params
        super().__init__(template.format(**params) if params else template)


class NotFoundError(ServiceError):
    status = 404


# --- Formatting & time windows (used by routes, templates and the bot) ---

def format_rupiah(amount):
    """Format number as Indonesian Rupiah: Rp 150.000"""
    sign = '-' if amount < 0 else ''
    amount = abs(int(round(amount)))
    formatted = f'{amount:,}'.replace(',', '.')
    return f'{sign}Rp {formatted}'


def format_percent(value, lang='en'):
    """Percentage to one decimal: '61.3%', or '61,3%' in Indonesian.

    One decimal rather than a whole number so a small contributor reads as 0.4%
    instead of vanishing to 0%. The decimal separator follows the language for the
    same reason format_rupiah uses dot-thousands.
    """
    out = f'{value:.1f}'
    if lang == 'id':
        out = out.replace('.', ',')
    return f'{out}%'


def get_date_range(unit, offset=0, tz=timezone.utc, now=None):
    """Half-open [start, end) as tz-aware datetimes in tz. (None, None) for an unknown unit."""
    now = now or datetime.now(tz)
    today = now.date()
    if unit == 'day':
        start = today - timedelta(days=offset)
        end = start + timedelta(days=1)
    elif unit == 'week':
        start = today - timedelta(days=today.weekday() + offset * 7)  # Monday
        end = start + timedelta(days=7)
    elif unit == 'month':
        month, year = now.month - offset, now.year
        while month <= 0:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    elif unit == 'year':
        y = now.year - offset
        start, end = date(y, 1, 1), date(y + 1, 1, 1)
    else:
        return None, None
    return (datetime.combine(start, dtime.min, tzinfo=tz),
            datetime.combine(end, dtime.min, tzinfo=tz))


def _to_utc_str(dt):
    """tz-aware datetime -> 'YYYY-MM-DD HH:MM:SS' UTC, matching CURRENT_TIMESTAMP storage."""
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def build_date_filter(start, end, column='o.created_at'):
    """SQL fragment + params for half-open [start, end).

    `column` is interpolated into the SQL: pass only trusted literals, never request input.
    """
    return f" AND {column} >= ? AND {column} < ?", (_to_utc_str(start), _to_utc_str(end))


# --- Products ---

def list_products(db, page=0, page_size=8, search=None):
    """Active products for browsing. Returns (rows, has_more)."""
    query = "SELECT * FROM products WHERE is_archived = 0"
    params = []
    if search:
        query += " AND (name LIKE ? OR sku LIKE ?)"
        params += [f'%{search}%', f'%{search}%']
    query += " ORDER BY name LIMIT ? OFFSET ?"
    params += [page_size + 1, page * page_size]
    rows = db.execute(query, params).fetchall()
    return rows[:page_size], len(rows) > page_size


# --- Orders ---

def list_orders(db, status=None, page=0, page_size=10):
    """Orders newest-first, optionally filtered by status. Returns (rows, has_more)."""
    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [page_size + 1, page * page_size]
    rows = db.execute(query, params).fetchall()
    return rows[:page_size], len(rows) > page_size


def get_order(db, order_id):
    """Return (order, items with product names)."""
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    items = db.execute("""
        SELECT oi.*, p.name as product_name, p.sku as product_sku
        FROM order_items oi JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ? ORDER BY oi.id
    """, (order_id,)).fetchall()
    return order, items


def create_order(db, items):
    """items = [{'product_id': int, 'quantity': int}] (shape pre-validated by caller).

    Returns {'order_id', 'total'}.
    """
    total = 0
    rows = []
    for item in items:
        product = db.execute("SELECT * FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        if not product:
            raise NotFoundError('Product {id} not found', id=item['product_id'])
        if product['stock_qty'] < item['quantity']:
            raise ServiceError('Insufficient stock for {name}', name=product['name'])
        subtotal = product['price'] * item['quantity']
        total += subtotal
        rows.append((item['product_id'], item['quantity'], product['price'], subtotal))

    cur = db.execute("INSERT INTO orders (status, total_amount) VALUES (?, ?)", ('draft', total))
    order_id = cur.lastrowid
    db.executemany("""
        INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)
        VALUES (?, ?, ?, ?, ?)
    """, [(order_id, pid, qty, price, subtotal) for pid, qty, price, subtotal in rows])
    db.commit()
    return {'order_id': order_id, 'total': total}


def confirm_order(db, order_id):
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    if order['status'] != 'draft':
        raise ServiceError('Only draft orders can be confirmed')
    db.execute("UPDATE orders SET status = 'confirmed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order_id,))
    db.commit()


def complete_order(db, order_id):
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    if order['status'] != 'confirmed':
        raise ServiceError('Only confirmed orders can be completed')
    items = db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    for item in items:
        # Conditional decrement is atomic: no read-modify-write window for concurrent
        # requests to double-count against.
        cur = db.execute(
            "UPDATE products SET stock_qty = stock_qty - ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ? AND stock_qty >= ?",
            (item['quantity'], item['product_id'], item['quantity']))
        if cur.rowcount == 0:
            db.rollback()
            raise ServiceError('Insufficient stock for product #{id}', id=item['product_id'])
        db.execute("INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (?, ?, ?)",
                   (item['product_id'], -item['quantity'], f'sale order #{order_id}'))
    db.execute("UPDATE orders SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order_id,))
    db.commit()


def cancel_order(db, order_id):
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    if order['status'] == 'completed':
        raise ServiceError('Cannot cancel completed orders')
    if order['status'] == 'cancelled':
        raise ServiceError('Order already cancelled')
    db.execute("UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order_id,))
    db.commit()


# --- Restock ---

def create_restock(db, items, total_cost):
    """items = [{'product_id': int, 'qty': int}] (shape pre-validated). Returns batch_id."""
    total_qty = 0
    for item in items:
        product = db.execute("SELECT * FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        if not product:
            raise NotFoundError('Product {id} not found', id=item['product_id'])
        total_qty += item['qty']

    cur = db.execute("INSERT INTO restock_batches (total_cost) VALUES (?)", (total_cost,))
    batch_id = cur.lastrowid
    for item in items:
        qty_added = item['qty']
        allocated_cost = (qty_added / total_qty) * total_cost if total_qty > 0 else 0
        cur = db.execute(
            "UPDATE products SET stock_qty = stock_qty + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (qty_added, item['product_id']))
        if cur.rowcount == 0:
            db.rollback()
            raise NotFoundError('Product {id} not found', id=item['product_id'])
        db.execute("INSERT INTO restock_items (batch_id, product_id, qty_added, allocated_cost) VALUES (?, ?, ?, ?)",
                   (batch_id, item['product_id'], qty_added, allocated_cost))
        db.execute("INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (?, ?, ?)",
                   (item['product_id'], qty_added, f'restock batch #{batch_id}'))
    db.commit()
    return batch_id


# --- Self use ---

def create_self_use(db, items):
    """items = [{'product_id': int, 'qty': int}] (shape pre-validated by caller).

    Stock the seller took for themself: decrements stock and values each line at
    the product's current retail price, exactly like an order line. It produces
    no revenue and no cost -- the money was already booked as restock spend --
    so it is reported as its own metric and never enters net_profit.

    Returns {'batch_id', 'total_value'}.
    """
    # Sum per product first: a submission may list the same product twice (the
    # web form does not dedupe), and checking each line against the full stock
    # would let such a batch pass this named check only to fail below on the
    # guarded UPDATE with the less helpful id-only message.
    needed = {}
    for item in items:
        needed[item['product_id']] = needed.get(item['product_id'], 0) + item['qty']

    total_value = 0
    rows = []
    for item in items:
        product = db.execute("SELECT * FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        if not product:
            raise NotFoundError('Product {id} not found', id=item['product_id'])
        if product['stock_qty'] < needed[item['product_id']]:
            raise ServiceError('Insufficient stock for {name}', name=product['name'])
        subtotal = product['price'] * item['qty']
        total_value += subtotal
        rows.append((item['product_id'], item['qty'], product['price'], subtotal))

    cur = db.execute("INSERT INTO self_use_batches (total_value) VALUES (?)", (total_value,))
    batch_id = cur.lastrowid
    for product_id, qty, price, subtotal in rows:
        # Conditional decrement is atomic: no read-modify-write window for
        # concurrent requests to double-count against (see complete_order).
        cur = db.execute(
            "UPDATE products SET stock_qty = stock_qty - ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ? AND stock_qty >= ?",
            (qty, product_id, qty))
        if cur.rowcount == 0:
            db.rollback()
            raise ServiceError('Insufficient stock for product #{id}', id=product_id)
        db.execute("INSERT INTO self_use_items (batch_id, product_id, quantity, unit_price, subtotal)"
                   " VALUES (?, ?, ?, ?, ?)",
                   (batch_id, product_id, qty, price, subtotal))
        db.execute("INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (?, ?, ?)",
                   (product_id, -qty, f'self use batch #{batch_id}'))
    db.commit()
    return {'batch_id': batch_id, 'total_value': total_value}


# --- Sales summary ---

def sales_summary(db, unit, offset, tz, now=None):
    """Revenue/orders/items/restock-cost/profit for the window. Raises on bad unit.

    `now` overrides the clock the window is measured back from, so a caller that
    already fixed a window (the monthly report) gets a summary describing the same
    period rather than one re-derived from the real time.
    """
    start, end = get_date_range(unit, offset, tz, now=now)
    if not start:
        raise ServiceError('invalid unit')
    date_filter, params = build_date_filter(start, end)
    # Revenue/order count come from `orders` alone: joining order_items here would
    # repeat total_amount once per line item and inflate both revenue and profit.
    row = db.execute("""
        SELECT
            COALESCE(SUM(o.total_amount), 0) as total_revenue,
            COUNT(*) as total_orders
        FROM orders o
        WHERE o.status = 'completed'
    """ + date_filter, params).fetchone()
    items = db.execute("""
        SELECT
            COUNT(DISTINCT oi.product_id) as unique_skus,
            COALESCE(SUM(oi.quantity), 0) as total_items_sold
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        WHERE o.status = 'completed'
    """ + date_filter, params).fetchone()

    # Both batch tables date-filter on a bare `created_at`, so build the fragment once.
    batch_filter, batch_params = build_date_filter(start, end, 'created_at')
    restock_cost = db.execute(
        "SELECT COALESCE(SUM(total_cost), 0) as total FROM restock_batches WHERE 1=1" + batch_filter,
        batch_params
    ).fetchone()['total']
    self_use_value = db.execute(
        "SELECT COALESCE(SUM(total_value), 0) as total FROM self_use_batches WHERE 1=1" + batch_filter,
        batch_params
    ).fetchone()['total']

    return {
        'total_revenue': row['total_revenue'],
        'total_orders': row['total_orders'],
        'unique_skus': items['unique_skus'],
        'total_items_sold': items['total_items_sold'],
        'restock_cost': restock_cost,
        'self_use_value': self_use_value,
        # Self use sits alongside profit, never inside it: the goods were already
        # paid for as restock spend, so deducting their retail value here would
        # double-count. Keep this expression as-is.
        'net_profit': row['total_revenue'] - restock_cost,
        'start': start,
        'end': end,
    }


# --- Product performance (shared by the sales page and the monthly report) ---

# Sold quantity and revenue per product over a window. Revenue per product has to
# come from order_items.subtotal: orders.total_amount is a separately stored figure
# for the whole order and cannot be attributed to a line.
_SOLD_PER_PRODUCT = """
    SELECT p.id, p.name, p.sku,
           SUM(oi.quantity) AS total_sold,
           SUM(oi.subtotal) AS total_revenue
    FROM order_items oi
    JOIN orders o ON oi.order_id = o.id
    JOIN products p ON oi.product_id = p.id
    WHERE o.status = 'completed'
"""


def _sold_per_product(db, start, end, order_by, limit):
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    rows = db.execute(
        _SOLD_PER_PRODUCT + date_filter + f" GROUP BY p.id ORDER BY {order_by} LIMIT ?",
        params + (limit,)).fetchall()
    return [dict(r) for r in rows]


def top_products_by_quantity(db, start, end, limit=3):
    """Best sellers by units moved."""
    return _sold_per_product(db, start, end, 'total_sold DESC', limit)


def top_products_by_value(db, start, end, limit=3):
    """Best sellers by revenue, each with its `share` of the window's sales value.

    Share is a percentage of the summed line subtotals, NOT of sales_summary's
    total_revenue: that comes from orders.total_amount, a separately stored value
    that can disagree with the sum of its lines, so dividing by it would produce
    shares that never total 100.
    """
    rows = _sold_per_product(db, start, end, 'total_revenue DESC', limit)
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    total = db.execute("""
        SELECT COALESCE(SUM(oi.subtotal), 0) AS total
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.status = 'completed'
    """ + date_filter, params).fetchone()['total']
    for row in rows:
        row['share'] = (row['total_revenue'] / total * 100) if total else 0.0
    return rows


def products_without_sales(db, start, end):
    """Active products with no completed-order line in the window.

    The dead stock the top-seller queries structurally cannot show: they inner-join
    order_items, so a product must have sold at least once to appear at all.
    Ordered by the capital tied up in it, so the most expensive idle stock is first.
    Returns every match -- callers slice for display.
    """
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    rows = db.execute("""
        SELECT p.id, p.name, p.sku, p.stock_qty,
               p.price * p.stock_qty AS stock_value
        FROM products p
        WHERE p.is_archived = 0
          AND p.id NOT IN (
              SELECT oi.product_id
              FROM order_items oi
              JOIN orders o ON oi.order_id = o.id
              WHERE o.status = 'completed'
    """ + date_filter + """
          )
        ORDER BY stock_value DESC, p.name
    """, params).fetchall()
    return [dict(r) for r in rows]


# --- Stale-order alerts (used by the Telegram bot poller) ---

def find_stale_orders(db, hours, now=None):
    """Orders stuck in draft/confirmed longer than `hours`, not yet alerted for
    their current status.

    Staleness is measured from ``updated_at`` (time in the current state), so a
    freshly-confirmed order restarts the clock. ``hours`` of None or <= 0 disables
    alerting and returns an empty list.
    """
    if not hours or hours <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = _to_utc_str(now - timedelta(hours=hours))
    return db.execute(
        "SELECT * FROM orders "
        "WHERE status IN ('draft', 'confirmed') AND updated_at <= ? "
        "  AND (alerted_status IS NULL OR alerted_status != status) "
        "ORDER BY id",
        (cutoff,)).fetchall()


def mark_order_alerted(db, order_id, status):
    """Record that a stale-order alert was sent for `order_id` while in `status`.

    Keyed on the status value so an order that later stalls in a different state
    (draft -> confirmed) is flagged again exactly once.
    """
    db.execute("UPDATE orders SET alerted_status = ? WHERE id = ?", (status, order_id))
    db.commit()
