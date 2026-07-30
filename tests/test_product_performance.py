"""Product performance: volume leaders, value leaders and idle stock.

The point of the value ranking is that it must NOT agree with the quantity ranking
when cheap goods outsell expensive ones, so the fixtures here are built around
exactly that: Teh moves many cheap units, Kopi few expensive ones.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import database
import services

JKT = ZoneInfo("Asia/Jakarta")
JAKARTA = "Asia/Jakarta"


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def utc_now():
    return datetime.now(timezone.utc)


@pytest.fixture
def catalog(insert):
    """Teh: cheap, high volume. Kopi: expensive, low volume. Gula: never sells."""
    now = stamp(utc_now())
    return {
        "teh": insert("products", now, name="Teh", sku="TM-1", price=2000, stock_qty=50),
        "kopi": insert("products", now, name="Kopi", sku="KP-1", price=50000, stock_qty=20),
        "gula": insert("products", now, name="Gula", sku="GP-1", price=12000, stock_qty=30),
    }


@pytest.fixture
def sale(insert):
    """Record a completed order line. `total` defaults to the line subtotal."""
    def _sale(when, product_id, qty, unit_price, total=None):
        subtotal = qty * unit_price
        oid = insert("orders", stamp(when), status="completed",
                     total_amount=subtotal if total is None else total)
        conn = database.get_db()
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " subtotal) VALUES (?, ?, ?, ?, ?)",
                     (oid, product_id, qty, unit_price, subtotal))
        conn.commit()
        conn.close()
        return oid
    return _sale


def window(days=1):
    """A window comfortably around 'just now'."""
    now = datetime.now(JKT)
    return now - timedelta(days=days), now + timedelta(days=days)


def call(fn, *args, **kwargs):
    db = database.get_db()
    try:
        return fn(db, *args, **kwargs)
    finally:
        db.close()


# --- format_percent ---

def test_format_percent_uses_one_decimal():
    assert services.format_percent(61.34) == "61.3%"
    assert services.format_percent(100) == "100.0%"
    assert services.format_percent(0) == "0.0%"


def test_format_percent_keeps_small_contributors_visible():
    # A whole-number format would render this as 0%, hiding the product entirely.
    assert services.format_percent(0.42) == "0.4%"


def test_format_percent_uses_a_comma_in_indonesian():
    assert services.format_percent(61.34, "id") == "61,3%"


# --- Ranking ---

def test_value_ranking_disagrees_with_quantity_ranking(db_path, catalog, sale):
    # Teh: 100 x Rp 2.000 = Rp 200.000 over 100 units.
    # Kopi:  6 x Rp 50.000 = Rp 300.000 over 6 units.
    sale(utc_now(), catalog["teh"], qty=100, unit_price=2000)
    sale(utc_now(), catalog["kopi"], qty=6, unit_price=50000)
    start, end = window()

    by_qty = call(services.top_products_by_quantity, start, end)
    by_value = call(services.top_products_by_value, start, end)

    # This is the whole justification for the new metric: the volume leader is not
    # the product that actually earned the most.
    assert by_qty[0]["name"] == "Teh"
    assert by_value[0]["name"] == "Kopi"


def test_quantity_ranking_sums_units_across_orders(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=4, unit_price=2000)
    sale(utc_now(), catalog["teh"], qty=6, unit_price=2000)
    rows = call(services.top_products_by_quantity, *window())
    assert rows[0]["total_sold"] == 10
    assert rows[0]["total_revenue"] == 20000


def test_ranking_honours_the_limit(db_path, catalog, sale):
    for pid in catalog.values():
        sale(utc_now(), pid, qty=1, unit_price=1000)
    assert len(call(services.top_products_by_value, *window(), limit=2)) == 2


def test_ranking_ignores_orders_outside_the_window(db_path, catalog, sale):
    sale(utc_now() - timedelta(days=10), catalog["teh"], qty=5, unit_price=2000)
    assert call(services.top_products_by_quantity, *window()) == []


def test_ranking_ignores_orders_that_never_completed(db_path, catalog, insert):
    for status in ("draft", "confirmed", "cancelled"):
        oid = insert("orders", stamp(utc_now()), status=status, total_amount=10000)
        conn = database.get_db()
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " subtotal) VALUES (?, ?, ?, ?, ?)", (oid, catalog["teh"], 5, 2000, 10000))
        conn.commit()
        conn.close()
    assert call(services.top_products_by_quantity, *window()) == []


# --- Revenue share ---

def test_shares_total_one_hundred(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=100, unit_price=2000)    # 200000
    sale(utc_now(), catalog["kopi"], qty=6, unit_price=50000)    # 300000
    rows = call(services.top_products_by_value, *window())
    assert sum(r["share"] for r in rows) == pytest.approx(100.0)
    assert rows[0]["share"] == pytest.approx(60.0)   # 300000 / 500000
    assert rows[1]["share"] == pytest.approx(40.0)


def test_share_divides_by_line_revenue_not_the_stored_order_total(db_path, catalog, sale):
    # orders.total_amount is stored separately from its lines and can disagree (a
    # discount, a correction). Dividing by it would give shares that never total 100.
    sale(utc_now(), catalog["teh"], qty=10, unit_price=2000, total=999999)
    rows = call(services.top_products_by_value, *window())
    assert rows[0]["total_revenue"] == 20000
    assert rows[0]["share"] == pytest.approx(100.0)


def test_share_is_zero_for_an_empty_window(db_path, catalog):
    # Guards the division: no sales means no denominator.
    assert call(services.top_products_by_value, *window()) == []


def test_share_survives_a_zero_value_sale(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=1, unit_price=0)
    rows = call(services.top_products_by_value, *window())
    assert rows[0]["share"] == 0.0


# --- Products without sales ---

def test_unsold_lists_only_products_that_did_not_sell(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=1, unit_price=2000)
    names = [p["name"] for p in call(services.products_without_sales, *window())]
    assert "Teh" not in names
    assert set(names) == {"Kopi", "Gula"}


def test_unsold_surfaces_products_that_never_sold_at_all(db_path, catalog):
    # The gap the old Bottom 3 could not cover: its inner join on order_items meant
    # a product had to sell at least once to be ranked at all.
    names = [p["name"] for p in call(services.products_without_sales, *window())]
    assert set(names) == {"Teh", "Kopi", "Gula"}


def test_unsold_is_ordered_by_the_capital_tied_up(db_path, catalog):
    rows = call(services.products_without_sales, *window())
    # Kopi 20 x 50000 = 1.000.000; Gula 30 x 12000 = 360.000; Teh 50 x 2000 = 100.000
    assert [p["name"] for p in rows] == ["Kopi", "Gula", "Teh"]
    assert [p["stock_value"] for p in rows] == [1000000, 360000, 100000]
    assert rows[0]["stock_qty"] == 20


def test_unsold_excludes_archived_products(db_path, catalog):
    conn = database.get_db()
    conn.execute("UPDATE products SET is_archived = 1 WHERE id = ?", (catalog["kopi"],))
    conn.commit()
    conn.close()
    names = [p["name"] for p in call(services.products_without_sales, *window())]
    assert "Kopi" not in names


def test_a_sale_outside_the_window_still_counts_as_unsold(db_path, catalog, sale):
    sale(utc_now() - timedelta(days=10), catalog["teh"], qty=5, unit_price=2000)
    names = [p["name"] for p in call(services.products_without_sales, *window())]
    assert "Teh" in names


# --- Endpoint ---

def test_endpoint_returns_all_three_panels(client, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=100, unit_price=2000)
    sale(utc_now(), catalog["kopi"], qty=6, unit_price=50000)
    data = client.get(
        f"/api/sales/product-performance?unit=day&offset=0&tz={JAKARTA}").get_json()
    assert data["by_quantity"][0]["name"] == "Teh"
    assert data["by_value"][0]["name"] == "Kopi"
    assert data["by_value"][0]["share"] == pytest.approx(60.0)
    assert [p["name"] for p in data["unsold"]["items"]] == ["Gula"]
    assert data["unsold"]["total"] == 1
    assert data["unsold"]["total_stock_value"] == 360000


def test_endpoint_totals_cover_products_beyond_the_page_limit(client, insert):
    import app as app_module
    for i in range(app_module.UNSOLD_PAGE_LIMIT + 5):
        insert("products", stamp(utc_now()), name=f"P{i:02d}", sku=f"S{i}",
               price=1000, stock_qty=1)
    data = client.get(
        f"/api/sales/product-performance?unit=day&offset=0&tz={JAKARTA}").get_json()
    # The list is capped for display but the totals describe every idle product, so
    # the card can say how many were withheld.
    assert len(data["unsold"]["items"]) == app_module.UNSOLD_PAGE_LIMIT
    assert data["unsold"]["total"] == app_module.UNSOLD_PAGE_LIMIT + 5
    assert data["unsold"]["total_stock_value"] == (app_module.UNSOLD_PAGE_LIMIT + 5) * 1000


def test_endpoint_rejects_a_bad_offset(client):
    assert client.get(
        "/api/sales/product-performance?unit=day&offset=xyz").status_code == 400


def test_endpoint_rejects_a_bad_unit(client):
    assert client.get(
        "/api/sales/product-performance?unit=fortnight&offset=0").status_code == 400
