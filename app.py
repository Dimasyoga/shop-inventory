from flask import Flask, request, jsonify, render_template, redirect, url_for, session, g
from database import get_db, init_db
from functools import wraps
from datetime import datetime, timedelta, timezone, date, time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from werkzeug.security import check_password_hash
import sqlite3
import os
import secrets

_SECRET_PATH = os.path.join(os.path.dirname(__file__), '.secret_key')

def _load_secret_key():
    """Persisted random secret so sessions survive restarts without a key in the repo."""
    try:
        with open(_SECRET_PATH, 'rb') as f:
            key = f.read()
        if key:
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(32)
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

def format_rupiah(amount):
    """Format number as Indonesian Rupiah: Rp 150.000"""
    sign = '-' if amount < 0 else ''
    amount = abs(int(round(amount)))
    formatted = f'{amount:,}'.replace(',', '.')
    return f'{sign}Rp {formatted}'

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
        return render_template('login.html', error='Invalid credentials')
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
    month_revenue = db.execute("""
        SELECT COALESCE(SUM(total_amount), 0) as total FROM orders
        WHERE status = 'completed' AND created_at >= date('now', '-30 days')
    """).fetchone()['total']
    total_restock_cost = db.execute("""
        SELECT COALESCE(SUM(total_cost), 0) as total FROM restock_batches
        WHERE created_at >= date('now', '-30 days')
    """).fetchone()['total']
    net_profit = month_revenue - total_restock_cost
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
        month_revenue=format_rupiah(month_revenue),
        net_profit=format_rupiah(net_profit),
        total_product_value=format_rupiah(total_product_value),
        total_restock_cost_raw=total_restock_cost,
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
        return jsonify({'error': 'Invalid JSON body'}), 400
    name = data.get('name')
    name = name.strip() if isinstance(name, str) else ''
    if not name:
        return jsonify({'error': 'Name required'}), 400
    try:
        g.db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        g.db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Category already exists'}), 400

@app.route('/api/categories/<int:id>', methods=['PUT'])
@login_required
def api_update_category(id):
    data = _json_body()
    if data is None:
        return jsonify({'error': 'Invalid JSON body'}), 400
    name = data.get('name')
    name = name.strip() if isinstance(name, str) else ''
    if not name:
        return jsonify({'error': 'Name required'}), 400
    try:
        g.db.execute("UPDATE categories SET name = ? WHERE id = ?", (name, id))
        g.db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Category already exists'}), 400

