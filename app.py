from flask import (Flask, request, jsonify, render_template, redirect, url_for,
                   session, g, Response)
from database import get_db, init_db
from functools import wraps
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from werkzeug.security import check_password_hash
import logging
import math
import sqlite3
import os
import secrets
import threading
import time

import i18n
import reports
import services
import telegram_bot
from services import (ServiceError, format_rupiah, get_date_range,
                      build_date_filter)
from telegram_bot import TelegramAPI, TelegramError

log = logging.getLogger('app')

# Defaults keep local dev writing beside the source; a deployment points both
# SHOP_DB_PATH and this at a mounted volume so the source tree stays read-only.
_SECRET_PATH = (os.environ.get('SHOP_SECRET_KEY_PATH')
                or os.path.join(os.path.dirname(__file__), '.secret_key'))

def _load_secret_key():
    """Persisted random secret so sessions survive restarts without a key in the repo."""
    env_key = os.environ.get('SHOP_SECRET_KEY')
    if env_key:
        return env_key.encode()
    try:
        with open(_SECRET_PATH, 'rb') as f:
            key = f.read()
        if key:
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(32)
    os.makedirs(os.path.dirname(_SECRET_PATH) or '.', exist_ok=True)
    fd = os.open(_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key)
    return key

app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Ceiling on any request body, which until payment proofs existed nothing sent enough
# of to need one. Without it Werkzeug reads whatever arrives, and one phone video
# uploaded into the proof field would be held in the memory of a server pinned to a
# single worker. The slack over services.MAX_PROOF_BYTES is for the multipart envelope,
# so that a file right at the documented limit is judged by the service -- which can
# say what the limit is -- rather than cut off at the socket.
app.config['MAX_CONTENT_LENGTH'] = services.MAX_PROOF_BYTES + 64 * 1024

