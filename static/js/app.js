/* ===== i18n ===== */
/* window.I18N (source string -> translation) and window.LANG are injected by
   base.html. Missing keys fall back to the source string. Placeholders use the
   same {name} tokens as the server-side translator. */
function t(source, params) {
    let out = (window.I18N && window.I18N[source]) || source;
    if (params) out = out.replace(/\{(\w+)\}/g, (m, k) => (params[k] != null ? params[k] : m));
    return out;
}

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

const CLIENT_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
/* BCP-47 locale for date labels, derived from the UI language (window.LANG). */
const DATE_LOCALE = window.LANG === 'id' ? 'id-ID' : 'en-US';

/* User-entered text (product names, SKUs, ...) must be escaped before being
   interpolated into innerHTML, or it executes as markup. */
function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

async function api(url, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || `Request failed (${res.status})`);
    }
    return res.json();
}

/* Wraps a fetch of JSON that renders into the page, surfacing failures as a toast
   instead of an unhandled rejection. */
function fetchJson(url) {
    return fetch(url).then(res => {
        if (!res.ok) throw new Error(`Request failed (${res.status})`);
        return res.json();
    }).catch(err => {
        showToast(err.message, 'error');
        throw err;
    });
}

/* ===== Settings ===== */
function saveLanguage(e) {
    e.preventDefault();
    api('/api/settings/language', 'POST', {
        language: document.getElementById('uiLanguage').value
    }).then(() => {
        // Reload so the server re-renders every string in the new language.
        location.reload();
    }).catch(err => showToast(err.message, 'error'));
}

function saveTelegramSettings(e) {
    e.preventDefault();
    api('/api/settings/telegram', 'POST', {
        enabled: document.getElementById('tgEnabled').checked,
        token: document.getElementById('tgToken').value,
        whitelist: document.getElementById('tgWhitelist').value,
        timezone: document.getElementById('tgTimezone').value
    }).then(d => {
        showToast(d.warning || t('Telegram settings saved'), d.warning ? 'error' : 'success');
        document.getElementById('tgToken').value = '';
        document.getElementById('tgToken').placeholder = t('Saved — leave blank to keep');
    }).catch(err => showToast(err.message, 'error'));
}

function testTelegramConnection() {
    const result = document.getElementById('tgTestResult');
    result.textContent = t('Testing…');
    api('/api/settings/telegram/test', 'POST', {
        token: document.getElementById('tgToken').value
    }).then(d => {
        result.textContent = '✅ ' + t('Connected as @{name}', { name: d.bot_username });
    }).catch(err => {
        result.textContent = `❌ ${err.message}`;
    });
}

function saveAccount(e) {
    e.preventDefault();
    const newPass = document.getElementById('accNew').value;
    if (newPass && newPass !== document.getElementById('accConfirm').value) {
        showToast(t('New passwords do not match'), 'error');
        return;
    }
    api('/api/settings/account', 'POST', {
        current_password: document.getElementById('accCurrent').value,
        new_username: document.getElementById('accUsername').value,
        new_password: newPass
    }).then(() => {
        showToast(t('Account updated'));
        document.getElementById('accCurrent').value = '';
        document.getElementById('accNew').value = '';
        document.getElementById('accConfirm').value = '';
    }).catch(err => showToast(err.message, 'error'));
}

/* ===== Categories ===== */
function openCategoryModal(id = null, name = '') {
    document.getElementById('catId').value = id || '';
    document.getElementById('catName').value = name;
    document.getElementById('catModalTitle').textContent = id ? t('Edit Category') : t('Add Category');
    document.getElementById('categoryModal').classList.add('active');
}
function closeCategoryModal() { document.getElementById('categoryModal').classList.remove('active'); }

function editCategory(id, name) { openCategoryModal(id, name); }

function deleteCategory(id) {
    if (!confirm(t('Delete this category?'))) return;
    api('/api/categories/' + id, 'DELETE').then(d => {
        if (d.success) {
            showToast(t('Category deleted'));
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
            showToast(t('Category saved'));
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
            tbody.innerHTML = `<tr><td colspan="6" class="empty-row">${t('No products found')}</td></tr>`;
            return;
        }
        tbody.innerHTML = products.map(p => `
            <tr>
                <td>${escapeHtml(p.sku || '-')}</td>
                <td>${escapeHtml(p.name)}</td>
                <td>${escapeHtml(p.category_name || '-')}</td>
                <td>${formatRupiah(p.price)}</td>
                <td>${p.stock_qty}</td>
                <td class="action-cell">
                    <button class="btn-icon" onclick="editProduct(${p.id})" title="${t('Edit')}">✏️</button>
                    <button class="btn-icon" onclick="deleteProduct(${p.id})" title="${t('Archive')}">🗑️</button>
                </td>
            </tr>
        `).join('');
    });
}

