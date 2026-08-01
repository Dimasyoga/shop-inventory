import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

JAKARTA = "Asia/Jakarta"


def utc_now():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# --- Restock history ---

def test_history_all_returns_200(client, insert):
    """Regression: ORDER BY was appended before WHERE, so this raised OperationalError."""
    insert("restock_batches", stamp(utc_now()), total_cost=50000)
    res = client.get("/api/restock/history?period=all")
    assert res.status_code == 200
    assert len(res.get_json()) == 1


@pytest.mark.parametrize("period", ["today", "week", "month", "year"])
def test_history_period_filters_do_not_500(client, insert, period):
    insert("restock_batches", stamp(utc_now()), total_cost=50000)
    res = client.get(f"/api/restock/history?period={period}&tz={JAKARTA}")
    assert res.status_code == 200


def test_history_orders_newest_first(client, insert):
    insert("restock_batches", "2026-07-10 03:00:00", total_cost=100)
    insert("restock_batches", "2026-07-12 03:00:00", total_cost=200)
    rows = client.get("/api/restock/history?period=all").get_json()
    assert [r["total_cost"] for r in rows] == [200, 100]


def test_history_today_uses_client_timezone_boundary(client, insert):
    """23:00 WIB yesterday is 16:00 UTC today: it must not count as 'today' in Jakarta."""
    today_wib = datetime.now(timezone.utc).astimezone().date()
    # Build instants relative to the client's real "today" so the test is date-independent.
    from zoneinfo import ZoneInfo
    jkt = ZoneInfo(JAKARTA)
    now_jkt = datetime.now(jkt)
    this_morning = now_jkt.replace(hour=9, minute=0, second=0, microsecond=0)
    late_yesterday = this_morning - timedelta(hours=10)  # 23:00 previous local day

    insert("restock_batches", stamp(this_morning.astimezone(timezone.utc)), total_cost=111)
    insert("restock_batches", stamp(late_yesterday.astimezone(timezone.utc)), total_cost=999)

    rows = client.get(f"/api/restock/history?period=today&tz={JAKARTA}").get_json()
    costs = [r["total_cost"] for r in rows]
    assert 111 in costs
    assert 999 not in costs


def test_history_rejects_bad_period(client):
    assert client.get("/api/restock/history?period=bogus").status_code == 400


def test_history_includes_nested_items(client, insert):
    batch = insert("restock_batches", stamp(utc_now()), total_cost=5000)
    prod = insert("products", stamp(utc_now()), name="Kopi", sku="K1", price=5000,
                  stock_qty=10)
    conn_items = {"batch_id": batch, "product_id": prod, "qty_added": 3, "allocated_cost": 5000}
    import database
    conn = database.get_db()
    conn.execute(
        "INSERT INTO restock_items (batch_id, product_id, qty_added, allocated_cost) VALUES (?,?,?,?)",
        tuple(conn_items.values()))
    conn.commit()
    conn.close()

    rows = client.get("/api/restock/history?period=all").get_json()
    assert rows[0]["items"][0]["product_name"] == "Kopi"


# --- Self use history ---

def test_self_use_history_all_returns_200(client, insert):
    insert("self_use_batches", stamp(utc_now()), total_value=50000)
    res = client.get("/api/self-use/history?period=all")
    assert res.status_code == 200
    assert len(res.get_json()) == 1


@pytest.mark.parametrize("period", ["today", "week", "month", "year"])
def test_self_use_history_period_filters_do_not_500(client, insert, period):
    insert("self_use_batches", stamp(utc_now()), total_value=50000)
    res = client.get(f"/api/self-use/history?period={period}&tz={JAKARTA}")
    assert res.status_code == 200


def test_self_use_history_orders_newest_first(client, insert):
    insert("self_use_batches", "2026-07-10 03:00:00", total_value=100)
    insert("self_use_batches", "2026-07-12 03:00:00", total_value=200)
    rows = client.get("/api/self-use/history?period=all").get_json()
    assert [r["total_value"] for r in rows] == [200, 100]


def test_self_use_history_today_uses_client_timezone_boundary(client, insert):
    """23:00 WIB yesterday is 16:00 UTC today: it must not count as 'today' in Jakarta."""
    jkt = ZoneInfo(JAKARTA)
    this_morning = datetime.now(jkt).replace(hour=9, minute=0, second=0, microsecond=0)
    late_yesterday = this_morning - timedelta(hours=10)  # 23:00 previous local day

    insert("self_use_batches", stamp(this_morning.astimezone(timezone.utc)), total_value=111)
    insert("self_use_batches", stamp(late_yesterday.astimezone(timezone.utc)), total_value=999)

    values = [r["total_value"] for r in
              client.get(f"/api/self-use/history?period=today&tz={JAKARTA}").get_json()]
    assert 111 in values
    assert 999 not in values