@app.errorhandler(413)
def _too_large(e):
    """A body over MAX_CONTENT_LENGTH, in the shape the caller can use.

    Flask's own 413 is an HTML page. `api()` in app.js parses every response as JSON
    and would report a syntax error to a seller whose actual problem is that their
    photo is too big -- the same trap that made /api/ answer 401 rather than redirect.
    """
    if request.path.startswith('/api/'):
        return _err('Payment proof must be {n} MB or smaller', 413,
                    n=services.MAX_PROOF_BYTES // (1024 * 1024))
    return e

# How long a session survives without activity. Flask checks this against the
# signature's own timestamp on every request, so it holds server-side even if the
# browser keeps sending an old cookie -- and the default, had we left it, is 31 days.
#
# It is an *idle* timeout, not an absolute one: sessions are marked permanent at sign-in
# and Flask's SESSION_REFRESH_EACH_REQUEST (on by default) re-issues the cookie on every
# response, so the clock restarts while the shop is being used. An absolute limit would
# sign the seller out mid-order for no reason anyone at the counter could understand.
#
# Twelve hours covers a trading day and expires overnight. Deployment-level rather than
# a settings row because Flask reads it off app.config, which a running process does not
# revisit -- and unlike the bot's settings, nobody needs to change this while serving.
SESSION_HOURS = float(os.environ.get('SHOP_SESSION_HOURS') or 12)
app.permanent_session_lifetime = timedelta(hours=SESSION_HOURS)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return _unauthenticated()
        return f(*args, **kwargs)
    return decorated

def _unauthenticated():
    """Turn away a request with no session, in the shape its caller can use.

    A page navigation gets the login screen. A fetch() gets 401 JSON, because the
    redirect it would otherwise follow returns 200 and an HTML body -- `api()` then
    fails parsing it and reports a JSON syntax error to a seller whose actual problem
    is that they need to sign in again. Rare enough to ignore while sessions lasted a
    month; the whole point of this change is to make it routine.
    """
    if request.path.startswith('/api/'):
        return jsonify({'error': i18n.make_t(get_lang())(
            'Your session has expired. Please sign in again.')}), 401
    return redirect(url_for('login'))

# --- Login throttle ---
# The shop runs on a LAN behind a single admin account, so unlimited password
# guesses are the weakest thing in the app. Failures are bucketed by client
# address, not by username: a per-username counter would let anyone lock the
# owner out of their own shop just by guessing at the name, while an address
# bucket only ever penalises the machine doing the guessing.
#
# State is in process memory rather than a settings row on purpose. The
# deployment pins one worker (see AGENTS.md), so there is nothing to share it
# with, and a lockout that survived a restart would need an operator with host
# access to clear -- restarting is deliberately the way out when the shop owner
# locks themself out.
LOGIN_MAX_ATTEMPTS = 5
# Doubles as the window a failure keeps counting for and the wait once the limit
# is reached: five failures inside 15 minutes lock the bucket, and the lock lifts
# 15 minutes after the last attempt. One horizon, so there is no gap where a
# failure still counts but no longer holds the door shut.
LOGIN_LOCKOUT = 15 * 60

_login_failures = {}  # client key -> monotonic timestamps of failures still counting
_login_failures_lock = threading.Lock()

def _login_now():
    """Clock for the throttle. Monotonic, so moving the system clock cannot cut a
    lockout short, and a seam the tests replace."""
    return time.monotonic()

def _login_key():
    """Bucket an attempt belongs to. Note this is the peer address: behind a
    reverse proxy every request carries the proxy's, putting the whole LAN in one
    bucket -- the deployment publishes the port directly for that reason."""
    return request.remote_addr or 'unknown'

def _recent_failures(key, now):
    """Failures still counting against ``key``, pruned in place.

    Caller holds ``_login_failures_lock``.
    """
    recent = [t for t in _login_failures.get(key, []) if now - t < LOGIN_LOCKOUT]
    if recent:
        _login_failures[key] = recent
    else:
        _login_failures.pop(key, None)
    return recent

def _login_lockout_remaining(key, now=None):
    """Seconds before ``key`` may try again; 0 when it may try now."""
    now = _login_now() if now is None else now
    with _login_failures_lock:
        recent = _recent_failures(key, now)
        if len(recent) < LOGIN_MAX_ATTEMPTS:
            return 0
        return max(0.0, LOGIN_LOCKOUT - (now - recent[-1]))

def _record_login_failure(key, now=None):
    """Count a failed attempt against ``key`` and return its running total."""
    now = _login_now() if now is None else now
    with _login_failures_lock:
        recent = _recent_failures(key, now)
        recent.append(now)
        _login_failures[key] = recent
        # Buckets go stale as clients come and go, and nothing else ever revisits
        # them; drop them here so the map cannot grow without bound.
        for stale in [k for k, times in _login_failures.items()
                      if k != key and now - times[-1] >= LOGIN_LOCKOUT]:
            del _login_failures[stale]
        return len(recent)

def _clear_login_failures(key):
    """Forget a bucket's history, on a sign-in that succeeded."""
    with _login_failures_lock:
        _login_failures.pop(key, None)

def _actor():
    """Who the current request is, for the stock movement audit trail.

    The '?' is for a session that somehow has no username -- login_required means it
    should not happen, and an unattributed movement is still better than none.
    """
    return f"web:{session.get('username', '?')}"

def _json_body():
    """Parsed JSON object from the request, or None (caller returns 400)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None

def _products_json(rows):
    """Plain dicts for embedding in templates via |tojson (sqlite3.Row isn't serializable)."""
    # 'stock' is physical stock; 'available' is what an order can still draw on once
    # open orders have taken their hold. Restock and self use work on the first, the
    # order form on the second.
    return [{'id': p['id'], 'name': p['name'], 'sku': p['sku'],
             'price': p['price'], 'cost': p['cost_price'], 'stock': p['stock_qty'],
             'reserved': p['reserved_qty'],
             'available': p['stock_qty'] - p['reserved_qty']}
            for p in rows]

def _validate_product(data):
    """Return (fields, error). fields excludes stock_qty; callers add it where allowed."""
    name = data.get('name')
    name = name.strip() if isinstance(name, str) else ''
    if not name:
        return None, 'Name required'
    try:
        price = float(data.get('price', 0))
    except (TypeError, ValueError):
        return None, 'Price must be a number'
    if not (price >= 0):  # also rejects NaN
        return None, 'Price must be 0 or more'
    # Cost price is normally maintained by restocking. It is editable so that stock
    # already on hand at install time can be costed, and so a mistyped invoice can be
    # corrected -- the next restock averages onto whatever is set here.
    try:
        cost_price = float(data.get('cost_price', 0))
    except (TypeError, ValueError):
        return None, 'Cost price must be a number'
    if not (cost_price >= 0):
        return None, 'Cost price must be 0 or more'
    try:
        threshold = int(data.get('reorder_threshold', 0))
    except (TypeError, ValueError):
        return None, 'Reorder threshold must be a whole number'
    if threshold < 0:
        return None, 'Reorder threshold must be 0 or more'
    sku = data.get('sku')
    sku = (sku.strip() if isinstance(sku, str) else '') or None
    return {
        'name': name,
        'sku': sku,
        'price': price,
        'cost_price': cost_price,
        'reorder_threshold': threshold,
    }, None

@app.template_filter('format_datetime')
def format_datetime(utc_str):
    if not utc_str:
        return ''
    try:
        dt = datetime.fromisoformat(str(utc_str)).replace(tzinfo=timezone.utc)
    except ValueError:
        return str(utc_str)
    return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')

@app.before_request
def before_request():
    g.db = get_db()

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()

@app.route('/healthz')
def healthz():
    """Liveness probe for the container healthcheck. Deliberately unauthenticated,
    and returns raw JSON rather than translated text so it stays machine-readable."""
    try:
        g.db.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
    except sqlite3.Error as e:
        log.warning('health check failed: %s', e)
        return jsonify({'status': 'error'}), 503
    return jsonify({'status': 'ok'})

def get_lang():
    """Active shop-wide UI language, resolved to a supported code."""
    from database import get_setting
    return i18n.normalize_lang(get_setting(g.db, 'language', i18n.DEFAULT_LANG))

# Text scale, as a percentage of the browser's own default font size. Rendered onto
# <html> so every rem in style.css follows it. A percentage rather than a pixel size
# because it *multiplies* whatever the phone is already set to: a reader who has
# turned their system text up keeps that, and picking "Large" here enlarges from
# there instead of overriding it back down.
FONT_SCALES = (100, 115, 130, 150)
DEFAULT_FONT_SCALE = 100

def get_font_scale():
    """Active text scale as an int percentage, always one of FONT_SCALES.

    A stored value that is missing, unparseable or no longer offered falls back to
    the default rather than raising: an unreadable page is the one failure mode
    that would stop the shop owner from getting back to Settings to fix it."""
    from database import get_setting
    raw = get_setting(g.db, 'ui_font_scale', str(DEFAULT_FONT_SCALE))
    try:
        scale = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FONT_SCALE
    return scale if scale in FONT_SCALES else DEFAULT_FONT_SCALE

def _service_error(e):
    """JSON response for a ServiceError, translated to the active language."""
    return jsonify({'error': i18n.translate_error(e, i18n.make_t(get_lang()))}), e.status

def _err(msg, status=400, **params):
    """JSON error response. ``msg`` is the English source string; it is translated
    to the active language the same way template/bot strings are, since the
    browser shows these verbatim in a toast."""
    return jsonify({'error': i18n.make_t(get_lang())(msg, **params)}), status

@app.context_processor
def inject_i18n():
    """Make the translator and language metadata available to every template
    (including login, which renders before a session exists)."""
    lang = get_lang()
    return {
        't': i18n.make_t(lang),
        'lang': lang,
        'languages': i18n.LANGUAGES,
        'i18n_js': i18n.js_table(lang),
        # Every page, not just the ones that ask: the scale is written into the
        # <html> tag so the first paint is already the right size. Applying it from
        # JS after load would show a flash of small text on every navigation.
        'font_scale': get_font_scale(),
    }

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        t = i18n.make_t(get_lang())
        key = _login_key()
        remaining = _login_lockout_remaining(key)
        if remaining:
            # Refused before the password is looked at, so a locked-out guesser
            # cannot use the throttle itself to tell a right password from a wrong
            # one. Round up, so the message never invites a retry that is refused.
            return render_template('login.html', error=t(
                'Too many failed sign-in attempts. Try again in {n} minute(s).',
                n=max(1, math.ceil(remaining / 60)))), 429
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        db = g.db
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user['password'], password):
            _clear_login_failures(key)
            # permanent is what gives the cookie an expiry the browser also honours,
            # and what makes Flask re-issue it on each response so the idle clock
            # restarts while the seller is working. Without it the cookie lives until
            # the browser closes -- which on a counter PC is never.
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        failures = _record_login_failure(key)
        if failures >= LOGIN_MAX_ATTEMPTS:
            log.warning('sign-in locked out for %s after %d failed attempts', key, failures)
        return render_template('login.html', error=t('Invalid credentials'))
    return render_template('login.html', error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    db = g.db
    total_products = db.execute("SELECT COUNT(*) as cnt FROM products WHERE is_archived = 0").fetchone()['cnt']
    total_orders = db.execute("SELECT COUNT(*) as cnt FROM orders WHERE status != 'cancelled'").fetchone()['cnt']
    low_stock = db.execute("""
        SELECT COUNT(*) as cnt FROM products
        WHERE is_archived = 0 AND stock_qty <= reorder_threshold
    """).fetchone()['cnt']
    # Same source as the sales page and the bot, so the three never disagree: the
    # current calendar month in the shop's timezone.
    summary = services.sales_summary(db, 'month', 0, _shop_tz())
    month_label = i18n.month_label(summary['start'], get_lang())
    total_product_value = db.execute("""
        SELECT COALESCE(SUM(price * stock_qty), 0) as total FROM products
        WHERE is_archived = 0
    """).fetchone()['total']
    recent_orders = db.execute("""
        SELECT * FROM orders
        ORDER BY created_at DESC
        LIMIT 5
    """).fetchall()
    low_stock_products = db.execute("""
        SELECT * FROM products
        WHERE is_archived = 0 AND stock_qty <= reorder_threshold
        ORDER BY stock_qty ASC
        LIMIT 10
    """).fetchall()
    return render_template('dashboard.html',
        total_products=total_products,
        total_orders=total_orders,
        low_stock_count=low_stock,
        month_label=month_label,
        month_revenue=format_rupiah(summary['total_revenue']),
        net_profit=format_rupiah(summary['net_profit']),
        gross_profit=format_rupiah(summary['gross_profit']),
        uncosted_sales=summary['uncosted_sales'],
        self_use_value=format_rupiah(summary['self_use_value']),
        total_product_value=format_rupiah(total_product_value),
        total_restock_cost_raw=summary['restock_cost'],
        recent_orders=recent_orders,
        low_stock_products=low_stock_products,
        format_rupiah=format_rupiah
    )

# --- Products ---
@app.route('/products')
@login_required
def products_page():
    # No product rows here on purpose: the table body is filled by loadProducts() from
    # /api/products, which is also what the search and the filter chips re-fetch. This
    # route used to select the whole active catalogue into a template variable that
    # products.html never rendered -- the same dead query the orders page was carrying.
    archived_count = g.db.execute(
        "SELECT COUNT(*) AS n FROM products WHERE is_archived = 1").fetchone()['n']
    return render_template('products.html',
                           needs_cost_count=services.count_needs_cost(g.db),
                           archived_count=archived_count,
                           format_rupiah=format_rupiah)

@app.route('/api/products', methods=['GET'])
@login_required
def api_products():
    search = request.args.get('search', '')
    # Archived products are browsable on their own rather than mixed in: the point of
    # archiving is to get them out of the working catalogue, and the only thing to do
    # with one is put it back.
    archived = 1 if request.args.get('archived') == '1' else 0
    query = "SELECT * FROM products WHERE is_archived = ?"
    params = [archived]
    if search:
        query += " AND (name LIKE ? OR sku LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])
    if request.args.get('needs_cost') == '1':
        query += f" AND ({services.NEEDS_COST})"
    query += " ORDER BY name"
    products = g.db.execute(query, params).fetchall()
    # 'available' is derived rather than stored, and shipped here so the order form can
    # refresh what it may still sell without re-deriving the subtraction client-side.
    return jsonify([{**dict(r), 'available': r['stock_qty'] - r['reserved_qty']}
                    for r in products])

@app.route('/api/products', methods=['POST'])
@login_required
def api_create_product():
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    fields, err = _validate_product(data)
    if err:
        return _err(err)
    try:
        stock_qty = int(data.get('stock_qty', 0))
    except (TypeError, ValueError):
        return _err('Stock must be a whole number')
    if stock_qty < 0:
        return _err('Stock must be 0 or more')
    # Opening stock is inventory that was paid for, and the cost of a sale is snapshotted
    # when the order is created -- so a product that starts with stock and no cost sells
    # those units at an unknown cost permanently, with no way to repair it afterwards.
    # A product created empty needs nothing: its first restock records the cost.
    if stock_qty > 0 and fields['cost_price'] <= 0:
        return _err('Cost price is required for stock on hand')
    try:
        cur = g.db.execute("""
            INSERT INTO products (name, sku, price, cost_price, stock_qty, reorder_threshold)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (fields['name'], fields['sku'], fields['price'], fields['cost_price'],
              stock_qty, fields['reorder_threshold']))
        g.db.commit()
        return jsonify({'success': True, 'id': cur.lastrowid})
    except sqlite3.IntegrityError:
        return _err('SKU already exists')

@app.route('/api/products/<int:id>', methods=['PUT'])
@login_required
def api_update_product(id):
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    fields, err = _validate_product(data)
    if err:
        return _err(err)
    try:
        # stock_qty is deliberately not updatable here: overwriting it from a stale edit
        # form would erase concurrent sales. Stock changes go through orders and restock.
        #
        # Typing a cost here is the one thing that clears cost_review_needed: the flag
        # says the recorded figure is suspect, and only a person looking at the invoice
        # can settle that. A later restock deliberately does not clear it -- _blend_cost
        # would average the new cost onto the suspect base, so the doubt survives.
        g.db.execute("""
            UPDATE products SET name=?, sku=?, price=?, cost_price=?, reorder_threshold=?,
                   cost_review_needed = CASE WHEN ? > 0 THEN 0 ELSE cost_review_needed END,
                   updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (fields['name'], fields['sku'], fields['price'], fields['cost_price'],
              fields['reorder_threshold'], fields['cost_price'], id))
        g.db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return _err('SKU already exists')

@app.route('/api/products/<int:id>', methods=['DELETE'])
@login_required
def api_delete_product(id):
    g.db.execute("UPDATE products SET is_archived = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (id,))
    g.db.commit()
    return jsonify({'success': True})

@app.route('/api/products/<int:id>/restore', methods=['POST'])
@login_required
def api_restore_product(id):
    """Undo an archive. Never conflicts: the SKU unique index covers archived rows too,
    so nothing can have taken the SKU while this product was away."""
    cur = g.db.execute(
        "UPDATE products SET is_archived = 0, updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND is_archived = 1", (id,))
    g.db.commit()
    if cur.rowcount == 0:
        return _err('Product not found or not archived', 404)
    return jsonify({'success': True})

# --- Stock Adjustment ---
@app.route('/api/stock/adjust', methods=['POST'])
@login_required
def api_stock_adjust():
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    product_id = data.get('product_id')
    change_qty = data.get('change_qty')
    reason = data.get('reason')
    reason = reason.strip() if isinstance(reason, str) else ''
    if not product_id or not isinstance(change_qty, int) or isinstance(change_qty, bool) or change_qty == 0:
        return _err('Product ID and a non-zero whole-number quantity required')
    product = g.db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        return _err('Product not found', 404)
    cur = g.db.execute(
        "UPDATE products SET stock_qty = stock_qty + ?, updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND stock_qty + ? >= 0",
        (change_qty, product_id, change_qty))
    if cur.rowcount == 0:
        g.db.rollback()
        return _err('Insufficient stock')
    new_qty = g.db.execute("SELECT stock_qty FROM products WHERE id = ?", (product_id,)).fetchone()['stock_qty']
    g.db.execute(services.STOCK_LOG_INSERT, (product_id, change_qty, reason, _actor()))
    g.db.commit()
    return jsonify({'success': True, 'new_qty': new_qty})

# --- Stock movement history ---
MOVEMENTS_PAGE_SIZE = 25

@app.route('/stock-history')
@login_required
def stock_history_page():
    # Archived products included, unlike the order form: a product is usually archived
    # *after* its last movement, and a history that hid those rows would lose exactly
    # the stock it is most useful for accounting for.
    products = g.db.execute("SELECT id, name FROM products ORDER BY name").fetchall()
    return render_template('stock_history.html', products=products)

@app.route('/api/stock/movements', methods=['GET'])
@login_required
def api_stock_movements():
    """Every recorded change to physical stock, newest first.

    Reservations are absent by design and not an oversight: holding stock for an open
    order moves reserved_qty and nothing else, so there is no movement to show until
    the order completes. Settings > Data integrity is where held stock is checked.
    """
    page = _int_arg('page')
    if page is None or page < 0:
        return _err('Invalid page')
    product_id = None
    if request.args.get('product_id'):
        product_id = _int_arg('product_id')
        if product_id is None or product_id < 1:
            return _err('Invalid product')
    movements, has_more = services.list_stock_movements(
        g.db, product_id=product_id, page=page, page_size=MOVEMENTS_PAGE_SIZE)
    return jsonify({'movements': movements, 'has_more': has_more, 'page': page})

# --- Orders ---
@app.route('/orders')
@login_required
def orders_page():
    # No orders are fetched here: the table is filled by loadOrders() against the API,
    # which is where the paging lives. Rendering them server-side as well meant reading
    # every order the shop had ever taken, on every page load, to throw them away.
    # The whole active catalogue, not just what is sellable today: the form hides rows
    # with nothing available, but a draft being edited must still be able to show the
    # product it is already holding. Same list shape the form refetches on open, so
    # what it filters does not depend on which of the two it got.
    products = g.db.execute(
        "SELECT * FROM products WHERE is_archived = 0 ORDER BY name").fetchall()
    return render_template('orders.html', products_json=_products_json(products), format_rupiah=format_rupiah)

# One screenful. The bot's list uses the same size, so an order sits on the same page
# number whichever way the shop looks it up.
ORDERS_PAGE_SIZE = 10

@app.route('/api/orders', methods=['GET'])
@login_required
def api_orders():
    page = _int_arg('page')
    if page is None or page < 0:
        return _err('Invalid page')
    orders, has_more = services.list_orders(
        g.db,
        status=request.args.get('status') or None,
        search=request.args.get('search') or None,
        page=page, page_size=ORDERS_PAGE_SIZE)
    return jsonify({'orders': orders, 'has_more': has_more, 'page': page})

@app.route('/api/orders/<int:id>', methods=['GET'])
@login_required
def api_order(id):
    """One order with its lines. The page used to find an order by pulling the whole
    list and searching it client-side, which paging would have quietly broken -- an
    order on page 3 is not in the array the page happens to be holding."""
    try:
        order, items = services.get_order(g.db, id)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({**dict(order), 'items': [dict(i) for i in items]})

def _order_items_body():
    """Validated order lines from the request. Returns (items, error_response)."""
    data = _json_body()
    if data is None:
        return None, _err('Invalid JSON body')
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return None, _err('At least one item required')
    for item in items:
        pid = item.get('product_id') if isinstance(item, dict) else None
        qty = item.get('quantity') if isinstance(item, dict) else None
        # bool is an int subclass: True would slip through as quantity 1
        if not isinstance(pid, int) or isinstance(pid, bool) \
                or not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return None, _err('Each item needs a product_id and a positive whole-number quantity')
    return items, None

@app.route('/api/orders', methods=['POST'])
@login_required
def api_create_order():
    items, error = _order_items_body()
    if error:
        return error
    try:
        result = services.create_order(g.db, items)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, 'order_id': result['order_id'], 'total': result['total']})

@app.route('/api/orders/<int:id>', methods=['PUT'])
@login_required
def api_update_order(id):
    items, error = _order_items_body()
    if error:
        return error
    try:
        result = services.update_order(g.db, id, items)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, 'order_id': result['order_id'], 'total': result['total']})

