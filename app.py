from flask import (Flask, request, jsonify, render_template, redirect, url_for,
                   session, g, Response)
from database import get_db, init_db
from functools import wraps
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from werkzeug.security import check_password_hash
import logging
import sqlite3
import os
import secrets

import i18n
import reports
import services
import telegram_bot
from services import (ServiceError, format_rupiah, get_date_range,
                      build_date_filter, _to_utc_str)
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

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def _json_body():
    """Parsed JSON object from the request, or None (caller returns 400)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None

def _products_json(rows):
    """Plain dicts for embedding in templates via |tojson (sqlite3.Row isn't serializable)."""
    return [{'id': p['id'], 'name': p['name'], 'sku': p['sku'],
             'price': p['price'], 'stock': p['stock_qty']} for p in rows]

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
        'category_id': data.get('category_id') or None,
        'price': price,
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
    }

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        db = g.db
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        return render_template('login.html', error=i18n.make_t(get_lang())('Invalid credentials'))
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
        SELECT p.*, c.name as category_name FROM products p
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.is_archived = 0 AND p.stock_qty <= p.reorder_threshold
        ORDER BY p.stock_qty ASC
        LIMIT 10
    """).fetchall()
    return render_template('dashboard.html',
        total_products=total_products,
        total_orders=total_orders,
        low_stock_count=low_stock,
        month_label=month_label,
        month_revenue=format_rupiah(summary['total_revenue']),
        net_profit=format_rupiah(summary['net_profit']),
        self_use_value=format_rupiah(summary['self_use_value']),
        total_product_value=format_rupiah(total_product_value),
        total_restock_cost_raw=summary['restock_cost'],
        recent_orders=recent_orders,
        low_stock_products=low_stock_products,
        format_rupiah=format_rupiah
    )

# --- Categories ---
@app.route('/categories')
@login_required
def categories_page():
    categories = g.db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    return render_template('categories.html', categories=categories)

@app.route('/api/categories', methods=['GET'])
@login_required
def api_categories():
    categories = g.db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    return jsonify([dict(r) for r in categories])

@app.route('/api/categories', methods=['POST'])
@login_required
def api_create_category():
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    name = data.get('name')
    name = name.strip() if isinstance(name, str) else ''
    if not name:
        return _err('Name required')
    try:
        g.db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        g.db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return _err('Category already exists')

@app.route('/api/categories/<int:id>', methods=['PUT'])
@login_required
def api_update_category(id):
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    name = data.get('name')
    name = name.strip() if isinstance(name, str) else ''
    if not name:
        return _err('Name required')
    try:
        g.db.execute("UPDATE categories SET name = ? WHERE id = ?", (name, id))
        g.db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return _err('Category already exists')

@app.route('/api/categories/<int:id>', methods=['DELETE'])
@login_required
def api_delete_category(id):
    used = g.db.execute("SELECT COUNT(*) as cnt FROM products WHERE category_id = ?", (id,)).fetchone()['cnt']
    if used > 0:
        return _err('Category has products assigned')
    g.db.execute("DELETE FROM categories WHERE id = ?", (id,))
    g.db.commit()
    return jsonify({'success': True})

# --- Products ---
@app.route('/products')
@login_required
def products_page():
    products = g.db.execute("""
        SELECT p.*, c.name as category_name
        FROM products p LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.is_archived = 0
        ORDER BY p.name
    """).fetchall()
    categories = g.db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    return render_template('products.html', products=products, categories=categories, format_rupiah=format_rupiah)

@app.route('/api/products', methods=['GET'])
@login_required
def api_products():
    search = request.args.get('search', '')
    category = request.args.get('category', '')
    query = """
        SELECT p.*, c.name as category_name
        FROM products p LEFT JOIN categories c ON p.category_id = c.id
        WHERE p.is_archived = 0
    """
    params = []
    if search:
        query += " AND (p.name LIKE ? OR p.sku LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])
    if category:
        query += " AND p.category_id = ?"
        params.append(category)
    query += " ORDER BY p.name"
    products = g.db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in products])

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
    try:
        cur = g.db.execute("""
            INSERT INTO products (name, sku, category_id, price, stock_qty, reorder_threshold)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (fields['name'], fields['sku'], fields['category_id'],
              fields['price'], stock_qty, fields['reorder_threshold']))
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
        g.db.execute("""
            UPDATE products SET name=?, sku=?, category_id=?, price=?, reorder_threshold=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (fields['name'], fields['sku'], fields['category_id'],
              fields['price'], fields['reorder_threshold'], id))
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
    g.db.execute("INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (?, ?, ?)", (product_id, change_qty, reason))
    g.db.commit()
    return jsonify({'success': True, 'new_qty': new_qty})

# --- Orders ---
@app.route('/orders')
@login_required
def orders_page():
    status_filter = request.args.get('status', '')
    search = request.args.get('search', '')
    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if search:
        query += " AND id LIKE ?"
        params.append(f'%{search}%')
    query += " ORDER BY created_at DESC"
    orders = g.db.execute(query, params).fetchall()
    products = g.db.execute("SELECT * FROM products WHERE is_archived = 0 AND stock_qty > 0 ORDER BY name").fetchall()
    return render_template('orders.html', orders=orders, products_json=_products_json(products), format_rupiah=format_rupiah)

@app.route('/api/orders', methods=['GET'])
@login_required
def api_orders():
    status = request.args.get('status', '')
    search = request.args.get('search', '')
    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if search:
        query += " AND id LIKE ?"
        params.append(f'%{search}%')
    query += " ORDER BY created_at DESC"
    orders = g.db.execute(query, params).fetchall()
    result = []
    for o in orders:
        items = g.db.execute("""
            SELECT oi.*, p.name as product_name, p.sku as product_sku
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE oi.order_id = ?
        """, (o['id'],)).fetchall()
        result.append({**dict(o), 'items': [dict(i) for i in items]})
    return jsonify(result)

@app.route('/api/orders', methods=['POST'])
@login_required
def api_create_order():
    data = _json_body()
    if data is None:
        return _err('Invalid JSON body')
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return _err('At least one item required')
    for item in items:
        pid = item.get('product_id') if isinstance(item, dict) else None
        qty = item.get('quantity') if isinstance(item, dict) else None
        # bool is an int subclass: True would slip through as quantity 1
        if not isinstance(pid, int) or isinstance(pid, bool) \
                or not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return _err('Each item needs a product_id and a positive whole-number quantity')

    try:
        result = services.create_order(g.db, items)
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
        services.complete_order(g.db, id)
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
    batch_total_cost = data.get('total_cost', 0)
    if isinstance(batch_total_cost, bool) or not isinstance(batch_total_cost, (int, float)) or not (batch_total_cost >= 0):
        return _err('Total cost must be 0 or more')
    batch_total_cost = float(batch_total_cost)
    validated = []
    for item in items:
        product_id = item.get('product_id') if isinstance(item, dict) else None
        qty_added = item.get('qty') if isinstance(item, dict) else None
        if not product_id or not isinstance(qty_added, int) or isinstance(qty_added, bool) or qty_added <= 0:
            return _err('Valid product and positive whole-number quantity required')
        validated.append({'product_id': product_id, 'qty': qty_added})
    try:
        services.create_restock(g.db, validated, batch_total_cost)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, 'total_cost': batch_total_cost})

@app.route('/api/restock/history', methods=['GET'])
@login_required
def api_restock_history():
    period = request.args.get('period', 'all')
    tz = _client_tz(request.args.get('tz'))
    query = """
        SELECT rb.id, rb.total_cost, rb.created_at
        FROM restock_batches rb
        WHERE 1=1
    """
    params = ()
    unit = {'today': 'day', 'week': 'week', 'month': 'month', 'year': 'year'}.get(period)
    if unit:
        start, end = get_date_range(unit, 0, tz)
        clause, params = build_date_filter(start, end, 'rb.created_at')
        query += clause
    elif period != 'all':
        return _err('invalid period')
    query += " ORDER BY rb.created_at DESC"
    batches = g.db.execute(query, params).fetchall()
    result = []
    for b in batches:
        items = g.db.execute("""
            SELECT ri.*, p.name as product_name, p.sku as product_sku
            FROM restock_items ri
            JOIN products p ON ri.product_id = p.id
            WHERE ri.batch_id = ?
            ORDER BY ri.id
        """, (b['id'],)).fetchall()
        result.append({
            'id': b['id'],
            'total_cost': b['total_cost'],
            'created_at': b['created_at'],
            'items': [dict(i) for i in items]
        })
    return jsonify(result)

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
        result = services.create_self_use(g.db, validated)
    except ServiceError as e:
        return _service_error(e)
    return jsonify({'success': True, 'batch_id': result['batch_id'],
                    'total_value': result['total_value']})

@app.route('/api/self-use/history', methods=['GET'])
@login_required
def api_self_use_history():
    period = request.args.get('period', 'all')
    tz = _client_tz(request.args.get('tz'))
    query = """
        SELECT sb.id, sb.total_value, sb.created_at
        FROM self_use_batches sb
        WHERE 1=1
    """
    params = ()
    unit = {'today': 'day', 'week': 'week', 'month': 'month', 'year': 'year'}.get(period)
    if unit:
        start, end = get_date_range(unit, 0, tz)
        clause, params = build_date_filter(start, end, 'sb.created_at')
        query += clause
    elif period != 'all':
        return _err('invalid period')
    query += " ORDER BY sb.created_at DESC"
    batches = g.db.execute(query, params).fetchall()
    result = []
    for b in batches:
        items = g.db.execute("""
            SELECT su.*, p.name as product_name, p.sku as product_sku
            FROM self_use_items su
            JOIN products p ON su.product_id = p.id
            WHERE su.batch_id = ?
            ORDER BY su.id
        """, (b['id'],)).fetchall()
        result.append({
            'id': b['id'],
            'total_value': b['total_value'],
            'created_at': b['created_at'],
            'items': [dict(i) for i in items]
        })
    return jsonify(result)

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

@app.route('/api/settings/telegram', methods=['POST'])
@login_required
def api_settings_telegram():
    from database import get_setting, set_setting, get_secret_setting, set_secret_setting
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
        'total_revenue', 'total_orders', 'unique_skus',
        'total_items_sold', 'restock_cost', 'self_use_value', 'net_profit')})

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

@app.route('/api/sales/top-products', methods=['GET'])
@login_required
def api_sales_top_products():
    unit = request.args.get('unit', 'month')
    offset = _int_arg('offset')
    if offset is None:
        return _err('invalid offset')
    tz = _client_tz(request.args.get('tz'))
    start, end = get_date_range(unit, offset, tz)
    if not start:
        return _err('invalid unit')

    db = g.db
    date_filter, params = build_date_filter(start, end)
    base_query = """
        SELECT p.id, p.name, p.sku, SUM(oi.quantity) as total_sold, SUM(oi.subtotal) as total_revenue
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        JOIN products p ON oi.product_id = p.id
        WHERE o.status = 'completed'
    """ + date_filter
    top = db.execute(base_query + " GROUP BY p.id ORDER BY total_sold DESC LIMIT 3", params).fetchall()
    bottom = db.execute(base_query + " GROUP BY p.id ORDER BY total_sold ASC LIMIT 3", params).fetchall()
    return jsonify({
        'top': [{'id': r['id'], 'name': r['name'], 'sku': r['sku'], 'total_sold': r['total_sold'], 'total_revenue': r['total_revenue']} for r in top],
        'bottom': [{'id': r['id'], 'name': r['name'], 'sku': r['sku'], 'total_sold': r['total_sold'], 'total_revenue': r['total_revenue']} for r in bottom]
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
    if start_bot and os.environ.get('SHOP_ENABLE_BOT', '1').lower() not in ('0', 'false', 'no'):
        from telegram_bot import BotPoller
        BotPoller().start()

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
