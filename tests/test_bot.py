"""Bot handler tests: drive handle_update with synthetic Telegram updates
against the temp DB, with a FakeAPI recording outgoing calls. No network."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import database
import telegram_bot
from telegram_bot import ChatStates, handle_update, parse_cost, parse_qty

JKT = ZoneInfo("Asia/Jakarta")
OWNER = 111  # whitelisted
CHAT = 500


class FakeAPI:
    def __init__(self):
        self.calls = []

    def _record(self, method, **params):
        self.calls.append((method, params))
        return {}

    def send_message(self, chat_id, text, reply_markup=None):
        return self._record("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return self._record("editMessageText", chat_id=chat_id, message_id=message_id,
                            text=text, reply_markup=reply_markup)

    def answer_callback_query(self, cq_id, text=None, show_alert=False):
        return self._record("answerCallbackQuery", id=cq_id, text=text, show_alert=show_alert)

    def send_document(self, chat_id, filename, content, caption=None):
        return self._record("sendDocument", chat_id=chat_id, filename=filename,
                            content=content, caption=caption)

    # convenience accessors
    def sent(self, method):
        return [p for m, p in self.calls if m == method]

    def last_text(self):
        for m, p in reversed(self.calls):
            if m in ("sendMessage", "editMessageText"):
                return p["text"]
        return None

    def last_markup(self):
        for m, p in reversed(self.calls):
            if m in ("sendMessage", "editMessageText") and p.get("reply_markup"):
                return p["reply_markup"]
        return None

    def buttons(self):
        markup = self.last_markup()
        if not markup:
            return []
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]

    def buttons_text(self):
        markup = self.last_markup()
        if not markup:
            return []
        return [b["text"] for row in markup["inline_keyboard"] for b in row]


def text_update(text, sender=OWNER, chat=CHAT):
    return {"update_id": 1, "message": {"from": {"id": sender}, "chat": {"id": chat}, "text": text}}


def cb_update(data, sender=OWNER, chat=CHAT, message_id=99):
    return {"update_id": 2, "callback_query": {
        "id": "cq1", "from": {"id": sender},
        "data": data, "message": {"chat": {"id": chat}, "message_id": message_id}}}


@pytest.fixture
def bot(db_path):
    """(api, drive) — drive(update) runs handle_update with a fresh DB connection."""
    telegram_bot._denied_ids.clear()
    api = FakeAPI()
    states = ChatStates()

    def drive(update, whitelist=frozenset({OWNER})):
        db = database.get_db()
        try:
            handle_update(api, db, update, whitelist, JKT, states)
        finally:
            db.close()

    drive.states = states
    return api, drive


def make_product(name="Kopi", stock=10, price=5000, sku=None):
    db = database.get_db()
    cur = db.execute("INSERT INTO products (name, sku, price, stock_qty) VALUES (?,?,?,?)",
                     (name, sku, price, stock))
    db.commit()
    pid = cur.lastrowid
    db.close()
    return pid


def db_one(sql, params=()):
    db = database.get_db()
    row = db.execute(sql, params).fetchone()
    db.close()
    return row


# --- Auth ---

def test_unauthorized_message_gets_denial_with_id(bot):
    api, drive = bot
    drive(text_update("/start", sender=999))
    msgs = api.sent("sendMessage")
    assert len(msgs) == 1
    assert "999" in msgs[0]["text"]
    assert "Not authorized" in msgs[0]["text"]


def test_denial_sent_only_once(bot):
    api, drive = bot
    drive(text_update("/start", sender=999))
    drive(text_update("/start", sender=999))
    assert len(api.sent("sendMessage")) == 1


def test_unauthorized_callback_rejected_without_effect(bot):
    api, drive = bot
    pid = make_product(stock=5)
    db = database.get_db()
    import services
    oid = services.create_order(db, [{"product_id": pid, "quantity": 1}])["order_id"]
    services.confirm_order(db, oid)
    db.close()
    drive(cb_update(f"of!:{oid}", sender=999))
    assert db_one("SELECT status FROM orders WHERE id=?", (oid,))["status"] == "confirmed"
    answers = api.sent("answerCallbackQuery")
    assert answers and answers[0]["show_alert"]


# --- Menu & products ---

def test_start_shows_main_menu(bot):
    api, drive = bot
    drive(text_update("/start"))
    assert "p:0" in api.buttons() and "no" in api.buttons() and "r" in api.buttons()


def test_product_list_paginates(bot):
    api, drive = bot
    for i in range(9):
        make_product(name=f"Item{i:02d}")
    drive(cb_update("p:0"))
    assert "Item00" in api.last_text()
    assert "p:1" in api.buttons()  # Next
    drive(cb_update("p:1"))
    assert "Item08" in api.last_text()
    assert "p:0" in api.buttons()  # Prev
    assert api.sent("editMessageText")  # navigation edits, not resends


def test_product_name_html_escaped(bot):
    api, drive = bot
    make_product(name="<b>bold</b> & <script>")
    drive(cb_update("p:0"))
    assert "<b>bold</b>" not in api.last_text()
    assert "&lt;b&gt;" in api.last_text()


# --- Order management ---

@pytest.fixture
def confirmed_order(db_path):
    pid = make_product(stock=10)
    db = database.get_db()
    import services
    oid = services.create_order(db, [{"product_id": pid, "quantity": 4}])["order_id"]
    services.confirm_order(db, oid)
    db.close()
    return pid, oid


def test_order_detail_buttons_match_status(bot, confirmed_order):
    api, drive = bot
    _, oid = confirmed_order
    drive(cb_update(f"od:{oid}"))
    assert f"of?:{oid}" in api.buttons()  # complete offered
    assert f"ox?:{oid}" in api.buttons()  # cancel offered
    assert f"oc:{oid}" not in api.buttons()  # confirm not offered on confirmed


def test_complete_asks_then_executes(bot, confirmed_order):
    api, drive = bot
    pid, oid = confirmed_order
    drive(cb_update(f"of?:{oid}"))
    assert f"of!:{oid}" in api.buttons()  # Yes button
    drive(cb_update(f"of!:{oid}"))
    assert db_one("SELECT status FROM orders WHERE id=?", (oid,))["status"] == "completed"
    assert db_one("SELECT stock_qty FROM products WHERE id=?", (pid,))["stock_qty"] == 6
    log = db_one("SELECT change_qty, reason FROM stock_logs WHERE product_id=?", (pid,))
    assert (log["change_qty"], log["reason"]) == (-4, f"sale order #{oid}")


def test_double_complete_is_rejected_cleanly(bot, confirmed_order):
    api, drive = bot
    pid, oid = confirmed_order
    drive(cb_update(f"of!:{oid}"))
    drive(cb_update(f"of!:{oid}"))
    assert db_one("SELECT stock_qty FROM products WHERE id=?", (pid,))["stock_qty"] == 6  # one decrement
    alerts = [p for p in api.sent("answerCallbackQuery") if p["show_alert"]]
    assert alerts and "Only confirmed" in alerts[-1]["text"]


def test_cancel_flow(bot, confirmed_order):
    api, drive = bot
    _, oid = confirmed_order
    drive(cb_update(f"ox?:{oid}"))
    drive(cb_update(f"ox!:{oid}"))
    assert db_one("SELECT status FROM orders WHERE id=?", (oid,))["status"] == "cancelled"


# --- New order flow ---

def test_full_order_flow_creates_draft(bot):
    api, drive = bot
    p1 = make_product(name="Kopi", price=5000)
    p2 = make_product(name="Teh", price=3000)
    drive(cb_update("no"))               # start flow -> picker
    drive(cb_update(f"no:i:{p1}"))       # pick Kopi -> qty keyboard
    assert "no:q:2" in api.buttons()
    drive(cb_update("no:q:2"))           # 2x Kopi -> picker
    drive(cb_update(f"no:i:{p2}"))
    drive(cb_update("no:q:5"))           # 5x Teh
    drive(cb_update("no:d"))             # done -> review
    assert "25.000" in api.last_text()   # 2*5000 + 5*3000
    drive(cb_update("no:!"))             # create
    order = db_one("SELECT * FROM orders WHERE status='draft'")
    assert order["total_amount"] == 25000
    n_items = db_one("SELECT COUNT(*) AS c FROM order_items WHERE order_id=?", (order["id"],))["c"]
    assert n_items == 2
    assert drive.states.get(CHAT) is None  # state cleared


def test_order_flow_oversell_rejected_at_create(bot):
    api, drive = bot
    pid = make_product(stock=1)
    drive(cb_update("no"))
    drive(cb_update(f"no:i:{pid}"))
    drive(cb_update("no:q:5"))
    drive(cb_update("no:d"))
    drive(cb_update("no:!"))
    assert db_one("SELECT COUNT(*) AS c FROM orders")["c"] == 0
    alerts = [p for p in api.sent("answerCallbackQuery") if p["show_alert"]]
    assert alerts and "Insufficient stock" in alerts[-1]["text"]


def test_order_flow_custom_quantity(bot):
    api, drive = bot
    pid = make_product(name="Kopi", price=5000, stock=100)
    drive(cb_update("no"))               # start flow -> picker
    drive(cb_update(f"no:i:{pid}"))      # pick Kopi -> qty keyboard
    assert "no:qc" in api.buttons()      # custom-quantity option offered
    drive(cb_update("no:qc"))            # choose to type a custom amount
    assert "quantity" in api.last_text().lower()
    drive(text_update("37"))             # any number, not in QTY_CHOICES
    drive(cb_update("no:d"))             # done -> review
    assert "185.000" in api.last_text()  # 37 * 5000
    drive(cb_update("no:!"))             # create
    order = db_one("SELECT * FROM orders WHERE status='draft'")
    assert order["total_amount"] == 185000
    qty = db_one("SELECT quantity FROM order_items WHERE order_id=?", (order["id"],))["quantity"]
    assert qty == 37
    assert drive.states.get(CHAT) is None  # state cleared


def test_custom_quantity_rejects_bad_input_then_accepts(bot):
    api, drive = bot
    pid = make_product(name="Kopi", price=5000, stock=100)
    drive(cb_update("no"))
    drive(cb_update(f"no:i:{pid}"))
    drive(cb_update("no:qc"))
    drive(text_update("lots"))           # unparseable -> re-prompt, stays in flow
    assert "Couldn't read" in api.last_text()
    assert drive.states.get(CHAT)["await_qty"] is True
    drive(text_update("0"))              # zero is not a valid quantity
    assert "Couldn't read" in api.last_text()
    drive(text_update("4"))              # valid -> back to picker
    assert drive.states.get(CHAT)["items"] == {pid: 4}


def test_stale_flow_callback_shows_session_expired(bot):
    api, drive = bot
    make_product()
    drive(cb_update("no:q:2"))  # flow callback with no state (e.g. after restart)
    alerts = [p for p in api.sent("answerCallbackQuery") if p["show_alert"]]
    assert alerts and "expired" in alerts[-1]["text"].lower()


def test_abandon_clears_state(bot):
    api, drive = bot
    make_product()
    drive(cb_update("no"))
    drive(cb_update("no:c"))
    assert drive.states.get(CHAT) is None


# --- Restock flow ---

def test_restock_flow_prices_each_product_then_the_invoice_charges(bot):
    api, drive = bot
    pid = make_product(stock=5)
    drive(cb_update("r"))
    drive(cb_update(f"r:i:{pid}"))
    drive(cb_update("r:q:10"))           # -> await the unit price for this product
    assert "price per unit" in api.last_text()
    drive(text_update("nonsense"))       # bad price -> re-prompt, stays in flow
    assert "Couldn't read" in api.last_text()
    drive(text_update("Rp 15.000"))      # Indonesian-formatted price
    assert drive.states.get(CHAT)["prices"] == {pid: 15000}
    drive(cb_update("r:d"))              # -> discount, then shipping, then admin fee
    drive(text_update("Rp 20.000"))      # discount
    drive(text_update("15000"))          # shipping
    drive(cb_update("r:sk"))             # no bank fee -> review
    assert "145.000" in api.last_text()  # 150000 - 20000 + 15000
    drive(cb_update("r:!"))
    assert db_one("SELECT stock_qty FROM products WHERE id=?", (pid,))["stock_qty"] == 15
    batch = db_one("SELECT * FROM restock_batches")
    assert (batch["subtotal_cost"], batch["discount"], batch["shipping_cost"],
            batch["admin_fee"], batch["total_cost"]) == (150000, 20000, 15000, 0, 145000)
    item = db_one("SELECT * FROM restock_items")
    assert item["unit_price"] == 15000
    assert item["unit_cost"] == 14500          # the charges land on the one line
    assert item["allocated_cost"] == 145000
    # Stock was empty of cost before, so the batch sets it outright rather than averaging.
    assert db_one("SELECT cost_price FROM products WHERE id=?", (pid,))["cost_price"] == 14500
    log = db_one("SELECT change_qty, reason FROM stock_logs WHERE product_id=?", (pid,))
    assert log["change_qty"] == 10
    assert log["reason"] == f"restock batch #{batch['id']}"


def test_restock_refuses_to_save_before_every_product_is_priced(bot):
    api, drive = bot
    pid = make_product(stock=5)
    drive(cb_update("r"))
    drive(cb_update(f"r:i:{pid}"))
    drive(cb_update("r:q:10"))
    drive(cb_update("r:p:0"))            # walk away from the price prompt, back to picker
    drive(cb_update("r:d"))
    alerts = [p for p in api.sent("answerCallbackQuery") if p["show_alert"]]
    assert alerts and "unit price" in alerts[-1]["text"]
    drive(cb_update("r:!"))
    assert db_one("SELECT COUNT(*) AS n FROM restock_batches")["n"] == 0


# --- Self use flow ---

def test_self_use_button_on_main_menu(bot):
    api, drive = bot
    drive(text_update("/start"))
    assert "su" in api.buttons()


def test_self_use_flow_saves_batch_and_logs(bot):
    api, drive = bot
    pid = make_product(stock=10, price=5000)
    drive(cb_update("su"))
    drive(cb_update(f"su:i:{pid}"))
    drive(cb_update("su:q:2"))
    drive(cb_update("su:d"))             # straight to review: no cost prompt
    assert "10.000" in api.last_text()   # 2 x Rp 5.000 valued at retail price
    assert "su:!" in api.buttons()
    drive(cb_update("su:!"))

    assert db_one("SELECT stock_qty FROM products WHERE id=?", (pid,))["stock_qty"] == 8
    batch = db_one("SELECT * FROM self_use_batches")
    assert batch["total_value"] == 10000
    log = db_one("SELECT change_qty, reason FROM stock_logs WHERE product_id=?", (pid,))
    assert log["change_qty"] == -2
    assert log["reason"] == f"self use batch #{batch['id']}"
    assert drive.states.get(CHAT) is None


def test_self_use_flow_shows_stock_in_the_picker(bot):
    api, drive = bot
    make_product(stock=7, name="Kopi")
    drive(cb_update("su"))
    assert any("(7)" in b for b in api.buttons_text())


def test_self_use_flow_rejects_oversell(bot):
    api, drive = bot
    pid = make_product(stock=1)
    drive(cb_update("su"))
    drive(cb_update(f"su:i:{pid}"))
    drive(cb_update("su:q:5"))
    drive(cb_update("su:d"))
    drive(cb_update("su:!"))

    alerts = [p for p in api.sent("answerCallbackQuery") if p["show_alert"]]
    assert alerts and "Insufficient stock" in alerts[-1]["text"]
    assert db_one("SELECT COUNT(*) AS cnt FROM self_use_batches")["cnt"] == 0
    assert db_one("SELECT stock_qty FROM products WHERE id=?", (pid,))["stock_qty"] == 1


def test_self_use_abandon_clears_state(bot):
    api, drive = bot
    make_product()
    drive(cb_update("su"))
    drive(cb_update("su:c"))
    assert drive.states.get(CHAT) is None


def test_summary_shows_self_use_line(bot):
    api, drive = bot
    pid = make_product(stock=10, price=5000)
    drive(cb_update("su"))
    drive(cb_update(f"su:i:{pid}"))
    drive(cb_update("su:q:2"))
    drive(cb_update("su:d"))
    drive(cb_update("su:!"))

    drive(cb_update("s:d:0"))
    text = api.last_text()
    assert "Self use: Rp 10.000" in text
    assert "Net profit: Rp 0" in text  # self use never enters profit


def test_parse_cost():
    assert parse_cost("150000") == 150000
    assert parse_cost("Rp 150.000") == 150000
    assert parse_cost("150,000") == 150000
    assert parse_cost("abc") is None
    assert parse_cost("-5") is None


def set_language(lang):
    db = database.get_db()
    database.set_setting(db, "language", lang)
    db.commit()
    db.close()


def test_menu_renders_in_indonesian_when_set(bot):
    api, drive = bot
    set_language("id")
    drive(text_update("/start"))
    text = api.last_text()
    assert "Apa yang ingin Anda lakukan?" in text  # "What do you want to do?"
    assert "📦 Produk" in api.buttons_text()        # translated button label


def test_menu_stays_english_by_default(bot):
    api, drive = bot
    drive(text_update("/start"))
    assert "What do you want to do?" in api.last_text()
    assert "📦 Products" in api.buttons_text()


def test_bad_language_setting_falls_back_to_english(bot):
    api, drive = bot
    set_language("fr")  # unsupported value must never break rendering
    drive(text_update("/start"))
    assert "What do you want to do?" in api.last_text()


def test_service_error_alert_translated(bot):
    api, drive = bot
    set_language("id")
    make_product(name="Kopi", stock=1)
    drive(cb_update("no"))
    drive(cb_update(f"no:i:{db_one('SELECT id FROM products')['id']}"))
    drive(cb_update("no:q:5"))           # oversell
    drive(cb_update("no:d"))
    drive(cb_update("no:!"))             # create -> "Insufficient stock for {name}"
    alerts = [p for p in api.sent("answerCallbackQuery") if p["show_alert"]]
    # translated, with the product name interpolated into the template
    assert alerts and alerts[-1]["text"] == "Stok tidak cukup untuk Kopi"


def test_parse_qty():
    assert parse_qty("12") == 12
    assert parse_qty("  3 ") == 3
    assert parse_qty("0") is None
    assert parse_qty("-5") is None
    assert parse_qty("1.5") is None
    assert parse_qty("abc") is None
    assert parse_qty("") is None


# --- Summary ---

def test_summary_renders_revenue_and_navigates(bot):
    api, drive = bot
    pid = make_product(stock=10, price=5000)
    db = database.get_db()
    import services
    oid = services.create_order(db, [{"product_id": pid, "quantity": 2}])["order_id"]
    services.confirm_order(db, oid)
    services.complete_order(db, oid)
    db.close()

    drive(cb_update("s:d:0"))
    assert "10.000" in api.last_text()  # today's revenue
    assert "s:d:1" in api.buttons()     # back one day
    drive(cb_update("s:d:1"))           # yesterday: empty
    assert "Rp 0" in api.last_text()
    drive(cb_update("s:w:0"))           # switch unit
    assert "10.000" in api.last_text()


def test_summary_month_name_localized(bot):
    import i18n
    api, drive = bot
    set_language("id")
    drive(cb_update("s:m:0"))            # month view
    # The current month's Indonesian name appears in the label (built without strftime).
    month_id = i18n.month_name(datetime.now(timezone.utc).astimezone(JKT).month, "id")
    assert month_id in api.last_text()
    assert "Penjualan" in api.last_text()  # header also translated


def test_plain_text_resets_to_menu(bot):
    api, drive = bot
    drive(cb_update("no"))
    drive(text_update("hello"))
    assert drive.states.get(CHAT) is None
    assert "What do you want to do?" in api.last_text()


def test_menu_offers_the_monthly_report(bot):
    api, drive = bot
    drive(text_update("/start"))
    assert "rp" in api.buttons()


def test_report_picker_lists_closed_months(bot):
    import i18n
    api, drive = bot
    drive(cb_update("rp"))
    # Six closed months, each an offset >= 1: the current month is still running
    # and would make a misleading audit record.
    assert [b for b in api.buttons() if b.startswith("rp:")] == [
        f"rp:{n}" for n in range(1, 7)]
    now = datetime.now(timezone.utc).astimezone(JKT)
    last_closed = (now.replace(day=1) - timedelta(days=1))
    assert i18n.month_label(last_closed, "en") in api.buttons_text()


def test_report_picker_localized(bot):
    import i18n
    api, drive = bot
    set_language("id")
    drive(cb_update("rp"))
    assert "Laporan Bulanan" in api.last_text()
    last_closed = datetime.now(timezone.utc).astimezone(JKT).replace(day=1) - timedelta(days=1)
    assert i18n.month_label(last_closed, "id") in api.buttons_text()


def test_report_request_uploads_a_pdf_to_the_asking_chat(bot):
    api, drive = bot
    make_product()
    drive(cb_update("rp:1"))
    uploads = api.sent("sendDocument")
    assert len(uploads) == 1
    assert uploads[0]["chat_id"] == CHAT
    assert uploads[0]["filename"].startswith("shop-report-")
    assert uploads[0]["content"].startswith(b"%PDF-")
    # Acked before the slow render, so Telegram doesn't time the callback out.
    acks = api.sent("answerCallbackQuery")
    assert acks and acks[0]["text"] == "Building the report…"
    assert api.calls[0][0] == "answerCallbackQuery"


def test_report_request_reports_a_send_failure_without_claiming_total_loss(bot, monkeypatch):
    api, drive = bot

    def boom(*a, **k):
        raise telegram_bot.TelegramError("Bad Gateway", 502)

    monkeypatch.setattr(api, "send_document", boom)
    drive(cb_update("rp:1"))
    assert "saved on the server" in api.last_text()