@app.route('/api/orders/<int:id>/confirm', methods=['POST'])
@login_required
def api_confirm_order(id):
    try:
        services.confirm_order(g.db, id)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True})

@app.route('/api/orders/<int:id>/complete', methods=['POST'])
@login_required
def api_complete_order(id):
    try:
        services.complete_order(g.db, id, actor=_actor())
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True})

@app.route('/api/orders/<int:id>/cancel', methods=['POST'])
@login_required
def api_cancel_order(id):
    try:
        services.cancel_order(g.db, id)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True})

@app.route('/api/orders/<int:id>/payment', methods=['POST'])
@login_required
def api_order_payment(id):
    """Buyer name, payment method and proof of payment for one order.

    multipart/form-data rather than JSON, because a receipt is a file. Every text
    field the editor shows is submitted every time, so an empty box clears the
    column -- that is what the seller just did to it. The file is the exception: a
    file input cannot be pre-filled with what is already stored, so an empty one
    means "leave it", and removing a proof is the explicit remove_proof checkbox.
    """
    method = request.form.get('payment_method') or None
    proof = request.files.get('proof')
    # A form submitted with no file chosen still carries the field, with an empty
    # filename. Reading it would store zero bytes over a perfectly good receipt.
    data = proof.read() if proof and proof.filename else None
    try:
        services.set_order_payment(
            g.db, id,
            buyer_name=request.form.get('buyer_name'),
            payment_method=method,
            proof=data,
            remove_proof=request.form.get('remove_proof') == '1')
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True})

