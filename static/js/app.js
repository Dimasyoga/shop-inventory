/* ===== Utility Functions ===== */
function formatRupiah(amount) {
    const sign = amount < 0 ? '-' : '';
    amount = Math.abs(Math.round(amount));
    const formatted = amount.toLocaleString('id-ID').replace(/\./g, '.').replace(/,/g, '.');
    return sign + 'Rp ' + formatted;
}

function showToast(msg, type = 'success') {
    const t = document.createElement('div');
    t.className = `toast toast-${type}`;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.classList.add('show'), 10);
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 3000);
}

async function api(url, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    return res.json();
}

/* ===== Categories ===== */
function openCategoryModal(id = null, name = '') {
    document.getElementById('catId').value = id || '';
    document.getElementById('catName').value = name;
    document.getElementById('catModalTitle').textContent = id ? 'Edit Category' : 'Add Category';
    document.getElementById('categoryModal').classList.add('active');
}
function closeCategoryModal() { document.getElementById('categoryModal').classList.remove('active'); }

function editCategory(id, name) { openCategoryModal(id, name); }

function deleteCategory(id) {
    if (!confirm('Delete this category?')) return;
    api('/api/categories/' + id, 'DELETE').then(d => {
        if (d.success) {
            showToast('Category deleted');
            location.reload();
        } else showToast(d.error, 'error');
    });
}

function saveCategory(e) {
    e.preventDefault();
    const id = document.getElementById('catId').value;
    const name = document.getElementById('catName').value.trim();
    if (!name) return;
    const method = id ? 'PUT' : 'POST';
    const url = id ? '/api/categories/' + id : '/api/categories';
    api(url, method, { name }).then(d => {
        if (d.success) {
            showToast('Category saved');
            location.reload();
        } else showToast(d.error, 'error');
    });
}

/* ===== Products ===== */
function loadProducts() {
    const search = document.getElementById('searchProduct').value;
    const category = document.getElementById('filterCategory').value;
    let url = '/api/products?';
    if (search) url += 'search=' + encodeURIComponent(search) + '&';
    if (category) url += 'category=' + category + '&';
    fetch(url).then(r => r.json()).then(products => {
        const tbody = document.getElementById('productsBody');
        if (!products.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No products found</td></tr>';
            return;
        }
        tbody.innerHTML = products.map(p => `
            <tr>
                <td>${p.sku || '-'}</td>
                <td>${p.name}</td>
                <td>${p.category_name || '-'}</td>
                <td>${formatRupiah(p.price)}</td>
                <td>${p.stock_qty}</td>
                <td class="action-cell">
                    <button class="btn-icon" onclick="editProduct(${p.id})" title="Edit">✏️</button>
                    <button class="btn-icon" onclick="openStockModal(${p.id}, '${p.name}')" title="Stock">📊</button>
                    <button class="btn-icon" onclick="deleteProduct(${p.id})" title="Archive">🗑️</button>
                </td>
            </tr>
        `).join('');
    });
}

function openProductModal(id = null) {
    document.getElementById('productForm').reset();
    document.getElementById('productId').value = '';
    document.getElementById('modalTitle').textContent = 'Add Product';
    if (id) {
        fetch('/api/products').then(r => r.json()).then(products => {
            const p = products.find(x => x.id === id);
            if (p) {
                document.getElementById('productId').value = p.id;
                document.getElementById('productName').value = p.name;
                document.getElementById('productSku').value = p.sku || '';
                document.getElementById('productCategory').value = p.category_id || '';
                document.getElementById('productPrice').value = p.price;
                document.getElementById('productStock').value = p.stock_qty;
                document.getElementById('productThreshold').value = p.reorder_threshold;
                document.getElementById('modalTitle').textContent = 'Edit Product';
            }
        });
    }
    document.getElementById('productModal').classList.add('active');
}
function closeProductModal() { document.getElementById('productModal').classList.remove('active'); }

function editProduct(id) { openProductModal(id); }

