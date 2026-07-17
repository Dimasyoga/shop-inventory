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

BotConfig = namedtuple('BotConfig', 'enabled token whitelist tz')


def parse_whitelist(raw):
    ids = set()
    for tok in (raw or '').replace(',', ' ').split():
        if tok.lstrip('-').isdigit():
            ids.add(int(tok))
    return ids


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
        tz=tz)


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


def screen_main():
    text = '<b>📦 Shop Inventory</b>\nWhat do you want to do?'
    return text, kb(
        [btn('📦 Products', 'p:0'), btn('🛒 Orders', 'o')],
        [btn('🆕 New order', 'no'), btn('📥 Restock', 'r')],
        [btn('📈 Sales summary', 's:d:0')])


def screen_products(db, page):
    rows, has_more = services.list_products(db, page=page, page_size=8)
    if not rows and page == 0:
        return 'No products yet.', kb([btn('« Menu', 'm')])
    lines = ['<b>📦 Products</b>']
    for p in rows:
        sku = f" [{esc(p['sku'])}]" if p['sku'] else ''
        lines.append(f"• {esc(p['name'])}{sku} — {format_rupiah(p['price'])} (stock {p['stock_qty']})")
    nav = []
    if page > 0:
        nav.append(btn('◀ Prev', f'p:{page - 1}'))
    if has_more:
        nav.append(btn('Next ▶', f'p:{page + 1}'))
    rows_kb = [nav] if nav else []
    rows_kb.append([btn('« Menu', 'm')])
    return '\n'.join(lines), kb(*rows_kb)


def screen_orders_menu():
    return '<b>🛒 Orders</b>\nPick a status:', kb(
        [btn('📝 Draft', 'ol:d:0'), btn('💳 Confirmed', 'ol:c:0')],
        [btn('✅ Completed', 'ol:f:0'), btn('❌ Cancelled', 'ol:x:0')],
        [btn('All', 'ol:a:0'), btn('« Menu', 'm')])


def screen_orders_list(db, status_code, page):
    status = STATUS_CODES.get(status_code)
    rows, has_more = services.list_orders(db, status=status, page=page, page_size=10)
    label = STATUS_LABELS.get(status, 'All orders') if status else 'All orders'
    if not rows and page == 0:
        return f'{label}: nothing here.', kb([btn('« Orders', 'o'), btn('« Menu', 'm')])
    lines = [f'<b>🛒 {label}</b>']
    buttons = []
    for o in rows:
        lines.append(f"#{o['id']} — {format_rupiah(o['total_amount'])} — {esc(o['status'])}")
        buttons.append(btn(f"#{o['id']}", f"od:{o['id']}"))
    # order buttons in rows of 5
    rows_kb = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    nav = []
    if page > 0:
        nav.append(btn('◀ Prev', f'ol:{status_code}:{page - 1}'))
    if has_more:
        nav.append(btn('Next ▶', f'ol:{status_code}:{page + 1}'))
    if nav:
        rows_kb.append(nav)
    rows_kb.append([btn('« Orders', 'o'), btn('« Menu', 'm')])
    return '\n'.join(lines), kb(*rows_kb)


def screen_order_detail(db, order_id):
    order, items = services.get_order(db, order_id)
    lines = [f"<b>Order #{order['id']}</b> — {STATUS_LABELS.get(order['status'], esc(order['status']))}"]
    for i in items:
        lines.append(f"• {esc(i['product_name'])} ×{i['quantity']} = {format_rupiah(i['subtotal'])}")
    lines.append(f"<b>Total: {format_rupiah(order['total_amount'])}</b>")
    actions = []
    if order['status'] == 'draft':
        actions.append(btn('✅ Confirm payment', f'oc:{order_id}'))
        actions.append(btn('❌ Cancel', f'ox?:{order_id}'))
    elif order['status'] == 'confirmed':
        actions.append(btn('💰 Complete', f'of?:{order_id}'))
        actions.append(btn('❌ Cancel', f'ox?:{order_id}'))
    rows_kb = [actions] if actions else []
    rows_kb.append([btn('« Orders', 'o'), btn('« Menu', 'm')])
    return '\n'.join(lines), kb(*rows_kb)


