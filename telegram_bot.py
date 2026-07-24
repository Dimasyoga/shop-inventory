"""Telegram bot: browse products, manage orders, create restocks, view sales summary.

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
import threading
import time
import urllib.error
import urllib.request
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import namedtuple

import database
import i18n
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
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{self.token}/{method}',
            data=json.dumps(params).encode(),
            headers={'Content-Type': 'application/json'})
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

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        params = {'callback_query_id': callback_query_id}
        if text:
            params['text'] = text[:200]
        if show_alert:
            params['show_alert'] = True
        return self.call('answerCallbackQuery', **params)


# --- Config ---

BotConfig = namedtuple('BotConfig', 'enabled token whitelist tz alert_hours')


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
    from database import get_setting
    tz_name = get_setting(db, 'shop_timezone', 'Asia/Jakarta')
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc  # never crash the poller over a bad setting
    return BotConfig(
        enabled=get_setting(db, 'telegram_enabled', '0') == '1',
        token=get_setting(db, 'telegram_bot_token', '') or '',
        whitelist=parse_whitelist(get_setting(db, 'telegram_whitelist', '')),
        tz=tz,
        alert_hours=parse_alert_hours(get_setting(db, 'order_alert_hours', '24')))


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
UNIT_CODES = {'d': 'day', 'w': 'week', 'm': 'month', 'y': 'year'}
UNIT_LABELS = {'d': 'Day', 'w': 'Week', 'm': 'Month', 'y': 'Year'}


def screen_main(t):
    text = f'<b>📦 {t("Shop Inventory")}</b>\n{t("What do you want to do?")}'
    return text, kb(
        [btn(t('📦 Products'), 'p:0'), btn(t('🛒 Orders'), 'o')],
        [btn(t('🆕 New order'), 'no'), btn(t('📥 Restock'), 'r')],
        [btn(t('📈 Sales summary'), 's:d:0')])


def screen_products(db, page, t):
    rows, has_more = services.list_products(db, page=page, page_size=8)
    if not rows and page == 0:
        return t('No products yet.'), kb([btn(t('« Menu'), 'm')])
    lines = [f'<b>📦 {t("Products")}</b>']
    for p in rows:
        sku = f" [{esc(p['sku'])}]" if p['sku'] else ''
        lines.append(f"• {esc(p['name'])}{sku} — {format_rupiah(p['price'])} ({t('stock {n}', n=p['stock_qty'])})")
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
        label = f'{i18n.month_name(start.month, lang)} {start.year}'
    else:
        label = str(start.year)
    text = '\n'.join([
        f'<b>{t("📈 Sales — {label}", label=esc(label))}</b>',
        t('Revenue: {amount}', amount=format_rupiah(s['total_revenue'])),
        t('Orders: {orders}   Items sold: {items}', orders=s['total_orders'], items=s['total_items_sold']),
        t('Restock cost: {amount}', amount=format_rupiah(s['restock_cost'])),
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

def _cart_lines(db, items):
    lines = []
    for pid, qty in items.items():
        p = db.execute("SELECT name, price FROM products WHERE id = ?", (pid,)).fetchone()
        name = esc(p['name']) if p else f'#{pid}'
        lines.append(f"• {name} ×{qty}")
    return lines


def screen_flow_picker(db, flow, items, page, t):
    prefix = 'no' if flow == 'order' else 'r'
    title = t('🆕 New order') if flow == 'order' else t('📥 Restock')
    rows, has_more = services.list_products(db, page=page, page_size=8)
    lines = [f'<b>{title}</b>']
    if items:
        lines += [t('Selected:')] + _cart_lines(db, items)
    lines.append(t('Pick a product:'))
    buttons = []
    for p in rows:
        stock = f" ({p['stock_qty']})" if flow == 'order' else ''
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
    prefix = 'no' if flow == 'order' else 'r'
    p = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    name = esc(p['name']) if p else f'#{pid}'
    text = t('How many <b>{name}</b>?', name=name)
    if flow == 'order' and p:
        text += t(' (stock: {n})', n=p['stock_qty'])
    text += '\n' + t('Tap a number, or ✏️ Custom to type any amount.')
    qty_row = [btn(str(n), f'{prefix}:q:{n}') for n in QTY_CHOICES]
    return text, kb(qty_row[:3], qty_row[3:],
                    [btn(t('✏️ Custom'), f'{prefix}:qc'), btn(t('« Back'), f'{prefix}:p:0')])


def screen_flow_review(db, flow, state, t):
    prefix = 'no' if flow == 'order' else 'r'
    title = t('🆕 New order — review') if flow == 'order' else t('📥 Restock — review')
    lines = [f'<b>{title}</b>'] + _cart_lines(db, state['items'])
    if flow == 'order':
        total = 0
        for pid, qty in state['items'].items():
            p = db.execute("SELECT price FROM products WHERE id = ?", (pid,)).fetchone()
            if p:
                total += p['price'] * qty
        lines.append(f"<b>{t('Total: {amount}', amount=format_rupiah(total))}</b>")
        action = btn(t('✅ Create draft order'), 'no:!')
    else:
        cost = state.get('cost')
        cost_str = format_rupiah(cost) if cost is not None else '—'
        lines.append(t('Total cost: <b>{cost}</b>', cost=cost_str))
        action = btn(t('✅ Save restock'), 'r:!')
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
        state = dict(state, items=items, pending_pid=None, await_qty=False)
        states.set(chat_id, state)
        body, markup = screen_flow_picker(db, state['flow'], items, 0, t)
        api.send_message(chat_id, body, markup)
        return
    if state and state.get('await_cost'):
        cost = parse_cost(text)
        if cost is None:
            api.send_message(chat_id, t("Couldn't read that amount. Send the total cost as a number, e.g. <code>150000</code>"))
            return
        state = dict(state, cost=cost, await_cost=False)
        states.set(chat_id, state)
        body, markup = screen_flow_review(db, 'restock', state, t)
        api.send_message(chat_id, body, markup)
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
        elif parts[0] in ('no', 'r'):
            _handle_flow_callback(api, db, callback, parts, states, show, ack, t)
            return  # flow handler does its own ack
        else:
            ack()
            return
        ack()
    except ServiceError as e:
        ack(i18n.translate_error(e, t), alert=True)


def _handle_flow_callback(api, db, callback, parts, states, show, ack, t):
    chat_id = ((callback.get('message') or {}).get('chat') or {}).get('id')
    prefix = parts[0]
    flow = 'order' if prefix == 'no' else 'restock'
    sub = parts[1] if len(parts) > 1 else None
    state = states.get(chat_id)

    if sub is None:  # flow entry: 'no' or 'r'
        state = {'flow': flow, 'items': {}, 'pending_pid': None, 'await_qty': False}
        if flow == 'restock':
            state.update(await_cost=False, cost=None)
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
            states.set(chat_id, dict(state, pending_pid=None, await_qty=False))
            show(*screen_flow_picker(db, flow, state['items'], int(parts[2]), t))
            ack()
        elif sub == 'i':
            pid = int(parts[2])
            states.set(chat_id, dict(state, pending_pid=pid, await_qty=False))
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
            states.set(chat_id, dict(state, items=items, pending_pid=None, await_qty=False))
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
            if flow == 'order':
                show(*screen_flow_review(db, 'order', state, t))
                ack()
            else:
                states.set(chat_id, dict(state, await_cost=True))
                api.send_message(chat_id, t('Send the <b>total cost</b> of this restock as a message, e.g. <code>150000</code>'))
                ack()
        elif sub == '!':
            if flow == 'order':
                items = [{'product_id': pid, 'quantity': qty} for pid, qty in state['items'].items()]
                result = services.create_order(db, items)
                states.pop(chat_id)
                show(t('✅ Draft order <b>#{id}</b> created — total {total}',
                       id=result['order_id'], total=format_rupiah(result['total'])),
                     kb([btn(t('View order'), f"od:{result['order_id']}")], [btn(t('« Menu'), 'm')]))
                ack(t('Order created'))
            else:
                if state.get('cost') is None:
                    ack(t('Send the total cost first'), alert=True)
                    return
                items = [{'product_id': pid, 'qty': qty} for pid, qty in state['items'].items()]
                batch_id = services.create_restock(db, items, state['cost'])
                states.pop(chat_id)
                show(t('✅ Restock batch <b>#{id}</b> saved — {cost}',
                       id=batch_id, cost=format_rupiah(state["cost"])),
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


# --- Poller ---

class BotPoller(threading.Thread):
    def __init__(self, db_factory=database.get_db, api_factory=TelegramAPI,
                 poll_timeout=25, sleep=time.sleep, clock=time.monotonic,
                 alert_interval=300):
        super().__init__(daemon=True, name='telegram-bot')
        self.db_factory = db_factory
        self.api_factory = api_factory
        self.poll_timeout = poll_timeout
        self.sleep = sleep
        self.clock = clock
        self.alert_interval = alert_interval
        self.states = ChatStates()
        self._token = None
        self._api = None
        self._offset = None
        self._next_alert_check = 0

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
