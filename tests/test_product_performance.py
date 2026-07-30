"""Product performance: volume leaders, profit leaders and idle stock.

The point of each ranking is that it must NOT agree with the others, so the fixtures
are built around exactly that: Teh moves many cheap units at a fat margin, Kopi a few
expensive ones at a thin one. Teh leads on quantity and on profit, Kopi on revenue.
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
    """Teh: cheap, high volume, 50% margin. Kopi: expensive, low volume, 10% margin.
    Gula: never sells."""
    now = stamp(utc_now())
    return {
        "teh": insert("products", now, name="Teh", sku="TM-1", price=2000,
                      cost_price=1000, stock_qty=50),
        "kopi": insert("products", now, name="Kopi", sku="KP-1", price=50000,
                       cost_price=45000, stock_qty=20),
        "gula": insert("products", now, name="Gula", sku="GP-1", price=12000,
                       cost_price=6000, stock_qty=30),
    }


@pytest.fixture
def sale(insert):
    """Record a completed order line, costed as create_order would cost it.

    `total` defaults to the line subtotal; `unit_cost` to the product's current cost, so
    a test only names a cost when it cares about one.
    """
    def _sale(when, product_id, qty, unit_price, total=None, unit_cost=None):
        subtotal = qty * unit_price
        oid = insert("orders", stamp(when), status="completed",
                     total_amount=subtotal if total is None else total)
        conn = database.get_db()
        if unit_cost is None:
            unit_cost = conn.execute("SELECT cost_price FROM products WHERE id = ?",
                                     (product_id,)).fetchone()["cost_price"]
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity, unit_price,"
                     " unit_cost, subtotal) VALUES (?, ?, ?, ?, ?, ?)",
                     (oid, product_id, qty, unit_price, unit_cost, subtotal))
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

def test_profit_ranking_disagrees_with_the_revenue_it_replaced(db_path, catalog, sale):
    # Teh: 100 x Rp 2.000 = Rp 200.000 revenue, Rp 100.000 profit at 50%.
    # Kopi:  6 x Rp 50.000 = Rp 300.000 revenue, Rp  30.000 profit at 10%.
    sale(utc_now(), catalog["teh"], qty=100, unit_price=2000)
    sale(utc_now(), catalog["kopi"], qty=6, unit_price=50000)
    start, end = window()

    by_qty = call(services.top_products_by_quantity, start, end)
    by_profit = call(services.top_products_by_profit, start, end)

    # This is the whole justification for ranking on profit: Kopi takes the most money
    # and keeps the least of it, so a revenue ranking would have promoted the wrong
    # product. The volume leader is not automatically right either -- it is here only
    # because Teh happens to be the fat-margin line.
    assert by_qty[0]["name"] == "Teh"
    assert [r["name"] for r in by_profit] == ["Teh", "Kopi"]
    assert by_profit[0]["total_revenue"] == 200000
    assert by_profit[0]["total_cost"] == 100000
    assert by_profit[0]["total_profit"] == 100000
    assert by_profit[1]["total_profit"] == 30000


def test_margin_is_profit_over_that_product_s_own_revenue(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=100, unit_price=2000)
    sale(utc_now(), catalog["kopi"], qty=6, unit_price=50000)
    rows = {r["name"]: r for r in call(services.top_products_by_profit, *window())}
    assert rows["Teh"]["margin"] == pytest.approx(50.0)
    assert rows["Kopi"]["margin"] == pytest.approx(10.0)


def test_ranking_uses_the_cost_recorded_on_the_line_not_the_current_one(db_path, catalog, sale):
    # The snapshot is the point: restocking at a new price must not rewrite the margin of
    # a sale already made and reported.
    sale(utc_now(), catalog["teh"], qty=10, unit_price=2000, unit_cost=1000)
    conn = database.get_db()
    conn.execute("UPDATE products SET cost_price = 1900 WHERE id = ?", (catalog["teh"],))
    conn.commit()
    conn.close()
    rows = call(services.top_products_by_profit, *window())
    assert rows[0]["total_profit"] == 10000     # not 1000
    assert rows[0]["margin"] == pytest.approx(50.0)


def test_a_sale_with_no_recorded_cost_is_left_out_and_counted(db_path, catalog, sale):
    # With cost 0 it would report as pure profit and outrank everything real.
    sale(utc_now(), catalog["teh"], qty=100, unit_price=2000, unit_cost=0)
    sale(utc_now(), catalog["kopi"], qty=6, unit_price=50000)
    rows = call(services.top_products_by_profit, *window())
    assert [r["name"] for r in rows] == ["Kopi"]
    assert call(services.sales_missing_cost, *window()) == 1


def test_a_product_still_ranks_on_the_part_of_its_sales_that_has_a_cost(db_path, catalog, sale):
    # Sold before its first restock, then after: excluding per line rather than per product
    # keeps the costed half of its history in the ranking instead of dropping the product.
    sale(utc_now(), catalog["teh"], qty=10, unit_price=2000, unit_cost=0)
    sale(utc_now(), catalog["teh"], qty=10, unit_price=2000, unit_cost=1000)
    rows = call(services.top_products_by_profit, *window())
    assert [r["name"] for r in rows] == ["Teh"]
    # Only the costed line: 10 x 2000 revenue, not 20 x 2000, so the margin stays true.
    assert rows[0]["total_revenue"] == 20000
    assert rows[0]["total_profit"] == 10000
    assert rows[0]["margin"] == pytest.approx(50.0)
    assert call(services.sales_missing_cost, *window()) == 1


def test_the_quantity_ranking_still_counts_uncosted_sales(db_path, catalog, sale):
    # Units moved is a fact regardless of what they cost; only the money figures filter.
    sale(utc_now(), catalog["teh"], qty=10, unit_price=2000, unit_cost=0)
    rows = call(services.top_products_by_quantity, *window())
    assert rows[0]["total_sold"] == 10


def test_nothing_is_reported_missing_when_every_sale_is_costed(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=1, unit_price=2000)
    assert call(services.sales_missing_cost, *window()) == 0


def test_quantity_ranking_sums_units_across_orders(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=4, unit_price=2000)
    sale(utc_now(), catalog["teh"], qty=6, unit_price=2000)
    rows = call(services.top_products_by_quantity, *window())
    assert rows[0]["total_sold"] == 10
    assert rows[0]["total_revenue"] == 20000


def test_ranking_honours_the_limit(db_path, catalog, sale):
    for pid in catalog.values():
        sale(utc_now(), pid, qty=1, unit_price=100000)
    assert len(call(services.top_products_by_profit, *window(), limit=2)) == 2


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


# --- Profit share ---

def test_shares_total_one_hundred(db_path, catalog, sale):
    sale(utc_now(), catalog["teh"], qty=100, unit_price=2000)    # profit 100000
    sale(utc_now(), catalog["kopi"], qty=6, unit_price=50000)    # profit  30000
    rows = call(services.top_products_by_profit, *window())
    assert sum(r["share"] for r in rows) == pytest.approx(100.0)
    assert rows[0]["share"] == pytest.approx(100000 / 130000 * 100)
    assert rows[1]["share"] == pytest.approx(30000 / 130000 * 100)


def test_share_divides_by_line_figures_not_the_stored_order_total(db_path, catalog, sale):
    # orders.total_amount is stored separately from its lines and can disagree (a
    # discount, a correction). Dividing by it would give shares that never total 100.
    sale(utc_now(), catalog["teh"], qty=10, unit_price=2000, total=999999)
    rows = call(services.top_products_by_profit, *window())
    assert rows[0]["total_revenue"] == 20000
    assert rows[0]["share"] == pytest.approx(100.0)


def test_share_is_zero_for_an_empty_window(db_path, catalog):
    # Guards the division: no sales means no denominator.
    assert call(services.top_products_by_profit, *window()) == []


def test_share_is_zero_when_the_window_lost_money(db_path, catalog, sale):
    # Sold below cost: a negative denominator has no meaningful percentage to give, and
    # the loss itself still has to be visible in the ranking.
    sale(utc_now(), catalog["teh"], qty=10, unit_price=500, unit_cost=1000)
    rows = call(services.top_products_by_profit, *window())
    assert rows[0]["total_profit"] == -5000
    assert rows[0]["share"] == 0.0
    assert rows[0]["margin"] == pytest.approx(-100.0)


def test_margin_survives_a_zero_value_sale(db_path, catalog, sale):
    # A giveaway has no revenue to divide by; it must not raise.
    sale(utc_now(), catalog["teh"], qty=1, unit_price=0, unit_cost=1000)
    rows = call(services.top_products_by_profit, *window())
    assert rows[0]["margin"] == 0.0
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
    assert data["by_profit"][0]["name"] == "Teh"
    assert data["by_profit"][0]["total_profit"] == 100000
    assert data["by_profit"][0]["margin"] == pytest.approx(50.0)
    assert data["uncosted_sales"] == 0
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
