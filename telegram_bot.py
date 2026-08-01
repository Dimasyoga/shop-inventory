"""Telegram bot: browse products, manage orders, create restocks, view sales
summary, and deliver the monthly audit report.

Runs as a daemon thread (BotPoller) long-polling api.telegram.org with stdlib
urllib — no external dependencies, no public URL needed. Only whitelisted
Telegram user IDs may interact; config lives in the settings table and is
re-read every poll cycle, so web-UI changes apply without a restart.

This module must not import app.py (no Flask): handlers open their own DB
connections and call services.py directly.
"""
import html
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import namedtuple

import database
import i18n
import reports
import services
from services import ServiceError, format_rupiah

log = logging.getLogger('telegram_bot')

# --- Transport ---

class TelegramError(Exception):
    def __init__(self, description, error_code=None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code


class TelegramAPI:
    """Thin JSON client for the Telegram Bot API."""

    def __init__(self, token, timeout=35):
        # timeout must exceed the getUpdates long-poll timeout (25s) or every
        # quiet cycle raises a spurious socket timeout.
        self.token = token
        self.timeout = timeout

    def call(self, method, **params):
        return self._request(method, json.dumps(params).encode(), 'application/json')

    def _request(self, method, body, content_type):
        """POST a prepared body and unwrap Telegram's {ok, result} envelope.

        Split out of `call` so sendDocument can reuse the error handling with a
        multipart body; `call` itself can only ever send JSON.
        """
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{self.token}/{method}',
            data=body, headers={'Content-Type': content_type})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                payload = json.load(res)
        except urllib.error.HTTPError as e:
            try:
                payload = json.load(e)
            except Exception:
                raise TelegramError(f'HTTP {e.code}', e.code) from e
            raise TelegramError(payload.get('description', f'HTTP {e.code}'),
                                payload.get('error_code', e.code)) from e
        if not payload.get('ok'):
            raise TelegramError(payload.get('description', 'unknown error'),
                                payload.get('error_code'))
        return payload['result']

    def get_updates(self, offset=None, timeout=25):
        params = {'timeout': timeout}
        if offset is not None:
            params['offset'] = offset
        return self.call('getUpdates', **params)

    def send_message(self, chat_id, text, reply_markup=None):
        params = {'chat_id': chat_id, 'text': text[:4000], 'parse_mode': 'HTML'}
        if reply_markup:
            params['reply_markup'] = reply_markup
        return self.call('sendMessage', **params)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        params = {'chat_id': chat_id, 'message_id': message_id,
                  'text': text[:4000], 'parse_mode': 'HTML'}
        if reply_markup:
            params['reply_markup'] = reply_markup
        try:
            return self.call('editMessageText', **params)
        except TelegramError as e:
            # Re-tapping a button re-renders identical content; not an error.
            if 'message is not modified' in str(e):
                return None
            raise

    def send_document(self, chat_id, filename, content, caption=None):
        """Upload a file to a chat.

        sendDocument needs multipart/form-data, which `call`'s JSON body cannot
        express, so the body is assembled here -- keeping the module's promise of
        no dependencies beyond the standard library.
        """
        fields = {'chat_id': str(chat_id)}
        if caption:
            # 1024 is Telegram's caption limit; a longer one rejects the whole upload.
            fields['caption'] = caption[:1024]
            fields['parse_mode'] = 'HTML'
        boundary = '----shopinv' + secrets.token_hex(16)
        body = bytearray()
        for name, value in fields.items():
            body += (f'--{boundary}\r\n'
                     f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     f'{value}\r\n').encode()
        body += (f'--{boundary}\r\n'
                 f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                 f'Content-Type: application/pdf\r\n\r\n').encode()
        body += content
        body += f'\r\n--{boundary}--\r\n'.encode()
        return self._request('sendDocument', bytes(body),
                             f'multipart/form-data; boundary={boundary}')

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        params = {'callback_query_id': callback_query_id}
        if text:
            params['text'] = text[:200]
        if show_alert:
            params['show_alert'] = True
        return self.call('answerCallbackQuery', **params)


# --- Config ---

BotConfig = namedtuple('BotConfig', 'enabled token whitelist tz alert_hours report_enabled')

# Settings row holding the last month a report was delivered for, as 'YYYY-MM'.
REPORT_MARKER = 'last_report_period'


def parse_whitelist(raw):
    ids = set()
    for tok in (raw or '').replace(',', ' ').split():
        if tok.lstrip('-').isdigit():
            ids.add(int(tok))
    return ids


def parse_alert_hours(raw):
    """Stale-order threshold in hours as a positive float; None (disabled) for
    blank, zero, negative, or unparseable values."""
    try:
        hours = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


def load_bot_config(db):
    from database import get_setting, get_secret_setting
    tz_name = get_setting(db, 'shop_timezone', 'Asia/Jakarta')
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc  # never crash the poller over a bad setting
    return BotConfig(
        enabled=get_setting(db, 'telegram_enabled', '0') == '1',
        token=get_secret_setting(db, 'telegram_bot_token') or '',
        whitelist=parse_whitelist(get_setting(db, 'telegram_whitelist', '')),
        tz=tz,
        alert_hours=parse_alert_hours(get_setting(db, 'order_alert_hours', '24')),
        report_enabled=get_setting(db, 'monthly_report_enabled', '1') == '1')


# --- Conversation state (order/restock flows only; everything else is stateless) ---

