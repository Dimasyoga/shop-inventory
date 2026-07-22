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


# --- Sales summary ---

def sales_summary(db, unit, offset, tz):
    """Revenue/orders/items/restock-cost/profit for the window. Raises on bad unit."""
    start, end = get_date_range(unit, offset, tz)
    if not start:
        raise ServiceError('invalid unit')
    date_filter, params = build_date_filter(start, end)
    row = db.execute("""
        SELECT
            COALESCE(SUM(o.total_amount), 0) as total_revenue,
            COUNT(DISTINCT o.id) as total_orders,
            COUNT(DISTINCT oi.product_id) as unique_skus,
            COALESCE(SUM(oi.quantity), 0) as total_items_sold
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        WHERE o.status = 'completed'
    """ + date_filter, params).fetchone()

    restock_filter, restock_params = build_date_filter(start, end, 'created_at')
    restock_cost = db.execute(
        "SELECT COALESCE(SUM(total_cost), 0) as total FROM restock_batches WHERE 1=1" + restock_filter,
        restock_params
    ).fetchone()['total']

    return {
        'total_revenue': row['total_revenue'],
        'total_orders': row['total_orders'],
        'unique_skus': row['unique_skus'],
        'total_items_sold': row['total_items_sold'],
        'restock_cost': restock_cost,
        'net_profit': row['total_revenue'] - restock_cost,
        'start': start,
        'end': end,
    }