def screen_confirm(question, yes_data, no_data):
    return question, kb([btn('✅ Yes', yes_data), btn('« No', no_data)])


def screen_summary(db, unit_code, offset, tz):
    unit = UNIT_CODES[unit_code]
    s = services.sales_summary(db, unit, offset, tz)
    start = s['start']
    if unit == 'day':
        label = start.strftime('%a %d %b %Y')
    elif unit == 'week':
        label = f"Week of {start.strftime('%d %b %Y')}"
    elif unit == 'month':
        label = start.strftime('%B %Y')
    else:
        label = start.strftime('%Y')
    text = '\n'.join([
        f'<b>📈 Sales — {esc(label)}</b>',
        f"Revenue: {format_rupiah(s['total_revenue'])}",
        f"Orders: {s['total_orders']}   Items sold: {s['total_items_sold']}",
        f"Restock cost: {format_rupiah(s['restock_cost'])}",
        f"<b>Net profit: {format_rupiah(s['net_profit'])}</b>",
    ])
    unit_row = [btn(('· ' if c == unit_code else '') + UNIT_LABELS[c], f's:{c}:0')
                for c in ('d', 'w', 'm', 'y')]
    nav = [btn('◀', f's:{unit_code}:{offset + 1}')]
    if offset > 0:
        nav.append(btn('▶', f's:{unit_code}:{offset - 1}'))
    nav.append(btn('« Menu', 'm'))
    return text, kb(unit_row, nav)


# --- Order / restock flow screens (stateful) ---

def _cart_lines(db, items):
    lines = []
    for pid, qty in items.items():
        p = db.execute("SELECT name, price FROM products WHERE id = ?", (pid,)).fetchone()
        name = esc(p['name']) if p else f'#{pid}'
        lines.append(f"• {name} ×{qty}")
    return lines


def screen_flow_picker(db, flow, items, page):
    prefix = 'no' if flow == 'order' else 'r'
    title = '🆕 New order' if flow == 'order' else '📥 Restock'
    rows, has_more = services.list_products(db, page=page, page_size=8)
    lines = [f'<b>{title}</b>']
    if items:
        lines += ['Selected:'] + _cart_lines(db, items)
    lines.append('Pick a product:')
    buttons = []
    for p in rows:
        stock = f" ({p['stock_qty']})" if flow == 'order' else ''
        buttons.append([btn(f"{p['name'][:28]}{stock}", f'{prefix}:i:{p["id"]}')])
    nav = []
    if page > 0:
        nav.append(btn('◀ Prev', f'{prefix}:p:{page - 1}'))
    if has_more:
        nav.append(btn('Next ▶', f'{prefix}:p:{page + 1}'))
    if nav:
        buttons.append(nav)
    tail = [btn('✔ Done', f'{prefix}:d')] if items else []
    tail.append(btn('✖ Abandon', f'{prefix}:c'))
    buttons.append(tail)
    return '\n'.join(lines), {'inline_keyboard': buttons}


def screen_flow_qty(db, flow, pid):
    prefix = 'no' if flow == 'order' else 'r'
    p = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    name = esc(p['name']) if p else f'#{pid}'
    text = f'How many <b>{name}</b>?'
    if flow == 'order' and p:
        text += f" (stock: {p['stock_qty']})"
    qty_row = [btn(str(n), f'{prefix}:q:{n}') for n in QTY_CHOICES]
    return text, kb(qty_row[:3], qty_row[3:], [btn('« Back', f'{prefix}:p:0')])