@app.route('/api/orders/<int:id>/proof', methods=['GET'])
@login_required
def api_order_proof(id):
    """The stored proof of payment, as the type it was found to be.

    The served Content-Type comes from the whitelist the bytes were matched against
    on upload, never from anything the uploader said, and nosniff stops the browser
    reconsidering it. PDFs download rather than open inline: an image is what the
    detail view draws, and a document from outside has no reason to be rendered in
    the shop's own origin.
    """
    try:
        proof = services.get_payment_proof(g.db, id)
    except ServiceError as e:
        return _service_error(e)
    if not proof:
        return _err('No payment proof recorded for this order', 404)
    disposition = 'inline' if proof['mime_type'].startswith('image/') else 'attachment'
    filename = services.payment_proof_filename(id, proof['mime_type'])
    return Response(proof['data'], mimetype=proof['mime_type'], headers={
        'Content-Disposition': f'{disposition}; filename="{filename}"',
        'X-Content-Type-Options': 'nosniff',
        # A receipt carries a customer's name and bank details; it is not something
        # a shared machine's browser cache should keep after the seller signs out.
        'Cache-Control': 'private, no-store',
    })

# --- Restock ---
@app.route('/restock')
@login_required
def restock_page():
    products = g.db.execute("SELECT * FROM products WHERE is_archived = 0 AND stock_qty >= 0 ORDER BY name").fetchall()
    return render_template('restock.html', products_json=_products_json(products), format_rupiah=format_rupiah)

