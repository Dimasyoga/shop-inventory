"""Monthly report: collection windows, PDF rendering, the archive, and the web routes.

Rows are backdated with the `insert` fixture and every call passes an explicit
`now`, so these assert on fixed month boundaries instead of whenever the suite runs.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import database
import i18n
import reports

JKT = ZoneInfo("Asia/Jakarta")
# Mid-July, so offset=1 is June and offset=0 is the running month.
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=JKT)
EN = i18n.make_t("en")


@pytest.fixture
def shop(insert):
    """A product, and helpers to backdate records against it."""
    pid = insert("products", "2026-01-01 00:00:00", name="Kopi", sku="KP-1",
                 price=15000, stock_qty=100)

    def order(created_at, qty, status="completed", total=45000):
        oid = insert("orders", created_at, status=status, total_amount=total)
        conn = database.get_db()
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)"
                     " VALUES (?, ?, ?, ?, ?)", (oid, pid, qty, 15000, 15000 * qty))
        conn.commit()
        conn.close()
        return oid

    def restock(created_at, qty, cost=100000):
        bid = insert("restock_batches", created_at, total_cost=cost)
        conn = database.get_db()
        conn.execute("INSERT INTO restock_items (batch_id, product_id, qty_added, allocated_cost)"
                     " VALUES (?, ?, ?, ?)", (bid, pid, qty, cost))
        conn.commit()
        conn.close()
        return bid

    def self_use(created_at, qty):
        bid = insert("self_use_batches", created_at, total_value=15000 * qty)
        conn = database.get_db()
        conn.execute("INSERT INTO self_use_items (batch_id, product_id, quantity, unit_price, subtotal)"
                     " VALUES (?, ?, ?, ?, ?)", (bid, pid, qty, 15000, 15000 * qty))
        conn.commit()
        conn.close()
        return bid

    return type("Shop", (), {"product_id": pid, "order": staticmethod(order),
                             "restock": staticmethod(restock),
                             "self_use": staticmethod(self_use)})


def collect(offset=1):
    db = database.get_db()
    try:
        return reports.collect(db, offset, JKT, "en", now=NOW)
    finally:
        db.close()


# --- Month identity ---

def test_period_key_and_filename():
    assert reports.period_key(datetime(2026, 6, 3)) == "2026-06"
    assert reports.report_filename("2026-06") == "shop-report-2026-06.pdf"


def test_month_offset_counts_back_from_now():
    assert reports.month_offset("2026-07", JKT, now=NOW) == 0
    assert reports.month_offset("2026-06", JKT, now=NOW) == 1
    assert reports.month_offset("2025-07", JKT, now=NOW) == 12


def test_month_offset_rejects_future_and_garbage():
    assert reports.month_offset("2026-08", JKT, now=NOW) is None
    assert reports.month_offset("not-a-month", JKT, now=NOW) is None
    assert reports.month_offset(None, JKT, now=NOW) is None


# --- Collection windows ---

def test_collects_the_previous_calendar_month(db_path, shop):
    shop.order("2026-06-10 03:00:00", qty=3)      # 10:00 Jakarta, inside June
    shop.restock("2026-06-11 03:00:00", qty=20)
    shop.self_use("2026-06-12 03:00:00", qty=2)
    data = collect(offset=1)
    assert data["period"] == "2026-06"
    assert data["label"] == "June 2026"
    assert len(data["orders"]) == 1
    assert len(data["restocks"]) == 1
    assert len(data["self_uses"]) == 1


def test_records_outside_the_month_are_excluded(db_path, shop):
    shop.order("2026-07-02 03:00:00", qty=3)   # July
    shop.order("2026-05-30 03:00:00", qty=3)   # May
    assert collect(offset=1)["orders"] == []


def test_month_boundary_follows_the_shop_timezone(db_path, shop):
    # 2026-06-30 17:30 UTC is 2026-07-01 00:30 in Jakarta, so it belongs to July.
    shop.order("2026-06-30 17:30:00", qty=1)
    assert collect(offset=1)["orders"] == []
    assert len(collect(offset=0)["orders"]) == 1


def test_only_completed_orders_are_reported(db_path, shop):
    shop.order("2026-06-10 03:00:00", qty=1, status="draft")
    shop.order("2026-06-11 03:00:00", qty=1, status="cancelled")
    shop.order("2026-06-12 03:00:00", qty=1, status="confirmed")
    shop.order("2026-06-13 03:00:00", qty=2, status="completed")
    data = collect(offset=1)
    assert len(data["orders"]) == 1
    assert data["orders"][0]["items"][0]["quantity"] == 2


def test_line_items_are_grouped_under_their_order(db_path, shop, insert):
    oid = shop.order("2026-06-10 03:00:00", qty=3)
    conn = database.get_db()
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)"
                 " VALUES (?, ?, ?, ?, ?)", (oid, shop.product_id, 5, 15000, 75000))
    conn.commit()
    conn.close()
    orders = collect(offset=1)["orders"]
    assert len(orders) == 1
    assert [i["quantity"] for i in orders[0]["items"]] == [3, 5]
    assert orders[0]["items"][0]["product_name"] == "Kopi"


def test_an_order_with_no_line_items_still_appears(db_path, insert):
    # A header with no rows under it is exactly what an audit needs to see.
    insert("orders", "2026-06-10 03:00:00", status="completed", total_amount=0)
    orders = collect(offset=1)["orders"]
    assert len(orders) == 1
    assert orders[0]["items"] == []


def test_summary_describes_the_reported_month_not_today(db_path, shop):
    shop.order("2026-06-10 03:00:00", qty=3)       # June: revenue 45000
    shop.order("2026-07-10 03:00:00", qty=3)       # July, must not leak in
    shop.restock("2026-06-11 03:00:00", qty=20, cost=100000)
    shop.self_use("2026-06-12 03:00:00", qty=2)
    s = collect(offset=1)["summary"]
    assert s["total_revenue"] == 45000
    assert s["restock_cost"] == 100000
    assert s["self_use_value"] == 30000
    # Self use is reported beside profit, never inside it.
    assert s["net_profit"] == 45000 - 100000


# --- Rendering ---

def test_render_produces_a_pdf(db_path, shop):
    shop.order("2026-06-10 03:00:00", qty=3)
    shop.restock("2026-06-11 03:00:00", qty=20)
    shop.self_use("2026-06-12 03:00:00", qty=2)
    content = reports.render(collect(offset=1), EN)
    assert content.startswith(b"%PDF-")
    assert content.rstrip().endswith(b"%%EOF")
    assert len(content) > 2000


def test_render_handles_a_month_with_no_records(db_path, shop):
    content = reports.render(collect(offset=1), EN)
    assert content.startswith(b"%PDF-")


def test_render_handles_non_latin1_product_names(db_path, insert):
    # The vendored Unicode font exists for this: a curly quote or an emoji in a
    # product name must not take the whole report down.
    pid = insert("products", "2026-01-01 00:00:00", name="Kopi “Spesial” ☕ — 100%",
                 sku="KP-é", price=15000, stock_qty=10)
    oid = insert("orders", "2026-06-10 03:00:00", status="completed", total_amount=15000)
    conn = database.get_db()
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)"
                 " VALUES (?, ?, ?, ?, ?)", (oid, pid, 1, 15000, 15000))
    conn.commit()
    conn.close()
    assert reports.render(collect(offset=1), EN).startswith(b"%PDF-")


def test_render_is_translated(db_path, shop):
    db = database.get_db()
    try:
        data = reports.collect(db, 1, JKT, "id", now=NOW)
    finally:
        db.close()
    assert data["label"] == "Juni 2026"
    # Text is compressed inside the PDF stream, so assert via the translator the
    # renderer is handed rather than by grepping the bytes.
    assert i18n.make_t("id")("Sales Records") == "Catatan Penjualan"
    assert reports.render(data, i18n.make_t("id")).startswith(b"%PDF-")


# --- Archive ---

def test_save_writes_into_the_report_dir(db_path, shop):
    path = reports.save(b"%PDF-fake", "2026-06")
    assert path == os.path.join(reports.REPORT_DIR, "shop-report-2026-06.pdf")
    with open(path, "rb") as f:
        assert f.read() == b"%PDF-fake"


def test_regenerating_a_month_overwrites_rather_than_duplicates(db_path, shop):
    reports.save(b"first", "2026-06")
    reports.save(b"second", "2026-06")
    assert os.listdir(reports.REPORT_DIR) == ["shop-report-2026-06.pdf"]
    with open(reports.save(b"second", "2026-06"), "rb") as f:
        assert f.read() == b"second"


def test_build_collects_renders_and_archives(db_path, shop):
    shop.order("2026-06-10 03:00:00", qty=3)
    db = database.get_db()
    try:
        path, content, data = reports.build(db, 1, JKT, "en", now=NOW)
    finally:
        db.close()
    assert data["period"] == "2026-06"
    assert content.startswith(b"%PDF-")
    with open(path, "rb") as f:
        assert f.read() == content


# --- Web routes ---

def test_download_returns_a_pdf_attachment(client, shop):
    res = client.get("/api/reports/monthly?offset=1")
    assert res.status_code == 200
    assert res.mimetype == "application/pdf"
    assert "shop-report-" in res.headers["Content-Disposition"]
    assert res.data.startswith(b"%PDF-")


def test_download_defaults_to_the_month_that_just_closed(client, shop):
    res = client.get("/api/reports/monthly")
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]


@pytest.mark.parametrize("offset", ["-1", "999", "abc", ""])
def test_download_rejects_a_bad_offset(client, shop, offset):
    assert client.get(f"/api/reports/monthly?offset={offset}").status_code == 400


def test_months_endpoint_lists_selectable_months(client, shop):
    months = client.get("/api/reports/months").get_json()
    assert len(months) == 13          # current month plus twelve back
    assert months[0]["offset"] == 0
    assert months[1]["offset"] == 1
    assert all(m["period"] for m in months)


def test_send_needs_a_bot_token(client, shop):
    res = client.post("/api/reports/monthly/send", json={"offset": 1})
    assert res.status_code == 400
    assert "token" in res.get_json()["error"].lower()


def test_send_needs_a_whitelist(client, shop):
    db = database.get_db()
    database.set_secret_setting(db, "telegram_bot_token", "123:ABC")
    db.commit()
    db.close()
    res = client.post("/api/reports/monthly/send", json={"offset": 1})
    assert res.status_code == 400
    assert res.get_json()["error"]


def test_report_routes_require_login(db_path):
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as anon:
        # login_required redirects rather than 401s, so assert on the redirect.
        assert anon.get("/api/reports/monthly").status_code == 302
        assert anon.post("/api/reports/monthly/send", json={}).status_code == 302