def screen_flow_review(db, flow, state):
    prefix = 'no' if flow == 'order' else 'r'
    title = '🆕 New order — review' if flow == 'order' else '📥 Restock — review'
    lines = [f'<b>{title}</b>'] + _cart_lines(db, state['items'])
    if flow == 'order':
        total = 0
        for pid, qty in state['items'].items():
            p = db.execute("SELECT price FROM products WHERE id = ?", (pid,)).fetchone()
            if p:
                total += p['price'] * qty
        lines.append(f'<b>Total: {format_rupiah(total)}</b>')
        action = btn('✅ Create draft order', 'no:!')
    else:
        cost = state.get('cost')
        lines.append(f"Total cost: <b>{format_rupiah(cost) if cost is not None else '—'}</b>")
        action = btn('✅ Save restock', 'r:!')
    return '\n'.join(lines), kb([action],
                                [btn('+ Add more', f'{prefix}:p:0'), btn('✖ Abandon', f'{prefix}:c')])


def parse_cost(text):
    """'Rp 150.000', '150000', '150,000' -> 150000.0; None when unparseable."""
    cleaned = text.strip().lower().replace('rp', '').replace(' ', '')
    cleaned = cleaned.replace('.', '').replace(',', '')
    if not cleaned.isdigit():
        return None
    return float(cleaned)


# --- Update handling ---

_denied_ids = set()  # reply to unauthorized users once per process, not per message


def handle_update(api, db, update, whitelist, tz, states):
    message = update.get('message')
    callback = update.get('callback_query')
    if message and isinstance(message.get('text'), str):
        sender = (message.get('from') or {}).get('id')
        chat_id = (message.get('chat') or {}).get('id')
        if sender not in whitelist:
            if sender is not None and sender not in _denied_ids:
                _denied_ids.add(sender)
                api.send_message(chat_id, f'Not authorized. Your Telegram ID: <code>{sender}</code>')
            return
        _handle_text(api, db, chat_id, message['text'], states)
    elif callback:
        sender = (callback.get('from') or {}).get('id')
        if sender not in whitelist:
            api.answer_callback_query(callback['id'], 'Not authorized', show_alert=True)
            return
        _handle_callback(api, db, callback, tz, states)
    # other update types are ignored


def _handle_text(api, db, chat_id, text, states):
    state = states.get(chat_id)
    if state and state.get('await_cost'):
        cost = parse_cost(text)
        if cost is None:
            api.send_message(chat_id, "Couldn't read that amount. Send the total cost as a number, e.g. <code>150000</code>")
            return
        state = dict(state, cost=cost, await_cost=False)
        states.set(chat_id, state)
        body, markup = screen_flow_review(db, 'restock', state)
        api.send_message(chat_id, body, markup)
        return
    # any other text: reset and show the menu
    states.pop(chat_id)
    body, markup = screen_main()
    api.send_message(chat_id, body, markup)


def _handle_callback(api, db, callback, tz, states):
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
            show(*screen_main())
        elif data == 'noop':
            pass
        elif parts[0] == 'p':
            show(*screen_products(db, int(parts[1])))
        elif data == 'o':
            show(*screen_orders_menu())
        elif parts[0] == 'ol':
            show(*screen_orders_list(db, parts[1], int(parts[2])))
        elif parts[0] == 'od':
            show(*screen_order_detail(db, int(parts[1])))
        elif parts[0] == 'oc':
            services.confirm_order(db, int(parts[1]))
            ack('Payment confirmed')
            show(*screen_order_detail(db, int(parts[1])))
        elif parts[0] == 'of?':
            show(*screen_confirm(f'Complete order #{parts[1]}? Stock will be deducted.',
                                 f'of!:{parts[1]}', f'od:{parts[1]}'))
        elif parts[0] == 'of!':
            services.complete_order(db, int(parts[1]))
            ack('Order completed')
            show(*screen_order_detail(db, int(parts[1])))
        elif parts[0] == 'ox?':
            show(*screen_confirm(f'Cancel order #{parts[1]}?',
                                 f'ox!:{parts[1]}', f'od:{parts[1]}'))
        elif parts[0] == 'ox!':
            services.cancel_order(db, int(parts[1]))
            ack('Order cancelled')
            show(*screen_order_detail(db, int(parts[1])))
        elif parts[0] == 's':
            offset = max(0, int(parts[2]))
            show(*screen_summary(db, parts[1], offset, tz))
        elif parts[0] in ('no', 'r'):
            _handle_flow_callback(api, db, callback, parts, states, show, ack)
            return  # flow handler does its own ack
        else:
            ack()
            return
        ack()
    except ServiceError as e:
        ack(str(e), alert=True)