@app.route('/api/restock', methods=['POST'])
@login_required
def api_restock():
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return _err('At least one item required')
    # Invoice-level charges: each is optional and defaults to nothing, since the common
    # invoice has no voucher and no bank fee.
    charges = {}
    for key in ('discount', 'shipping_cost', 'admin_fee'):
        value = data.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (value >= 0):
            # One message for all three: a literal here is what the i18n coverage test
            # scans for, and a per-field table would slip past it untranslated.
            return _err('Discount, shipping and admin fee must each be 0 or more')
        charges[key] = float(value)
    validated = []
    for item in items:
        product_id = item.get('product_id') if isinstance(item, dict) else None
        qty_added = item.get('qty') if isinstance(item, dict) else None
        unit_price = item.get('unit_price', 0) if isinstance(item, dict) else None
        if not product_id or not isinstance(qty_added, int) or isinstance(qty_added, bool) or qty_added <= 0:
            return _err('Valid product and positive whole-number quantity required')
        if isinstance(unit_price, bool) or not isinstance(unit_price, (int, float)) or not (unit_price >= 0):
            return _err('Unit price must be 0 or more')
        validated.append({'product_id': product_id, 'qty': qty_added, 'unit_price': float(unit_price)})
    try:
        result = services.create_restock(g.db, validated, **charges, actor=_actor())
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, 'total_cost': result['total_cost']})

@app.route('/api/restock/<int:id>/void', methods=['POST'])
@login_required
def api_restock_void(id):
    try:
        result = services.void_restock(g.db, id, actor=_actor())
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, **result})

# Batch history pages. Smaller than the orders page because every row expands into a
# per-product breakdown, so ten batches is already a long page.
HISTORY_PAGE_SIZE = 10

def _history_window():
    """(start, end, error) for a batch-history request's ``period``/``tz`` params.

    A period of 'all' is (None, None) -- no window, every batch, which paging now makes
    affordable. Anything unrecognised is a 400 rather than a silent full listing.
    """
    period = request.args.get('period', 'all')
    unit = {'today': 'day', 'week': 'week', 'month': 'month', 'year': 'year'}.get(period)
    if unit:
        start, end = get_date_range(unit, 0, _client_tz(request.args.get('tz')))
        return start, end, None
    if period != 'all':
        return None, None, _err('invalid period')
    return None, None, None

def _history_page():
    """Validated page number for a batch-history request. Returns (page, error)."""
    page = _int_arg('page')
    if page is None or page < 0:
        return None, _err('Invalid page')
    return page, None

@app.route('/api/restock/history', methods=['GET'])
@login_required
def api_restock_history():
    start, end, err = _history_window()
    if err:
        return err
    page, err = _history_page()
    if err:
        return err
    batches, has_more = services.list_restock_batches(
        g.db, start=start, end=end, page=page, page_size=HISTORY_PAGE_SIZE)
    return jsonify({'batches': batches, 'has_more': has_more, 'page': page})

# --- Self Use ---
@app.route('/self-use')
@login_required
def self_use_page():
    # stock_qty > 0 like the orders page: self use takes stock out, so a product
    # already at zero cannot be picked (restock uses >= 0 because it puts stock in).
    products = g.db.execute(
        "SELECT * FROM products WHERE is_archived = 0 AND stock_qty > 0 ORDER BY name").fetchall()
    return render_template('selfuse.html', products_json=_products_json(products),
                           format_rupiah=format_rupiah)

@app.route('/api/self-use', methods=['POST'])
@login_required
def api_self_use():
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return _err('At least one item required')
    validated = []
    for item in items:
        product_id = item.get('product_id') if isinstance(item, dict) else None
        qty = item.get('qty') if isinstance(item, dict) else None
        if not product_id or not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return _err('Valid product and positive whole-number quantity required')
        validated.append({'product_id': product_id, 'qty': qty})
    try:
        result = services.create_self_use(g.db, validated, actor=_actor())
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, 'batch_id': result['batch_id'],
                    'total_value': result['total_value']})

