from datetime import datetime, timezone

import pytest

import database


def utc_now():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def run_sql(sql, params=()):
    conn = database.get_db()
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return rows, row_id


@pytest.fixture
def product(insert):
    def _make(stock=10, price=5000, name="Kopi", sku=None):
        return insert("products", stamp(utc_now()), name=name, sku=sku,
                      price=price, stock_qty=stock)
    return _make


@pytest.fixture
def order(insert):
    """Create an order with items in a given status. items = [(product_id, qty, unit_price)]."""
    def _make(items, status="confirmed"):
        total = sum(q * p for _, q, p in items)
        order_id = insert("orders", stamp(utc_now()), status=status, total_amount=total)
        for pid, qty, price in items:
            run_sql(
                "INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal)"
                " VALUES (?,?,?,?,?)", (order_id, pid, qty, price, qty * price))
        return order_id
    return _make


def stock_of(product_id):
    rows, _ = run_sql("SELECT stock_qty FROM products WHERE id = ?", (product_id,))
    return rows[0]["stock_qty"]


# --- Phase 1: quick wins ---

def test_oversell_returns_status_400_with_dict_body(client, product, order):
    """Regression: jsonify({...}, 400) returned HTTP 200 with a JSON array body."""
    pid = product(stock=2)
    oid = order([(pid, 5, 5000)])
    res = client.post(f"/api/orders/{oid}/complete")
    assert res.status_code == 400
    body = res.get_json()
    assert isinstance(body, dict)
    assert "error" in body


def test_format_datetime_handles_none_and_garbage():
    from app import format_datetime
    assert format_datetime(None) == ""
    assert format_datetime("") == ""
    assert format_datetime("not-a-date") == "not-a-date"
    assert format_datetime("2026-07-17 02:00:00")  # valid input still formats


def test_dashboard_lists_multi_item_order_once(client, product, order):
    """Regression: LEFT JOIN order_items + LIMIT 5 made a 3-item order fill 3 slots."""
    p1, p2, p3 = product(name="A"), product(name="B"), product(name="C")
    oid = order([(p1, 1, 100), (p2, 1, 100), (p3, 1, 100)], status="draft")
    html = client.get("/").get_data(as_text=True)
    assert html.count(f"Order #{oid}") == 1


# --- Phase 3: atomic stock updates + audit logging ---

def stock_logs_for(product_id):
    rows, _ = run_sql("SELECT change_qty, reason FROM stock_logs WHERE product_id = ?", (product_id,))
    return [(r["change_qty"], r["reason"]) for r in rows]


def test_partial_completion_rolls_back_everything(client, product, order):
    """Item 1 in stock, item 2 not: order must stay confirmed with NO stock or log changes."""
    p_ok = product(stock=10, name="InStock")
    p_short = product(stock=1, name="Short")
    oid = order([(p_ok, 5, 1000), (p_short, 3, 1000)])

    res = client.post(f"/api/orders/{oid}/complete")
    assert res.status_code == 400
    assert stock_of(p_ok) == 10  # decrement of item 1 rolled back
    rows, _ = run_sql("SELECT status FROM orders WHERE id = ?", (oid,))
    assert rows[0]["status"] == "confirmed"
    assert stock_logs_for(p_ok) == []


def test_completion_decrements_stock_and_logs(client, product, order):
    pid = product(stock=10)
    oid = order([(pid, 4, 1000)])
    res = client.post(f"/api/orders/{oid}/complete")
    assert res.status_code == 200
    assert stock_of(pid) == 6
    assert stock_logs_for(pid) == [(-4, f"sale order #{oid}")]


def test_restock_increments_stock_and_logs(client, product):
    pid = product(stock=5)
    res = client.post("/api/restock", json={"items": [{"product_id": pid, "qty": 7}], "total_cost": 10000})
    assert res.status_code == 200
    assert stock_of(pid) == 12
    logs = stock_logs_for(pid)
    assert len(logs) == 1
    assert logs[0][0] == 7
    assert logs[0][1].startswith("restock batch #")


def test_adjust_below_zero_rejected_and_unchanged(client, product):
    pid = product(stock=3)
    res = client.post("/api/stock/adjust", json={"product_id": pid, "change_qty": -5, "reason": "shrinkage"})
    assert res.status_code == 400
    assert stock_of(pid) == 3
    assert stock_logs_for(pid) == []
