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
import services

JKT = ZoneInfo("Asia/Jakarta")
# Mid-July, so offset=1 is June and offset=0 is the running month.
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=JKT)
EN = i18n.make_t("en")


@pytest.fixture
def shop(insert):
    """A product, and helpers to backdate records against it."""
    pid = insert("products", "2026-01-01 00:00:00", name="Kopi", sku="KP-1",
                 price=15000, cost_price=9000, stock_qty=100)

    def order(created_at, qty, status="completed", total=45000):
        oid = insert("orders", created_at, status=status, total_amount=total)
        conn = database.get_db()
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " unit_cost, subtotal) VALUES (?, ?, ?, ?, ?, ?)",
                     (oid, pid, qty, 15000, 9000, 15000 * qty))
        conn.commit()
        conn.close()
        return oid

    def restock(created_at, qty, cost=100000):
        bid = insert("restock_batches", created_at, subtotal_cost=cost, total_cost=cost)
        conn = database.get_db()
        conn.execute("INSERT INTO restock_items (batch_id, product_id, qty_added, unit_price,"
                     " unit_cost, allocated_cost) VALUES (?, ?, ?, ?, ?, ?)",
                     (bid, pid, qty, cost / qty, cost / qty, cost))
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


def test_voiding_later_leaves_a_closed_month_with_the_figures_it_printed(db_path, shop):
    """The whole reason a void is a new batch rather than an edit: June was reported at
    100.000 of restock spend, and it still reads that way. The credit belongs to the
    month the correction was actually made."""
    shop.restock("2026-06-11 03:00:00", qty=20, cost=100000)
    db = database.get_db()
    services.void_restock(db, 1)   # today, months after June closed
    db.close()

    data = collect(offset=1)
    assert len(data["restocks"]) == 1
    assert data["restocks"][0]["voids_batch_id"] is None
    assert data["summary"]["restock_cost"] == 100000


def test_a_void_inside_the_month_cancels_it_out(db_path, shop, insert):
    """Caught in the same month, the pair nets to nothing -- both rows still reported,
    because an audit wants the mistake and the correction, not a silent gap."""
    shop.restock("2026-06-11 03:00:00", qty=20, cost=100000)
    void_id = insert("restock_batches", "2026-06-12 03:00:00", subtotal_cost=-100000,
                     total_cost=-100000, voids_batch_id=1)

    data = collect(offset=1)
    assert len(data["restocks"]) == 2
    void = next(b for b in data["restocks"] if b["voids_batch_id"])
    assert void["voids_batch_id"] == 1
    assert sum(b["total_cost"] for b in data["restocks"]) == 0
    assert data["summary"]["restock_cost"] == 0
    assert reports._batch_label(void) == f"{void_id}/1"


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
    # Gross profit costs only what was sold (3 x 9000), so the month reads as profitable
    # on the goods even while the restock spend puts net profit under water.
    assert s["cogs"] == 27000
    assert s["gross_profit"] == 45000 - 27000


def test_collect_ranks_products_by_profit_as_well_as_quantity(db_path, insert):
    cheap = insert("products", "2026-01-01 00:00:00", name="Teh", sku="TM-1",
                   price=2000, cost_price=1000, stock_qty=50)
    pricey = insert("products", "2026-01-01 00:00:00", name="Kopi", sku="KP-1",
                    price=50000, cost_price=45000, stock_qty=20)
    conn = database.get_db()
    for pid, qty, price, cost in ((cheap, 100, 2000, 1000), (pricey, 6, 50000, 45000)):
        cur = conn.execute("INSERT INTO orders (status, total_amount, created_at)"
                           " VALUES ('completed', ?, '2026-06-10 03:00:00')", (qty * price,))
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " unit_cost, subtotal) VALUES (?, ?, ?, ?, ?, ?)",
                     (cur.lastrowid, pid, qty, price, cost, qty * price))
    conn.commit()
    conn.close()
    data = collect(offset=1)
    assert data["by_quantity"][0]["name"] == "Teh"       # 100 units
    # Kopi took Rp 300.000 of the Rp 500.000 revenue but kept only Rp 30.000 of the
    # Rp 130.000 profit, so the profit ranking puts Teh first.
    assert data["by_profit"][0]["name"] == "Teh"
    assert data["by_profit"][0]["total_profit"] == 100000
    assert data["by_profit"][0]["share"] == pytest.approx(100000 / 130000 * 100)
    assert data["uncosted_sales"] == 0


def test_collect_lists_products_with_no_sales(db_path, shop):
    # The product exists and never sold in June, so it belongs in the appendix.
    unsold = collect(offset=1)["unsold"]
    assert [p["name"] for p in unsold] == ["Kopi"]
    assert unsold[0]["stock_value"] == 15000 * 100


def test_collect_omits_a_product_that_sold_from_the_unsold_list(db_path, shop):
    shop.order("2026-06-10 03:00:00", qty=1)
    assert collect(offset=1)["unsold"] == []


def test_unsold_is_not_truncated_for_the_report(db_path, insert):
    # The web page caps its panel; an audit document must account for all of them.
    import app as app_module
    for i in range(app_module.UNSOLD_PAGE_LIMIT + 3):
        insert("products", "2026-01-01 00:00:00", name=f"P{i:02d}", sku=f"S{i}",
               price=1000, stock_qty=1)
    assert len(collect(offset=1)["unsold"]) == app_module.UNSOLD_PAGE_LIMIT + 3


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