@app.route('/api/self-use/<int:id>/void', methods=['POST'])
@login_required
def api_self_use_void(id):
    try:
        result = services.void_self_use(g.db, id, actor=_actor())
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, **result})

@app.route('/api/self-use/history', methods=['GET'])
@login_required
def api_self_use_history():
    start, end, err = _history_window()
    if err:
        return err
    page, err = _history_page()
    if err:
        return err
    batches, has_more = services.list_self_use_batches(
        g.db, start=start, end=end, page=page, page_size=HISTORY_PAGE_SIZE)
    return jsonify({'batches': batches, 'has_more': has_more, 'page': page})

# --- Settings ---
def _parse_whitelist(raw):
    """Comma/space separated Telegram user IDs -> list[int]. Raises ValueError
    carrying the bad entry, so the caller can build a translated message."""
    ids = []
    for tok in raw.replace(',', ' ').split():
        if not tok.lstrip('-').isdigit():
            raise ValueError(tok)
        ids.append(int(tok))
    return ids

@app.route('/settings')
@login_required
def settings_page():
    from database import get_setting, get_secret_setting
    return render_template('settings.html',
        telegram_enabled=get_setting(g.db, 'telegram_enabled', '0') == '1',
        # Decrypted rather than a bare presence check, so a token the app can no
        # longer read shows as unset instead of claiming a working bot.
        token_set=bool(get_secret_setting(g.db, 'telegram_bot_token')),
        whitelist=get_setting(g.db, 'telegram_whitelist', ''),
        shop_timezone=get_setting(g.db, 'shop_timezone', 'Asia/Jakarta'),
        order_alert_hours=get_setting(g.db, 'order_alert_hours', '24'),
        monthly_report_enabled=get_setting(g.db, 'monthly_report_enabled', '1') == '1',
        current_lang=get_lang(),
        font_scales=FONT_SCALES,
        username=session.get('username', ''))

@app.route('/api/settings/language', methods=['POST'])
@login_required
def api_settings_language():
    from database import set_setting
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    lang = data.get('language')
    if lang not in i18n.LANGUAGES:
        return _err('Unsupported language')
    set_setting(g.db, 'language', lang)
    g.db.commit()
    return jsonify({'success': True})

@app.route('/api/settings/font-scale', methods=['POST'])
@login_required
def api_settings_font_scale():
    from database import set_setting
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    try:
        scale = int(data.get('scale'))
    except (TypeError, ValueError):
        return _err('Unsupported text size')
    if scale not in FONT_SCALES:
        return _err('Unsupported text size')
    set_setting(g.db, 'ui_font_scale', str(scale))
    g.db.commit()
    return jsonify({'success': True})

@app.route('/api/settings/telegram', methods=['POST'])
@login_required
def api_settings_telegram():
    from database import set_setting, get_secret_setting, set_secret_setting
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    enabled = bool(data.get('enabled'))
    token = data.get('token')
    token = token.strip() if isinstance(token, str) else ''
    whitelist_raw = data.get('whitelist')
    whitelist_raw = whitelist_raw.strip() if isinstance(whitelist_raw, str) else ''
    tz_name = data.get('timezone')
    tz_name = tz_name.strip() if isinstance(tz_name, str) else ''

    try:
        ids = _parse_whitelist(whitelist_raw)
    except ValueError as e:
        return _err("'{token}' is not a numeric Telegram user ID", token=str(e))
    if tz_name:
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            return _err("Unknown timezone '{name}'", name=tz_name)

    # Stale-order alert threshold in hours. Absent -> leave unchanged; blank -> disable (0).
    alert_hours_val = None
    if 'alert_hours' in data:
        raw = data.get('alert_hours')
        s = str(raw).strip() if raw is not None else ''
        if s == '':
            alert_hours_val = '0'
        else:
            try:
                hours = float(s)
            except ValueError:
                return _err('Alert threshold must be a number')
            if hours < 0:
                return _err('Alert threshold cannot be negative')
            alert_hours_val = s

    # Blank token means keep the saved one; the saved value is never echoed to the browser.
    effective_token = token or get_secret_setting(g.db, 'telegram_bot_token')
    if enabled and not effective_token:
        return _err('Bot token required to enable the bot')

    set_setting(g.db, 'telegram_enabled', '1' if enabled else '0')
    if token:
        set_secret_setting(g.db, 'telegram_bot_token', token)
    set_setting(g.db, 'telegram_whitelist', ','.join(str(i) for i in ids))
    if tz_name:
        set_setting(g.db, 'shop_timezone', tz_name)
    if alert_hours_val is not None:
        set_setting(g.db, 'order_alert_hours', alert_hours_val)
    # Absent -> leave unchanged, so a client that predates this field can still save.
    if 'monthly_report' in data:
        set_setting(g.db, 'monthly_report_enabled',
                    '1' if data.get('monthly_report') else '0')
    g.db.commit()
    warning = (i18n.make_t(get_lang())('No users whitelisted — the bot will reject everyone')
               if enabled and not ids else None)
    return jsonify({'success': True, 'warning': warning})

@app.route('/api/settings/telegram/test', methods=['POST'])
@login_required
def api_settings_telegram_test():
    from database import get_secret_setting
    data = _json_body() or {}
    token = data.get('token')
    token = token.strip() if isinstance(token, str) else ''
    token = token or get_secret_setting(g.db, 'telegram_bot_token')
    if not token:
        return _err('No bot token saved or provided')
    try:
        me = TelegramAPI(token, timeout=10).call('getMe')
    except TelegramError as e:
        return _err('Telegram rejected the token: {error}', error=str(e))
    except OSError:
        return _err('Could not reach api.telegram.org')
    return jsonify({'success': True, 'bot_username': me.get('username', '')})

@app.route('/api/settings/account', methods=['POST'])
@login_required
def api_settings_account():
    from werkzeug.security import generate_password_hash
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    current = data.get('current_password') or ''
    new_username = data.get('new_username')
    new_username = new_username.strip() if isinstance(new_username, str) else ''
    new_password = data.get('new_password') or ''

    user = g.db.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
    if not user or not check_password_hash(user['password'], current):
        return _err('Current password is incorrect')
    if not new_username and not new_password:
        return _err('Nothing to change')
    if new_password and len(new_password) < 6:
        return _err('New password must be at least 6 characters')

    try:
        if new_username and new_username != user['username']:
            g.db.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user['id']))
            session['username'] = new_username
        if new_password:
            g.db.execute("UPDATE users SET password = ? WHERE id = ?",
                         (generate_password_hash(new_password), user['id']))
        g.db.commit()
    except sqlite3.IntegrityError:
        return _err('Username already taken')
    return jsonify({'success': True})