@app.route('/api/categories/<int:id>', methods=['DELETE'])
@login_required
def api_delete_category(id):
    used = g.db.execute("SELECT COUNT(*) as cnt FROM products WHERE category_id = ?", (id,)).fetchone()['cnt']
    if used > 0:
        return jsonify({'error': 'Category has products assigned'}), 400
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
        return jsonify({'error': 'Invalid JSON body'}), 400
    fields, err = _validate_product(data)
    if err:
        return jsonify({'error': err}), 400
    try:
        stock_qty = int(data.get('stock_qty', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Stock must be a whole number'}), 400
    if stock_qty < 0:
        return jsonify({'error': 'Stock must be 0 or more'}), 400
    try:
        cur = g.db.execute("""
            INSERT INTO products (name, sku, category_id, price, stock_qty, reorder_threshold)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (fields['name'], fields['sku'], fields['category_id'],
              fields['price'], stock_qty, fields['reorder_threshold']))
        g.db.commit()
        return jsonify({'success': True, 'id': cur.lastrowid})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'SKU already exists'}), 400

@app.route('/api/products/<int:id>', methods=['PUT'])
@login_required
def api_update_product(id):
    data = _json_body()
    if data is None:
        return jsonify({'error': 'Invalid JSON body'}), 400
    fields, err = _validate_product(data)
    if err:
        return jsonify({'error': err}), 400
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
        return jsonify({'error': 'SKU already exists'}), 400

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
        return jsonify({'error': 'Invalid JSON body'}), 400
    product_id = data.get('product_id')
    change_qty = data.get('change_qty')
    reason = data.get('reason')
    reason = reason.strip() if isinstance(reason, str) else ''
    if not product_id or not isinstance(change_qty, int) or isinstance(change_qty, bool) or change_qty == 0:
        return jsonify({'error': 'Product ID and a non-zero whole-number quantity required'}), 400
    product = g.db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    cur = g.db.execute(
        "UPDATE products SET stock_qty = stock_qty + ?, updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ? AND stock_qty + ? >= 0",
        (change_qty, product_id, change_qty))
    if cur.rowcount == 0:
        g.db.rollback()
        return jsonify({'error': 'Insufficient stock'}), 400
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
    return render_template('orders.html', orders=orders, products=products, format_rupiah=format_rupiah)

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
        return jsonify({'error': 'Invalid JSON body'}), 400
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'At least one item required'}), 400
    for item in items:
        pid = item.get('product_id') if isinstance(item, dict) else None
        qty = item.get('quantity') if isinstance(item, dict) else None
        # bool is an int subclass: True would slip through as quantity 1
        if not isinstance(pid, int) or isinstance(pid, bool) \
                or not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return jsonify({'error': 'Each item needs a product_id and a positive whole-number quantity'}), 400

    total = 0
    rows = []
    for item in items:
        product = g.db.execute("SELECT * FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        if not product:
            return jsonify({'error': f"Product {item['product_id']} not found"}), 404
        if product['stock_qty'] < item['quantity']:
            return jsonify({'error': f"Insufficient stock for {product['name']}"}), 400
        subtotal = product['price'] * item['quantity']
        total += subtotal
        rows.append((item['product_id'], item['quantity'], product['price'], subtotal))

    cur = g.db.execute("INSERT INTO orders (status, total_amount) VALUES (?, ?)", ('draft', total))
    order_id = cur.lastrowid
    g.db.executemany("""
        INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)
        VALUES (?, ?, ?, ?, ?)
    """, [(order_id, pid, qty, price, subtotal) for pid, qty, price, subtotal in rows])

    g.db.commit()
    return jsonify({'success': True, 'order_id': order_id, 'total': total})

@app.route('/api/orders/<int:id>/confirm', methods=['POST'])
@login_required
def api_confirm_order(id):
    order = g.db.execute("SELECT * FROM orders WHERE id = ?", (id,)).fetchone()
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    if order['status'] != 'draft':
        return jsonify({'error': 'Only draft orders can be confirmed'}), 400
    g.db.execute("UPDATE orders SET status = 'confirmed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (id,))
    g.db.commit()
    return jsonify({'success': True})

@app.route('/api/orders/<int:id>/complete', methods=['POST'])
@login_required
def api_complete_order(id):
    order = g.db.execute("SELECT * FROM orders WHERE id = ?", (id,)).fetchone()
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    if order['status'] != 'confirmed':
        return jsonify({'error': 'Only confirmed orders can be completed'}), 400
    items = g.db.execute("SELECT * FROM order_items WHERE order_id = ?", (id,)).fetchall()
    for item in items:
        # Conditional decrement is atomic: no read-modify-write window for concurrent
        # requests to double-count against.
        cur = g.db.execute(
            "UPDATE products SET stock_qty = stock_qty - ?, updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ? AND stock_qty >= ?",
            (item['quantity'], item['product_id'], item['quantity']))
        if cur.rowcount == 0:
            g.db.rollback()
            return jsonify({'error': f"Insufficient stock for product #{item['product_id']}"}), 400
        g.db.execute("INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (?, ?, ?)",
                     (item['product_id'], -item['quantity'], f'sale order #{id}'))
    g.db.execute("UPDATE orders SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (id,))
    g.db.commit()
    return jsonify({'success': True})

@app.route('/api/orders/<int:id>/cancel', methods=['POST'])
@login_required
def api_cancel_order(id):
    order = g.db.execute("SELECT * FROM orders WHERE id = ?", (id,)).fetchone()
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    if order['status'] == 'completed':
        return jsonify({'error': 'Cannot cancel completed orders'}), 400
    if order['status'] == 'cancelled':
        return jsonify({'error': 'Order already cancelled'}), 400
    g.db.execute("UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (id,))
    g.db.commit()
    return jsonify({'success': True})

# --- Restock ---
@app.route('/restock')
@login_required
def restock_page():
    products = g.db.execute("SELECT * FROM products WHERE is_archived = 0 AND stock_qty >= 0 ORDER BY name").fetchall()
    return render_template('restock.html', products=products, format_rupiah=format_rupiah)

@app.route('/api/restock', methods=['POST'])
@login_required
def api_restock():
    data = _json_body()
    if data is None:
        return jsonify({'error': 'Invalid JSON body'}), 400
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'At least one item required'}), 400
    batch_total_cost = data.get('total_cost', 0)
    if isinstance(batch_total_cost, bool) or not isinstance(batch_total_cost, (int, float)) or not (batch_total_cost >= 0):
        return jsonify({'error': 'Total cost must be 0 or more'}), 400
    batch_total_cost = float(batch_total_cost)
    total_qty = 0
    for item in items:
        product_id = item.get('product_id') if isinstance(item, dict) else None
        qty_added = item.get('qty') if isinstance(item, dict) else None
        if not product_id or not isinstance(qty_added, int) or isinstance(qty_added, bool) or qty_added <= 0:
            return jsonify({'error': 'Valid product and positive whole-number quantity required'}), 400
        product = g.db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not product:
            return jsonify({'error': f"Product {product_id} not found"}), 404
        total_qty += qty_added
    cur = g.db.execute("INSERT INTO restock_batches (total_cost) VALUES (?)", (batch_total_cost,))
    batch_id = cur.lastrowid
    for item in items:
        product_id = item.get('product_id')
        qty_added = int(item.get('qty', 0))
        allocated_cost = (qty_added / total_qty) * batch_total_cost if total_qty > 0 else 0
        cur = g.db.execute(
            "UPDATE products SET stock_qty = stock_qty + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (qty_added, product_id))
        if cur.rowcount == 0:
            g.db.rollback()
            return jsonify({'error': f'Product {product_id} not found'}), 404
        g.db.execute("INSERT INTO restock_items (batch_id, product_id, qty_added, allocated_cost) VALUES (?, ?, ?, ?)",
                     (batch_id, product_id, qty_added, allocated_cost))
        g.db.execute("INSERT INTO stock_logs (product_id, change_qty, reason) VALUES (?, ?, ?)",
                     (product_id, qty_added, f'restock batch #{batch_id}'))
    g.db.commit()
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
        return jsonify({'error': 'invalid period'}), 400
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

def _int_arg(name, default=0):
    """Read an int query param. Returns None when unparseable so callers can 400."""
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return None

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

@app.route('/api/sales/summary', methods=['GET'])
@login_required
def api_sales_summary():
    unit = request.args.get('unit', 'month')
    offset = _int_arg('offset')
    if offset is None:
        return jsonify({'error': 'invalid offset'}), 400
    tz = _client_tz(request.args.get('tz'))
    start, end = get_date_range(unit, offset, tz)
    if not start:
        return jsonify({'error': 'invalid unit'}), 400

    db = g.db
    date_filter, params = build_date_filter(start, end)
    query = """
        SELECT
            COALESCE(SUM(o.total_amount), 0) as total_revenue,
            COUNT(DISTINCT o.id) as total_orders,
            COUNT(DISTINCT oi.product_id) as unique_skus,
            COALESCE(SUM(oi.quantity), 0) as total_items_sold
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        WHERE o.status = 'completed'
    """ + date_filter
    row = db.execute(query, params).fetchone()

    restock_filter, restock_params = build_date_filter(start, end, 'created_at')
    restock_cost = db.execute(
        "SELECT COALESCE(SUM(total_cost), 0) as total FROM restock_batches WHERE 1=1" + restock_filter,
        restock_params
    ).fetchone()['total']

    return jsonify({
        'total_revenue': row['total_revenue'],
        'total_orders': row['total_orders'],
        'unique_skus': row['unique_skus'],
        'total_items_sold': row['total_items_sold'],
        'restock_cost': restock_cost,
        'net_profit': row['total_revenue'] - restock_cost
    })

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
        return jsonify({'error': 'invalid offset'}), 400
    tz = _client_tz(request.args.get('tz'))
    start, end = get_date_range(unit, offset, tz)
    if not start:
        return jsonify({'error': 'invalid unit'}), 400

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

    if unit == 'month':
        return jsonify([{'label': f'Week {k}', 'revenue': v} for k, v in sorted(buckets.items())])
    elif unit == 'year':
        month_names = {f"{m:02d}": n for m, n in enumerate(
            ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}
        return jsonify([{'label': month_names.get(k, k), 'revenue': v} for k, v in sorted(buckets.items())])
    return jsonify([{'label': k, 'revenue': v} for k, v in sorted(buckets.items())])

@app.route('/api/sales/top-products', methods=['GET'])
@login_required
def api_sales_top_products():
    unit = request.args.get('unit', 'month')
    offset = _int_arg('offset')
    if offset is None:
        return jsonify({'error': 'invalid offset'}), 400
    tz = _client_tz(request.args.get('tz'))
    start, end = get_date_range(unit, offset, tz)
    if not start:
        return jsonify({'error': 'invalid unit'}), 400

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

if __name__ == '__main__':
    init_db()
    # debug exposes the Werkzeug console (remote code execution) to anyone on the
    # network; it must never default on for a 0.0.0.0 bind.
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(host='0.0.0.0', port=5000, debug=debug)