class ChatStates:
    def __init__(self):
        self._states = {}
        self._lock = threading.Lock()

    def get(self, chat_id):
        with self._lock:
            return self._states.get(chat_id)

    def set(self, chat_id, state):
        with self._lock:
            self._states[chat_id] = state

    def pop(self, chat_id):
        with self._lock:
            return self._states.pop(chat_id, None)


# --- Rendering helpers ---

def esc(s):
    return html.escape(str(s if s is not None else ''))


def kb(*rows):
    return {'inline_keyboard': [list(r) for r in rows]}


def btn(text, data):
    return {'text': text, 'callback_data': data}


STATUS_CODES = {'d': 'draft', 'c': 'confirmed', 'f': 'completed', 'x': 'cancelled', 'a': None}
STATUS_LABELS = {'draft': '📝 Draft', 'confirmed': '💳 Payment Confirmed',
                 'completed': '✅ Completed', 'cancelled': '❌ Cancelled'}
QTY_CHOICES = (1, 2, 3, 5, 10, 20)
# Callback-data prefix <-> stateful flow. Dispatch compares whole parts[0]
# tokens, so these never collide with the single-letter screen prefixes.
FLOW_PREFIXES = {'no': 'order', 'r': 'restock', 'su': 'selfuse'}
FLOW_PREFIX = {v: k for k, v in FLOW_PREFIXES.items()}
UNIT_CODES = {'d': 'day', 'w': 'week', 'm': 'month', 'y': 'year'}
UNIT_LABELS = {'d': 'Day', 'w': 'Week', 'm': 'Month', 'y': 'Year'}


def screen_main(t):
    text = f'<b>📦 {t("Shop Inventory")}</b>\n{t("What do you want to do?")}'
    return text, kb(
        [btn(t('📦 Products'), 'p:0'), btn(t('🛒 Orders'), 'o')],
        [btn(t('🆕 New order'), 'no'), btn(t('📥 Restock'), 'r')],
        [btn(t('🏠 Self use'), 'su'), btn(t('📈 Sales summary'), 's:d:0')],
        [btn(t('📄 Monthly report'), 'rp')])


def screen_products(db, page, t):
    rows, has_more = services.list_products(db, page=page, page_size=8)
    if not rows and page == 0:
        return t('No products yet.'), kb([btn(t('« Menu'), 'm')])
    lines = [f'<b>📦 {t("Products")}</b>']
    for p in rows:
        sku = f" [{esc(p['sku'])}]" if p['sku'] else ''
        stock = t('stock {n}', n=p['stock_qty'])
        # Only worth saying when some of that stock is spoken for; otherwise the two
        # numbers are the same and the second is noise.
        if p['reserved_qty']:
            stock += t(', {n} held', n=p['reserved_qty'])
        lines.append(f"• {esc(p['name'])}{sku} — {format_rupiah(p['price'])} ({stock})")
    nav = []
    if page > 0:
        nav.append(btn(t('◀ Prev'), f'p:{page - 1}'))
    if has_more:
        nav.append(btn(t('Next ▶'), f'p:{page + 1}'))
    rows_kb = [nav] if nav else []
    rows_kb.append([btn(t('« Menu'), 'm')])
    return '\n'.join(lines), kb(*rows_kb)


def screen_orders_menu(t):
    return f'<b>🛒 {t("Orders")}</b>\n{t("Pick a status:")}', kb(
        [btn(t('📝 Draft'), 'ol:d:0'), btn(t('💳 Confirmed'), 'ol:c:0')],
        [btn(t('✅ Completed'), 'ol:f:0'), btn(t('❌ Cancelled'), 'ol:x:0')],
        [btn(t('All'), 'ol:a:0'), btn(t('« Menu'), 'm')])


def screen_orders_list(db, status_code, page, t):
    status = STATUS_CODES.get(status_code)
    rows, has_more = services.list_orders(db, status=status, page=page, page_size=10)
    label = t(STATUS_LABELS[status]) if status in STATUS_LABELS else t('All orders')
    if not rows and page == 0:
        return t('{label}: nothing here.', label=label), kb([btn(t('« Orders'), 'o'), btn(t('« Menu'), 'm')])
    lines = [f'<b>🛒 {label}</b>']
    buttons = []
    for o in rows:
        lines.append(f"#{o['id']} — {format_rupiah(o['total_amount'])} — {t(o['status'])}")
        buttons.append(btn(f"#{o['id']}", f"od:{o['id']}"))
    # order buttons in rows of 5
    rows_kb = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    nav = []
    if page > 0:
        nav.append(btn(t('◀ Prev'), f'ol:{status_code}:{page - 1}'))
    if has_more:
        nav.append(btn(t('Next ▶'), f'ol:{status_code}:{page + 1}'))
    if nav:
        rows_kb.append(nav)
    rows_kb.append([btn(t('« Orders'), 'o'), btn(t('« Menu'), 'm')])
    return '\n'.join(lines), kb(*rows_kb)


def screen_order_detail(db, order_id, t):
    order, items = services.get_order(db, order_id)
    status_label = t(STATUS_LABELS[order['status']]) if order['status'] in STATUS_LABELS else esc(order['status'])
    lines = [f"<b>{t('Order #{n}', n=order['id'])}</b> — {status_label}"]
    for i in items:
        lines.append(f"• {esc(i['product_name'])} ×{i['quantity']} = {format_rupiah(i['subtotal'])}")
    lines.append(f"<b>{t('Total: {amount}', amount=format_rupiah(order['total_amount']))}</b>")
    actions = []
    if order['status'] == 'draft':
        actions.append(btn(t('✅ Confirm payment'), f'oc:{order_id}'))
        actions.append(btn(t('❌ Cancel'), f'ox?:{order_id}'))
    elif order['status'] == 'confirmed':
        actions.append(btn(t('💰 Complete'), f'of?:{order_id}'))
        actions.append(btn(t('❌ Cancel'), f'ox?:{order_id}'))
    rows_kb = [actions] if actions else []
    rows_kb.append([btn(t('« Orders'), 'o'), btn(t('« Menu'), 'm')])
    return '\n'.join(lines), kb(*rows_kb)