# Held stock is the one figure in the app with no visible audit trail: a sale shows up
# in stock_logs, but a reservation only ever moves a counter. These two routes are the
# way to see it and, separately, to put it right -- separately on purpose, so the
# discrepancy is read before it is erased.
def _drift_payload(rows):
    return [{'id': r['id'], 'name': r['name'],
             'reserved': r['reserved_qty'], 'expected': r['expected'],
             'difference': r['reserved_qty'] - r['expected']} for r in rows]

@app.route('/api/stock/reservations/check', methods=['GET'])
@login_required
def api_reservations_check():
    return jsonify({'drift': _drift_payload(services.reservation_drift(g.db))})

@app.route('/api/stock/reservations/repair', methods=['POST'])
@login_required
def api_reservations_repair():
    repaired = services.repair_reservations(g.db)
    if repaired:
        # Worth a log line even though the UI reports it: this is a write to stock
        # figures made by a button rather than by a sale, and the actor belongs on record.
        log.warning('%s repaired held stock for %d product(s): %s', _actor(), len(repaired),
                    ', '.join(f"{r['name']} {r['reserved_qty']}->{r['expected']}" for r in repaired))
    return jsonify({'success': True, 'repaired': _drift_payload(repaired)})

# --- Sales Dashboard ---
@app.route('/sales')
@login_required
def sales_page():
    return render_template('sales.html', format_rupiah=format_rupiah)

def _client_tz(name=None):
    """Resolve an IANA timezone name sent by the client. Falls back to UTC."""
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc

def _shop_tz():
    """Timezone for server-rendered date windows (the dashboard has no client `tz`
    param to work from). Same setting the bot summaries use."""
    from database import get_setting
    return _client_tz(get_setting(g.db, 'shop_timezone', 'Asia/Jakarta'))

def _int_arg(name, default=0):
    """Read an int query param. Returns None when unparseable so callers can 400."""
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return None

@app.route('/api/sales/summary', methods=['GET'])
@login_required
def api_sales_summary():
    unit = request.args.get('unit', 'month')
    offset = _int_arg('offset')
    if offset is None:
        return _err('invalid offset')
    tz = _client_tz(request.args.get('tz'))
    try:
        summary = services.sales_summary(g.db, unit, offset, tz)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({k: summary[k] for k in (
        'total_revenue', 'total_orders', 'unique_skus', 'total_items_sold',
        'restock_cost', 'self_use_value', 'cogs', 'gross_profit',
        'uncosted_sales', 'net_profit')})

@app.route('/api/sales/product-value', methods=['GET'])
@login_required
def api_sales_product_value():
    total = g.db.execute("""
        SELECT COALESCE(SUM(price * stock_qty), 0) as total FROM products
        WHERE is_archived = 0
    """).fetchone()['total']
    return jsonify({'total_value': total})

@app.route('/api/sales/trend', methods=['GET'])
@login_required
def api_sales_trend():
    unit = request.args.get('unit', 'month')
    offset = _int_arg('offset')
    if offset is None:
        return _err('invalid offset')
    tz = _client_tz(request.args.get('tz'))
    start, end = get_date_range(unit, offset, tz)
    if not start:
        return _err('invalid unit')

    date_filter, params = build_date_filter(start, end)
    rows = g.db.execute("""
        SELECT o.created_at, o.total_amount
        FROM orders o WHERE o.status = 'completed'
    """ + date_filter, params).fetchall()

    # Bucket in Python: SQLite's 'localtime' modifier would use the server's timezone,
    # and a fixed offset modifier breaks across DST. Both must follow the client's tz.
    buckets = {}
    for r in rows:
        local = datetime.fromisoformat(r['created_at']).replace(tzinfo=timezone.utc).astimezone(tz)
        if unit in ('day', 'week'):
            key = local.date().isoformat()
        elif unit == 'month':
            key = local.isocalendar().week
        else:
            key = local.strftime('%m')
        buckets[key] = buckets.get(key, 0) + r['total_amount']

    lang = get_lang()
    if unit == 'month':
        t = i18n.make_t(lang)
        return jsonify([{'label': t('Week {n}', n=k), 'revenue': v} for k, v in sorted(buckets.items())])
    elif unit == 'year':
        # Chart labels are built from the i18n calendar tables; strftime('%b') would
        # follow the server's C locale instead of the shop language.
        month_names = {f'{m:02d}': i18n.month_name(m, lang, abbr=True) for m in range(1, 13)}
        return jsonify([{'label': month_names.get(k, k), 'revenue': v} for k, v in sorted(buckets.items())])
    return jsonify([{'label': k, 'revenue': v} for k, v in sorted(buckets.items())])

# Idle products listed on the sales page. The totals alongside cover every match,
# so the card can say how many were left out and what they are collectively worth.
UNSOLD_PAGE_LIMIT = 10

@app.route('/api/sales/product-performance', methods=['GET'])
@login_required
def api_sales_product_performance():
    """How products did over the window: volume leaders, profit leaders, and idlers.

    One endpoint for three panels that always refresh together, so switching period
    costs one round trip.
    """
    unit = request.args.get('unit', 'month')
    offset = _int_arg('offset')
    if offset is None:
        return _err('invalid offset')
    tz = _client_tz(request.args.get('tz'))
    start, end = get_date_range(unit, offset, tz)
    if not start:
        return _err('invalid unit')

    db = g.db
    unsold = services.products_without_sales(db, start, end)
    return jsonify({
        'by_quantity': services.top_products_by_quantity(db, start, end),
        'by_profit': services.top_products_by_profit(db, start, end),
        'uncosted_sales': services.sales_missing_cost(db, start, end),
        'unsold': {
            'items': unsold[:UNSOLD_PAGE_LIMIT],
            'total': len(unsold),
            'total_stock_value': sum(p['stock_value'] for p in unsold),
        },
    })

# --- Monthly report ---
# Months offered in the picker on the sales page. A year back covers an audit
# question about any month the shop has been running under this feature.
REPORT_MONTHS = 12

