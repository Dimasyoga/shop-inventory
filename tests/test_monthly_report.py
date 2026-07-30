"""Scheduled monthly report: when it fires, when it must not, and how it uploads.

The schedule is driven by the persisted `last_report_period` marker rather than a
timer, so these drive `send_monthly_report` with an explicit `now` and assert on
the marker — the thing that has to survive a restart.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import database
import i18n
import reports
import telegram_bot
from telegram_bot import (REPORT_MARKER, BotConfig, TelegramAPI, TelegramError,
                          _pending_report_periods, send_monthly_report)

JKT = ZoneInfo("Asia/Jakarta")
EN = i18n.make_t("en")
# Early July: the month that just closed is June 2026.
JULY = datetime(2026, 7, 3, 9, 0, tzinfo=JKT)


def cfg(whitelist=(111, 222), report_enabled=True):
    return BotConfig(enabled=True, token="t", whitelist=set(whitelist),
                     tz=JKT, alert_hours=24, report_enabled=report_enabled)


class FakeAPI:
    """Records uploads. `fail` makes every send raise, as a Telegram outage would."""

    def __init__(self, fail=False):
        self.documents = []
        self.fail = fail

    def send_document(self, chat_id, filename, content, caption=None):
        if self.fail:
            raise TelegramError("Bad Gateway", 502)
        self.documents.append({"chat_id": chat_id, "filename": filename,
                               "content": content, "caption": caption})
        return {}

    def send_message(self, *a, **k):
        return {}

    def periods(self):
        return [d["filename"] for d in self.documents]


def marker():
    db = database.get_db()
    try:
        return database.get_setting(db, REPORT_MARKER)
    finally:
        db.close()


def set_marker(value):
    db = database.get_db()
    database.set_setting(db, REPORT_MARKER, value)
    db.commit()
    db.close()


def run(api, config=None, now=JULY):
    db = database.get_db()
    try:
        return send_monthly_report(api, db, config or cfg(), EN, now=now)
    finally:
        db.close()


# --- Which months are outstanding ---

def test_no_pending_months_when_already_current():
    assert _pending_report_periods("2026-06", "2026-06") == []


def test_one_pending_month():
    assert _pending_report_periods("2026-05", "2026-06") == ["2026-06"]


def test_pending_months_are_oldest_first_and_cross_the_year():
    assert _pending_report_periods("2025-11", "2026-02") == [
        "2025-12", "2026-01", "2026-02"]


def test_pending_months_are_capped_so_a_long_outage_catches_up_gradually():
    pending = _pending_report_periods("2020-01", "2026-06", limit=3)
    assert pending == ["2020-02", "2020-03", "2020-04"]


def test_a_marker_ahead_of_the_target_yields_nothing():
    # Clock skew or a hand-edited setting must not make it walk backwards.
    assert _pending_report_periods("2026-09", "2026-06") == []


def test_an_unreadable_marker_falls_back_to_the_latest_month():
    assert _pending_report_periods("garbage", "2026-06") == ["2026-06"]
    assert _pending_report_periods(None, "2026-06") == ["2026-06"]


# --- First run ---

def test_first_run_plants_the_marker_without_sending(db_path):
    api = FakeAPI()
    assert run(api) == []
    assert api.documents == []
    # June is recorded as already handled, so the first real report is July's,
    # sent once August starts.
    assert marker() == "2026-06"


# --- Steady state ---

def test_sends_the_closed_month_and_advances_the_marker(db_path):
    set_marker("2026-05")
    api = FakeAPI()
    assert run(api) == ["2026-06"]
    assert marker() == "2026-06"
    assert len(api.documents) == 2          # one per whitelisted id
    assert api.periods() == ["shop-report-2026-06.pdf"] * 2
    assert api.documents[0]["content"].startswith(b"%PDF-")


def test_does_not_send_again_for_a_month_already_reported(db_path):
    set_marker("2026-06")
    api = FakeAPI()
    assert run(api) == []
    assert api.documents == []


def test_a_restart_does_not_resend(db_path):
    set_marker("2026-05")
    assert run(FakeAPI()) == ["2026-06"]
    # A new poller (fresh in-memory deadlines) reads the same persisted marker.
    api = FakeAPI()
    assert run(api) == []
    assert api.documents == []


def test_catches_up_every_month_missed_while_offline(db_path):
    set_marker("2026-03")
    api = FakeAPI()
    assert run(api) == ["2026-04", "2026-05", "2026-06"]
    assert marker() == "2026-06"
    assert [d["filename"] for d in api.documents if d["chat_id"] == 111] == [
        "shop-report-2026-04.pdf", "shop-report-2026-05.pdf", "shop-report-2026-06.pdf"]


def test_the_caption_carries_the_month_and_its_figures(db_path, insert):
    pid = insert("products", "2026-01-01 00:00:00", name="Kopi", sku="KP-1",
                 price=15000, stock_qty=10)
    oid = insert("orders", "2026-06-10 03:00:00", status="completed", total_amount=45000)
    conn = database.get_db()
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)"
                 " VALUES (?, ?, ?, ?, ?)", (oid, pid, 3, 15000, 45000))
    conn.commit()
    conn.close()
    set_marker("2026-05")
    api = FakeAPI()
    run(api)
    caption = api.documents[0]["caption"]
    assert "June 2026" in caption
    assert "Rp 45.000" in caption


# --- Failure handling ---

def test_marker_holds_when_no_recipient_could_be_reached(db_path):
    set_marker("2026-05")
    api = FakeAPI(fail=True)
    assert run(api) == []
    # Unchanged, so the next check retries instead of losing the month.
    assert marker() == "2026-05"


def test_a_failed_month_stops_the_catch_up_to_keep_the_backlog_ordered(db_path):
    set_marker("2026-03")
    assert run(FakeAPI(fail=True)) == []
    assert marker() == "2026-03"


def test_the_archive_is_written_even_when_sending_fails(db_path):
    import os
    set_marker("2026-05")
    run(FakeAPI(fail=True))
    assert "shop-report-2026-06.pdf" in os.listdir(reports.REPORT_DIR)


def test_an_empty_whitelist_still_archives_and_advances(db_path):
    import os
    set_marker("2026-05")
    api = FakeAPI()
    # Nobody to deliver to, so the archived file is the whole deliverable and the
    # month counts as done -- otherwise it would retry forever.
    assert run(api, config=cfg(whitelist=())) == ["2026-06"]
    assert marker() == "2026-06"
    assert api.documents == []
    assert "shop-report-2026-06.pdf" in os.listdir(reports.REPORT_DIR)


def test_the_toggle_disables_the_whole_job(db_path):
    set_marker("2026-05")
    api = FakeAPI()
    assert run(api, config=cfg(report_enabled=False)) == []
    assert api.documents == []
    assert marker() == "2026-05"        # not even the marker moves


# --- Poller integration ---

def save_settings(**kv):
    db = database.get_db()
    for k, v in kv.items():
        database.set_setting(db, k, v)
    db.commit()
    db.close()


def test_the_poller_checks_for_a_report_once_per_interval(db_path, monkeypatch):
    save_settings(telegram_enabled="1", telegram_bot_token="123:ABC")
    calls = []
    monkeypatch.setattr(telegram_bot, "send_monthly_report",
                        lambda *a, **k: calls.append(1) or [])

    class API:
        def __init__(self, token):
            pass

        def get_updates(self, offset=None, timeout=25):
            return []

    # A settable clock, not a tick iterator: each cycle also asks the clock for the
    # stale-order check, so the number of calls per cycle is not the test's business.
    fake_clock = {"now": 0}
    poller = telegram_bot.BotPoller(api_factory=API, sleep=lambda s: None,
                                    clock=lambda: fake_clock["now"],
                                    alert_interval=10 ** 9, report_interval=3600)
    poller._cycle()
    assert len(calls) == 1        # fires on the first cycle
    fake_clock["now"] = 10
    poller._cycle()
    assert len(calls) == 1        # throttled well inside the interval
    fake_clock["now"] = 5000
    poller._cycle()
    assert len(calls) == 2        # fires again once the interval has elapsed


def test_a_failing_report_check_does_not_kill_the_poller(db_path, monkeypatch):
    save_settings(telegram_enabled="1", telegram_bot_token="123:ABC")

    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(telegram_bot, "send_monthly_report", boom)

    class API:
        def __init__(self, token):
            self.polled = False

        def get_updates(self, offset=None, timeout=25):
            self.polled = True
            return []

    poller = telegram_bot.BotPoller(api_factory=API, sleep=lambda s: None,
                                    alert_interval=10 ** 9)
    poller._cycle()          # must not raise
    assert poller._api.polled


# --- sendDocument transport ---

def multipart_body(monkeypatch, **kwargs):
    captured = {}

    def fake_request(self, method, body, content_type):
        captured.update(method=method, body=body, content_type=content_type)
        return {}

    monkeypatch.setattr(TelegramAPI, "_request", fake_request)
    TelegramAPI("123:ABC").send_document(**kwargs)
    return captured


def test_send_document_posts_multipart_with_the_file_bytes(monkeypatch):
    captured = multipart_body(monkeypatch, chat_id=111, filename="r.pdf",
                              content=b"%PDF-1.4\x00\x01binary", caption="hello")
    assert captured["method"] == "sendDocument"
    ctype = captured["content_type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1]
    body = captured["body"]
    assert isinstance(body, bytes)
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert body.endswith(f"\r\n--{boundary}--\r\n".encode())
    # Binary content must survive verbatim, not as escaped or re-encoded text.
    assert b"%PDF-1.4\x00\x01binary" in body
    assert b'name="document"; filename="r.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert b'name="chat_id"' in body and b"111" in body
    assert b'name="caption"' in body and b"hello" in body
    assert b"HTML" in body            # parse_mode travels with the caption


def test_send_document_omits_caption_fields_when_there_is_none(monkeypatch):
    body = multipart_body(monkeypatch, chat_id=1, filename="r.pdf",
                          content=b"x")["body"]
    assert b"caption" not in body
    assert b"parse_mode" not in body


def test_send_document_truncates_an_overlong_caption(monkeypatch):
    # Telegram rejects the whole upload over 1024 chars, which would lose the report.
    body = multipart_body(monkeypatch, chat_id=1, filename="r.pdf", content=b"x",
                          caption="A" * 2000)["body"]
    assert b"A" * 1024 in body
    assert b"A" * 1025 not in body


def test_call_still_sends_json(monkeypatch):
    captured = {}
    monkeypatch.setattr(TelegramAPI, "_request",
                        lambda self, m, b, c: captured.update(method=m, body=b, ctype=c) or {})
    TelegramAPI("123:ABC").call("sendMessage", chat_id=5, text="hi")
    assert captured["ctype"] == "application/json"
    assert captured["body"] == b'{"chat_id": 5, "text": "hi"}'