def screen_confirm(question, yes_data, no_data, t):
    return question, kb([btn(t('✅ Yes'), yes_data), btn(t('« No'), no_data)])


def screen_summary(db, unit_code, offset, tz, t):
    unit = UNIT_CODES[unit_code]
    s = services.sales_summary(db, unit, offset, tz)
    start = s['start']
    lang = t.lang
    if unit == 'day':
        label = (f'{i18n.weekday_abbr(start.weekday(), lang)} {start.day:02d} '
                 f'{i18n.month_name(start.month, lang, abbr=True)} {start.year}')
    elif unit == 'week':
        date = f'{start.day:02d} {i18n.month_name(start.month, lang, abbr=True)} {start.year}'
        label = t('Week of {date}', date=date)
    elif unit == 'month':
        label = i18n.month_label(start, lang)
    else:
        label = str(start.year)
    text = '\n'.join([
        f'<b>{t("📈 Sales — {label}", label=esc(label))}</b>',
        t('Revenue: {amount}', amount=format_rupiah(s['total_revenue'])),
        t('Orders: {orders}   Items sold: {items}', orders=s['total_orders'], items=s['total_items_sold']),
        t('Restock cost: {amount}', amount=format_rupiah(s['restock_cost'])),
        t('Self use: {amount}', amount=format_rupiah(s['self_use_value'])),
        _gross_profit_line(s, t),
        f"<b>{t('Net profit: {amount}', amount=format_rupiah(s['net_profit']))}</b>",
    ])
    unit_row = [btn(('· ' if c == unit_code else '') + t(UNIT_LABELS[c]), f's:{c}:0')
                for c in ('d', 'w', 'm', 'y')]
    nav = [btn('◀', f's:{unit_code}:{offset + 1}')]
    if offset > 0:
        nav.append(btn('▶', f's:{unit_code}:{offset - 1}'))
    nav.append(btn(t('« Menu'), 'm'))
    return text, kb(unit_row, nav)


# --- Order / restock flow screens (stateful) ---

# Closed months offered in the bot's report picker. Six keeps the keyboard short;
# older months stay reachable from the web page, which offers a full year.
REPORT_MONTHS = 6


def screen_report_picker(t, tz, now=None):
    text = '\n'.join([f'<b>📄 {t("Monthly Report")}</b>', t('Pick a month:')])
    rows = []
    for offset in range(1, REPORT_MONTHS + 1):
        start, _ = services.get_date_range('month', offset, tz, now=now)
        rows.append([btn(i18n.month_label(start, t.lang), f'rp:{offset}')])
    return text, kb(*rows, [btn(t('« Menu'), 'm')])


def report_caption(data, t):
    """Summary shown alongside the uploaded PDF, so the numbers are visible
    without opening it. Reuses the sales-summary wording."""
    s = data['summary']
    return '\n'.join([
        f'<b>📄 {t("Monthly Report")} — {esc(data["label"])}</b>',
        t('Revenue: {amount}', amount=format_rupiah(s['total_revenue'])),
        t('Orders: {orders}   Items sold: {items}',
          orders=s['total_orders'], items=s['total_items_sold']),
        t('Restock cost: {amount}', amount=format_rupiah(s['restock_cost'])),
        t('Self use: {amount}', amount=format_rupiah(s['self_use_value'])),
        _gross_profit_line(s, t),
        f"<b>{t('Net profit: {amount}', amount=format_rupiah(s['net_profit']))}</b>",
    ])


def _gross_profit_line(s, t):
    """Gross profit, saying so when sales without a recorded cost were held back.

    Silently omitting them would make the figure look like it covered every sale.
    """
    line = t('Gross profit: {amount}', amount=format_rupiah(s['gross_profit']))
    if s['uncosted_sales']:
        line += ' ' + t('({n} sale(s) excluded)', n=s['uncosted_sales'])
    return line


def _cart_lines(db, items):
    lines = []
    for pid, qty in items.items():
        p = db.execute("SELECT name, price FROM products WHERE id = ?", (pid,)).fetchone()
        name = esc(p['name']) if p else f'#{pid}'
        lines.append(f"• {name} ×{qty}")
    return lines


