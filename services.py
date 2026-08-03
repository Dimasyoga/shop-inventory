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


# --- Stock movement audit trail ---

# Who caused a movement. 'web:<username>' from a route, 'telegram:<chat_id>' from the
# bot, and this for anything with no request behind it -- a migration, a script, a
# test. The default is deliberately honest rather than convenient: a call that did not
# say who it was is not the admin, and recording a guess would make the column worse
# than useless the one time somebody reads it.
ACTOR_SYSTEM = 'system'

STOCK_LOG_INSERT = ("INSERT INTO stock_logs (product_id, change_qty, reason, actor)"
                    " VALUES (?, ?, ?, ?)")


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

# An order holds its stock from the moment it is written until it is completed or
# cancelled, so what a new order can draw on is stock minus what is already spoken
# for. stock_qty stays physical stock throughout -- see the reserved_qty migration.
AVAILABLE = "(stock_qty - reserved_qty)"

# The statuses that hold stock. Anything else has either taken its units out of
# stock_qty (completed) or given them back (cancelled).
OPEN_STATUSES = ('draft', 'confirmed')


def _hold_stock(db, product_id, qty):
    """Claim ``qty`` units for an order. False when they are not available.

    The condition and the claim are one statement on purpose: checking first and
    updating after leaves a window for a second order to pass the same check.
    """
    cur = db.execute(
        f"UPDATE products SET reserved_qty = reserved_qty + ?,"
        f" updated_at = CURRENT_TIMESTAMP WHERE id = ? AND {AVAILABLE} >= ?",
        (qty, product_id, qty))
    return cur.rowcount > 0


def _release_stock(db, product_id, qty):
    """Give back units an order was holding.

    Clamped at zero: a negative reservation would read as extra availability and
    hand out stock that isn't there, which is worse than losing track of a release.
    """
    db.execute(
        "UPDATE products SET reserved_qty = MAX(0, reserved_qty - ?),"
        " updated_at = CURRENT_TIMESTAMP WHERE id = ?", (qty, product_id))


def _release_order_holds(db, order_id):
    """Release everything an open order was holding."""
    for item in db.execute("SELECT product_id, quantity FROM order_items WHERE order_id = ?",
                           (order_id,)).fetchall():
        _release_stock(db, item['product_id'], item['quantity'])


def _unavailable_error(db, product):
    """The refusal for a line that could not be held.

    Plain out-of-stock and stock-that-exists-but-is-spoken-for read identically on
    the products page, and only the second one is something the seller can act on
    (chase the open order holding it), so they get different messages.
    """
    row = db.execute("SELECT stock_qty, reserved_qty FROM products WHERE id = ?",
                     (product['id'],)).fetchone()
    if row['reserved_qty'] > 0:
        return ServiceError('Only {n} of {name} available, {held} held by other orders',
                            n=max(0, row['stock_qty'] - row['reserved_qty']),
                            name=product['name'], held=row['reserved_qty'])
    return ServiceError('Insufficient stock for {name}', name=product['name'])


def _price_and_hold(db, items):
    """Price ``items`` against current products and hold their stock.

    Returns (rows, total) for insertion into order_items. Raises before any commit,
    so the caller's rollback releases whatever was held on the way.
    """
    total = 0
    rows = []
    for item in items:
        product = db.execute("SELECT * FROM products WHERE id = ?",
                             (item['product_id'],)).fetchone()
        if not product:
            raise NotFoundError('Product {id} not found', id=item['product_id'])
        if not _hold_stock(db, product['id'], item['quantity']):
            raise _unavailable_error(db, product)
        subtotal = product['price'] * item['quantity']
        total += subtotal
        # unit_cost is snapshotted alongside unit_price for the same reason: a later
        # restock at a different price must not rewrite the margin of a sale already made.
        rows.append((item['product_id'], item['quantity'], product['price'],
                     product['cost_price'], subtotal))
    return rows, total


