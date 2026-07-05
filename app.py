from flask import Flask, request, jsonify, render_template, redirect, url_for, session, g
from database import get_db, init_db
from functools import wraps
from datetime import datetime, timezone
import math

app = Flask(__name__)
app.secret_key = 'shop-inventory-secret-key-2024'

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def format_rupiah(amount):
    """Format number as Indonesian Rupiah: Rp 150.000"""
    sign = '-' if amount < 0 else ''
    amount = abs(int(round(amount)))
    formatted = f'{amount:,}'.replace(',', '.')
    return f'{sign}Rp {formatted}'

@app.template_filter('format_datetime')
def format_datetime(utc_str):
    dt = datetime.fromisoformat(utc_str).replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')

@app.before_request
def before_request():
    g.db = get_db()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        db = g.db
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and user['password'] == password:
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
    total_orders = db.execute("SELECT COUNT(*) as cnt FROM orders").fetchone()['cnt']
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
        SELECT o.*, p.name as product_name
        FROM orders o
        LEFT JOIN order_items oi ON o.id = oi.order_id
        LEFT JOIN products p ON oi.product_id = p.id
        ORDER BY o.created_at DESC
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
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    try:
        g.db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        g.db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/categories/<int:id>', methods=['PUT'])
@login_required
def api_update_category(id):
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    try:
        g.db.execute("UPDATE categories SET name = ? WHERE id = ?", (name, id))
        g.db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

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
    data = request.get_json()
    try:
        cur = g.db.execute("""
            INSERT INTO products (name, sku, category_id, price, stock_qty, reorder_threshold)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data.get('name', '').strip(),
            data.get('sku', '').strip() or None,
            data.get('category_id') or None,
            float(data.get('price', 0)),
            int(data.get('stock_qty', 0)),
            int(data.get('reorder_threshold', 0))
        ))
        g.db.commit()
        return jsonify({'success': True, 'id': cur.lastrowid})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/products/<int:id>', methods=['PUT'])
@login_required
def api_update_product(id):
    data = request.get_json()
    try:
        g.db.execute("""
            UPDATE products SET name=?, sku=?, category_id=?, price=?, stock_qty=?, reorder_threshold=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            data.get('name', '').strip(),
            data.get('sku', '').strip() or None,
            data.get('category_id') or None,
            float(data.get('price', 0)),
            int(data.get('stock_qty', 0)),
            int(data.get('reorder_threshold', 0)),
            id
        ))
        g.db.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

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
    data = request.get_json()
    product_id = data.get('product_id')
    change_qty = int(data.get('change_qty', 0))
    reason = data.get('reason', '').strip()
    if not product_id or change_qty == 0:
        return jsonify({'error': 'Product ID and quantity required'}), 400
    product = g.db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    new_qty = product['stock_qty'] + change_qty
    if new_qty < 0:
        return jsonify({'error': 'Insufficient stock'}), 400
    g.db.execute("UPDATE products SET stock_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_qty, product_id))
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
    data = request.get_json()
    items = data.get('items', [])
    if not items:
        return jsonify({'error': 'At least one item required'}), 400
    total = 0
    product_ids = set()
    for item in items:
        product = g.db.execute("SELECT * FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        if not product:
            return jsonify({'error': f"Product {item['product_id']} not found"}), 404
        if product['stock_qty'] < item['quantity']:
            return jsonify({'error': f"Insufficient stock for {product['name']}"}), 400
        subtotal = product['price'] * item['quantity']
        total += subtotal
        product_ids.add(product['id'])

    cur = g.db.execute("INSERT INTO orders (status, total_amount) VALUES (?, ?)", ('draft', total))
    order_id = cur.lastrowid

    for item in items:
        product = g.db.execute("SELECT * FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        subtotal = product['price'] * item['quantity']
        g.db.execute("""
            INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)
            VALUES (?, ?, ?, ?, ?)
        """, (order_id, item['product_id'], item['quantity'], product['price'], subtotal))

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
        current = g.db.execute("SELECT stock_qty FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        new_qty = current['stock_qty'] - item['quantity']
        if new_qty < 0:
            g.db.rollback()
            return jsonify({'error': f"Insufficient stock for product #{item['product_id']}"}, 400)
        g.db.execute("UPDATE products SET stock_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_qty, item['product_id']))
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
    g.db.execute("DELETE FROM order_items WHERE order_id = ?", (id,))
    g.db.execute("DELETE FROM orders WHERE id = ?", (id,))
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
    data = request.get_json()
    items = data.get('items', [])
    batch_total_cost = float(data.get('total_cost', 0))
    if not items:
        return jsonify({'error': 'At least one item required'}), 400
    total_qty = 0
    for item in items:
        product_id = item.get('product_id')
        qty_added = int(item.get('qty', 0))
        if not product_id or qty_added <= 0:
            return jsonify({'error': 'Valid product and quantity required'}), 400
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
        product = g.db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        new_qty = product['stock_qty'] + qty_added
        g.db.execute("UPDATE products SET stock_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_qty, product_id))
        g.db.execute("INSERT INTO restock_items (batch_id, product_id, qty_added, allocated_cost) VALUES (?, ?, ?, ?)",
                     (batch_id, product_id, qty_added, allocated_cost))
    g.db.commit()
    return jsonify({'success': True, 'total_cost': batch_total_cost})

@app.route('/api/restock/history', methods=['GET'])
@login_required
def api_restock_history():
    period = request.args.get('period', 'all')
    query = """
        SELECT rb.id, rb.total_cost, rb.created_at
        FROM restock_batches rb
        ORDER BY rb.created_at DESC
    """
    params = []
    if period == 'today':
        query += " WHERE date(rb.created_at) = date('now')"
    elif period == 'week':
        query += " WHERE rb.created_at >= date('now', '-7 days')"
    elif period == 'month':
        query += " WHERE rb.created_at >= date('now', '-30 days')"
    elif period == 'year':
        query += " WHERE rb.created_at >= date('now', '-365 days')"
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

@app.route('/api/sales/summary', methods=['GET'])
@login_required
def api_sales_summary():
    period = request.args.get('period', 'all')
    db = g.db
    query = """
        SELECT
            COALESCE(SUM(o.total_amount), 0) as total_revenue,
            COUNT(DISTINCT o.id) as total_orders,
            COUNT(DISTINCT oi.product_id) as unique_skus,
            COALESCE(SUM(oi.quantity), 0) as total_items_sold
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        WHERE o.status = 'completed'
    """
    params = []
    if period == 'today':
        query += " AND date(o.created_at) = date('now')"
    elif period == 'week':
        query += " AND o.created_at >= date('now', '-7 days')"
    elif period == 'month':
        query += " AND o.created_at >= date('now', '-30 days')"
    elif period == 'year':
        query += " AND o.created_at >= date('now', '-365 days')"
    row = db.execute(query, params).fetchone()
    restock_query = "SELECT COALESCE(SUM(total_cost), 0) as total FROM restock_batches"
    if period == 'today':
        restock_query += " WHERE date(created_at) = date('now')"
    elif period == 'week':
        restock_query += " WHERE created_at >= date('now', '-7 days')"
    elif period == 'month':
        restock_query += " WHERE created_at >= date('now', '-30 days')"
    elif period == 'year':
        restock_query += " WHERE created_at >= date('now', '-365 days')"
    restock_cost = db.execute(restock_query).fetchone()['total']
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
    period = request.args.get('period', 'all')
    db = g.db
    query = """
        SELECT date(o.created_at) as sale_date, SUM(o.total_amount) as daily_revenue
        FROM orders o
        WHERE o.status = 'completed'
    """
    params = []
    if period == 'today':
        query += " AND date(o.created_at) = date('now')"
    elif period == 'week':
        query += " AND o.created_at >= date('now', '-7 days')"
    elif period == 'month':
        query += " AND o.created_at >= date('now', '-30 days')"
    elif period == 'year':
        query += " AND o.created_at >= date('now', '-365 days')"
    query += " GROUP BY date(o.created_at) ORDER BY sale_date ASC"
    rows = db.execute(query, params).fetchall()
    return jsonify([{'date': r['sale_date'], 'revenue': r['daily_revenue']} for r in rows])

@app.route('/api/sales/top-products', methods=['GET'])
@login_required
def api_sales_top_products():
    period = request.args.get('period', 'all')
    db = g.db
    base_query = """
        SELECT p.id, p.name, p.sku, SUM(oi.quantity) as total_sold, SUM(oi.subtotal) as total_revenue
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        JOIN products p ON oi.product_id = p.id
        WHERE o.status = 'completed'
    """
    params = []
    if period == 'today':
        base_query += " AND date(o.created_at) = date('now')"
    elif period == 'week':
        base_query += " AND o.created_at >= date('now', '-7 days')"
    elif period == 'month':
        base_query += " AND o.created_at >= date('now', '-30 days')"
    elif period == 'year':
        base_query += " AND o.created_at >= date('now', '-365 days')"
    top = db.execute(base_query + " GROUP BY p.id ORDER BY total_sold DESC LIMIT 3", params).fetchall()
    bottom = db.execute(base_query + " GROUP BY p.id ORDER BY total_sold ASC LIMIT 3", params).fetchall()
    return jsonify({
        'top': [{'id': r['id'], 'name': r['name'], 'sku': r['sku'], 'total_sold': r['total_sold'], 'total_revenue': r['total_revenue']} for r in top],
        'bottom': [{'id': r['id'], 'name': r['name'], 'sku': r['sku'], 'total_sold': r['total_sold'], 'total_revenue': r['total_revenue']} for r in bottom]
    })

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