def screen_flow_picker(db, flow, items, page, t):
    prefix = FLOW_PREFIX[flow]
    title = {'order': t('🆕 New order'), 'restock': t('📥 Restock'),
             'selfuse': t('🏠 Self use')}[flow]
    rows, has_more = services.list_products(db, page=page, page_size=8)
    lines = [f'<b>{title}</b>']
    if items:
        lines += [t('Selected:')] + _cart_lines(db, items)
    lines.append(t('Pick a product:'))
    buttons = []
    for p in rows:
        # Restock adds stock, so the current level is noise there; the flows that
        # take stock out need it visible. An order can only draw on what other open
        # orders have not already claimed, while self use takes off the shelf directly.
        if flow == 'restock':
            stock = ''
        elif flow == 'order':
            stock = f" ({p['stock_qty'] - p['reserved_qty']})"
        else:
            stock = f" ({p['stock_qty']})"
        buttons.append([btn(f"{p['name'][:28]}{stock}", f'{prefix}:i:{p["id"]}')])
    nav = []
    if page > 0:
        nav.append(btn(t('◀ Prev'), f'{prefix}:p:{page - 1}'))
    if has_more:
        nav.append(btn(t('Next ▶'), f'{prefix}:p:{page + 1}'))
    if nav:
        buttons.append(nav)
    tail = [btn(t('✔ Done'), f'{prefix}:d')] if items else []
    tail.append(btn(t('✖ Abandon'), f'{prefix}:c'))
    buttons.append(tail)
    return '\n'.join(lines), {'inline_keyboard': buttons}


def screen_flow_qty(db, flow, pid, t):
    prefix = FLOW_PREFIX[flow]
    p = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    name = esc(p['name']) if p else f'#{pid}'
    text = t('How many <b>{name}</b>?', name=name)
    if p and flow == 'order':
        text += t(' (available: {n})', n=p['stock_qty'] - p['reserved_qty'])
    elif p and flow != 'restock':
        text += t(' (stock: {n})', n=p['stock_qty'])
    text += '\n' + t('Tap a number, or ✏️ Custom to type any amount.')
    qty_row = [btn(str(n), f'{prefix}:q:{n}') for n in QTY_CHOICES]
    return text, kb(qty_row[:3], qty_row[3:],
                    [btn(t('✏️ Custom'), f'{prefix}:qc'), btn(t('« Back'), f'{prefix}:p:0')])


def _restock_invoice_lines(db, state, t):
    """The picked products priced as the invoice prices them, then the batch charges.

    Written out in full so the review screen can be checked against the paper invoice
    before anything is saved.
    """
    lines = []
    for pid, qty in state['items'].items():
        p = db.execute("SELECT name FROM products WHERE id = ?", (pid,)).fetchone()
        name = esc(p['name']) if p else f'#{pid}'
        price = state['prices'].get(pid)
        if price is None:
            lines.append(t('• {name} ×{qty} — <i>price missing</i>', name=name, qty=qty))
        else:
            lines.append(f'• {name} ×{qty} @ {format_rupiah(price)} = {format_rupiah(price * qty)}')
    subtotal = sum(state['prices'].get(pid, 0) * qty for pid, qty in state['items'].items())
    total = subtotal - state['discount'] + state['shipping'] + state['admin_fee']
    lines.append(t('Subtotal: {amount}', amount=format_rupiah(subtotal)))
    if state['discount']:
        lines.append(t('Discount: −{amount}', amount=format_rupiah(state['discount'])))
    if state['shipping']:
        lines.append(t('Shipping: +{amount}', amount=format_rupiah(state['shipping'])))
    if state['admin_fee']:
        lines.append(t('Admin fee: +{amount}', amount=format_rupiah(state['admin_fee'])))
    lines.append(f"<b>{t('Total paid: {amount}', amount=format_rupiah(total))}</b>")
    return lines


# The three invoice-wide charges, in the order the bot asks for them. Each is optional,
# so every prompt carries a Skip button.
RESTOCK_CHARGES = ('discount', 'shipping', 'admin_fee')


def restock_charge_prompt(charge, t):
    # Built as literals inside t(...) rather than a module-level table: the i18n coverage
    # test scans for exactly that shape, and a table would slip past it untranslated.
    return {
        'discount': t('Send the <b>discount</b> on this invoice, or tap Skip.'),
        'shipping': t('Send the <b>shipping cost</b>, or tap Skip.'),
        'admin_fee': t('Send the <b>bank admin fee</b>, or tap Skip.'),
    }[charge]


def next_restock_charge(state):
    """The charge to ask for after the current one, or None when the review is due."""
    current = state.get('await_charge')
    if current is None:
        return RESTOCK_CHARGES[0]
    position = RESTOCK_CHARGES.index(current) + 1
    return RESTOCK_CHARGES[position] if position < len(RESTOCK_CHARGES) else None


def screen_flow_review(db, flow, state, t):
    prefix = FLOW_PREFIX[flow]
    if flow == 'restock':
        title = t('📥 Restock — review')
        lines = [f'<b>{title}</b>'] + _restock_invoice_lines(db, state, t)
        action = btn(t('✅ Save restock'), 'r:!')
    else:
        # Order and self use both value the cart at the current retail price;
        # only the wording and the commit button differ.
        title = t('🆕 New order — review') if flow == 'order' else t('🏠 Self use — review')
        lines = [f'<b>{title}</b>'] + _cart_lines(db, state['items'])
        total = 0
        for pid, qty in state['items'].items():
            p = db.execute("SELECT price FROM products WHERE id = ?", (pid,)).fetchone()
            if p:
                total += p['price'] * qty
        lines.append(f"<b>{t('Total: {amount}', amount=format_rupiah(total))}</b>")
        action = (btn(t('✅ Create draft order'), 'no:!') if flow == 'order'
                  else btn(t('✅ Save self use'), 'su:!'))
    return '\n'.join(lines), kb([action],
                                [btn(t('+ Add more'), f'{prefix}:p:0'), btn(t('✖ Abandon'), f'{prefix}:c')])


def parse_cost(text):
    """'Rp 150.000', '150000', '150,000' -> 150000.0; None when unparseable."""
    cleaned = text.strip().lower().replace('rp', '').replace(' ', '')
    cleaned = cleaned.replace('.', '').replace(',', '')
    if not cleaned.isdigit():
        return None
    return float(cleaned)