def test_render_includes_the_no_sales_appendix(db_path, insert):
    # A long idle list must paginate rather than overflow page 1, so assert the page
    # count grows with it: four fixed sections plus the appendix's own pages.
    for i in range(60):
        insert("products", "2026-01-01 00:00:00", name=f"P{i:02d}", sku=f"S{i}",
               price=1000, stock_qty=1)
    data = collect(offset=1)
    assert len(data["unsold"]) == 60
    content = reports.render(data, EN)
    assert content.startswith(b"%PDF-")
    assert content.count(b"/Type /Page\n") > 4


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


# --- Reusing an archived render ---
#
# Rendering a month costs seconds of CPU (fpdf2 measures every cell) in a server pinned
# to one worker, and every download of the same closed month used to pay it again.


@pytest.fixture
def renders(monkeypatch):
    """Counts how many times build() actually rendered, rather than served the archive."""
    calls = []
    real = reports.render

    def counted(data, t):
        calls.append(data["period"])
        return real(data, t)

    monkeypatch.setattr(reports, "render", counted)
    return calls


def build(offset=1, lang="en", now=NOW):
    db = database.get_db()
    try:
        return reports.build(db, offset, JKT, lang, now=now)
    finally:
        db.close()


def test_an_unchanged_month_is_served_from_the_archive(db_path, shop, renders):
    shop.order("2026-06-10 03:00:00", qty=3)
    _, first, _ = build()
    _, second, _ = build()
    assert renders == ["2026-06"]  # the second call rendered nothing
    assert second == first


def test_a_late_completion_in_the_window_forces_a_re_render(db_path, shop, renders):
    """The figures changed, so the archived PDF no longer describes the month. An order
    completed after the month closed still lands in it, which is exactly the case a
    plain 'closed months never change' rule would get wrong."""
    shop.order("2026-06-10 03:00:00", qty=3)
    build()
    shop.order("2026-06-11 03:00:00", qty=5)
    _, content, _ = build()
    assert renders == ["2026-06", "2026-06"]
    assert content.startswith(b"%PDF-")


def test_a_renamed_product_forces_a_re_render(db_path, shop, renders):
    # Nothing numeric moved, but the PDF prints the name on every line it appears in.
    shop.order("2026-06-10 03:00:00", qty=3)
    build()
    conn = database.get_db()
    conn.execute("UPDATE products SET name = 'Kopi Susu'")
    conn.commit()
    conn.close()
    build()
    assert renders == ["2026-06", "2026-06"]


def test_the_other_language_is_not_served_the_english_archive(db_path, shop, renders):
    shop.order("2026-06-10 03:00:00", qty=3)
    build(lang="en")
    build(lang="id")
    assert renders == ["2026-06", "2026-06"]


def test_the_clock_alone_does_not_invalidate_the_archive(db_path, shop, renders):
    """generated_at moves on every collect() and is left out of the hash, or nothing
    would ever hit. Both of these resolve offset=1 to June."""
    shop.order("2026-06-10 03:00:00", qty=3)
    build(now=NOW)
    build(now=datetime(2026, 7, 20, 18, 30, tzinfo=JKT))
    assert renders == ["2026-06"]


def test_a_month_still_running_is_re_rendered_as_it_fills(db_path, shop, renders):
    # offset=0 is the incomplete month: each sale changes it, so it must not stick.
    shop.order("2026-07-02 03:00:00", qty=1)
    build(offset=0)
    shop.order("2026-07-03 03:00:00", qty=2)
    build(offset=0)
    assert renders == ["2026-07", "2026-07"]


def test_a_missing_stamp_re_renders_rather_than_serving_a_stale_pdf(db_path, shop, renders):
    """The archive can outlive its sidecar -- an older release wrote no stamp at all,
    and a restore may bring back only the PDFs. Missing means unknown, not valid."""
    shop.order("2026-06-10 03:00:00", qty=3)
    path, first, _ = build()
    os.remove(os.path.join(reports.REPORT_DIR, "shop-report-2026-06.sha256"))
    _, second, _ = build()
    assert renders == ["2026-06", "2026-06"]
    assert second == first  # same records, so the same report -- just paid for again


def test_a_stamp_whose_pdf_is_gone_re_renders(db_path, shop, renders):
    shop.order("2026-06-10 03:00:00", qty=3)
    path, _, _ = build()
    os.remove(path)
    build()
    assert renders == ["2026-06", "2026-06"]
    assert os.path.exists(path)


def test_repeated_downloads_of_a_closed_month_render_once(client, shop, renders):
    """The route this was built for: the seller clicking Download twice.

    No `now` to pin here -- the route reads the real clock, so which month offset=1
    lands on depends on when the suite runs. The render count is the point.
    """
    shop.order("2026-06-10 03:00:00", qty=3)
    first = client.get("/api/reports/monthly?offset=1")
    second = client.get("/api/reports/monthly?offset=1")
    assert first.status_code == second.status_code == 200
    assert second.data == first.data
    assert len(renders) == 1


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
        assert anon.get("/api/reports/monthly").status_code == 401
        assert anon.post("/api/reports/monthly/send", json={}).status_code == 401
