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

/* Mirrors services.format_percent. toLocaleString handles the Indonesian decimal
   comma, so the separator follows the UI language without a table. */
function formatPercent(value) {
    return value.toLocaleString(DATE_LOCALE, {
        minimumFractionDigits: 1, maximumFractionDigits: 1
    }) + '%';
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

/* Resolves only on a 2xx; anything else **throws** with the server's translated error
   text. So every caller needs a .catch to say so -- `.then(d => d.success ? ... : ...)`
   is a trap, because the failure branch is unreachable and the rejection goes nowhere.
   Eight call sites were written that way and swallowed every refusal the server made,
   which is what tests/browser exists to catch. */
/* The session expired mid-page. Send the seller to sign in rather than toasting a
   failure they cannot act on, and hand back a promise that never settles so no
   caller's .catch fires a message against a page already navigating away. */
function goToLogin() {
    window.location = '/login';
    return new Promise(() => {});
}

/* Shared by both helpers so a refusal reads the same whoever asked: the server's own
   translated message where there is one, its status code where there is not. */
async function jsonOrThrow(res) {
    if (res.status === 401) return goToLogin();
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || t('Request failed ({status})', { status: res.status }));
    }
    return res.json();
}

async function api(url, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    return jsonOrThrow(await fetch(url, opts));
}

/* Wraps a fetch of JSON that renders into the page, surfacing failures as a toast
   instead of an unhandled rejection. Still rethrows, so a caller that would go on to
   render nothing can bail; add .catch(() => {}) when there is nothing more to do. */