def parse_qty(text):
    """'12', ' 3 ' -> positive int; None when not a positive whole number."""
    cleaned = text.strip()
    if not cleaned.isdigit():
        return None
    n = int(cleaned)
    return n if n > 0 else None


def _ask_unit_price(db, pid, t):
    """Prompt for one product's invoice price. Its own cost is offered as a hint, since a
    repeat order is usually at the same price."""
    p = db.execute("SELECT name, cost_price FROM products WHERE id = ?", (pid,)).fetchone()
    name = esc(p['name']) if p else f'#{pid}'
    text = t('Send the <b>price per unit</b> of {name} from the invoice, e.g. <code>12000</code>',
             name=name)
    if p and p['cost_price']:
        text += '\n' + t('Last known: {amount}', amount=format_rupiah(p['cost_price']))
    return text


def _advance_restock_charge(api, db, chat_id, state, states, t):
    """Move to the next invoice charge, or to the review screen once they are all in."""
    charge = next_restock_charge(state)
    if charge is None:
        state = dict(state, await_charge=None)
        states.set(chat_id, state)
        body, markup = screen_flow_review(db, 'restock', state, t)
        api.send_message(chat_id, body, markup)
        return
    states.set(chat_id, dict(state, await_charge=charge))
    api.send_message(chat_id, restock_charge_prompt(charge, t),
                     kb([btn(t('Skip'), 'r:sk'), btn(t('✖ Abandon'), 'r:c')]))


# --- Update handling ---

_denied_ids = set()  # reply to unauthorized users once per process, not per message


def handle_update(api, db, update, whitelist, tz, states):
    # Language is a shop-wide setting, re-read per update so web-UI changes apply
    # without restarting the poller (mirrors how config is loaded each cycle).
    from database import get_setting
    t = i18n.make_t(i18n.normalize_lang(get_setting(db, 'language', i18n.DEFAULT_LANG)))
    message = update.get('message')
    callback = update.get('callback_query')
    if message and isinstance(message.get('text'), str):
        sender = (message.get('from') or {}).get('id')
        chat_id = (message.get('chat') or {}).get('id')
        if sender not in whitelist:
            if sender is not None and sender not in _denied_ids:
                _denied_ids.add(sender)
                api.send_message(chat_id, t('Not authorized. Your Telegram ID: <code>{id}</code>', id=sender))
            return
        _handle_text(api, db, chat_id, message['text'], states, t)
    elif callback:
        sender = (callback.get('from') or {}).get('id')
        if sender not in whitelist:
            api.answer_callback_query(callback['id'], t('Not authorized'), show_alert=True)
            return
        _handle_callback(api, db, callback, tz, states, t)
    # other update types are ignored


def _handle_text(api, db, chat_id, text, states, t):
    state = states.get(chat_id)
    if state and state.get('await_qty'):
        pid = state.get('pending_pid')
        if pid is None:  # lost track of which product — bail to the menu
            states.pop(chat_id)
            body, markup = screen_main(t)
            api.send_message(chat_id, body, markup)
            return
        qty = parse_qty(text)
        if qty is None:
            api.send_message(chat_id, t("Couldn't read that number. Send the quantity as a whole number, e.g. <code>12</code>"))
            return
        items = dict(state['items'])
        items[pid] = items.get(pid, 0) + qty
        state = dict(state, items=items, await_qty=False)
        if state['flow'] == 'restock':
            # Restock keeps pending_pid: the invoice price for this product is the next
            # thing asked for, and the prompt has to know which product it belongs to.
            states.set(chat_id, dict(state, await_price=True))
            api.send_message(chat_id, _ask_unit_price(db, pid, t))
            return
        state = dict(state, pending_pid=None)
        states.set(chat_id, state)
        body, markup = screen_flow_picker(db, state['flow'], items, 0, t)
        api.send_message(chat_id, body, markup)
        return
    if state and state.get('await_price'):
        pid = state.get('pending_pid')
        if pid is None:  # lost track of which product — bail to the menu
            states.pop(chat_id)
            body, markup = screen_main(t)
            api.send_message(chat_id, body, markup)
            return
        price = parse_cost(text)
        if price is None:
            api.send_message(chat_id, t("Couldn't read that amount. Send the price per unit as a number, e.g. <code>12000</code>"))
            return
        prices = dict(state['prices'])
        prices[pid] = price
        state = dict(state, prices=prices, pending_pid=None, await_price=False)
        states.set(chat_id, state)
        body, markup = screen_flow_picker(db, state['flow'], state['items'], 0, t)
        api.send_message(chat_id, body, markup)
        return
    if state and state.get('await_charge'):
        amount = parse_cost(text)
        if amount is None:
            api.send_message(chat_id, t("Couldn't read that amount. Send it as a number, e.g. <code>15000</code>, or tap Skip"))
            return
        state = dict(state, **{state['await_charge']: amount})
        _advance_restock_charge(api, db, chat_id, state, states, t)
        return
    # any other text: reset and show the menu
    states.pop(chat_id)
    body, markup = screen_main(t)
    api.send_message(chat_id, body, markup)