function saveProduct(e) {
    e.preventDefault();
    const id = document.getElementById('productId').value;
    const data = {
        name: document.getElementById('productName').value,
        sku: document.getElementById('productSku').value,
        category_id: document.getElementById('productCategory').value || null,
        price: parseFloat(document.getElementById('productPrice').value) || 0,
        stock_qty: parseInt(document.getElementById('productStock').value) || 0,
        reorder_threshold: parseInt(document.getElementById('productThreshold').value) || 0
    };
    const method = id ? 'PUT' : 'POST';
    const url = id ? '/api/products/' + id : '/api/products';
    api(url, method, data).then(d => {
        if (d.success) {
            showToast('Product saved');
            closeProductModal();
            loadProducts();
        } else showToast(d.error, 'error');
    });
}

function deleteProduct(id) {
    if (!confirm('Archive this product?')) return;
    api('/api/products/' + id, 'DELETE').then(d => {
        if (d.success) {
            showToast('Product archived');
            loadProducts();
        } else showToast(d.error, 'error');
    });
}

/* ===== Stock Adjustment ===== */
function openStockModal(id, name) {
    document.getElementById('stockProductId').value = id;
    document.getElementById('stockProductName').textContent = name;
    document.getElementById('stockChangeQty').value = '';
    document.getElementById('stockReason').value = '';
    document.getElementById('stockModal').classList.add('active');
}
function closeStockModal() { document.getElementById('stockModal').classList.remove('active'); }

function adjustStock(e) {
    e.preventDefault();
    const data = {
        product_id: document.getElementById('stockProductId').value,
        change_qty: parseInt(document.getElementById('stockChangeQty').value),
        reason: document.getElementById('stockReason').value
    };
    api('/api/stock/adjust', 'POST', data).then(d => {
        if (d.success) {
            showToast(`Stock updated. New qty: ${d.new_qty}`);
            closeStockModal();
            loadProducts();
        } else showToast(d.error, 'error');
    });
}

/* ===== Orders ===== */
let orderItems = [];

function loadOrders() {
    const search = document.getElementById('searchOrder').value;
    const status = document.getElementById('filterStatus').value;
    let url = '/api/orders?';
    if (search) url += 'search=' + encodeURIComponent(search) + '&';
    if (status) url += 'status=' + status + '&';
    fetch(url).then(r => r.json()).then(orders => {
        const tbody = document.getElementById('ordersBody');
        if (!orders.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No orders found</td></tr>';
            return;
        }
        tbody.innerHTML = orders.map(o => `
            <tr>
                <td>Order #${o.id}</td>
                <td>${o.created_at}</td>
                <td>${o.items ? o.items.length : 0} items</td>
                <td>${formatRupiah(o.total_amount)}</td>
                <td><span class="badge badge-${o.status}">${o.status}</span></td>
                <td class="action-cell">
                    <button class="btn-icon" onclick="viewOrder(${o.id})" title="View">👁️</button>
                    ${o.status === 'draft' ? `<button class="btn-icon" onclick="confirmOrder(${o.id})" title="Confirm">✅</button>` : ''}
                    ${o.status === 'confirmed' ? `<button class="btn-icon" onclick="completeOrder(${o.id})" title="Complete">💰</button>` : ''}
                    ${o.status !== 'completed' ? `<button class="btn-icon" onclick="cancelOrder(${o.id})" title="Cancel">❌</button>` : ''}
                </td>
            </tr>
        `).join('');
    });
}

function openOrderModal() {
    orderItems = [];
    document.getElementById('orderItems').innerHTML = '';
    document.getElementById('orderTotal').textContent = 'Rp 0';
    document.getElementById('orderModal').classList.add('active');
}
function closeOrderModal() { document.getElementById('orderModal').classList.remove('active'); }

