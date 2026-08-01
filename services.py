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

# Products whose cost figure cannot be relied on, in the two ways that can happen:
# nothing was ever recorded (0 = unknown, see _COSTED_LINE below), or a void left a
# figure standing that the restock behind it no longer supports. Stock on hand is part
# of the first case only -- a product sitting at zero costs nothing until it is
# restocked, and that restock will record a cost -- while a suspect figure is worth
# fixing whether or not there is stock, because sales already snapshotted it.
NEEDS_COST = "(cost_review_needed = 1 OR (cost_price <= 0 AND stock_qty > 0))"


def count_needs_cost(db):
    """How many active products need a cost typed in or checked."""
    return db.execute(
        f"SELECT COUNT(*) AS n FROM products WHERE is_archived = 0 AND {NEEDS_COST}"
    ).fetchone()['n']


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
        # unit_cost is snapshotted alongside unit_price for the same reason: a later
        # restock at a different price must not rewrite the margin of a sale already made.
        rows.append((item['product_id'], item['quantity'], product['price'],
                     product['cost_price'], subtotal))

    cur = db.execute("INSERT INTO orders (status, total_amount) VALUES (?, ?)", ('draft', total))
    order_id = cur.lastrowid
    db.executemany("""
        INSERT INTO order_items (order_id, product_id, quantity, unit_price, unit_cost, subtotal)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [(order_id, pid, qty, price, cost, subtotal) for pid, qty, price, cost, subtotal in rows])
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

def allocate_restock_costs(items, discount=0, shipping_cost=0, admin_fee=0):
    """Landed cost per line from a supplier invoice.

    items = [{'product_id': int, 'qty': int, 'unit_price': float}] -- the invoice as
    written, one line per product at its listed price. The three charges apply to the
    invoice as a whole, so each line absorbs them in proportion to its own value: a
    voucher discounts what was actually bought, and shipping rides on the goods rather
    than falling equally on a cheap line and an expensive one.

    Returns (lines, subtotal, total_cost) where each line is
    {'product_id', 'qty', 'unit_price', 'unit_cost', 'line_cost'} and the line costs sum
    to total_cost -- the money actually paid.
    """
    subtotal = sum(item['qty'] * item['unit_price'] for item in items)
    total_qty = sum(item['qty'] for item in items)
    total_cost = subtotal - discount + shipping_cost + admin_fee

    lines = []
    for item in items:
        qty = item['qty']
        if subtotal > 0:
            share = (qty * item['unit_price']) / subtotal
        elif total_qty > 0:
            # A batch priced entirely at zero -- a supplier sample, say -- still has
            # shipping to spread, and there are no line values to weight it by.
            share = qty / total_qty
        else:
            share = 0
        line_cost = total_cost * share
        lines.append({
            'product_id': item['product_id'],
            'qty': qty,
            'unit_price': item['unit_price'],
            'unit_cost': line_cost / qty if qty else 0,
            'line_cost': line_cost,
        })
    return lines, subtotal, total_cost


def _blend_cost(old_cost, stock_qty, new_unit_cost, qty):
    """Weighted average of the stock on hand and an incoming batch.

    Blending against an unrecorded cost (0) would halve the real figure, and stock that
    ran out carries no cost to average, so both cases adopt the new cost outright.
    """
    if stock_qty <= 0 or not old_cost:
        return new_unit_cost
    return (stock_qty * old_cost + qty * new_unit_cost) / (stock_qty + qty)


def create_restock(db, items, discount=0, shipping_cost=0, admin_fee=0):
    """items = [{'product_id': int, 'qty': int, 'unit_price': float}] (shape pre-validated).

    Records the invoice, adds the stock, and rolls each product's cost_price forward as a
    weighted average of what it already held and what this batch cost.

    Returns {'batch_id', 'subtotal', 'total_cost'}.
    """
    for item in items:
        product = db.execute("SELECT id FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        if not product:
            raise NotFoundError('Product {id} not found', id=item['product_id'])

    lines, subtotal, total_cost = allocate_restock_costs(items, discount, shipping_cost, admin_fee)
    if discount > subtotal:
        # Left through, the excess would land as a negative cost on every line.
        raise ServiceError('Discount cannot exceed the invoice subtotal')

    cur = db.execute(
        "INSERT INTO restock_batches (subtotal_cost, discount, shipping_cost, admin_fee, total_cost)"
        " VALUES (?, ?, ?, ?, ?)",
        (subtotal, discount, shipping_cost, admin_fee, total_cost))
    batch_id = cur.lastrowid
    for line in lines:
        product_id = line['product_id']
        # Read the stock and cost fresh per line rather than reusing the validation pass:
        # a batch may list the same product twice, and the second line has to average onto
        # what the first one already produced.
        product = db.execute("SELECT stock_qty, cost_price FROM products WHERE id = ?",
                             (product_id,)).fetchone()
        if not product:
            db.rollback()
            raise NotFoundError('Product {id} not found', id=product_id)
        cost_price = _blend_cost(product['cost_price'], product['stock_qty'],
                                 line['unit_cost'], line['qty'])
        db.execute(
            "UPDATE products SET stock_qty = stock_qty + ?, cost_price = ?,"
            " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (line['qty'], cost_price, product_id))
        db.execute(
            "INSERT INTO restock_items (batch_id, product_id, qty_added, unit_price, unit_cost,"
            " allocated_cost) VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, product_id, line['qty'], line['unit_price'], line['unit_cost'],
             line['line_cost']))
        db.execute("INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (?, ?, ?)",
                   (product_id, line['qty'], f'restock batch #{batch_id}'))
    db.commit()
    return {'batch_id': batch_id, 'subtotal': subtotal, 'total_cost': total_cost}


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

# A sale line only carries a cost if one had been recorded for the product when the order
# was created -- stock that predates its first restock, or a product created without a cost
# price, sells at unit_cost 0. Zero is "unknown", not "free", so every profit figure filters
# on this rather than treating such a line as pure margin. Both the ranking and gross profit
# use it, so the two always describe the same set of sales.
_COSTED_LINE = "oi.unit_cost > 0"


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
    # Gross profit is built from the line columns, not from total_revenue above: COGS only
    # exists per line, and orders.total_amount is a separately stored figure that can
    # disagree with the sum of its lines, which would make the two halves of the
    # subtraction describe different money.
    #
    # Both halves cover only lines whose cost is known (see _COSTED_LINE). Counting an
    # uncosted line's revenue against no cost at all would report it as pure profit and
    # overstate the figure, while the profit ranking beside it leaves the same sale out --
    # two numbers describing different sets of sales. `uncosted_sales` is how many lines
    # were held back, so a caller can say so rather than quietly differing.
    items = db.execute(f"""
        SELECT
            COUNT(DISTINCT oi.product_id) as unique_skus,
            COALESCE(SUM(oi.quantity), 0) as total_items_sold,
            COALESCE(SUM(CASE WHEN {_COSTED_LINE} THEN oi.subtotal END), 0) as line_revenue,
            COALESCE(SUM(CASE WHEN {_COSTED_LINE} THEN oi.quantity * oi.unit_cost END), 0) as cogs,
            COUNT(CASE WHEN NOT ({_COSTED_LINE}) THEN 1 END) as uncosted_sales
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
        'cogs': items['cogs'],
        # What the goods sold in this window actually cost, as opposed to net_profit's
        # cash view of the same period. The two answer different questions and both are
        # reported: a month of heavy restocking shows a thin net profit and a healthy
        # gross one, and neither figure is wrong.
        'gross_profit': items['line_revenue'] - items['cogs'],
        'uncosted_sales': items['uncosted_sales'],
        # Self use sits alongside profit, never inside it: the goods were already
        # paid for as restock spend, so deducting their retail value here would
        # double-count. Keep this expression as-is.
        'net_profit': row['total_revenue'] - restock_cost,
        'start': start,
        'end': end,
    }


