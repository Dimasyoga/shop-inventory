from datetime import datetime, timedelta, timezone

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
    cat = insert("categories", stamp(utc_now()), name="Drinks")
    prod = insert("products", stamp(utc_now()), name="Kopi", sku="K1", price=5000,
                  stock_qty=10, category_id=cat)
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
    cat = insert("categories", stamp(utc_now()), name="Drinks")
    return insert("products", stamp(utc_now()), name="Kopi", sku="K1", price=5000,
                  stock_qty=10, category_id=cat)


def test_day_summary_finds_an_order_from_seconds_ago(client, insert, product):
    """Regression: the day filter was `>= X AND < X`, so this always returned zero."""
    _completed_order(insert, utc_now() - timedelta(seconds=5), 50000, product)
    data = client.get(f"/api/sales/summary?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert data["total_orders"] == 1
    assert data["total_revenue"] == 50000


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


def test_top_products_respects_day_window(client, insert, product):
    _completed_order(insert, utc_now() - timedelta(seconds=5), 3000, product)
    data = client.get(f"/api/sales/top-products?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert data["top"][0]["name"] == "Kopi"