def list_orders(db, status=None, search=None, page=0, page_size=10):
    """Orders newest-first with their lines. Returns (orders, has_more).

    Each order is a plain dict carrying an ``items`` list. The lines for the whole
    page come back in one query rather than one per order: the orders page used to
    fan a query out per row, so its cost grew with every order the shop had ever
    taken rather than with what it was showing.

    ``search`` matches the order id or the buyer's name as a substring. The id is
    how the box has always behaved -- the shop looks up an order by the number on
    the note -- and the buyer is what the name was recorded for: "which order was
    Bu Rina's" is the question, and the id is exactly what the asker does not have.
    """
    # has_payment_proof rather than the proof itself: the list renders a paperclip,
    # not the screenshot, and no page should carry megabytes it will not draw. The
    # subquery is a rowid lookup on order_payment_proofs' primary key and runs at
    # most page_size times, so it does not grow with the shop's order history.
    query = ("SELECT o.*, EXISTS(SELECT 1 FROM order_payment_proofs pp"
             " WHERE pp.order_id = o.id) AS has_payment_proof"
             " FROM orders o WHERE 1=1")
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if search:
        # LIKE is case-insensitive for ASCII in SQLite, which is what buyer names
        # here are; neither branch can use an index, but the filter runs over one
        # page's worth of a walk that already stops at page_size.
        query += " AND (id LIKE ? OR buyer_name LIKE ?)"
        params += [f'%{search}%', f'%{search}%']
    # id breaks ties on created_at, which is only second-resolution: two orders taken
    # in the same second have no inherent order, and an unstable one lets a row shift
    # across the page boundary between requests and be shown twice or not at all.
    # id is the rowid here, so the created_at index carries it and still serves this.
    query += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    params += [page_size + 1, page * page_size]
    rows = db.execute(query, params).fetchall()
    orders = [dict(r) for r in rows[:page_size]]
    if orders:
        marks = ', '.join('?' * len(orders))
        lines = db.execute(f"""
            SELECT oi.*, p.name AS product_name, p.sku AS product_sku
            FROM order_items oi JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id IN ({marks}) ORDER BY oi.id
        """, [o['id'] for o in orders]).fetchall()
        by_order = {}
        for line in lines:
            by_order.setdefault(line['order_id'], []).append(dict(line))
        for order in orders:
            order['items'] = by_order.get(order['id'], [])
    return orders, len(rows) > page_size


def get_order(db, order_id):
    """Return (order, items with product names).

    The order carries ``has_payment_proof`` but never the proof's bytes: the detail
    view asks for those separately, through get_payment_proof, and only if it is
    going to show them.
    """
    order = db.execute(
        "SELECT o.*, EXISTS(SELECT 1 FROM order_payment_proofs pp"
        " WHERE pp.order_id = o.id) AS has_payment_proof"
        " FROM orders o WHERE o.id = ?", (order_id,)).fetchone()
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

    Holds the stock as it goes, so an order that is accepted can be filled.
    Returns {'order_id', 'total'}.
    """
    try:
        rows, total = _price_and_hold(db, items)
        cur = db.execute("INSERT INTO orders (status, total_amount) VALUES (?, ?)", ('draft', total))
        order_id = cur.lastrowid
        db.executemany("""
            INSERT INTO order_items (order_id, product_id, quantity, unit_price, unit_cost, subtotal)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [(order_id, pid, qty, price, cost, subtotal) for pid, qty, price, cost, subtotal in rows])
        db.commit()
    except Exception:
        # Lines already held before the failing one would otherwise stay held against
        # an order that was never written.
        db.rollback()
        raise
    return {'order_id': order_id, 'total': total}