def test_self_use_history_rejects_bad_period(client):
    assert client.get("/api/self-use/history?period=bogus").status_code == 400


def test_self_use_history_includes_nested_items(client, insert, product):
    client.post("/api/self-use", json={"items": [{"product_id": product, "qty": 3}]})
    rows = client.get("/api/self-use/history?period=all").get_json()
    assert rows[0]["items"][0]["product_name"] == "Kopi"
    assert rows[0]["items"][0]["quantity"] == 3


# --- Voiding a batch ---

def test_void_restock_reverses_the_batch(client, product):
    client.post("/api/restock", json={
        "items": [{"product_id": product, "qty": 5, "unit_price": 1000}]})
    batch = client.get("/api/restock/history?period=all").get_json()[0]

    res = client.post(f"/api/restock/{batch['id']}/void")
    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    assert body["restored"] == ["Kopi"]

    rows = client.get("/api/restock/history?period=all").get_json()
    void = next(r for r in rows if r["voids_batch_id"])
    original = next(r for r in rows if r["id"] == batch["id"])
    assert void["voids_batch_id"] == batch["id"]
    assert original["voided_by"] == void["id"]
    assert void["total_cost"] == -batch["total_cost"]


def test_void_restock_rejects_a_second_void(client, product):
    client.post("/api/restock", json={
        "items": [{"product_id": product, "qty": 5, "unit_price": 1000}]})
    batch_id = client.get("/api/restock/history?period=all").get_json()[0]["id"]
    client.post(f"/api/restock/{batch_id}/void")

    res = client.post(f"/api/restock/{batch_id}/void")
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_void_restock_on_a_missing_batch_is_404(client):
    assert client.post("/api/restock/999/void").status_code == 404


def test_void_self_use_puts_the_stock_back(client, product):
    client.post("/api/self-use", json={"items": [{"product_id": product, "qty": 3}]})
    batch_id = client.get("/api/self-use/history?period=all").get_json()[0]["id"]

    assert client.post(f"/api/self-use/{batch_id}/void").status_code == 200
    rows = client.get("/api/self-use/history?period=all").get_json()
    assert next(r for r in rows if r["id"] == batch_id)["voided_by"] is not None
    products = client.get("/api/products").get_json()
    assert next(p for p in products if p["id"] == product)["stock_qty"] == 10


def test_void_self_use_on_a_missing_batch_is_404(client):
    assert client.post("/api/self-use/999/void").status_code == 404


# --- Sales ---

def _completed_order(insert, when, amount, product_id):
    order = insert("orders", stamp(when), status="completed", total_amount=amount)
    import database
    conn = database.get_db()
    conn.execute(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal) VALUES (?,?,?,?,?)",
        (order, product_id, 1, amount, amount))
    conn.commit()
    conn.close()
    return order


@pytest.fixture
def product(insert):
    return insert("products", stamp(utc_now()), name="Kopi", sku="K1", price=5000,
                  stock_qty=10)


