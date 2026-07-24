"""Stale-order alert tests: the query in services and the bot's push sender."""
from datetime import datetime, timedelta, timezone

import database
import i18n
import services
from telegram_bot import BotConfig, parse_alert_hours, send_stale_order_alerts

EN = i18n.make_t("en")


def utc(hours_ago):
    """A 'YYYY-MM-DD HH:MM:SS' UTC stamp `hours_ago` hours in the past."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def add_order(status="draft", updated_hours_ago=25, total=1000, alerted_status=None):
    db = database.get_db()
    cur = db.execute(
        "INSERT INTO orders (status, total_amount, updated_at, alerted_status) VALUES (?, ?, ?, ?)",
        (status, total, utc(updated_hours_ago), alerted_status))
    db.commit()
    oid = cur.lastrowid
    db.close()
    return oid


def cfg(alert_hours=24, whitelist=(111, 222)):
    return BotConfig(enabled=True, token="t", whitelist=set(whitelist),
                     tz=timezone.utc, alert_hours=alert_hours)


class FakeAPI:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise OSError("boom")
        self.sent.append({"chat_id": chat_id, "text": text})


# --- find_stale_orders ---

def test_stale_draft_found(db_path):
    oid = add_order(status="draft", updated_hours_ago=25)
    db = database.get_db()
    rows = services.find_stale_orders(db, 24)
    db.close()
    assert [r["id"] for r in rows] == [oid]


def test_recent_order_not_stale(db_path):
    add_order(status="draft", updated_hours_ago=1)
    db = database.get_db()
    assert services.find_stale_orders(db, 24) == []
    db.close()


def test_confirmed_counts_completed_and_cancelled_excluded(db_path):
    confirmed = add_order(status="confirmed", updated_hours_ago=48)
    add_order(status="completed", updated_hours_ago=48)
    add_order(status="cancelled", updated_hours_ago=48)
    db = database.get_db()
    rows = services.find_stale_orders(db, 24)
    db.close()
    assert [r["id"] for r in rows] == [confirmed]


def test_already_alerted_for_status_skipped(db_path):
    add_order(status="draft", updated_hours_ago=25, alerted_status="draft")
    db = database.get_db()
    assert services.find_stale_orders(db, 24) == []
    db.close()


def test_disabled_threshold_returns_nothing(db_path):
    add_order(status="draft", updated_hours_ago=100)
    db = database.get_db()
    assert services.find_stale_orders(db, None) == []
    assert services.find_stale_orders(db, 0) == []
    db.close()


# --- parse_alert_hours ---

def test_parse_alert_hours():
    assert parse_alert_hours("24") == 24
    assert parse_alert_hours("12.5") == 12.5
    assert parse_alert_hours("0") is None
    assert parse_alert_hours("-3") is None
    assert parse_alert_hours("") is None
    assert parse_alert_hours("abc") is None
    assert parse_alert_hours(None) is None


# --- send_stale_order_alerts ---

def test_alerts_sent_to_each_whitelisted_id_then_marked(db_path):
    oid = add_order(status="draft", updated_hours_ago=25)
    api = FakeAPI()
    db = database.get_db()
    send_stale_order_alerts(api, db, cfg(), EN)
    db.close()

    assert sorted(m["chat_id"] for m in api.sent) == [111, 222]
    assert f"#{oid}" in api.sent[0]["text"]
    # Second run is a no-op: the order was marked alerted for its status.
    api2 = FakeAPI()
    db = database.get_db()
    send_stale_order_alerts(api2, db, cfg(), EN)
    db.close()
    assert api2.sent == []


def test_realert_once_when_status_changes(db_path):
    oid = add_order(status="draft", updated_hours_ago=25)
    api = FakeAPI()
    db = database.get_db()
    send_stale_order_alerts(api, db, cfg(whitelist=(111,)), EN)
    # Order gets confirmed and then goes stale again in the new state.
    db.execute("UPDATE orders SET status = 'confirmed', updated_at = ? WHERE id = ?",
               (utc(30), oid))
    db.commit()
    api2 = FakeAPI()
    send_stale_order_alerts(api2, db, cfg(whitelist=(111,)), EN)
    db.close()
    assert len(api2.sent) == 1


def test_not_marked_when_all_sends_fail(db_path):
    add_order(status="draft", updated_hours_ago=25)
    api = FakeAPI(fail=True)
    db = database.get_db()
    send_stale_order_alerts(api, db, cfg(whitelist=(111,)), EN)
    # Nothing delivered -> still stale, retried next time.
    assert len(services.find_stale_orders(db, 24)) == 1
    db.close()


def test_no_send_when_whitelist_empty(db_path):
    add_order(status="draft", updated_hours_ago=25)
    api = FakeAPI()
    db = database.get_db()
    send_stale_order_alerts(api, db, cfg(whitelist=()), EN)
    db.close()
    assert api.sent == []