function fetchJson(url) {
    return fetch(url).then(jsonOrThrow).catch(err => {
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
        timezone: document.getElementById('tgTimezone').value,
        alert_hours: document.getElementById('tgAlertHours').value,
        monthly_report: document.getElementById('tgMonthlyReport').checked
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

/* Held stock versus the orders holding it. Two steps rather than a single "fix it"
   button: the numbers say what customers were promised, so they get shown before
   anything rewrites them. */
function checkReservations() {
    const box = document.getElementById('reservationResult');
    box.innerHTML = `<p class="help-text">${t('Checking…')}</p>`;
    api('/api/stock/reservations/check').then(d => renderReservationDrift(d.drift)).catch(err => {
        box.innerHTML = '';
        showToast(err.message, 'error');
    });
}

function renderReservationDrift(drift) {
    const box = document.getElementById('reservationResult');
    if (!drift.length) {
        box.innerHTML = `<p class="help-text">✅ ${t('Held stock matches the open orders. Nothing to fix.')}</p>`;
        return;
    }
    const rows = drift.map(d => `<tr>
        <td>${escapeHtml(d.name)}</td>
        <td>${d.reserved}</td>
        <td>${d.expected}</td>
        <td>${d.difference > 0 ? '+' : ''}${d.difference}</td>
    </tr>`).join('');
    box.innerHTML = `
        <p class="help-text">⚠️ ${escapeHtml(t('{n} product(s) hold stock that open orders do not account for.', { n: drift.length }))}</p>
        <table class="data-table">
            <thead><tr>
                <th>${t('Product')}</th><th>${t('Held now')}</th>
                <th>${t('Orders justify')}</th><th>${t('Difference')}</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <div class="form-actions">
            <button type="button" class="btn btn-primary" onclick="repairReservations()">
                ${t('Correct held stock')}
            </button>
        </div>`;
}

function repairReservations() {
    api('/api/stock/reservations/repair', 'POST').then(d => {
        showToast(t('Corrected held stock for {n} product(s)', { n: d.repaired.length }));
        // Re-check rather than assuming the repair emptied the list: an order written
        // while the table was on screen is drift the repair legitimately did not cover.
        checkReservations();
    }).catch(err => showToast(err.message, 'error'));
}

/* ===== Products ===== */
// The chip is a filter, not a search term, so it lives outside the search box and
// survives typing in it. Deep-linkable as /products?needs_cost=1 so the uncosted-sales
// notes on the dashboard and sales page can point straight at the work.
let needsCostOnly = false;
let archivedOnly = false;

// The two views are exclusive: "Needs cost" counts active products only, so combining
// them would always come back empty and read as a bug.
function setChip(id, on) {
    const chip = document.getElementById(id);
    if (chip) chip.classList.toggle('active', on);
}

function toggleNeedsCost() {
    needsCostOnly = !needsCostOnly;
    if (needsCostOnly) archivedOnly = false;
    setChip('needsCostChip', needsCostOnly);
    setChip('archivedChip', archivedOnly);
    loadProducts();
}

function toggleArchived() {
    archivedOnly = !archivedOnly;
    if (archivedOnly) needsCostOnly = false;
    setChip('archivedChip', archivedOnly);
    setChip('needsCostChip', needsCostOnly);
    loadProducts();
}

function restoreProduct(id) {
    if (!confirm(t('Restore this product to the catalogue?'))) return;
    fetchJson(`/api/products/${id}/restore`, { method: 'POST' })
        .then(() => {
            showToast(t('Product restored'), 'success');
            // Reload the page: both chip counts moved, and they are rendered server-side.
            location.reload();
        })
        .catch(err => showToast(err.message, 'error'));
}

// Two different problems, and conflating them would hide the worse one: no cost at all
// reads as "—" and keeps the sale out of every profit figure, while a cost left in doubt
// by a void is a real number that is quietly wrong.
function costCell(p) {
    if (p.cost_review_needed) {
        return `<td class="cost-suspect" title="${t('A voided restock left this cost in doubt — check it against the invoice')}">`
            + `${formatRupiah(p.cost_price)} ⚠</td>`;
    }
    if (p.cost_price > 0) return `<td>${formatRupiah(p.cost_price)}</td>`;
    return `<td class="cost-unknown" title="${t('No cost recorded — sales of this product are left out of profit')}">—</td>`;
}

// Physical stock and what is sellable are different numbers once open orders hold
// units, and both matter: a stock count reconciles against the first, a new order
// against the second. Shown together rather than netted, and only when they differ.
function stockCell(p) {
    if (!p.reserved_qty) return `<td>${p.stock_qty}</td>`;
    return `<td title="${t('Held: {n}', { n: p.reserved_qty })}">${p.stock_qty}`
        + ` <span class="stock-held">${t('Available: {n}', { n: p.available })}</span></td>`;
}

function loadProducts() {
    const search = document.getElementById('searchProduct').value;
    let url = '/api/products?';
    if (search) url += 'search=' + encodeURIComponent(search) + '&';
    if (needsCostOnly) url += 'needs_cost=1&';
    if (archivedOnly) url += 'archived=1&';
    fetchJson(url).then(products => {
        const tbody = document.getElementById('productsBody');
        if (!products.length) {
            let empty = t('No products found');
            if (needsCostOnly) empty = t('Every product has a cost recorded');
            else if (archivedOnly) empty = t('No archived products');
            tbody.innerHTML = `<tr><td colspan="6" class="empty-row">${empty}</td></tr>`;
            return;
        }
        // An archived product is out of the catalogue: editing or re-archiving it makes
        // no sense, so the only thing offered is putting it back.
        const actions = p => archivedOnly
            ? `<button class="btn-icon" onclick="restoreProduct(${p.id})" title="${t('Restore')}">♻️</button>`
            : `<button class="btn-icon" onclick="editProduct(${p.id})" title="${t('Edit')}">✏️</button>
               <button class="btn-icon" onclick="deleteProduct(${p.id})" title="${t('Archive')}">🗑️</button>`;
        tbody.innerHTML = products.map(p => `
            <tr${archivedOnly ? ' class="row-archived"' : ''}>
                <td>${escapeHtml(p.sku || '-')}</td>
                <td>${escapeHtml(p.name)}</td>
                <td>${formatRupiah(p.price)}</td>
                ${costCell(p)}
                ${stockCell(p)}
                <td class="action-cell">${actions(p)}</td>
            </tr>
        `).join('');
    }).catch(() => { /* fetchJson has toasted; there is no table to draw */ });
}

// Opening stock is inventory that was paid for, and a sale snapshots its cost at order
// time -- so stock entered without a cost sells at an unknown cost permanently. The server
// enforces this; marking the field here just says so before the form is submitted.
function syncCostPriceRequirement() {
    const stock = parseInt(document.getElementById('productStock').value) || 0;
    const needed = stock > 0 && !document.getElementById('productStock').disabled;
    const cost = document.getElementById('productCostPrice');
    cost.required = needed;
    document.getElementById('costPriceLabel').textContent =
        needed ? t('Cost Price (Rp) *') : t('Cost Price (Rp)');
    document.getElementById('costPriceHint').textContent = needed
        ? t('What you paid per unit for this opening stock.')
        : t('Kept up to date by restocking; set it here for stock you already had.');
}

function openProductModal(id = null) {
    document.getElementById('productForm').reset();
    document.getElementById('productId').value = '';
    document.getElementById('modalTitle').textContent = t('Add Product');
    document.getElementById('productStock').disabled = false;
    document.getElementById('stockWarning').style.display = 'none';
    syncCostPriceRequirement();
    if (id) {
        fetchJson('/api/products').then(products => {
            const p = products.find(x => x.id === id);
            if (p) {
                document.getElementById('productId').value = p.id;
                document.getElementById('productName').value = p.name;
                document.getElementById('productSku').value = p.sku || '';
                document.getElementById('productPrice').value = p.price;
                document.getElementById('productCostPrice').value = p.cost_price;
                document.getElementById('productStock').value = p.stock_qty;
                document.getElementById('productStock').disabled = true;
                document.getElementById('stockWarning').style.display = 'block';
                document.getElementById('productThreshold').value = p.reorder_threshold;
                document.getElementById('modalTitle').textContent = t('Edit Product');
                // Stock is not editable here, so an existing product's cost is never forced.
                syncCostPriceRequirement();
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
        price: parseFloat(document.getElementById('productPrice').value) || 0,
        cost_price: parseFloat(document.getElementById('productCostPrice').value) || 0,
        reorder_threshold: parseInt(document.getElementById('productThreshold').value) || 0
    };
    if (!id) {
        data.stock_qty = parseInt(document.getElementById('productStock').value) || 0;
    }
    const method = id ? 'PUT' : 'POST';
    const url = id ? '/api/products/' + id : '/api/products';
    api(url, method, data).then(() => {
        showToast(t('Product saved'));
        closeProductModal();
        loadProducts();
    }).catch(err => showToast(err.message, 'error'));
}

function deleteProduct(id) {
    if (!confirm(t('Archive this product?'))) return;
    api('/api/products/' + id, 'DELETE').then(() => {
        showToast(t('Product archived'));
        loadProducts();
    }).catch(err => showToast(err.message, 'error'));
}

/* ===== Orders ===== */
function formatLocalDate(utcStr) {
    // Explicit locale: toLocaleString() would follow the browser's language, not the shop's.
    return new Date(utcStr.replace(' ', 'T') + 'Z').toLocaleString(DATE_LOCALE);
}

let ordersPage = 0;

// Filtering changes what the pages contain, so staying on page 3 would land the
// seller somewhere arbitrary -- or past the end of a shorter result.
function reloadOrdersFromStart() {
    ordersPage = 0;
    loadOrders();
}

function goToOrdersPage(delta) {
    ordersPage = Math.max(0, ordersPage + delta);
    loadOrders();
}

function loadOrders() {
    const search = document.getElementById('searchOrder').value;
    const status = document.getElementById('filterStatus').value;
    let url = '/api/orders?page=' + ordersPage + '&';
    if (search) url += 'search=' + encodeURIComponent(search) + '&';
    if (status) url += 'status=' + status + '&';
    fetchJson(url).then(data => {
        const orders = data.orders || [];
        const tbody = document.getElementById('ordersBody');
        renderOrdersPager(data.has_more, orders.length);
        if (!orders.length) {
            // Landing on an empty page past the end is reachable by deleting the last
            // draft on it; step back rather than showing "no orders found" over a
            // list that does have some.
            if (ordersPage > 0) return goToOrdersPage(-1);
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
                    ${o.status === 'draft' ? `<button class="btn-icon" onclick="editOrder(${o.id})" title="${t('Edit')}">✏️</button>` : ''}
                    ${o.status === 'draft' ? `<button class="btn-icon" onclick="confirmOrder(${o.id})" title="${t('Confirm')}">✅</button>` : ''}
                    ${o.status === 'confirmed' ? `<button class="btn-icon" onclick="completeOrder(${o.id})" title="${t('Complete')}">💰</button>` : ''}
                    ${o.status === 'draft' || o.status === 'confirmed' ? `<button class="btn-icon" onclick="cancelOrder(${o.id})" title="${t('Cancel')}">❌</button>` : ''}
                </td>
            </tr>
        `).join('');
    }).catch(() => { /* fetchJson has toasted; there is no list to draw */ });
}

// Hidden entirely on a single page of results: a pager offering nothing to page to
// is just furniture. Page numbers are 1-based here and 0-based on the wire.
function renderOrdersPager(hasMore, shown) {
    const pager = document.getElementById('ordersPager');
    if (!hasMore && ordersPage === 0) {
        pager.innerHTML = '';
        return;
    }
    pager.innerHTML = `
        <button class="btn btn-secondary" onclick="goToOrdersPage(-1)"
                ${ordersPage === 0 ? 'disabled' : ''}>${t('◀ Prev')}</button>
        <span class="pager-label">${t('Page {n}', { n: ordersPage + 1 })}</span>
        <button class="btn btn-secondary" onclick="goToOrdersPage(1)"
                ${hasMore ? '' : 'disabled'}>${t('Next ▶')}</button>
    `;
}

/* ===== Stock history ===== */
let movementsPage = 0;

function reloadMovementsFromStart() {
    movementsPage = 0;
    loadMovements();
}

function goToMovementsPage(delta) {
    movementsPage = Math.max(0, movementsPage + delta);
    loadMovements();
}

function loadMovements() {
    const productId = document.getElementById('filterMovementProduct').value;
    let url = '/api/stock/movements?page=' + movementsPage;
    if (productId) url += '&product_id=' + encodeURIComponent(productId);
    fetchJson(url).then(data => {
        const rows = data.movements || [];
        const tbody = document.getElementById('movementsBody');
        renderMovementsPager(data.has_more);
        if (!rows.length) {
            if (movementsPage > 0) return goToMovementsPage(-1);
            tbody.innerHTML = `<tr><td colspan="5" class="empty-row">${t('No stock movements recorded')}</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(m => `
            <tr>
                <td>${formatLocalDate(m.created_at)}</td>
                <td>${escapeHtml(m.product_name)}${m.product_sku ? ` <span class="stock-held">${escapeHtml(m.product_sku)}</span>` : ''}</td>
                <td class="${m.change_qty < 0 ? 'qty-out' : 'qty-in'}">${m.change_qty > 0 ? '+' : ''}${m.change_qty}</td>
                <td>${escapeHtml(m.reason || '')}</td>
                <td>${escapeHtml(m.actor || t('unknown'))}</td>
            </tr>`).join('');
    }).catch(() => {});
}

function renderMovementsPager(hasMore) {
    const pager = document.getElementById('movementsPager');
    if (!hasMore && movementsPage === 0) {
        pager.innerHTML = '';
        return;
    }
    pager.innerHTML = `
        <button class="btn btn-secondary" onclick="goToMovementsPage(-1)"
                ${movementsPage === 0 ? 'disabled' : ''}>${t('◀ Prev')}</button>
        <span class="pager-label">${t('Page {n}', { n: movementsPage + 1 })}</span>
        <button class="btn btn-secondary" onclick="goToMovementsPage(1)"
                ${hasMore ? '' : 'disabled'}>${t('Next ▶')}</button>
    `;
}

// Set while the modal is editing an existing draft; null while creating one.
let editingOrderId = null;
// product_id -> units the draft being edited already holds. Those units are
// reserved against this very order, so they are still spendable here even though
// the products page counts them as unavailable.
let editingHolds = {};

function availableFor(p) {
    return p.available + (editingHolds[p.id] || 0);
}

// Availability baked into the page at render time is stale as soon as anyone else
// writes an order -- a second device, or the Telegram bot. The form refetches it on
// every open so the seller is never choosing from numbers that have moved on.
function refreshProducts() {
    return fetch('/api/products')
        // Not fetchJson: a failure here is survivable, and toasting it would talk over
        // the modal that is opening. An expired session is not survivable, though --
        // the form would offer a catalogue nothing can be saved against.
        .then(res => (res.status === 401 ? goToLogin() : res.json()))
        .then(rows => { PRODUCTS = rows; })
        .catch(() => { /* keep the page's copy; the server still enforces the hold */ });
}

function newOrder() {
    refreshProducts().then(() => openOrderModal(null));
}

function editOrder(id) {
    fetchJson('/api/orders/' + id)
        .then(o => refreshProducts().then(() => openOrderModal(o)))
        .catch(() => { /* fetchJson has toasted; there is no order to open */ });
}

function openOrderModal(order) {
    editingOrderId = order ? order.id : null;
    editingHolds = {};
    if (order) {
        (order.items || []).forEach(i => {
            editingHolds[i.product_id] = (editingHolds[i.product_id] || 0) + i.quantity;
            // A product archived while this draft sat open is out of the catalogue and
            // so absent from PRODUCTS, but it is still on the order. Without this the
            // line has no option to select and saving would drop it unannounced.
            if (!PRODUCTS.some(p => p.id === i.product_id)) {
                PRODUCTS.push({ id: i.product_id, name: i.product_name, price: i.unit_price,
                                stock_qty: 0, reserved_qty: i.quantity, available: 0 });
            }
        });
    }
    document.getElementById('orderModalTitle').textContent = order ? t('Edit Order') : t('New Order');
    document.getElementById('orderSubmitBtn').textContent = order ? t('Save Changes') : t('Create Order');
    document.getElementById('orderItems').innerHTML = '';
    document.getElementById('orderTotal').textContent = 'Rp 0';
    if (order) {
        (order.items || []).forEach(i => addOrderItem(i.product_id, i.quantity));
        calcOrderTotal();
    }
    document.getElementById('orderModal').classList.add('active');
}
function closeOrderModal() { document.getElementById('orderModal').classList.remove('active'); }

function addOrderItem(productId, qty) {
    const div = document.createElement('div');
    div.className = 'form-row';
    div.style.marginBottom = '8px';
    // A product with nothing available is not offered -- except the one this row is
    // already on, which would otherwise vanish from its own order on opening the editor.
    const options = PRODUCTS.filter(p => availableFor(p) > 0 || p.id === productId)
        .map(p => `<option value="${p.id}" data-price="${p.price}"${p.id === productId ? ' selected' : ''}>`
            + `${escapeHtml(p.name)} (${t('Available: {n}', { n: availableFor(p) })})</option>`).join('');
    div.innerHTML = `
        <div class="form-group">
            <select onchange="calcOrderTotal()">
                <option value="">${t('Select product')}</option>
                ${options}
            </select>
        </div>
        <div class="form-group">
            <input type="number" min="1" value="${qty > 0 ? qty : 1}" class="qty-input" oninput="calcOrderTotal()">
        </div>
        <div class="form-group">
            <span class="item-subtotal" style="font-weight:600">Rp 0</span>
        </div>
        <button type="button" class="btn-icon" title="${t('Remove')}" onclick="removeOrderItem(this)">✕</button>
    `;
    document.getElementById('orderItems').appendChild(div);
}

function removeOrderItem(btn) {
    btn.closest('.form-row').remove();
    calcOrderTotal();
}

// Read per row rather than by walking three parallel NodeLists: rows can be removed
// now, and positional matching across separate queries is a trap waiting for the day
// a row holds one input but not the other.
function orderRows() {
    return [...document.querySelectorAll('#orderItems .form-row')].map(row => ({
        row,
        select: row.querySelector('select'),
        qtyInput: row.querySelector('.qty-input'),
        subtotal: row.querySelector('.item-subtotal'),
    }));
}

function calcOrderTotal() {
    let total = 0;
    orderRows().forEach(({ select, qtyInput, subtotal }) => {
        const opt = select.options[select.selectedIndex];
        const price = parseFloat(opt.dataset.price) || 0;
        const sub = price * (parseInt(qtyInput.value) || 0);
        total += sub;
        subtotal.textContent = formatRupiah(sub);
    });
    document.getElementById('orderTotal').textContent = formatRupiah(total);
}

function saveOrder() {
    const items = [];
    orderRows().forEach(({ select, qtyInput }) => {
        const pid = parseInt(select.value);
        const qty = parseInt(qtyInput.value) || 0;
        if (pid && qty > 0) items.push({ product_id: pid, quantity: qty });
    });
    if (!items.length) return showToast(t('Add at least one item'), 'error');
    const editing = editingOrderId !== null;
    const request = editing
        ? api('/api/orders/' + editingOrderId, 'PUT', { items })
        : api('/api/orders', 'POST', { items });
    request.then(d => {
        showToast(editing ? t('Order ID {id} updated', { id: d.order_id })
                          : t('Order ID {id} created', { id: d.order_id }));
        closeOrderModal();
        loadOrders();
    }).catch(err => showToast(err.message, 'error'));
}

function confirmOrder(id) {
    if (!confirm(t('Confirm payment for this order?'))) return;
    api('/api/orders/' + id + '/confirm', 'POST').then(() => {
        showToast(t('Payment confirmed'));
        loadOrders();
    }).catch(err => showToast(err.message, 'error'));
}

function completeOrder(id) {
    if (!confirm(t('Complete this order? Stock will be deducted.'))) return;
    api('/api/orders/' + id + '/complete', 'POST').then(() => {
        showToast(t('Order completed'));
        loadOrders();
    }).catch(err => showToast(err.message, 'error'));
}

function cancelOrder(id) {
    if (!confirm(t('Cancel this order?'))) return;
    api('/api/orders/' + id + '/cancel', 'POST').then(() => {
        showToast(t('Order cancelled'));
        loadOrders();
    }).catch(err => showToast(err.message, 'error'));
}

function viewOrder(id) {
    fetchJson('/api/orders/' + id).then(o => {
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
    }).catch(() => { /* fetchJson has toasted; there is no order to show */ });
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
    loadProductPerformance();
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
            document.getElementById('stat-gross-profit').textContent = formatRupiah(d.gross_profit);
            document.getElementById('stat-self-use').textContent = formatRupiah(d.self_use_value);
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

function loadProductPerformance() {
    fetchJson('/api/sales/product-performance?' + buildSalesParams())
        .then(d => {
            const row = cells => `<tr>${cells.map(c => `<td>${c}</td>`).join('')}</tr>`;
            const fill = (id, items, cells, empty, colspan = 4) => {
                document.getElementById(id).innerHTML = items.length
                    ? items.map(p => row(cells(p))).join('')
                    : `<tr><td colspan="${colspan}" class="empty-row">${empty}</td></tr>`;
            };
            const name = p => escapeHtml(p.name);
            const sku = p => escapeHtml(p.sku || '-');

            fill('qty-sellers-body', d.by_quantity,
                 p => [name(p), sku(p), p.total_sold, formatRupiah(p.total_revenue)],
                 t('No data yet'));
            fill('profit-sellers-body', d.by_profit,
                 p => [name(p), sku(p), formatRupiah(p.total_profit),
                       formatPercent(p.margin), formatPercent(p.share)],
                 t('No data yet'), 5);
            // A sale missing from the profit figures is missing a cost, not misbehaving.
            // The same exclusion applies to Gross Profit above, so it is said once, and
            // it links to the products that need the cost typed in.
            const note = document.getElementById('profit-sellers-note');
            note.innerHTML = d.uncosted_sales
                ? `<a href="/products?needs_cost=1">${escapeHtml(
                    t('{n} sale(s) excluded from Profit and Gross Profit — cost not recorded yet',
                      { n: d.uncosted_sales }))}</a>`
                : '';
            fill('unsold-body', d.unsold.items,
                 p => [name(p), sku(p), p.stock_qty, formatRupiah(p.stock_value)],
                 t('All products sold at least once'));

            // Only worth saying how much was withheld when something was.
            const shown = d.unsold.items.length;
            const summary = shown
                ? t('Stock value at risk: {amount}', { amount: formatRupiah(d.unsold.total_stock_value) })
                  + (shown < d.unsold.total
                     ? ' — ' + t('Showing {shown} of {total}', { shown: shown, total: d.unsold.total })
                     : '')
                : '';
            document.getElementById('unsold-summary').textContent = summary;
        });
}

/* ===== Monthly Report ===== */
/* Month labels come from the server rather than toLocaleString: the report is
   titled in the shop's language and timezone, and the picker must name the same
   month the generated PDF will cover. */
function loadReportMonths() {
    fetchJson('/api/reports/months').then(months => {
        document.getElementById('reportMonth').innerHTML = months.map(m =>
            `<option value="${m.offset}"${m.offset === 1 ? ' selected' : ''}>` +
            `${escapeHtml(m.label)}</option>`).join('');
    });
}

function reportOffset() {
    return document.getElementById('reportMonth').value;
}

function downloadReport() {
    /* A plain navigation, not fetch: the response is a PDF attachment, and this
       lets the browser's own download handling name and save it. */
    window.location = `/api/reports/monthly?offset=${reportOffset()}`;
}

function downloadCsv() {
    /* Same month selector as the PDF, same navigation trick. */
    window.location = `/api/reports/monthly.csv?offset=${reportOffset()}`;
}

function sendReport() {
    const btn = document.getElementById('reportSendBtn');
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = t('Sending…');
    api('/api/reports/monthly/send', 'POST', { offset: Number(reportOffset()) })
        .then(d => showToast(d.warning || t('Report for {month} sent to {n} recipient(s)',
                                            { month: d.month, n: d.sent }),
                             d.warning ? 'error' : 'success'))
        .catch(err => showToast(err.message, 'error'))
        .finally(() => { btn.disabled = false; btn.textContent = label; });
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
        // Dispatch on the selector's data-history so several history tables can
        // coexist; the active reset is scoped to the clicked group so two
        // selectors on one page would not clear each other.
        const group = e.target.closest('.period-selector');
        (group || document).querySelectorAll('.btn-period').forEach(b => b.classList.remove('active'));
        e.target.classList.add('active');
        const which = group && group.dataset.history;
        // Back to page 1: the new period holds different batches, so keeping the
        // page number lands the seller somewhere arbitrary or past the end.
        if (which === 'selfuse') {
            selfUsePeriod = e.target.dataset.period;
            selfUseHistoryPage = 0;
            loadSelfUseHistory();
        } else if (which === 'restock') {
            restockPeriod = e.target.dataset.period;
            restockHistoryPage = 0;
            loadRestockHistory();
        }
    }
});

/* ===== Restock ===== */
let restockPeriod = 'all';

function addRestockItem() {
    const idx = document.getElementById('restockItems').children.length;
    const div = document.createElement('div');
    div.className = 'restock-item-row';
    div.innerHTML = `
        <div class="form-group">
            <select id="restock-product-${idx}" onchange="prefillRestockCost(this)">
                <option value="">${t('Select product')}</option>
                ${PRODUCTS.map(p => `<option value="${p.id}">${escapeHtml(p.name)} (${escapeHtml(p.sku || '-')}) - ${t('Stock: {n}', { n: p.stock })}</option>`).join('')}
            </select>
        </div>
        <div class="form-group">
            <input type="number" id="restock-qty-${idx}" min="1" value="1" placeholder="${t('Qty')}" oninput="updateRestockTotals()">
        </div>
        <div class="form-group">
            <input type="number" id="restock-price-${idx}" min="0" value="0" placeholder="${t('Price per unit')}" oninput="updateRestockTotals()">
        </div>
        <div class="form-group">
            <button class="btn-remove-item" onclick="this.closest('.restock-item-row').remove(); updateRestockTotals();">&times;</button>
        </div>
    `;
    document.getElementById('restockItems').appendChild(div);
    updateRestockTotals();
}

// Last known cost is the likeliest price for the next invoice, so it saves typing on a
// repeat order. Only fills a field the user has not put a figure in.
function prefillRestockCost(select) {
    const priceInput = select.closest('.restock-item-row').querySelectorAll('input')[1];
    const product = PRODUCTS.find(p => p.id === parseInt(select.value));
    if (product && product.cost > 0 && !(parseFloat(priceInput.value) > 0)) {
        priceInput.value = product.cost;
    }
    updateRestockTotals();
}

function readRestockForm() {
    const items = [];
    document.querySelectorAll('.restock-item-row').forEach(row => {
        const inputs = row.querySelectorAll('input');
        const pid = parseInt(row.querySelector('select').value);
        const qty = parseInt(inputs[0].value) || 0;
        const unitPrice = parseFloat(inputs[1].value) || 0;
        if (pid && qty > 0) items.push({ product_id: pid, qty: qty, unit_price: unitPrice });
    });
    return {
        items,
        discount: parseFloat(document.getElementById('restockDiscountInput').value) || 0,
        shipping_cost: parseFloat(document.getElementById('restockShippingInput').value) || 0,
        admin_fee: parseFloat(document.getElementById('restockAdminFeeInput').value) || 0,
    };
}

// Mirrors services.allocate_restock_costs: the total is what the invoice says was paid,
// shown while typing so a mismatch with the paper invoice is caught before saving.
function updateRestockTotals() {
    const form = readRestockForm();
    const subtotal = form.items.reduce((sum, i) => sum + i.qty * i.unit_price, 0);
    const total = subtotal - form.discount + form.shipping_cost + form.admin_fee;
    const parts = [`${t('Subtotal')}: ${formatRupiah(subtotal)}`];
    if (form.discount) parts.push(`${t('Discount')}: −${formatRupiah(form.discount)}`);
    if (form.shipping_cost) parts.push(`${t('Shipping')}: +${formatRupiah(form.shipping_cost)}`);
    if (form.admin_fee) parts.push(`${t('Admin Fee')}: +${formatRupiah(form.admin_fee)}`);
    document.getElementById('restockTotals').innerHTML =
        `${parts.join(' &nbsp;·&nbsp; ')}<br><span class="restock-total-paid">${t('Invoice Total')}: ${formatRupiah(total)}</span>`;
}

function submitRestock() {
    const form = readRestockForm();
    if (!form.items.length) return showToast(t('Add at least one product'), 'error');
    api('/api/restock', 'POST', form).then(d => {
        showToast(t('Restock saved! Total cost: {cost}', { cost: formatRupiah(d.total_cost) }));
        document.getElementById('restockItems').innerHTML = '';
        ['restockDiscountInput', 'restockShippingInput', 'restockAdminFeeInput']
            .forEach(id => document.getElementById(id).value = '0');
        addRestockItem();
        loadRestockHistory();
    }).catch(err => showToast(err.message, 'error'));
}

/* A batch reads three ways in the history: ordinary and voidable, the void itself, or
   the original a void has since reversed. Restock and self use render them identically,
   so the label and the action cell are built once here. */
function batchLabel(b) {
    if (b.voids_batch_id) return t('Void of #{id}', { id: b.voids_batch_id });
    if (b.voided_by) return `${t('Batch #{id}', { id: b.id })} — ${t('voided')}`;
    return t('Batch #{id}', { id: b.id });
}

function voidCell(b, fn) {
    if (b.voids_batch_id || b.voided_by) return '<td></td>';
    // stopPropagation: the row itself toggles the detail panel underneath.
    return `<td class="action-cell"><button class="btn-icon" title="${t('Void')}"
        onclick="event.stopPropagation(); ${fn}(${b.id})">↩️</button></td>`;
}

function voidRowClass(b) {
    return b.voids_batch_id || b.voided_by ? ' batch-void' : '';
}

function voidRestock(id) {
    if (!confirm(t('Void batch #{id}? The stock it added comes back out and the invoice is reversed.', { id }))) return;
    fetchJson(`/api/restock/${id}/void`, { method: 'POST' })
        .then(r => {
            showToast(t('Batch #{id} voided', { id }), 'success');
            // Say what the void could and could not repair, rather than leaving the
            // shop owner to discover it on the products page.
            if (r.flagged.length) {
                showToast(t('Cost left in doubt for: {names} — a later restock had already averaged onto it',
                            { names: r.flagged.join(', ') }), 'error');
            }
            if (r.affected_sales) {
                showToast(t('{n} completed sale(s) already recorded the old cost and are unchanged',
                            { n: r.affected_sales }), 'error');
            }
            loadRestockHistory();
        })
        .catch(err => showToast(err.message, 'error'));
}

function voidSelfUse(id) {
    if (!confirm(t('Void batch #{id}? The stock it took out goes back in.', { id }))) return;
    fetchJson(`/api/self-use/${id}/void`, { method: 'POST' })
        .then(() => {
            showToast(t('Batch #{id} voided', { id }), 'success');
            loadSelfUseHistory();
        })
        .catch(err => showToast(err.message, 'error'));
}

// Both batch histories page identically, and the markup is the orders pager's. Hidden
// entirely on a single page of results: a pager offering nothing to page to is just
// furniture. Page numbers are 1-based here and 0-based on the wire.
function renderHistoryPager(pagerId, navFn, page, hasMore) {
    const pager = document.getElementById(pagerId);
    if (!pager) return;
    if (!hasMore && page === 0) {
        pager.innerHTML = '';
        return;
    }
    pager.innerHTML = `
        <button class="btn btn-secondary" onclick="${navFn}(-1)"
                ${page === 0 ? 'disabled' : ''}>${t('◀ Prev')}</button>
        <span class="pager-label">${t('Page {n}', { n: page + 1 })}</span>
        <button class="btn btn-secondary" onclick="${navFn}(1)"
                ${hasMore ? '' : 'disabled'}>${t('Next ▶')}</button>
    `;
}

let restockHistoryPage = 0;

function goToRestockHistoryPage(delta) {
    restockHistoryPage = Math.max(0, restockHistoryPage + delta);
    loadRestockHistory();
}

function loadRestockHistory() {
    fetchJson(`/api/restock/history?period=${restockPeriod}&page=${restockHistoryPage}`
              + `&tz=${encodeURIComponent(CLIENT_TZ)}`)
        .then(res => {
            const d = res.batches || [];
            const tbody = document.getElementById('restockHistoryBody');
            renderHistoryPager('restockHistoryPager', 'goToRestockHistoryPage',
                               restockHistoryPage, res.has_more);
            if (!d.length) {
                // An empty page past the end is reachable by switching to a shorter
                // period; step back rather than claiming there is no history at all.
                if (restockHistoryPage > 0) return goToRestockHistoryPage(-1);
                tbody.innerHTML = `<tr><td colspan="5" class="empty-row">${t('No restock history yet')}</td></tr>`;
                return;
            }
            tbody.innerHTML = d.map(b => {
                // A void's lines are negated, so the sign comes from the value, not a literal.
                const productList = b.items.map(i =>
                    `${escapeHtml(i.product_name)} (${escapeHtml(i.product_sku || '-')}): ${i.qty_added > 0 ? '+' : '−'}${Math.abs(i.qty_added)} × ${formatRupiah(i.unit_cost)} = ${formatRupiah(i.allocated_cost)}`
                ).join('<br>');
                // The charge lines are what turn a listed price into the landed cost above,
                // so the breakdown has to be readable back from the history.
                const charges = [`${t('Subtotal')}: ${formatRupiah(b.subtotal_cost)}`];
                if (b.discount) charges.push(`${t('Discount')}: −${formatRupiah(b.discount)}`);
                if (b.shipping_cost) charges.push(`${t('Shipping')}: +${formatRupiah(b.shipping_cost)}`);
                if (b.admin_fee) charges.push(`${t('Admin Fee')}: +${formatRupiah(b.admin_fee)}`);
                const detail = `${productList}<br><span style="color:#888">${charges.join(' &nbsp;·&nbsp; ')}</span>`;
                return `
                    <tr class="restock-batch-row${voidRowClass(b)}" onclick="const d = this.nextElementSibling; d.style.display = d.style.display === 'none' ? '' : 'none'">
                        <td>${batchLabel(b)}</td>
                        <td>${t('{n} products', { n: b.items.length })}</td>
                        <td>${formatRupiah(b.total_cost)}</td>
                        <td>${formatLocalDate(b.created_at)}</td>
                        ${voidCell(b, 'voidRestock')}
                    </tr>
                    <tr class="restock-detail-row" style="display:none">
                        <td colspan="5" style="background:#f8f9ff;padding:12px 16px;font-size:13px;color:#555">
                            ${detail}
                        </td>
                    </tr>
                `;
            }).join('');
        });
}

/* ===== Self Use ===== */
let selfUsePeriod = 'all';

function addSelfUseItem() {
    const idx = document.getElementById('selfUseItems').children.length;
    const div = document.createElement('div');
    div.className = 'self-use-item-row';
    div.innerHTML = `
        <div class="form-group">
            <select id="selfuse-product-${idx}" onchange="calcSelfUseTotal()">
                <option value="">${t('Select product')}</option>
                ${PRODUCTS.map(p => `<option value="${p.id}">${escapeHtml(p.name)} (${escapeHtml(p.sku || '-')}) - ${t('Stock: {n}', { n: p.stock })}</option>`).join('')}
            </select>
        </div>
        <div class="form-group">
            <input type="number" id="selfuse-qty-${idx}" min="1" value="1" placeholder="${t('Qty')}" oninput="calcSelfUseTotal()">
        </div>
        <div class="form-group">
            <button class="btn-remove-item" onclick="this.closest('.self-use-item-row').remove(); calcSelfUseTotal();">&times;</button>
        </div>
    `;
    document.getElementById('selfUseItems').appendChild(div);
}

/* Client-side estimate only — the server re-reads the live price when saving. */
function calcSelfUseTotal() {
    let total = 0;
    document.querySelectorAll('.self-use-item-row').forEach(row => {
        const pid = parseInt(row.querySelector('select').value);
        const qty = parseInt(row.querySelector('input').value) || 0;
        const product = PRODUCTS.find(p => p.id === pid);
        if (product && qty > 0) total += product.price * qty;
    });
    document.getElementById('selfUseTotal').textContent = formatRupiah(total);
}

function submitSelfUse() {
    const rows = document.querySelectorAll('.self-use-item-row');
    const items = [];
    rows.forEach(row => {
        const pid = parseInt(row.querySelector('select').value);
        const qty = parseInt(row.querySelector('input').value) || 0;
        if (pid && qty > 0) {
            items.push({ product_id: pid, qty: qty });
        }
    });
    if (!items.length) return showToast(t('Add at least one product'), 'error');
    api('/api/self-use', 'POST', { items }).then(d => {
        showToast(t('Self use saved! Total value: {value}', { value: formatRupiah(d.total_value) }));
        document.getElementById('selfUseItems').innerHTML = '';
        addSelfUseItem();
        calcSelfUseTotal();
        loadSelfUseHistory();
    }).catch(err => showToast(err.message, 'error'));
}

let selfUseHistoryPage = 0;

function goToSelfUseHistoryPage(delta) {
    selfUseHistoryPage = Math.max(0, selfUseHistoryPage + delta);
    loadSelfUseHistory();
}

function loadSelfUseHistory() {
    fetchJson(`/api/self-use/history?period=${selfUsePeriod}&page=${selfUseHistoryPage}`
              + `&tz=${encodeURIComponent(CLIENT_TZ)}`)
        .then(res => {
            const d = res.batches || [];
            const tbody = document.getElementById('selfUseHistoryBody');
            renderHistoryPager('selfUseHistoryPager', 'goToSelfUseHistoryPage',
                               selfUseHistoryPage, res.has_more);
            if (!d.length) {
                if (selfUseHistoryPage > 0) return goToSelfUseHistoryPage(-1);
                tbody.innerHTML = `<tr><td colspan="5" class="empty-row">${t('No self use history yet')}</td></tr>`;
                return;
            }
            tbody.innerHTML = d.map(b => {
                const productList = b.items.map(i => `${escapeHtml(i.product_name)} (${escapeHtml(i.product_sku || '-')}): ${i.quantity > 0 ? '-' : '+'}${Math.abs(i.quantity)}`).join('<br>');
                return `
                    <tr class="self-use-batch-row${voidRowClass(b)}" onclick="const d = this.nextElementSibling; d.style.display = d.style.display === 'none' ? '' : 'none'">
                        <td>${batchLabel(b)}</td>
                        <td>${t('{n} products', { n: b.items.length })}</td>
                        <td>${formatRupiah(b.total_value)}</td>
                        <td>${formatLocalDate(b.created_at)}</td>
                        ${voidCell(b, 'voidSelfUse')}
                    </tr>
                    <tr class="self-use-detail-row" style="display:none">
                        <td colspan="5" style="background:#f8f9ff;padding:12px 16px;font-size:13px;color:#555">
                            ${productList}
                        </td>
                    </tr>
                `;
            }).join('');
        });
}

/* ===== Init ===== */
document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('productsBody')) {
        // ?needs_cost=1 arrives from the profit notes elsewhere; reflect it in the chip
        // so the filtered view does not look like the whole catalogue.
        if (new URLSearchParams(location.search).get('needs_cost') === '1'
            && document.getElementById('needsCostChip')) {
            needsCostOnly = true;
            document.getElementById('needsCostChip').classList.add('active');
        }
        loadProducts();
    }
    if (document.getElementById('ordersBody')) loadOrders();
    if (document.getElementById('movementsBody')) loadMovements();
    if (document.getElementById('trendChart')) loadSalesData();
    if (document.getElementById('reportMonth')) loadReportMonths();
    if (document.getElementById('restockItems')) {
        addRestockItem();
        loadRestockHistory();
    }
    if (document.getElementById('selfUseItems')) {
        addSelfUseItem();
        calcSelfUseTotal();
        loadSelfUseHistory();
    }
});