def _report_offset():
    """Validated month offset from the request: 1 is the month that just closed.

    Read from the JSON body on POST and the query string on GET, so the download
    link stays a plain URL the browser can navigate to. Returns
    (offset, error_response). Offset 0 (the current, incomplete month) is allowed:
    the page offers it explicitly as a month-to-date figure.
    """
    body = _json_body() if request.method == 'POST' else None
    raw = body.get('offset', 1) if body else request.args.get('offset', 1)
    try:
        offset = int(raw)
    except (TypeError, ValueError):
        return None, _err('invalid offset')
    if not (0 <= offset <= REPORT_MONTHS):
        return None, _err('invalid offset')
    return offset, None

def _build_report(offset):
    """Render and archive a month, in the shop's timezone.

    Deliberately not the client's timezone: the archived file and the copy the bot
    pushes must describe the same month boundaries no matter who asked for it.
    """
    return reports.build(g.db, offset, _shop_tz(), get_lang())

@app.route('/api/reports/monthly', methods=['GET'])
@login_required
def api_report_download():
    offset, err = _report_offset()
    if err:
        return err
    _, content, data = _build_report(offset)
    return Response(content, mimetype='application/pdf', headers={
        'Content-Disposition':
            f'attachment; filename="{reports.report_filename(data["period"])}"'})

@app.route('/api/reports/monthly.csv', methods=['GET'])
@login_required
def api_report_csv():
    """The month's sold lines as CSV, for a spreadsheet or a tax return.

    Same month selector and same window as the PDF, deliberately: two exports of one
    month that disagreed about where the month starts would be worse than one.
    """
    offset, err = _report_offset()
    if err:
        return err
    text = reports.sales_csv(g.db, offset, _shop_tz(), get_lang())
    period = reports.period_key(services.get_date_range('month', offset, _shop_tz())[0])
    # utf-8-sig, not plain utf-8: Excel reads a BOM-less CSV as the local codepage and
    # mangles any product name with a curly quote or a non-ASCII letter in it. Product
    # names are user data -- the same reason the PDF fonts are vendored Unicode ones.
    return Response(text.encode('utf-8-sig'), mimetype='text/csv; charset=utf-8', headers={
        'Content-Disposition':
            f'attachment; filename="{reports.report_filename(period, "csv")}"'})

@app.route('/api/reports/monthly/send', methods=['POST'])
@login_required
def api_report_send():
    from database import get_secret_setting, get_setting
    offset, err = _report_offset()
    if err:
        return err
    token = get_secret_setting(g.db, 'telegram_bot_token')
    whitelist = telegram_bot.parse_whitelist(get_setting(g.db, 'telegram_whitelist', ''))
    if not token:
        return _err('No bot token saved or provided')
    if not whitelist:
        return _err('No whitelisted Telegram IDs to send to')
    _, content, data = _build_report(offset)
    api = TelegramAPI(token, timeout=60)  # a PDF upload is slower than a message
    filename = reports.report_filename(data['period'])
    caption = telegram_bot.report_caption(data, i18n.make_t(get_lang()))
    sent, failed = 0, []
    for chat_id in sorted(whitelist):
        try:
            api.send_document(chat_id, filename, content, caption)
            sent += 1
        except (TelegramError, OSError) as e:
            log.warning('report %s to %s failed: %s', data['period'], chat_id, e)
            failed.append(chat_id)
    if not sent:
        return _err('Could not send to any recipient. The report was saved on the server.')
    result = {'success': True, 'sent': sent, 'month': data['label']}
    if failed:
        result['warning'] = i18n.make_t(get_lang())(
            'Sent to {sent} of {total} recipients.', sent=sent, total=len(whitelist))
    return jsonify(result)

@app.route('/api/reports/months', methods=['GET'])
@login_required
def api_report_months():
    """Selectable months, newest first, labelled in the shop's language."""
    tz, lang = _shop_tz(), get_lang()
    months = []
    for offset in range(0, REPORT_MONTHS + 1):
        start, _ = get_date_range('month', offset, tz)
        months.append({'offset': offset, 'label': i18n.month_label(start, lang),
                       'period': reports.period_key(start)})
    return jsonify(months)

def _configure_logging():
    """Attach a timestamped stderr handler. Without this the module loggers fall
    back to Python's lastResort handler, which drops INFO entirely -- so nothing
    ever confirmed the bot poller had started."""
    logging.basicConfig(
        level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )

def bootstrap(start_bot=True):
    """Prepare a process to serve: logging, migrations, then the bot poller.

    Both the dev server and the WSGI entrypoint (wsgi.py) go through here, so a
    deployment can never end up with an unmigrated database or a dead bot. Call
    once per process: the poller is an in-process thread holding a single
    getUpdates offset, so a second one duplicates every Telegram message.
    """
    _configure_logging()
    init_db()
    _warn_on_reservation_drift()
    if start_bot and os.environ.get('SHOP_ENABLE_BOT', '1').lower() not in ('0', 'false', 'no'):
        from telegram_bot import BotPoller
        BotPoller().start()


def _warn_on_reservation_drift():
    """Log held stock that no open order accounts for, once per start.

    Reports but never repairs: rewriting what customers are owed is not something to
    do to a database unattended, and Settings has the button. Startup is the moment
    worth checking because an unclean shutdown is the likeliest way to get here.
    Wrapped whole -- a diagnostic must not be the reason the shop cannot boot.
    """
    try:
        db = get_db()
        try:
            drifted = services.reservation_drift(db)
        finally:
            db.close()
    except Exception:
        log.exception('could not check reserved stock against open orders')
        return
    if not drifted:
        return
    log.warning('%d product(s) hold stock that open orders do not account for; '
                'review under Settings > Data integrity', len(drifted))
    for row in drifted:
        log.warning('  %s: holding %d, open orders justify %d',
                    row['name'], row['reserved_qty'], row['expected'])

if __name__ == '__main__':
    # debug exposes the Werkzeug console (remote code execution) to anyone on the
    # network; it must never default on for a 0.0.0.0 bind.
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    # With the reloader active, only the child process serves requests; the
    # WERKZEUG_RUN_MAIN guard stops the parent from starting a second poller.
    bootstrap(start_bot=not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true')
    app.run(host=os.environ.get('HOST', '0.0.0.0'),
            port=int(os.environ.get('PORT', '5000')),
            debug=debug)