def _handle_callback(api, db, callback, tz, states, t):
    data = callback.get('data') or ''
    msg = callback.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    message_id = msg.get('message_id')

    def show(text, markup):
        api.edit_message_text(chat_id, message_id, text, markup)

    def ack(text=None, alert=False):
        api.answer_callback_query(callback['id'], text, show_alert=alert)

    parts = data.split(':')
    try:
        if data == 'm':
            states.pop(chat_id)
            show(*screen_main(t))
        elif data == 'noop':
            pass
        elif parts[0] == 'p':
            show(*screen_products(db, int(parts[1]), t))
        elif data == 'o':
            show(*screen_orders_menu(t))
        elif parts[0] == 'ol':
            show(*screen_orders_list(db, parts[1], int(parts[2]), t))
        elif parts[0] == 'od':
            show(*screen_order_detail(db, int(parts[1]), t))
        elif parts[0] == 'oc':
            services.confirm_order(db, int(parts[1]))
            ack(t('Payment confirmed'))
            show(*screen_order_detail(db, int(parts[1]), t))
        elif parts[0] == 'of?':
            show(*screen_confirm(t('Complete order #{id}? Stock will be deducted.', id=parts[1]),
                                 f'of!:{parts[1]}', f'od:{parts[1]}', t))
        elif parts[0] == 'of!':
            services.complete_order(db, int(parts[1]))
            ack(t('Order completed'))
            show(*screen_order_detail(db, int(parts[1]), t))
        elif parts[0] == 'ox?':
            show(*screen_confirm(t('Cancel order #{id}?', id=parts[1]),
                                 f'ox!:{parts[1]}', f'od:{parts[1]}', t))
        elif parts[0] == 'ox!':
            services.cancel_order(db, int(parts[1]))
            ack(t('Order cancelled'))
            show(*screen_order_detail(db, int(parts[1]), t))
        elif parts[0] == 's':
            offset = max(0, int(parts[2]))
            show(*screen_summary(db, parts[1], offset, tz, t))
        elif data == 'rp':
            show(*screen_report_picker(t, tz))
        elif parts[0] == 'rp':
            _send_report_on_demand(api, db, chat_id, max(1, int(parts[1])), tz, show, ack, t)
            return  # acked before the slow work starts
        elif parts[0] in FLOW_PREFIXES:
            _handle_flow_callback(api, db, callback, parts, states, show, ack, t)
            return  # flow handler does its own ack
        else:
            ack()
            return
        ack()
    except ServiceError as e:
        ack(i18n.translate_error(e, t), alert=True)


def _send_report_on_demand(api, db, chat_id, offset, tz, show, ack, t):
    """Build, archive and upload one month's report to the chat that asked for it.

    Acknowledges first: rendering and uploading a PDF takes far longer than the
    few seconds Telegram gives a callback query before it shows an error.
    """
    ack(t('Building the report…'))
    try:
        _, content, data = reports.build(db, offset, tz, t.lang)
    except Exception:
        log.exception('on-demand report build (offset %s) failed', offset)
        show(t('Could not build the report.'), kb([btn(t('« Menu'), 'm')]))
        return
    try:
        api.send_document(chat_id, reports.report_filename(data['period']),
                          content, report_caption(data, t))
    except (TelegramError, OSError) as e:
        log.warning('on-demand report %s to %s failed: %s', data['period'], chat_id, e)
        # The archive write already succeeded, so say so rather than implying total failure.
        show(t('Could not send the report, but it was saved on the server.'),
             kb([btn(t('« Menu'), 'm')]))
        return
    show(t('📄 Report for {month} sent.', month=esc(data['label'])),
         kb([btn(t('« Menu'), 'm')]))