def test_day_summary_finds_an_order_from_seconds_ago(client, insert, product):
    """Regression: the day filter was `>= X AND < X`, so this always returned zero."""
    _completed_order(insert, utc_now() - timedelta(seconds=5), 50000, product)
    data = client.get(f"/api/sales/summary?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert data["total_orders"] == 1
    assert data["total_revenue"] == 50000


def test_summary_does_not_multiply_revenue_by_line_item_count(client, insert, product):
    """Regression: SUM(o.total_amount) over a JOIN to order_items counted a
    two-line order's total twice, inflating revenue and net profit."""
    order = insert("orders", stamp(utc_now() - timedelta(seconds=5)),
                   status="completed", total_amount=50000)
    second = insert("products", stamp(utc_now()), name="Teh", sku="T1", price=20000, stock_qty=5)
    import database
    conn = database.get_db()
    conn.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)"
        " VALUES (?,?,?,?,?)",
        [(order, product, 1, 30000, 30000), (order, second, 2, 10000, 20000)])
    conn.commit()
    conn.close()

    data = client.get(f"/api/sales/summary?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert data["total_revenue"] == 50000
    assert data["total_orders"] == 1
    assert data["unique_skus"] == 2
    assert data["total_items_sold"] == 3
    assert data["net_profit"] == 50000


def test_summary_counts_a_completed_order_with_no_items(client, insert):
    """An INNER JOIN to order_items used to drop these from revenue entirely."""
    insert("orders", stamp(utc_now() - timedelta(seconds=5)), status="completed", total_amount=9000)
    data = client.get(f"/api/sales/summary?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert data["total_revenue"] == 9000
    assert data["total_orders"] == 1


def test_summary_rejects_non_integer_offset(client):
    res = client.get("/api/sales/summary?unit=day&offset=abc")
    assert res.status_code == 400


def test_summary_rejects_unknown_unit(client):
    assert client.get("/api/sales/summary?unit=decade").status_code == 400


def test_summary_falls_back_to_utc_for_bad_tz(client, product, insert):
    _completed_order(insert, utc_now() - timedelta(seconds=5), 1000, product)
    res = client.get("/api/sales/summary?unit=year&offset=0&tz=Not/AZone")
    assert res.status_code == 200


def test_trend_buckets_by_client_local_date(client, insert, product):
    from zoneinfo import ZoneInfo
    jkt = ZoneInfo(JAKARTA)
    morning = datetime.now(jkt).replace(hour=9, minute=0, second=0, microsecond=0)
    _completed_order(insert, morning.astimezone(timezone.utc), 7000, product)

    rows = client.get(f"/api/sales/trend?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert rows == [{"label": morning.date().isoformat(), "revenue": 7000}]


def test_trend_rejects_bad_offset(client):
    assert client.get("/api/sales/trend?unit=day&offset=xyz").status_code == 400


def test_product_performance_respects_day_window(client, insert, product):
    _completed_order(insert, utc_now() - timedelta(seconds=5), 3000, product)
    data = client.get(
        f"/api/sales/product-performance?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert data["by_quantity"][0]["name"] == "Kopi"


# --- Dashboard ---

# The stat cards render as a <div class="stat-value"> followed by its label; pairing
# them lets a test read one card without matching amounts elsewhere on the page
# (the recent-orders table also prints rupiah totals).
STAT_CARD = re.compile(
    r'<div class="stat-value">(.*?)</div>\s*<div class="stat-label">(.*?)</div>', re.S)


def _dashboard_stats(client):
    html = client.get("/").get_data(as_text=True)
    return {label.strip(): value.strip() for value, label in STAT_CARD.findall(html)}


def _this_month_label():
    import i18n
    now = datetime.now(ZoneInfo(JAKARTA))
    return f"{i18n.month_name(now.month, 'en')} {now.year}"


def test_dashboard_month_window_matches_the_sales_page(client, insert, product):
    """Both must report the current calendar month in the shop timezone. The
    dashboard used to use a rolling 30-day UTC window and disagree."""
    jkt = ZoneInfo(JAKARTA)
    month_start = datetime.now(jkt).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Both instants fall on the last day of the previous month in UTC; only Jakarta's
    # midnight separates them, so this also pins the window to the shop timezone.
    early_this_month = month_start + timedelta(hours=1)
    late_last_month = month_start - timedelta(hours=1)

    _completed_order(insert, early_this_month.astimezone(timezone.utc), 120000, product)
    _completed_order(insert, late_last_month.astimezone(timezone.utc), 999000, product)
    insert("restock_batches", stamp(early_this_month.astimezone(timezone.utc)), total_cost=20000)

    api = client.get(f"/api/sales/summary?unit=month&offset=0&tz={JAKARTA}").get_json()
    assert (api["total_revenue"], api["restock_cost"], api["net_profit"]) == (120000, 20000, 100000)

    month = _this_month_label()
    stats = _dashboard_stats(client)
    assert stats[f"Revenue ({month})"] == "Rp 120.000"
    assert stats[f"Restock Cost ({month})"] == "Rp 20.000"
    assert stats[f"Net Profit ({month})"] == "Rp 100.000"


def test_dashboard_labels_name_the_month(client):
    assert f"Net Profit ({_this_month_label()})" in _dashboard_stats(client)


def test_summary_reports_self_use_without_touching_net_profit(client, insert, product):
    """Self use is its own metric: the goods were already booked as restock spend,
    so deducting their retail value from profit again would double-count."""
    when = utc_now() - timedelta(seconds=5)
    _completed_order(insert, when, 50000, product)
    insert("restock_batches", stamp(when), total_cost=20000)
    insert("self_use_batches", stamp(when), total_value=30000)

    api = client.get(f"/api/sales/summary?unit=year&offset=0&tz={JAKARTA}").get_json()
    assert api["self_use_value"] == 30000
    assert api["restock_cost"] == 20000
    assert api["net_profit"] == 30000  # 50000 - 20000, self use excluded


def test_summary_reports_gross_profit_beside_the_cash_view(client, insert, product):
    """The two profit figures answer different questions and both are reported: gross
    profit costs what was sold, net profit counts what was spent."""
    when = utc_now() - timedelta(seconds=5)
    order = insert("orders", stamp(when), status="completed", total_amount=50000)
    import database
    conn = database.get_db()
    conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                 " unit_cost, subtotal) VALUES (?,?,?,?,?,?)",
                 (order, product, 2, 25000, 15000, 50000))
    conn.commit()
    conn.close()
    insert("restock_batches", stamp(when), subtotal_cost=90000, total_cost=90000)

    api = client.get(f"/api/sales/summary?unit=year&offset=0&tz={JAKARTA}").get_json()
    assert api["cogs"] == 30000
    assert api["gross_profit"] == 20000     # 50000 - 2 x 15000
    assert api["net_profit"] == -40000      # 50000 - 90000 of restock spend


def test_gross_profit_leaves_out_sales_whose_cost_is_unknown(client, insert, product):
    """An uncosted line has no cost to subtract, so counting its revenue would report it
    as pure profit -- and the profit ranking beside it excludes the same sale. Both figures
    describe the costed sales only, and the count says how many were held back."""
    when = utc_now() - timedelta(seconds=5)
    _completed_order(insert, when, 50000, product)       # no cost recorded
    api = client.get(f"/api/sales/summary?unit=year&offset=0&tz={JAKARTA}").get_json()
    assert api["total_revenue"] == 50000                 # revenue is still whole
    assert api["cogs"] == 0
    assert api["gross_profit"] == 0                      # not 50000
    assert api["uncosted_sales"] == 1


def test_products_created_with_stock_must_declare_a_cost(client):
    """Opening stock was paid for, and a sale snapshots its cost at order time -- so stock
    entered without one sells at an unknown cost with no way to repair it afterwards."""
    res = client.post("/api/products", json={"name": "A", "price": 20000, "stock_qty": 10})
    assert res.status_code == 400
    assert "Cost price" in res.get_json()["error"]
    # Starting empty is fine: the first restock records the cost.
    assert client.post("/api/products",
                       json={"name": "B", "price": 20000, "stock_qty": 0}).status_code == 200
    assert client.post("/api/products", json={"name": "C", "price": 20000, "stock_qty": 10,
                                              "cost_price": 12000}).status_code == 200


def test_dashboard_shows_self_use_card(client, insert, product):
    jkt = ZoneInfo(JAKARTA)
    early_this_month = datetime.now(jkt).replace(
        day=1, hour=1, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    _completed_order(insert, early_this_month, 50000, product)
    insert("restock_batches", stamp(early_this_month), total_cost=20000)
    insert("self_use_batches", stamp(early_this_month), total_value=30000)

    month = _this_month_label()
    stats = _dashboard_stats(client)
    assert stats[f"Self Use ({month})"] == "Rp 30.000"
    assert stats[f"Net Profit ({month})"] == "Rp 30.000"


def test_self_use_window_matches_the_dashboard_month(client, insert, product):
    """A batch from last month must not leak into this month's card."""
    jkt = ZoneInfo(JAKARTA)
    month_start = datetime.now(jkt).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    insert("self_use_batches", stamp((month_start + timedelta(hours=1)).astimezone(timezone.utc)),
           total_value=7000)
    insert("self_use_batches", stamp((month_start - timedelta(hours=1)).astimezone(timezone.utc)),
           total_value=999000)

    assert _dashboard_stats(client)[f"Self Use ({_this_month_label()})"] == "Rp 7.000"


def test_dashboard_net_profit_survives_a_multi_line_order(client, insert, product):
    """Guards the same JOIN fan-out as the sales summary, through the rendered page."""
    order = insert("orders", stamp(utc_now()), status="completed", total_amount=80000)
    import database
    conn = database.get_db()
    conn.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)"
        " VALUES (?,?,?,?,?)",
        [(order, product, 1, 50000, 50000), (order, product, 1, 30000, 30000)])
    conn.commit()
    conn.close()

    stats = _dashboard_stats(client)
    assert stats[f"Net Profit ({_this_month_label()})"] == "Rp 80.000"