function addOrderItem() {
    const idx = document.getElementById('orderItems').children.length;
    const div = document.createElement('div');
    div.className = 'form-row';
    div.style.marginBottom = '8px';
    div.innerHTML = `
        <div class="form-group">
            <select onchange="onProductSelect(this, ${idx})">
                <option value="">Select product</option>
                ${PRODUCTS.map(p => `<option value="${p.id}" data-price="${p.price}" data-stock="${p.stock}">${p.name} (Stock: ${p.stock})</option>`).join('')}
            </select>
        </div>
        <div class="form-group">
            <input type="number" min="1" value="1" class="qty-input" data-idx="${idx}" oninput="calcOrderTotal()">
        </div>
        <div class="form-group">
            <span class="item-subtotal" data-idx="${idx}" style="font-weight:600">Rp 0</span>
        </div>
    `;
    document.getElementById('orderItems').appendChild(div);
}

function onProductSelect(sel, idx) {
    const opt = sel.options[sel.selectedIndex];
    const price = parseFloat(opt.dataset.price) || 0;
    const qtyInput = document.querySelector(`.qty-input[data-idx="${idx}"]`);
    calcOrderTotal();
}

function calcOrderTotal() {
    let total = 0;
    const selects = document.querySelectorAll('#orderItems select');
    const qtyInputs = document.querySelectorAll('.qty-input');
    const subtotals = document.querySelectorAll('.item-subtotal');
    selects.forEach((sel, i) => {
        const opt = sel.options[sel.selectedIndex];
        const price = parseFloat(opt.dataset.price) || 0;
        const qty = parseInt(qtyInputs[i].value) || 0;
        const sub = price * qty;
        total += sub;
        subtotals[i].textContent = formatRupiah(sub);
    });
    document.getElementById('orderTotal').textContent = formatRupiah(total);
}

function createOrder() {
    const selects = document.querySelectorAll('#orderItems select');
    const qtyInputs = document.querySelectorAll('.qty-input');
    const items = [];
    for (let i = 0; i < selects.length; i++) {
        const pid = parseInt(selects[i].value);
        const qty = parseInt(qtyInputs[i].value) || 0;
        if (pid && qty > 0) items.push({ product_id: pid, quantity: qty });
    }
    if (!items.length) return showToast('Add at least one item', 'error');
    api('/api/orders', 'POST', { items }).then(d => {
        if (d.success) {
            showToast(`Order #${d.order_id} created`);
            closeOrderModal();
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function confirmOrder(id) {
    if (!confirm('Confirm this order? Stock will be deducted.')) return;
    api('/api/orders/' + id + '/confirm', 'POST').then(d => {
        if (d.success) {
            showToast('Order confirmed');
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function completeOrder(id) {
    if (!confirm('Mark order as completed?')) return;
    api('/api/orders/' + id + '/complete', 'POST').then(d => {
        if (d.success) {
            showToast('Order completed');
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function cancelOrder(id) {
    if (!confirm('Cancel this order? Stock will be restored.')) return;
    api('/api/orders/' + id + '/cancel', 'POST').then(d => {
        if (d.success) {
            showToast('Order cancelled');
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function viewOrder(id) {
    fetch('/api/orders?').then(r => r.json()).then(orders => {
        const o = orders.find(x => x.id === id);
        if (!o) return;
        document.getElementById('detailOrderId').textContent = `Order #${o.id}`;
        let html = `
            <p><strong>Status:</strong> <span class="badge badge-${o.status}">${o.status}</span></p>
            <p><strong>Date:</strong> ${o.created_at}</p>
            <table class="data-table" style="margin:12px 0">
                <thead><tr><th>Product</th><th>Qty</th><th>Price</th><th>Subtotal</th></tr></thead>
                <tbody>
                    ${(o.items || []).map(i => `
                        <tr>
                            <td>${i.product_name}</td>
                            <td>${i.quantity}</td>
                            <td>${formatRupiah(i.unit_price)}</td>
                            <td>${formatRupiah(i.subtotal)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
            <p style="text-align:right;font-size:18px;font-weight:700">Total: ${formatRupiah(o.total_amount)}</p>
        `;
        document.getElementById('orderDetailContent').innerHTML = html;
        document.getElementById('orderDetailModal').classList.add('active');
    });
}
function closeOrderDetail() { document.getElementById('orderDetailModal').classList.remove('active'); }

/* ===== Init ===== */
document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('productsBody')) loadProducts();
    if (document.getElementById('ordersBody')) loadOrders();
});