def update_order(db, order_id, items):
    """Replace the lines of a draft order. Returns {'order_id', 'total'}.

    Drafts only: confirming an order means the money has been taken, and the lines
    are what the customer paid for. Cancel and re-enter if a confirmed order is wrong.

    Every old line is released and every new one taken afresh, rather than working
    out per-product deltas. Releasing first is what lets an edit that frees units
    spend them again in the same breath -- dropping a line to add another of the last
    item in stock, or just correcting 3 to 2 -- and rollback restores the old holds
    exactly if any new line cannot be met.
    """
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    if order['status'] != 'draft':
        raise ServiceError('Only draft orders can be edited')
    try:
        _release_order_holds(db, order_id)
        # Re-priced from the products as they stand now: nothing has been sold yet,
        # so an edited draft quotes today's price, exactly as a new order would.
        rows, total = _price_and_hold(db, items)
        db.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        db.executemany("""
            INSERT INTO order_items (order_id, product_id, quantity, unit_price, unit_cost, subtotal)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [(order_id, pid, qty, price, cost, subtotal) for pid, qty, price, cost, subtotal in rows])
        db.execute("UPDATE orders SET total_amount = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                   (total, order_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {'order_id': order_id, 'total': total}


def confirm_order(db, order_id):
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    if order['status'] != 'draft':
        raise ServiceError('Only draft orders can be confirmed')
    db.execute("UPDATE orders SET status = 'confirmed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order_id,))
    db.commit()


def complete_order(db, order_id, *, actor=ACTOR_SYSTEM):
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    if order['status'] != 'confirmed':
        raise ServiceError('Only confirmed orders can be completed')
    items = db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    for item in items:
        # The hold becomes a real withdrawal: the units leave stock_qty and stop being
        # reserved in the same statement, so no instant exists where they are counted
        # in neither or in both. Conditional on stock_qty for the one case a hold does
        # not cover -- self use or a stock adjustment physically removed the units
        # since, and no column can conjure them back.
        cur = db.execute(
            "UPDATE products SET stock_qty = stock_qty - ?,"
            " reserved_qty = MAX(0, reserved_qty - ?), updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ? AND stock_qty >= ?",
            (item['quantity'], item['quantity'], item['product_id'], item['quantity']))
        if cur.rowcount == 0:
            db.rollback()
            raise ServiceError('Insufficient stock for product #{id}', id=item['product_id'])
        db.execute(STOCK_LOG_INSERT,
                   (item['product_id'], -item['quantity'], f'sale order #{order_id}', actor))
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
    # Whatever is cancellable is still open, so it is still holding its units; they go
    # back to being available the moment the order stops laying claim to them.
    _release_order_holds(db, order_id)
    db.execute("UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (order_id,))
    db.commit()


# --- Buyer and payment ---
#
# All of this is optional: an order with no buyer and no method is a complete order,
# exactly as it was before the columns existed. What it answers is "who was this
# for, and how did they pay" -- a question the shop was asking of its own memory.

# Stored slugs. The label a slug renders as is a t(...) call site (see
# PAYMENT_METHOD_LABELS); the slug itself is never shown and never translated.
PAYMENT_METHODS = ('cash', 'bank_transfer')

PAYMENT_METHOD_LABELS = {
    'cash': 'Cash',
    'bank_transfer': 'Bank Transfer',
}

# Long enough for a name plus the note a shop actually writes ("Bu Rina - kantor"),
# short enough that the column cannot be used as free storage.
MAX_BUYER_NAME = 120

# A phone screenshot is comfortably under this; a photo from a modern camera is not,
# and neither is a video someone picked by mistake. app.py enforces the same ceiling
# at the request level (MAX_CONTENT_LENGTH) so an oversized body is refused before it
# is read into memory -- this is the check for callers that are not a web request.
MAX_PROOF_BYTES = 5 * 1024 * 1024

# Accepted proof types, keyed by what the bytes actually start with. The browser's
# declared Content-Type is not consulted: it is chosen by the client, and the value
# that matters is the one we later serve the file back as.
#
# Deliberately no SVG. It is an image to a seller and a scriptable document to a
# browser, and this app serves proofs from its own origin.
_PROOF_MAGIC = (
    (b'\xff\xd8\xff', 'image/jpeg', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png', 'png'),
    (b'%PDF-', 'application/pdf', 'pdf'),
)

# Extension per accepted type, for the derived download name.
PROOF_EXTENSIONS = {mime: ext for _, mime, ext in _PROOF_MAGIC} | {'image/webp': 'webp'}


def sniff_proof_type(data):
    """MIME type of ``data`` if it is an accepted proof, else None."""
    for magic, mime, _ in _PROOF_MAGIC:
        if data.startswith(magic):
            return mime
    # WebP is a RIFF container: the tag sits after a 4-byte length, so it cannot be
    # matched by a prefix like the others. Phone screenshots arrive as WebP often
    # enough that leaving it out would look like a broken upload.
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def payment_proof_filename(order_id, mime_type):
    """Download name for a proof. Derived, never the uploader's own filename."""
    return f'order-{order_id}-payment-proof.{PROOF_EXTENSIONS.get(mime_type, "bin")}'


def set_order_payment(db, order_id, *, buyer_name=None, payment_method=None,
                      proof=None, remove_proof=False):
    """Record who an order was for and how it was paid.

    ``buyer_name`` and ``payment_method`` are written as given -- passing None for
    either clears it, because the editor submits every field it shows and a blanked
    box has to mean blank. ``proof`` is the raw bytes of a receipt and is the one
    exception: None leaves whatever is stored alone, since the editor cannot round-trip
    a file back into its own input, and ``remove_proof`` is how it is actually removed.

    Allowed in every status but cancelled. Not draft-only, unlike editing an order's
    lines: a transfer receipt usually arrives after the sale is entered, sometimes
    after it is completed, and a record that could not accept it then would be a
    record of nothing. It also touches no money and no stock -- the lines stay
    exactly as they were paid for.
    """
    order = db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise NotFoundError('Order not found')
    if order['status'] == 'cancelled':
        raise ServiceError('Cannot record payment details on a cancelled order')

    if isinstance(buyer_name, str):
        buyer_name = buyer_name.strip()[:MAX_BUYER_NAME] or None
    elif buyer_name is not None:
        raise ServiceError('Buyer name must be text')
    if payment_method is not None and payment_method not in PAYMENT_METHODS:
        raise ServiceError('Unknown payment method')

    mime_type = None
    if proof is not None:
        if not proof:
            raise ServiceError('The payment proof file is empty')
        if len(proof) > MAX_PROOF_BYTES:
            raise ServiceError('Payment proof must be {n} MB or smaller',
                               n=MAX_PROOF_BYTES // (1024 * 1024))
        mime_type = sniff_proof_type(proof)
        if not mime_type:
            raise ServiceError('Payment proof must be a JPEG, PNG, WebP or PDF file')

    try:
        db.execute("UPDATE orders SET buyer_name = ?, payment_method = ?,"
                   " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                   (buyer_name, payment_method, order_id))
        if proof is not None:
            # Replaces any previous proof for this order: order_id is the primary key,
            # so a corrected receipt overwrites the wrong one instead of leaving both
            # with no way to say which is current.
            db.execute("INSERT OR REPLACE INTO order_payment_proofs"
                       " (order_id, mime_type, byte_size, data, uploaded_at)"
                       " VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                       (order_id, mime_type, len(proof), proof))
        elif remove_proof:
            db.execute("DELETE FROM order_payment_proofs WHERE order_id = ?", (order_id,))
        db.commit()
    except Exception:
        db.rollback()
        raise


def get_payment_proof(db, order_id):
    """The stored proof for an order, or None. Raises if the order does not exist.

    Separate from get_order so that reading an order never carries the bytes: this
    is the only query in the app that does, and only the download route runs it.
    """
    if not db.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone():
        raise NotFoundError('Order not found')
    return db.execute(
        "SELECT mime_type, byte_size, data, uploaded_at FROM order_payment_proofs"
        " WHERE order_id = ?", (order_id,)).fetchone()


# reserved_qty is a running total maintained by _hold_stock and _release_stock, but the
# open order lines are what actually justify it, and the two can only be checked against
# each other by recomputing. Nothing here is expected to find anything: every path that
# moves the column does so in one statement inside a transaction that rolls back whole.
# It exists because the failure is silent and self-concealing when it does happen --
# _release_stock clamps at zero, so an over-release is absorbed rather than surfaced, and
# a lost release leaves units held by an order that no longer exists. Either way the
# symptom is a shop owner looking at stock on the shelf that the app refuses to sell,
# with nothing in the UI to explain it and no way back short of editing the database.
#
# The held total is aggregated once over the open orders and then joined onto products,
# rather than recomputed per product by a correlated subquery. The two forms return the
# same rows, but the correlated one walks every line a product has ever sold to find the
# few that are still open, so its cost grew with the shop's whole order history: at two
# years of a thousand orders a month it took 194 ms, the slowest endpoint in the app and
# the same query the startup drift check runs. What this actually depends on is the
# number of *open* orders, which does not accumulate -- they get completed or cancelled.
_DRIFT_SQL = f"""
    SELECT p.id AS id, p.name AS name, p.reserved_qty AS reserved_qty,
           COALESCE(h.held, 0) AS expected
    FROM products p
    LEFT JOIN (
        SELECT oi.product_id AS product_id, SUM(oi.quantity) AS held
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE o.status IN ({','.join('?' * len(OPEN_STATUSES))})
        GROUP BY oi.product_id
    ) h ON h.product_id = p.id
    WHERE p.reserved_qty != COALESCE(h.held, 0)
    ORDER BY p.name
"""


def reservation_drift(db):
    """Products whose reserved_qty disagrees with the open orders holding their stock.

    Read-only. Each row carries the held figure and the ``expected`` one recomputed
    from draft and confirmed order lines; ``expected`` is the truth, since the lines
    are what a customer was actually promised. Archived products are included --
    stock held against one is exactly the kind of thing that goes unnoticed.
    """
    return db.execute(_DRIFT_SQL, OPEN_STATUSES).fetchall()


def repair_reservations(db):
    """Reset drifted reserved_qty figures to what the open order lines justify.

    Returns the rows as they were *before* the fix, so the caller can report what it
    changed. Safe to run with the shop live: every row is rewritten to a figure derived
    from the same transaction that reads it, and a concurrent order that holds stock in
    between simply shows up as drift on the next run rather than being clobbered here.

    Deliberately not automatic. Repair is the right call almost every time, but it
    rewrites a number that says what customers are owed, and a shop owner is entitled
    to see the discrepancy before it disappears.
    """
    drifted = reservation_drift(db)
    if not drifted:
        return []
    db.executemany("UPDATE products SET reserved_qty = ?, updated_at = CURRENT_TIMESTAMP"
                   " WHERE id = ?",
                   [(row['expected'], row['id']) for row in drifted])
    db.commit()
    return drifted


# --- Stock movement history ---

def list_stock_movements(db, product_id=None, page=0, page_size=25):
    """Recorded stock movements, newest first. Returns (rows, has_more).

    Ordered by id alone rather than the ``created_at DESC, id DESC`` the orders list
    uses. stock_logs is append-only and id is the rowid, so id order *is* chronological
    here and is a total order by construction -- there is no tie for created_at's
    second resolution to leave unbroken. It is also what makes this cheap: descending
    rowid is a reverse walk of the table itself, and the product filter rides
    idx_stock_logs_product, whose entries carry the rowid. Neither plan sorts.

    ``reason`` is stored English written at the time of the movement (``sale order #12``,
    or whatever a human typed into a stock adjustment) and is shown as recorded, not
    translated: it is a record of what happened, and rewriting records to suit the
    reader's language is not what an audit trail is for.
    """
    query = ("SELECT s.id, s.product_id, s.change_qty, s.reason, s.actor, s.created_at,"
             " p.name AS product_name, p.sku AS product_sku"
             " FROM stock_logs s JOIN products p ON p.id = s.product_id")
    params = []
    if product_id:
        query += " WHERE s.product_id = ?"
        params.append(product_id)
    query += " ORDER BY s.id DESC LIMIT ? OFFSET ?"
    params += [page_size + 1, page * page_size]
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows[:page_size]], len(rows) > page_size


# --- Batch history (restock and self use) ---

# The two batch kinds differ only in their columns, so the paging is written once and
# the table names are interpolated -- as in _voidable_batch, and for the same reason:
# they are module constants below, never anything a caller supplies.
_RESTOCK_HISTORY = {
    'table': 'restock_batches',
    'item_table': 'restock_items',
    'columns': ('id', 'subtotal_cost', 'discount', 'shipping_cost', 'admin_fee',
                'total_cost', 'created_at', 'voids_batch_id'),
}
_SELF_USE_HISTORY = {
    'table': 'self_use_batches',
    'item_table': 'self_use_items',
    'columns': ('id', 'total_value', 'created_at', 'voids_batch_id'),
}


def _list_batches(db, spec, start=None, end=None, page=0, page_size=10):
    """One page of batches newest-first, each carrying its ``items``.

    Returns (batches, has_more), the same contract as list_orders, and costs the same
    fixed two queries: the page, then every line on it in one IN query. Both history
    tables used to be read whole -- no LIMIT at all -- and then fan a query out per
    batch for its lines, so opening the restock page cost one query per batch the shop
    had ever recorded and shipped the lot to the browser in a single JSON array.

    ``voided_by`` is the other half of voids_batch_id: a batch knows what it reverses,
    and this correlated subquery says what reversed it, so one row carries both states.
    It rides idx_%_batches_voids and now runs for a page rather than for every batch.

    Ordered by ``created_at DESC, id DESC`` rather than created_at alone. The column is
    second-resolution and a restock invoice is entered in one sitting, so several
    batches sharing a timestamp is ordinary here -- an unstable tiebreak would let one
    cross the page boundary between requests and show up twice or not at all. id is the
    rowid, so the created_at index carries it and serves the sort without a pass over
    the table.
    """
    cols = ', '.join(f'b.{c}' for c in spec['columns'])
    query = (f"SELECT {cols},"
             f" (SELECT v.id FROM {spec['table']} v WHERE v.voids_batch_id = b.id) AS voided_by"
             f" FROM {spec['table']} b WHERE 1=1")
    params = []
    if start and end:
        clause, date_params = build_date_filter(start, end, 'b.created_at')
        query += clause
        params += list(date_params)
    query += " ORDER BY b.created_at DESC, b.id DESC LIMIT ? OFFSET ?"
    params += [page_size + 1, page * page_size]
    rows = db.execute(query, params).fetchall()
    batches = [dict(r) for r in rows[:page_size]]
    if batches:
        marks = ', '.join('?' * len(batches))
        lines = db.execute(f"""
            SELECT i.*, p.name AS product_name, p.sku AS product_sku
            FROM {spec['item_table']} i JOIN products p ON i.product_id = p.id
            WHERE i.batch_id IN ({marks}) ORDER BY i.id
        """, [b['id'] for b in batches]).fetchall()
        by_batch = {}
        for line in lines:
            by_batch.setdefault(line['batch_id'], []).append(dict(line))
        for batch in batches:
            batch['items'] = by_batch.get(batch['id'], [])
    return batches, len(rows) > page_size


def list_restock_batches(db, start=None, end=None, page=0, page_size=10):
    """One page of restock batches with their lines. Returns (batches, has_more)."""
    return _list_batches(db, _RESTOCK_HISTORY, start, end, page, page_size)


def list_self_use_batches(db, start=None, end=None, page=0, page_size=10):
    """One page of self-use batches with their lines. Returns (batches, has_more)."""
    return _list_batches(db, _SELF_USE_HISTORY, start, end, page, page_size)


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


def create_restock(db, items, discount=0, shipping_cost=0, admin_fee=0, *, actor=ACTOR_SYSTEM):
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
        # cost_before is the pre-blend figure this line overwrote: the only exact way
        # back if the batch is later voided (see void_restock).
        db.execute(
            "INSERT INTO restock_items (batch_id, product_id, qty_added, unit_price, unit_cost,"
            " allocated_cost, cost_before) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch_id, product_id, line['qty'], line['unit_price'], line['unit_cost'],
             line['line_cost'], product['cost_price']))
        db.execute(STOCK_LOG_INSERT,
                   (product_id, line['qty'], f'restock batch #{batch_id}', actor))
    db.commit()
    return {'batch_id': batch_id, 'subtotal': subtotal, 'total_cost': total_cost}


def _voidable_batch(db, table, batch_id):
    """The batch, if it can be voided at all. `table` is a literal from this module.

    Three refusals, all of them about keeping the reversal one-to-one: a batch that does
    not exist, one that is itself a void (reversing a reversal is just the original
    again, entered by hand), and one already voided (which would take the stock out
    twice).
    """
    batch = db.execute(f"SELECT * FROM {table} WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        raise NotFoundError('Batch #{id} not found', id=batch_id)
    if batch['voids_batch_id']:
        raise ServiceError('Batch #{id} is itself a void and cannot be voided', id=batch_id)
    existing = db.execute(
        f"SELECT id FROM {table} WHERE voids_batch_id = ?", (batch_id,)).fetchone()
    if existing:
        raise ServiceError('Batch #{id} was already voided by batch #{void_id}',
                           id=batch_id, void_id=existing['id'])
    return batch


def void_restock(db, batch_id, *, actor=ACTOR_SYSTEM):
    """Reverse a restock batch that should not have been entered, and repair the cost.

    A void is a batch of its own carrying the negated figures, not an edit of the
    original: restock spend stays append-only, so a month already reported keeps the
    total it printed and the credit falls in the month the correction was made.

    The stock has to still be there. Ten restocked and eight already sold leaves nothing
    to reverse, so the whole void is refused rather than driving stock negative -- the
    same all-or-nothing rule create_self_use applies.

    cost_price is restored from the cost_before snapshot when that is exact, which means
    when no later surviving batch has already blended onto it. When one has, the honest
    answer is that the original figure is unrecoverable: the average would have to be
    rebuilt from stock levels that sales have since moved. Those products are flagged
    cost_review_needed instead, and the products page asks for a human to look.

    Sale lines keep the unit_cost they snapshotted, per the rule that a snapshot is never
    rewritten -- `affected_sales` reports how many carry the voided cost.

    Returns {'void_batch_id', 'total_cost', 'restored', 'flagged', 'affected_sales'},
    where restored/flagged are product names.
    """
    batch = _voidable_batch(db, 'restock_batches', batch_id)
    lines = db.execute("""
        SELECT ri.*, p.name AS product_name
        FROM restock_items ri JOIN products p ON ri.product_id = p.id
        WHERE ri.batch_id = ? ORDER BY ri.id
    """, (batch_id,)).fetchall()

    cur = db.execute(
        "INSERT INTO restock_batches (subtotal_cost, discount, shipping_cost, admin_fee,"
        " total_cost, voids_batch_id) VALUES (?, ?, ?, ?, ?, ?)",
        (-batch['subtotal_cost'], -batch['discount'], -batch['shipping_cost'],
         -batch['admin_fee'], -batch['total_cost'], batch_id))
    void_id = cur.lastrowid

    for line in lines:
        # Conditional decrement, as in complete_order: atomic, and its failure is the
        # check that the goods have not already left the shop.
        cur = db.execute(
            "UPDATE products SET stock_qty = stock_qty - ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ? AND stock_qty >= ?",
            (line['qty_added'], line['product_id'], line['qty_added']))
        if cur.rowcount == 0:
            db.rollback()
            raise ServiceError(
                'Cannot void: {name} no longer has the {qty} restocked by this batch in stock',
                name=line['product_name'], qty=line['qty_added'])
        # The mirrored line carries no cost_before of its own: a void is never voided,
        # so nothing will ever read it.
        db.execute(
            "INSERT INTO restock_items (batch_id, product_id, qty_added, unit_price, unit_cost,"
            " allocated_cost, cost_before) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (void_id, line['product_id'], -line['qty_added'], line['unit_price'],
             line['unit_cost'], -line['allocated_cost']))
        db.execute(STOCK_LOG_INSERT,
                   (line['product_id'], -line['qty_added'], f'void of restock batch #{batch_id}', actor))

    restored, flagged, flagged_ids = [], [], []
    # dict.fromkeys keeps first-seen order: a batch may list a product twice, and only
    # the first line's snapshot describes the state before the batch as a whole.
    for product_id in dict.fromkeys(line['product_id'] for line in lines):
        first = next(row for row in lines if row['product_id'] == product_id)
        if _blended_since(db, product_id, batch_id):
            flagged.append(first['product_name'])
            flagged_ids.append(product_id)
        elif first['cost_before'] > 0 or not _restocked_before(db, product_id, batch_id):
            # Either a real snapshot, or this was the product's first restock and the
            # cost genuinely goes back to unknown. A 0 with earlier batches behind it is
            # a row from before cost_before existed, which is not a snapshot at all.
            db.execute("UPDATE products SET cost_price = ?, updated_at = CURRENT_TIMESTAMP"
                       " WHERE id = ?", (first['cost_before'], product_id))
            restored.append(first['product_name'])
        else:
            flagged.append(first['product_name'])
            flagged_ids.append(product_id)

    product_ids = [line['product_id'] for line in lines]
    if flagged_ids:
        marks = ', '.join('?' * len(flagged_ids))
        db.execute(f"UPDATE products SET cost_review_needed = 1,"
                   f" updated_at = CURRENT_TIMESTAMP WHERE id IN ({marks})", tuple(flagged_ids))
    affected_sales = _sales_since(db, product_ids, batch['created_at'])
    db.commit()
    return {'void_batch_id': void_id, 'total_cost': -batch['total_cost'],
            'restored': restored, 'flagged': flagged, 'affected_sales': affected_sales}


def _surviving_restock_lines(compare):
    """Restock lines for one product, on batches that still stand, `compare` this one.

    A void batch never counts, and neither does a batch that has since been voided --
    both describe stock movements that no longer apply.
    """
    return f"""
        SELECT 1 FROM restock_items ri
        JOIN restock_batches rb ON ri.batch_id = rb.id
        WHERE ri.product_id = ? AND ri.batch_id {compare} ?
          AND rb.voids_batch_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM restock_batches v WHERE v.voids_batch_id = rb.id)
        LIMIT 1
    """


def _blended_since(db, product_id, batch_id):
    """Has a later surviving batch already averaged onto this product's cost?"""
    return db.execute(_surviving_restock_lines('>'), (product_id, batch_id)).fetchone() is not None


def _restocked_before(db, product_id, batch_id):
    return db.execute(_surviving_restock_lines('<'), (product_id, batch_id)).fetchone() is not None


def _sales_since(db, product_ids, since):
    """Completed sale lines for these products dated on or after `since`.

    They snapshotted whatever cost was live at the time, and a void does not rewrite
    them -- this is how many the shop owner should know are affected.
    """
    if not product_ids:
        return 0
    marks = ', '.join('?' * len(product_ids))
    return db.execute(f"""
        SELECT COUNT(*) AS n FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.status = 'completed' AND oi.product_id IN ({marks}) AND o.created_at >= ?
    """, tuple(product_ids) + (since,)).fetchone()['n']


# --- Self use ---

def create_self_use(db, items, *, actor=ACTOR_SYSTEM):
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
        db.execute(STOCK_LOG_INSERT,
                   (product_id, -qty, f'self use batch #{batch_id}', actor))
    db.commit()
    return {'batch_id': batch_id, 'total_value': total_value}


def void_self_use(db, batch_id, *, actor=ACTOR_SYSTEM):
    """Reverse a self-use batch, putting the stock back.

    The same reversing-batch shape as void_restock and much less to do: self use touches
    no cost, so there is nothing to restore and nothing to flag. Stock going back in
    needs no guard either -- the only way to fail is the batch itself being unvoidable.

    Returns {'void_batch_id', 'total_value'}.
    """
    batch = _voidable_batch(db, 'self_use_batches', batch_id)
    lines = db.execute("SELECT * FROM self_use_items WHERE batch_id = ? ORDER BY id",
                       (batch_id,)).fetchall()

    cur = db.execute("INSERT INTO self_use_batches (total_value, voids_batch_id) VALUES (?, ?)",
                     (-batch['total_value'], batch_id))
    void_id = cur.lastrowid
    for line in lines:
        db.execute("UPDATE products SET stock_qty = stock_qty + ?,"
                   " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                   (line['quantity'], line['product_id']))
        db.execute("INSERT INTO self_use_items (batch_id, product_id, quantity, unit_price,"
                   " subtotal) VALUES (?, ?, ?, ?, ?)",
                   (void_id, line['product_id'], -line['quantity'], line['unit_price'],
                    -line['subtotal']))
        db.execute(STOCK_LOG_INSERT,
                   (line['product_id'], line['quantity'], f'void of self use batch #{batch_id}', actor))
    db.commit()
    return {'void_batch_id': void_id, 'total_value': -batch['total_value']}


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