def _handle_flow_callback(api, db, callback, parts, states, show, ack, t):
    chat_id = ((callback.get('message') or {}).get('chat') or {}).get('id')
    prefix = parts[0]
    flow = FLOW_PREFIXES[prefix]
    sub = parts[1] if len(parts) > 1 else None
    state = states.get(chat_id)

    if sub is None:  # flow entry: 'no', 'r' or 'su'
        state = {'flow': flow, 'items': {}, 'pending_pid': None, 'await_qty': False}
        if flow == 'restock':
            # prices holds the invoice price per product; the three charges apply to the
            # whole invoice and default to nothing, which is the common case.
            state.update(prices={}, await_price=False, await_charge=None,
                         discount=0.0, shipping=0.0, admin_fee=0.0)
        states.set(chat_id, state)
        show(*screen_flow_picker(db, flow, {}, 0, t))
        ack()
        return

    if sub == 'c':  # abandon
        states.pop(chat_id)
        show(*screen_main(t))
        ack(t('Abandoned'))
        return

    if not state or state.get('flow') != flow:
        ack(t('Session expired — start again from the menu'), alert=True)
        show(*screen_main(t))
        return

    try:
        if sub == 'p':
            states.set(chat_id, dict(state, pending_pid=None, await_qty=False, await_price=False))
            show(*screen_flow_picker(db, flow, state['items'], int(parts[2]), t))
            ack()
        elif sub == 'i':
            pid = int(parts[2])
            states.set(chat_id, dict(state, pending_pid=pid, await_qty=False, await_price=False))
            show(*screen_flow_qty(db, flow, pid, t))
            ack()
        elif sub == 'q':
            pid = state.get('pending_pid')
            if pid is None:
                ack(t('Session expired — start again from the menu'), alert=True)
                show(*screen_main(t))
                return
            items = dict(state['items'])
            items[pid] = items.get(pid, 0) + int(parts[2])
            state = dict(state, items=items, await_qty=False)
            if flow == 'restock':
                # pending_pid stays set: the invoice price for this product comes next.
                states.set(chat_id, dict(state, await_price=True))
                api.send_message(chat_id, _ask_unit_price(db, pid, t))
                ack(t('Added'))
                return
            states.set(chat_id, dict(state, pending_pid=None))
            show(*screen_flow_picker(db, flow, items, 0, t))
            ack(t('Added'))
        elif sub == 'qc':  # user wants to type a custom quantity
            pid = state.get('pending_pid')
            if pid is None:
                ack(t('Session expired — start again from the menu'), alert=True)
                show(*screen_main(t))
                return
            states.set(chat_id, dict(state, await_qty=True))
            api.send_message(chat_id, t('Send the <b>quantity</b> as a number, e.g. <code>12</code>'))
            ack()
        elif sub == 'd':
            if not state['items']:
                ack(t('Nothing selected yet'), alert=True)
                return
            if flow != 'restock':
                show(*screen_flow_review(db, flow, state, t))
                ack()
            elif any(pid not in state['prices'] for pid in state['items']):
                ack(t('Send the unit price for every product first'), alert=True)
            else:
                # Products are priced; what is left is the discount, shipping and fee that
                # apply to the invoice as a whole.
                _advance_restock_charge(api, db, chat_id, dict(state, await_charge=None),
                                        states, t)
                ack()
        elif sub == 'sk':  # skip the charge being asked for; it stays at 0
            if not state.get('await_charge'):
                ack()
                return
            _advance_restock_charge(api, db, chat_id, state, states, t)
            ack(t('Skipped'))
        elif sub == '!':
            if flow == 'order':
                items = [{'product_id': pid, 'quantity': qty} for pid, qty in state['items'].items()]
                result = services.create_order(db, items)
                states.pop(chat_id)
                show(t('✅ Draft order <b>#{id}</b> created — total {total}',
                       id=result['order_id'], total=format_rupiah(result['total'])),
                     kb([btn(t('View order'), f"od:{result['order_id']}")], [btn(t('« Menu'), 'm')]))
                ack(t('Order created'))
            elif flow == 'selfuse':
                items = [{'product_id': pid, 'qty': qty} for pid, qty in state['items'].items()]
                result = services.create_self_use(db, items)
                states.pop(chat_id)
                show(t('✅ Self use <b>#{id}</b> saved — {total}',
                       id=result['batch_id'], total=format_rupiah(result['total_value'])),
                     kb([btn(t('« Menu'), 'm')]))
                ack(t('Self use saved'))
            else:
                if any(pid not in state['prices'] for pid in state['items']):
                    ack(t('Send the unit price for every product first'), alert=True)
                    return
                items = [{'product_id': pid, 'qty': qty, 'unit_price': state['prices'][pid]}
                         for pid, qty in state['items'].items()]
                result = services.create_restock(
                    db, items, discount=state['discount'],
                    shipping_cost=state['shipping'], admin_fee=state['admin_fee'])
                states.pop(chat_id)
                show(t('✅ Restock batch <b>#{id}</b> saved — {cost}',
                       id=result['batch_id'], cost=format_rupiah(result['total_cost'])),
                     kb([btn(t('« Menu'), 'm')]))
                ack(t('Restock saved'))
        else:
            ack()
    except ServiceError as e:
        ack(i18n.translate_error(e, t), alert=True)


# --- Stale-order alerts ---

def _fmt_hours(hours):
    """'24.0' -> '24', '12.5' -> '12.5' for display in alert text."""
    return str(int(hours)) if float(hours).is_integer() else str(hours)


def send_stale_order_alerts(api, db, cfg, t):
    """Notify whitelisted users of orders stuck in draft/confirmed past the threshold.

    Sends at most one alert per order per stalling status: the order is flagged
    again only if it later stalls in a new status (draft -> confirmed). No-op when
    the threshold is disabled or the whitelist is empty. An order is marked alerted
    only once at least one recipient received the message, so transient send
    failures are retried on the next check.
    """
    if not cfg.alert_hours or not cfg.whitelist:
        return
    hh = _fmt_hours(cfg.alert_hours)
    for order in services.find_stale_orders(db, cfg.alert_hours):
        status_label = t(STATUS_LABELS.get(order['status'], order['status']))
        text = '\n'.join([
            f"<b>⏰ {t('Order needs attention')}</b>",
            t('Order #{n} — {status}', n=order['id'], status=status_label),
            t('Stuck in this state for over {hours}h.', hours=hh),
            t('Total: {amount}', amount=format_rupiah(order['total_amount'])),
        ])
        markup = kb([btn(t('View order'), f"od:{order['id']}")])
        delivered = False
        for chat_id in cfg.whitelist:
            try:
                api.send_message(chat_id, text, markup)
                delivered = True
            except (TelegramError, OSError) as e:
                log.warning('stale-order alert for #%s to %s failed: %s',
                            order['id'], chat_id, e)
        if delivered:
            services.mark_order_alerted(db, order['id'], order['status'])


# --- Monthly report ---

def _month_seq(period):
    """'2026-06' -> a monotonic month number, so month arithmetic is just ints."""
    year, month = (int(x) for x in period.split('-'))
    return year * 12 + (month - 1)


def _pending_report_periods(last, target, limit=12):
    """Closed months after `last` up to and including `target`, oldest first.

    Oldest first and capped: a shop that was offline across several month
    boundaries catches up in order over successive checks, rather than uploading a
    year of PDFs in one cycle or silently skipping the months it missed.
    """
    try:
        first, end = _month_seq(last) + 1, _month_seq(target)
    except (ValueError, AttributeError):
        log.warning('unreadable %s value %r; reporting the latest closed month only',
                    REPORT_MARKER, last)
        return [target]
    if first > end:
        return []
    return [f'{s // 12:04d}-{s % 12 + 1:02d}'
            for s in range(first, min(end, first + limit - 1) + 1)]