# --- Product performance (shared by the sales page and the monthly report) ---

# Sold quantity, revenue and cost per product over a window. Revenue per product has to
# come from order_items.subtotal: orders.total_amount is a separately stored figure
# for the whole order and cannot be attributed to a line. Cost comes from the unit_cost
# snapshotted on each line, so re-running a past window gives the same answer even after
# the product has been restocked at a new price.
_SOLD_PER_PRODUCT = """
    SELECT p.id, p.name, p.sku,
           SUM(oi.quantity) AS total_sold,
           SUM(oi.subtotal) AS total_revenue,
           SUM(oi.quantity * oi.unit_cost) AS total_cost,
           SUM(oi.subtotal) - SUM(oi.quantity * oi.unit_cost) AS total_profit
    FROM order_items oi
    JOIN orders o ON oi.order_id = o.id
    JOIN products p ON oi.product_id = p.id
    WHERE o.status = 'completed'
"""

def _sold_per_product(db, start, end, order_by, limit, costed_only=False):
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    query = _SOLD_PER_PRODUCT + (f" AND {_COSTED_LINE}" if costed_only else '')
    rows = db.execute(
        query + date_filter + f" GROUP BY p.id ORDER BY {order_by} LIMIT ?",
        params + (limit,)).fetchall()
    return [dict(r) for r in rows]


def top_products_by_quantity(db, start, end, limit=3):
    """Best sellers by units moved. Every completed line counts, costed or not."""
    return _sold_per_product(db, start, end, 'total_sold DESC', limit)


def top_products_by_profit(db, start, end, limit=3):
    """Best earners by gross profit, each with its `margin` and `share` of window profit.

    Ranked on money kept rather than money taken: revenue alone promotes whatever is
    cheap and moves in volume, which is not the same thing as what pays for the shop.

    Only lines whose cost is known take part (_COSTED_LINE), so every figure in a row --
    revenue, cost, profit, margin -- describes the same sales. An uncosted line left in
    would report as pure profit and outrank everything real; excluded per line rather than
    per product, a product sold both before and after its first restock still ranks on the
    part that has a cost. Pair with sales_missing_cost() to say what was held back.

    Share is a percentage of the window's profit over those same costed lines, NOT of
    sales_summary's gross_profit, so the shares of a full ranking total 100. A window that
    lost money has no meaningful shares and reports 0.
    """
    rows = _sold_per_product(db, start, end, 'total_profit DESC', limit, costed_only=True)
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    total = db.execute(f"""
        SELECT COALESCE(SUM(oi.subtotal) - SUM(oi.quantity * oi.unit_cost), 0) AS total
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.status = 'completed' AND {_COSTED_LINE}
    """ + date_filter, params).fetchone()['total']
    for row in rows:
        row['margin'] = (row['total_profit'] / row['total_revenue'] * 100) if row['total_revenue'] else 0.0
        row['share'] = (row['total_profit'] / total * 100) if total > 0 else 0.0
    return rows


def sales_missing_cost(db, start, end):
    """How many completed sale lines the profit figures had to leave out for want of a cost.

    Same number for the ranking and for gross profit, since both apply _COSTED_LINE.
    Without it the panel silently omits sales, which reads as a bug rather than as the
    missing restock data it actually is.
    """
    date_filter, params = build_date_filter(start, end, 'o.created_at')
    return db.execute(f"""
        SELECT COUNT(*) AS n
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.status = 'completed' AND NOT ({_COSTED_LINE})
    """ + date_filter, params).fetchone()['n']


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