function openProductModal(id = null) {
    document.getElementById('productForm').reset();
    document.getElementById('productId').value = '';
    document.getElementById('modalTitle').textContent = t('Add Product');
    document.getElementById('productStock').disabled = false;
    document.getElementById('stockWarning').style.display = 'none';
    if (id) {
        fetchJson('/api/products').then(products => {
            const p = products.find(x => x.id === id);
            if (p) {
                document.getElementById('productId').value = p.id;
                document.getElementById('productName').value = p.name;
                document.getElementById('productSku').value = p.sku || '';
                document.getElementById('productCategory').value = p.category_id || '';
                document.getElementById('productPrice').value = p.price;
                document.getElementById('productStock').value = p.stock_qty;
                document.getElementById('productStock').disabled = true;
                document.getElementById('stockWarning').style.display = 'block';
                document.getElementById('productThreshold').value = p.reorder_threshold;
                document.getElementById('modalTitle').textContent = t('Edit Product');
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
        reorder_threshold: parseInt(document.getElementById('productThreshold').value) || 0
    };
    if (!id) {
        data.stock_qty = parseInt(document.getElementById('productStock').value) || 0;
    }
    const method = id ? 'PUT' : 'POST';
    const url = id ? '/api/products/' + id : '/api/products';
    api(url, method, data).then(d => {
        if (d.success) {
            showToast(t('Product saved'));
            closeProductModal();
            loadProducts();
        } else showToast(d.error, 'error');
    });
}

function deleteProduct(id) {
    if (!confirm(t('Archive this product?'))) return;
    api('/api/products/' + id, 'DELETE').then(d => {
        if (d.success) {
            showToast(t('Product archived'));
            loadProducts();
        } else showToast(d.error, 'error');
    });
}

/* ===== Orders ===== */
function formatLocalDate(utcStr) {
    return new Date(utcStr.replace(' ', 'T') + 'Z').toLocaleString();
}

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
            tbody.innerHTML = `<tr><td colspan="6" class="empty-row">${t('No orders found')}</td></tr>`;
            return;
        }
        tbody.innerHTML = orders.map(o => `
            <tr>
                <td>${o.id}</td>
                <td>${formatLocalDate(o.created_at)}</td>
                <td>${t('{n} items', { n: o.items ? o.items.length : 0 })}</td>
                <td>${formatRupiah(o.total_amount)}</td>
                <td><span class="badge badge-${o.status}">${o.status === 'confirmed' ? t('Payment Confirmed') : t(o.status)}</span></td>
                <td class="action-cell">
                    <button class="btn-icon" onclick="viewOrder(${o.id})" title="${t('View')}">👁️</button>
                    ${o.status === 'draft' ? `<button class="btn-icon" onclick="confirmOrder(${o.id})" title="${t('Confirm')}">✅</button>` : ''}
                    ${o.status === 'confirmed' ? `<button class="btn-icon" onclick="completeOrder(${o.id})" title="${t('Complete')}">💰</button>` : ''}
                    ${o.status === 'draft' || o.status === 'confirmed' ? `<button class="btn-icon" onclick="cancelOrder(${o.id})" title="${t('Cancel')}">❌</button>` : ''}
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
                <option value="">${t('Select product')}</option>
                ${PRODUCTS.map(p => `<option value="${p.id}" data-price="${p.price}" data-stock="${p.stock}">${escapeHtml(p.name)} (${t('Stock: {n}', { n: p.stock })})</option>`).join('')}
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
    if (!items.length) return showToast(t('Add at least one item'), 'error');
    api('/api/orders', 'POST', { items }).then(d => {
        if (d.success) {
            showToast(t('Order ID {id} created', { id: d.order_id }));
            closeOrderModal();
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function confirmOrder(id) {
    if (!confirm(t('Confirm payment for this order?'))) return;
    api('/api/orders/' + id + '/confirm', 'POST').then(d => {
        if (d.success) {
            showToast(t('Payment confirmed'));
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function completeOrder(id) {
    if (!confirm(t('Complete this order? Stock will be deducted.'))) return;
    api('/api/orders/' + id + '/complete', 'POST').then(d => {
        if (d.success) {
            showToast(t('Order completed'));
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function cancelOrder(id) {
    if (!confirm(t('Cancel this order?'))) return;
    api('/api/orders/' + id + '/cancel', 'POST').then(d => {
        if (d.success) {
            showToast(t('Order cancelled'));
            loadOrders();
        } else showToast(d.error, 'error');
    });
}

function viewOrder(id) {
    fetch('/api/orders?').then(r => r.json()).then(orders => {
        const o = orders.find(x => x.id === id);
        if (!o) return;
        document.getElementById('detailOrderId').textContent = t('Order ID {id}', { id: o.id });
        let html = `
            <p><strong>${t('Status')}:</strong> <span class="badge badge-${o.status}">${o.status === 'confirmed' ? t('Payment Confirmed') : t(o.status)}</span></p>
            <p><strong>${t('Date')}:</strong> ${formatLocalDate(o.created_at)}</p>
            <table class="data-table" style="margin:12px 0">
                <thead><tr><th>${t('Product')}</th><th>${t('Qty')}</th><th>${t('Price')}</th><th>${t('Subtotal')}</th></tr></thead>
                <tbody>
                    ${(o.items || []).map(i => `
                        <tr>
                            <td>${escapeHtml(i.product_name)}</td>
                            <td>${i.quantity}</td>
                            <td>${formatRupiah(i.unit_price)}</td>
                            <td>${formatRupiah(i.subtotal)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
            <p style="text-align:right;font-size:18px;font-weight:700">${t('Total')}: ${formatRupiah(o.total_amount)}</p>
        `;
        document.getElementById('orderDetailContent').innerHTML = html;
        document.getElementById('orderDetailModal').classList.add('active');
    });
}
function closeOrderDetail() { document.getElementById('orderDetailModal').classList.remove('active'); }

/* ===== Sales Dashboard ===== */
let timeUnit = 'month';
let timeOffset = 0;
let trendChartInstance = null;

function buildSalesParams() {
    return `unit=${timeUnit}&offset=${timeOffset}&tz=${encodeURIComponent(CLIENT_TZ)}`;
}

function loadSalesData() {
    updateTimeLabel();
    loadSalesSummary();
    loadSalesTrend();
    loadTopProducts();
}

function loadSalesSummary() {
    fetchJson('/api/sales/summary?' + buildSalesParams())
        .then(d => {
            document.getElementById('stat-revenue').textContent = formatRupiah(d.total_revenue);
            document.getElementById('stat-orders').textContent = d.total_orders;
            document.getElementById('stat-skus').textContent = d.unique_skus;
            document.getElementById('stat-items').textContent = d.total_items_sold;
            document.getElementById('stat-restock-cost').textContent = formatRupiah(d.restock_cost);
            document.getElementById('stat-net-profit').textContent = formatRupiah(d.net_profit);
        });
    fetchJson('/api/sales/product-value')
        .then(d => {
            document.getElementById('stat-product-value').textContent = formatRupiah(d.total_value);
        });
}

function loadSalesTrend() {
    fetchJson('/api/sales/trend?' + buildSalesParams())
        .then(d => {
            const ctx = document.getElementById('trendChart');
            if (!ctx) return;
            if (trendChartInstance) trendChartInstance.destroy();
            trendChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: d.map(p => p.label),
                    datasets: [{
                        label: t('Revenue'),
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
                                label: ctx => t('Revenue') + ': ' + formatRupiah(ctx.raw)
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
    fetchJson('/api/sales/top-products?' + buildSalesParams())
        .then(d => {
            const topBody = document.getElementById('top-sellers-body');
            const bottomBody = document.getElementById('bottom-sellers-body');
            const sellerRow = p => `
                <tr>
                    <td>${escapeHtml(p.name)}</td>
                    <td>${escapeHtml(p.sku || '-')}</td>
                    <td>${p.total_sold}</td>
                    <td>${formatRupiah(p.total_revenue)}</td>
                </tr>
            `;
            const emptyRow = `<tr><td colspan="4" class="empty-row">${t('No data yet')}</td></tr>`;
            topBody.innerHTML = d.top.length ? d.top.map(sellerRow).join('') : emptyRow;
            bottomBody.innerHTML = d.bottom.length ? d.bottom.map(sellerRow).join('') : emptyRow;
        });
}

function updateTimeLabel() {
    const now = new Date();
    const labelEl = document.getElementById('timeLabel');
    if (!labelEl) return;

    if (timeUnit === 'day') {
        const d = new Date(now);
        d.setDate(d.getDate() - timeOffset);
        labelEl.textContent = d.toLocaleDateString(DATE_LOCALE, {month: 'short', day: 'numeric', year: 'numeric'});
    } else if (timeUnit === 'week') {
        const dow = now.getDay();
        const monday = new Date(now);
        monday.setDate(now.getDate() - ((dow + 6) % 7) - timeOffset * 7);
        const sunday = new Date(monday);
        sunday.setDate(monday.getDate() + 6);
        labelEl.textContent = monday.toLocaleDateString(DATE_LOCALE, {month: 'short', day: 'numeric'}) + ' - ' +
                              sunday.toLocaleDateString(DATE_LOCALE, {month: 'short', day: 'numeric', year: 'numeric'});
    } else if (timeUnit === 'month') {
        const d = new Date(now.getFullYear(), now.getMonth() - timeOffset, 1);
        const lastDay = new Date(now.getFullYear(), now.getMonth() - timeOffset + 1, 0);
        labelEl.textContent = d.toLocaleDateString(DATE_LOCALE, {month: 'short', year: 'numeric'}) +
                              ' (' + d.getDate() + ' - ' + lastDay.getDate() + ')';
    } else if (timeUnit === 'year') {
        labelEl.textContent = (now.getFullYear() - timeOffset).toString();
    }
}

document.addEventListener('click', e => {
    if (e.target.classList.contains('btn-unit')) {
        document.querySelectorAll('.btn-unit').forEach(b => b.classList.remove('active'));
        e.target.classList.add('active');
        if (document.getElementById('trendChart')) {
            timeUnit = e.target.dataset.unit;
            timeOffset = 0;
            loadSalesData();
        }
    } else if (e.target.id === 'prevPeriod') {
        timeOffset++;
        loadSalesData();
    } else if (e.target.id === 'nextPeriod') {
        if (timeOffset > 0) {
            timeOffset--;
            loadSalesData();
        }
    } else if (e.target.id === 'resetPeriod') {
        timeOffset = 0;
        loadSalesData();
    } else if (e.target.classList.contains('btn-period')) {
        document.querySelectorAll('.btn-period').forEach(b => b.classList.remove('active'));
        e.target.classList.add('active');
        if (document.getElementById('restockHistoryBody')) {
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
                <option value="">${t('Select product')}</option>
                ${PRODUCTS.map(p => `<option value="${p.id}">${escapeHtml(p.name)} (${escapeHtml(p.sku || '-')}) - ${t('Stock: {n}', { n: p.stock })}</option>`).join('')}
            </select>
        </div>
        <div class="form-group">
            <input type="number" id="restock-qty-${idx}" min="1" value="1" placeholder="${t('Qty')}">
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
    if (!items.length) return showToast(t('Add at least one product'), 'error');
    const batchCost = parseFloat(document.getElementById('restockTotalCostInput').value) || 0;
    api('/api/restock', 'POST', { items, total_cost: batchCost }).then(d => {
        if (d.success) {
            showToast(t('Restock saved! Total cost: {cost}', { cost: formatRupiah(d.total_cost) }));
            document.getElementById('restockItems').innerHTML = '';
            document.getElementById('restockTotalCostInput').value = '0';
            addRestockItem();
            loadRestockHistory();
        } else showToast(d.error, 'error');
    });
}

function loadRestockHistory() {
    fetchJson(`/api/restock/history?period=${restockPeriod}&tz=${encodeURIComponent(CLIENT_TZ)}`)
        .then(d => {
            const tbody = document.getElementById('restockHistoryBody');
            if (!d.length) {
                tbody.innerHTML = `<tr><td colspan="4" class="empty-row">${t('No restock history yet')}</td></tr>`;
                return;
            }
            tbody.innerHTML = d.map(b => {
                const productList = b.items.map(i => `${escapeHtml(i.product_name)} (${escapeHtml(i.product_sku || '-')}): +${i.qty_added}`).join('<br>');
                return `
                    <tr class="restock-batch-row" onclick="const d = this.nextElementSibling; d.style.display = d.style.display === 'none' ? '' : 'none'">
                        <td>${t('Batch #{id}', { id: b.id })}</td>
                        <td>${t('{n} products', { n: b.items.length })}</td>
                        <td>${formatRupiah(b.total_cost)}</td>
                        <td>${formatLocalDate(b.created_at)}</td>
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
