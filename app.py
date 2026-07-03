from flask import Flask, request, jsonify, render_template, redirect, url_for, session, g
from database import get_db, init_db
from functools import wraps
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
    today_revenue = db.execute("""
        SELECT COALESCE(SUM(total_amount), 0) as total FROM orders
        WHERE status = 'completed' AND date(created_at) = date('now')
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
        today_revenue=format_rupiah(today_revenue),
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
    items = g.db.execute("SELECT * FROM order_items WHERE order_id = ?", (id,)).fetchall()
    for item in items:
        current = g.db.execute("SELECT stock_qty FROM products WHERE id = ?", (item['product_id'],)).fetchone()
        new_qty = current['stock_qty'] - item['quantity']
        g.db.execute("UPDATE products SET stock_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_qty, item['product_id']))
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
    g.db.execute("UPDATE orders SET status = 'completed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (id,))
    g.db.commit()
    return jsonify({'success': True})

@app.route('/api/orders/<int:id>/cancel', methods=['POST'])
@login_required
def api_cancel_order(id):
    order = g.db.execute("SELECT * FROM orders WHERE id = ?", (id,)).fetchone()
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    if order['status'] not in ('draft', 'confirmed'):
        return jsonify({'error': 'Cannot cancel completed orders'}), 400
    if order['status'] == 'confirmed':
        items = g.db.execute("SELECT * FROM order_items WHERE order_id = ?", (id,)).fetchall()
        for item in items:
            current = g.db.execute("SELECT stock_qty FROM products WHERE id = ?", (item['product_id'],)).fetchone()
            new_qty = current['stock_qty'] + item['quantity']
            g.db.execute("UPDATE products SET stock_qty = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_qty, item['product_id']))
    g.db.execute("DELETE FROM order_items WHERE order_id = ?", (id,))
    g.db.execute("DELETE FROM orders WHERE id = ?", (id,))
    g.db.commit()
    return jsonify({'success': True})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