def send_monthly_report(api, db, cfg, t, now=None):
    """Archive and push the report for every closed month not yet reported.

    Progress is a settings row, not in-memory state: a monthly job tracking its
    deadline in monotonic time would re-fire on every restart. On a database with
    no marker yet the marker is planted without sending anything, so installing
    the app (or upgrading to this feature) never blasts out a report for a month
    the shop was not yet recording.

    Returns the list of periods delivered, for logging and tests.
    """
    if not cfg.report_enabled:
        return []
    from database import get_setting, set_setting
    start, _ = services.get_date_range('month', 1, cfg.tz, now=now)
    target = reports.period_key(start)
    last = get_setting(db, REPORT_MARKER)
    if last is None:
        set_setting(db, REPORT_MARKER, target)
        db.commit()
        log.info('%s initialised at %s; reporting starts with the next closed month',
                 REPORT_MARKER, target)
        return []

    sent = []
    for period in _pending_report_periods(last, target):
        offset = reports.month_offset(period, cfg.tz, now=now)
        if offset is None:
            continue
        path, content, data = reports.build(db, offset, cfg.tz, t.lang, now=now)
        caption = report_caption(data, t)
        filename = reports.report_filename(period)
        delivered = False
        for chat_id in cfg.whitelist:
            try:
                api.send_document(chat_id, filename, content, caption)
                delivered = True
            except (TelegramError, OSError) as e:
                log.warning('monthly report %s to %s failed: %s', period, chat_id, e)
        # Advance only once someone has it, so a Telegram outage retries next check.
        # With an empty whitelist there is nobody to deliver to and the archived
        # file is the whole deliverable, so that counts as done.
        if not delivered and cfg.whitelist:
            log.warning('monthly report %s reached nobody; will retry', period)
            break  # stop here so the backlog stays in order
        set_setting(db, REPORT_MARKER, period)
        db.commit()
        sent.append(period)
        log.info('monthly report %s delivered to %d chat(s), archived at %s',
                 period, len(cfg.whitelist), path)
    return sent


# --- Poller ---

class BotPoller(threading.Thread):
    def __init__(self, db_factory=database.get_db, api_factory=TelegramAPI,
                 poll_timeout=25, sleep=time.sleep, clock=time.monotonic,
                 alert_interval=300, report_interval=3600):
        super().__init__(daemon=True, name='telegram-bot')
        self.db_factory = db_factory
        self.api_factory = api_factory
        self.poll_timeout = poll_timeout
        self.sleep = sleep
        self.clock = clock
        self.alert_interval = alert_interval
        self.report_interval = report_interval
        self.states = ChatStates()
        self._token = None
        self._api = None
        self._offset = None
        self._next_alert_check = 0
        self._next_report_check = 0

    def _maybe_check_alerts(self, cfg):
        """Run the stale-order scan at most once per `alert_interval` seconds."""
        now = self.clock()
        if now < self._next_alert_check:
            return
        self._next_alert_check = now + self.alert_interval
        db = self.db_factory()
        try:
            from database import get_setting
            t = i18n.make_t(i18n.normalize_lang(get_setting(db, 'language', i18n.DEFAULT_LANG)))
            send_stale_order_alerts(self._api, db, cfg, t)
        except Exception:
            log.exception('stale-order alert check failed')
        finally:
            db.close()

    def _maybe_send_report(self, cfg):
        """Look for an unreported closed month at most once per `report_interval`.

        The interval only throttles a cheap settings lookup; the guard against
        sending twice is the persisted marker, which survives the restart that
        resets this in-memory deadline.
        """
        now = self.clock()
        if now < self._next_report_check:
            return
        self._next_report_check = now + self.report_interval
        db = self.db_factory()
        try:
            from database import get_setting
            t = i18n.make_t(i18n.normalize_lang(get_setting(db, 'language', i18n.DEFAULT_LANG)))
            send_monthly_report(self._api, db, cfg, t)
        except Exception:
            log.exception('monthly report check failed')
        finally:
            db.close()

    def _cycle(self):
        db = self.db_factory()
        try:
            cfg = load_bot_config(db)
        finally:
            db.close()
        if not cfg.enabled or not cfg.token:
            self.sleep(5)
            return
        if cfg.token != self._token:
            # update_id sequences are per-bot: a new token needs a fresh offset
            self._token = cfg.token
            self._api = self.api_factory(cfg.token)
            self._offset = None
        self._maybe_check_alerts(cfg)
        self._maybe_send_report(cfg)
        updates = self._api.get_updates(offset=self._offset, timeout=self.poll_timeout)
        for u in updates:
            # advance even if handling fails: never re-loop a poison update
            self._offset = u['update_id'] + 1
            db = self.db_factory()
            try:
                handle_update(self._api, db, u, cfg.whitelist, cfg.tz, self.states)
            except Exception:
                log.exception('error handling update %s', u.get('update_id'))
            finally:
                db.close()

    def run(self):
        log.info('telegram bot poller started')
        backoff = 1
        while True:
            try:
                self._cycle()
                backoff = 1
            except Exception as e:
                log.warning('poll cycle failed (%s); retrying in %ss', e, backoff)
                self.sleep(backoff)
                backoff = min(backoff * 2, 60)
