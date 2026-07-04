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
    document.getElementById('stockWarning').style.display = 'none';
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

function onStockChange() {
    const pid = document.getElementById('productId').value;
    const warning = document.getElementById('stockWarning');
    if (pid) {
        warning.style.display = 'block';
    } else {
        warning.style.display = 'none';
    }
}

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
                <td><span class="badge badge-${o.status}">${o.status === 'confirmed' ? 'Payment Confirmed' : o.status}</span></td>
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
    if (!confirm('Confirm payment for this order?')) return;
    api('/api/orders/' + id + '/confirm', 'POST').then(d => {
        if (d.success) {
            showToast('Payment confirmed');
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function completeOrder(id) {
    if (!confirm('Complete this order? Stock will be deducted.')) return;
    api('/api/orders/' + id + '/complete', 'POST').then(d => {
        if (d.success) {
            showToast('Order completed');
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function cancelOrder(id) {
    if (!confirm('Cancel this order?')) return;
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
            <p><strong>Status:</strong> <span class="badge badge-${o.status}">${o.status === 'confirmed' ? 'Payment Confirmed' : o.status}</span></p>
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

/* ===== Sales Dashboard ===== */
let currentPeriod = 'today';
let trendChartInstance = null;

function loadSalesData() {
    loadSalesSummary();
    loadSalesTrend();
    loadTopProducts();
}

function loadSalesSummary() {
    fetch('/api/sales/summary?period=' + currentPeriod)
        .then(r => r.json())
        .then(d => {
            document.getElementById('stat-revenue').textContent = formatRupiah(d.total_revenue);
            document.getElementById('stat-orders').textContent = d.total_orders;
            document.getElementById('stat-skus').textContent = d.unique_skus;
            document.getElementById('stat-items').textContent = d.total_items_sold;
            document.getElementById('stat-restock-cost').textContent = formatRupiah(d.restock_cost);
            document.getElementById('stat-net-profit').textContent = formatRupiah(d.net_profit);
        });
    fetch('/api/sales/product-value')
        .then(r => r.json())
        .then(d => {
            document.getElementById('stat-product-value').textContent = formatRupiah(d.total_value);
        });
}

function loadSalesTrend() {
    fetch('/api/sales/trend?period=' + currentPeriod)
        .then(r => r.json())
        .then(d => {
            const ctx = document.getElementById('trendChart');
            if (!ctx) return;
            if (trendChartInstance) trendChartInstance.destroy();
            trendChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: d.map(p => p.date),
                    datasets: [{
                        label: 'Revenue',
                        data: d.map(p => p.revenue),
                        borderColor: '#4361ee',
                        backgroundColor: 'rgba(67,97,238,0.1)',
                        fill: true,
                        tension: 0.3
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: ctx => 'Revenue: ' + formatRupiah(ctx.raw)
                            }
                        }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            ticks: {
                                callback: v => 'Rp ' + v.toLocaleString('id-ID')
                            }
                        }
                    }
                }
            });
        });
}

function loadTopProducts() {
    fetch('/api/sales/top-products?period=' + currentPeriod)
        .then(r => r.json())
        .then(d => {
            const topBody = document.getElementById('top-sellers-body');
            const bottomBody = document.getElementById('bottom-sellers-body');
            if (d.top.length) {
                topBody.innerHTML = d.top.map(p => `
                    <tr>
                        <td>${p.name}</td>
                        <td>${p.sku || '-'}</td>
                        <td>${p.total_sold}</td>
                        <td>${formatRupiah(p.total_revenue)}</td>
                    </tr>
                `).join('');
            } else {
                topBody.innerHTML = '<tr><td colspan="4" class="empty-row">No data yet</td></tr>';
            }
            if (d.bottom.length) {
                bottomBody.innerHTML = d.bottom.map(p => `
                    <tr>
                        <td>${p.name}</td>
                        <td>${p.sku || '-'}</td>
                        <td>${p.total_sold}</td>
                        <td>${formatRupiah(p.total_revenue)}</td>
                    </tr>
                `).join('');
            } else {
                bottomBody.innerHTML = '<tr><td colspan="4" class="empty-row">No data yet</td></tr>';
            }
        });
}

document.addEventListener('click', e => {
    if (e.target.classList.contains('btn-period')) {
        document.querySelectorAll('.btn-period').forEach(b => b.classList.remove('active'));
        e.target.classList.add('active');
        if (document.getElementById('trendChart')) {
            currentPeriod = e.target.dataset.period;
            loadSalesData();
        } else if (document.getElementById('restockHistoryBody')) {
            restockPeriod = e.target.dataset.period;
            loadRestockHistory();
        }
    }
});

/* ===== Restock ===== */
function addRestockItem() {
    const idx = document.getElementById('restockItems').children.length;
    const div = document.createElement('div');
    div.className = 'restock-item-row';
    div.innerHTML = `
        <div class="form-group">
            <select id="restock-product-${idx}">
                <option value="">Select product</option>
                ${PRODUCTS.map(p => `<option value="${p.id}">${p.name} (${p.sku}) - Stock: ${p.stock}</option>`).join('')}
            </select>
        </div>
        <div class="form-group">
            <input type="number" id="restock-qty-${idx}" min="1" value="1" placeholder="Qty">
        </div>
        <div class="form-group">
            <button class="btn-remove-item" onclick="this.closest('.restock-item-row').remove();">&times;</button>
        </div>
    `;
    document.getElementById('restockItems').appendChild(div);
}

function submitRestock() {
    const rows = document.querySelectorAll('.restock-item-row');
    const items = [];
    rows.forEach(row => {
        const select = row.querySelector('select');
        const inputs = row.querySelectorAll('input');
        const pid = parseInt(select.value);
        const qty = parseInt(inputs[0].value) || 0;
        if (pid && qty > 0) {
            items.push({ product_id: pid, qty: qty });
        }
    });
    if (!items.length) return showToast('Add at least one product', 'error');
    const batchCost = parseFloat(document.getElementById('restockTotalCostInput').value) || 0;
    api('/api/restock', 'POST', { items, total_cost: batchCost }).then(d => {
        if (d.success) {
            showToast(`Restock saved! Total cost: ${formatRupiah(d.total_cost)}`);
            document.getElementById('restockItems').innerHTML = '';
            document.getElementById('restockTotalCostInput').value = '0';
            addRestockItem();
            loadRestockHistory();
        } else showToast(d.error, 'error');
    });
}

function loadRestockHistory() {
    fetch('/api/restock/history?period=' + restockPeriod)
        .then(r => r.json())
        .then(d => {
            const tbody = document.getElementById('restockHistoryBody');
            if (!d.length) {
                tbody.innerHTML = '<tr><td colspan="4" class="empty-row">No restock history yet</td></tr>';
                return;
            }
            tbody.innerHTML = d.map(b => {
                const productList = b.items.map(i => `${i.product_name} (${i.product_sku || '-'}): +${i.qty_added}`).join('<br>');
                return `
                    <tr class="restock-batch-row" onclick="this.querySelector('.restock-detail-row').style.display = this.querySelector('.restock-detail-row').style.display === 'none' ? '' : 'none'">
                        <td>Batch #${b.id}</td>
                        <td>${b.items.length} product${b.items.length > 1 ? 's' : ''}</td>
                        <td>${formatRupiah(b.total_cost)}</td>
                        <td>${b.created_at}</td>
                    </tr>
                    <tr class="restock-detail-row" style="display:none">
                        <td colspan="4" style="background:#f8f9ff;padding:12px 16px;font-size:13px;color:#555">
                            ${productList}
                        </td>
                    </tr>
                `;
            }).join('');
        });
}

/* ===== Init ===== */
document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('productsBody')) loadProducts();
    if (document.getElementById('ordersBody')) loadOrders();
    if (document.getElementById('trendChart')) loadSalesData();
    if (document.getElementById('restockItems')) {
        addRestockItem();
        loadRestockHistory();
    }
});