def _handle_flow_callback(api, db, callback, parts, states, show, ack):
    chat_id = ((callback.get('message') or {}).get('chat') or {}).get('id')
    prefix = parts[0]
    flow = 'order' if prefix == 'no' else 'restock'
    sub = parts[1] if len(parts) > 1 else None
    state = states.get(chat_id)

    if sub is None:  # flow entry: 'no' or 'r'
        state = {'flow': flow, 'items': {}, 'pending_pid': None}
        if flow == 'restock':
            state.update(await_cost=False, cost=None)
        states.set(chat_id, state)
        show(*screen_flow_picker(db, flow, {}, 0))
        ack()
        return

    if sub == 'c':  # abandon
        states.pop(chat_id)
        show(*screen_main())
        ack('Abandoned')
        return

    if not state or state.get('flow') != flow:
        ack('Session expired — start again from the menu', alert=True)
        show(*screen_main())
        return

    try:
        if sub == 'p':
            show(*screen_flow_picker(db, flow, state['items'], int(parts[2])))
            ack()
        elif sub == 'i':
            pid = int(parts[2])
            states.set(chat_id, dict(state, pending_pid=pid))
            show(*screen_flow_qty(db, flow, pid))
            ack()
        elif sub == 'q':
            pid = state.get('pending_pid')
            if pid is None:
                ack('Session expired — start again from the menu', alert=True)
                show(*screen_main())
                return
            items = dict(state['items'])
            items[pid] = items.get(pid, 0) + int(parts[2])
            states.set(chat_id, dict(state, items=items, pending_pid=None))
            show(*screen_flow_picker(db, flow, items, 0))
            ack('Added')
        elif sub == 'd':
            if not state['items']:
                ack('Nothing selected yet', alert=True)
                return
            if flow == 'order':
                show(*screen_flow_review(db, 'order', state))
                ack()
            else:
                states.set(chat_id, dict(state, await_cost=True))
                api.send_message(chat_id, 'Send the <b>total cost</b> of this restock as a message, e.g. <code>150000</code>')
                ack()
        elif sub == '!':
            if flow == 'order':
                items = [{'product_id': pid, 'quantity': qty} for pid, qty in state['items'].items()]
                result = services.create_order(db, items)
                states.pop(chat_id)
                show(f"✅ Draft order <b>#{result['order_id']}</b> created — total {format_rupiah(result['total'])}",
                     kb([btn('View order', f"od:{result['order_id']}")], [btn('« Menu', 'm')]))
                ack('Order created')
            else:
                if state.get('cost') is None:
                    ack('Send the total cost first', alert=True)
                    return
                items = [{'product_id': pid, 'qty': qty} for pid, qty in state['items'].items()]
                batch_id = services.create_restock(db, items, state['cost'])
                states.pop(chat_id)
                show(f'✅ Restock batch <b>#{batch_id}</b> saved — {format_rupiah(state["cost"])}',
                     kb([btn('« Menu', 'm')]))
                ack('Restock saved')
        else:
            ack()
    except ServiceError as e:
        ack(str(e), alert=True)


# --- Poller ---

class BotPoller(threading.Thread):
    def __init__(self, db_factory=database.get_db, api_factory=TelegramAPI,
                 poll_timeout=25, sleep=time.sleep):
        super().__init__(daemon=True, name='telegram-bot')
        self.db_factory = db_factory
        self.api_factory = api_factory
        self.poll_timeout = poll_timeout
        self.sleep = sleep
        self.states = ChatStates()
        self._token = None
        self._api = None
        self._offset = None

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
