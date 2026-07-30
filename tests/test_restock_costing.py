"""Turning a supplier invoice into a per-product cost.

The invoice lists a price per product, then a discount voucher, shipping and sometimes a
bank fee that all apply to the invoice as a whole. Margin needs a per-unit cost, so those
three charges have to be spread across the lines -- by line value, since a voucher
discounts what was bought and shipping rides on the goods.
"""
from datetime import datetime, timezone

import pytest

import database
import services
from services import ServiceError


def utc_now():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def product(insert):
    def _make(name="Kopi", price=20000, cost=0, stock=0, sku=None):
        return insert("products", stamp(utc_now()), name=name, sku=sku, price=price,
                      cost_price=cost, stock_qty=stock)
    return _make


def call(fn, *args, **kwargs):
    db = database.get_db()
    try:
        return fn(db, *args, **kwargs)
    finally:
        db.close()


def one(sql, params=()):
    conn = database.get_db()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


# --- Allocation ---

def test_charges_are_spread_across_the_lines_by_value():
    # The worked invoice: Kopi 10 x 12.000 and Gula 5 x 8.000, less a 20.000 voucher,
    # plus 15.000 shipping and a 2.500 bank fee. Kopi is 75% of the value, so it takes
    # 75% of the net 2.500 the charges add up to.
    lines, subtotal, total = services.allocate_restock_costs(
        [{"product_id": 1, "qty": 10, "unit_price": 12000},
         {"product_id": 2, "qty": 5, "unit_price": 8000}],
        discount=20000, shipping_cost=15000, admin_fee=2500)
    assert subtotal == 160000
    assert total == 157500
    assert lines[0]["unit_cost"] == pytest.approx(11812.5)
    assert lines[1]["unit_cost"] == pytest.approx(7875.0)
    # Whatever the split, the lines must account for exactly what was paid.
    assert sum(line["line_cost"] for line in lines) == pytest.approx(total)


def test_a_line_costs_its_invoice_price_when_there_are_no_charges():
    lines, subtotal, total = services.allocate_restock_costs(
        [{"product_id": 1, "qty": 4, "unit_price": 9000}])
    assert (subtotal, total) == (36000, 36000)
    assert lines[0]["unit_cost"] == 9000


def test_an_invoice_priced_at_zero_splits_its_charges_by_quantity():
    # A supplier sample: no line values to weight shipping by, and dividing by the zero
    # subtotal would raise.
    lines, subtotal, total = services.allocate_restock_costs(
        [{"product_id": 1, "qty": 3, "unit_price": 0},
         {"product_id": 2, "qty": 1, "unit_price": 0}],
        shipping_cost=8000)
    assert subtotal == 0
    assert total == 8000
    assert lines[0]["line_cost"] == pytest.approx(6000)
    assert lines[1]["line_cost"] == pytest.approx(2000)


# --- Weighted moving average ---

def test_a_first_restock_sets_the_cost_outright(db_path, product):
    pid = product(stock=0)
    call(services.create_restock, [{"product_id": pid, "qty": 5, "unit_price": 10000}])
    assert one("SELECT cost_price FROM products WHERE id=?", (pid,))["cost_price"] == 10000


def test_a_later_restock_averages_over_the_stock_on_hand(db_path, product):
    # 4 left at 10.000 plus 6 at 15.000 is 130.000 of stock across 10 units. Adopting the
    # new price outright would revalue the four units already paid for at less.
    pid = product(stock=4, cost=10000)
    call(services.create_restock, [{"product_id": pid, "qty": 6, "unit_price": 15000}])
    assert one("SELECT cost_price FROM products WHERE id=?", (pid,))["cost_price"] == 13000


def test_an_unknown_cost_is_replaced_rather_than_averaged_with(db_path, product):
    # Averaging against a recorded 0 would halve the real cost of the incoming batch.
    pid = product(stock=10, cost=0)
    call(services.create_restock, [{"product_id": pid, "qty": 10, "unit_price": 8000}])
    assert one("SELECT cost_price FROM products WHERE id=?", (pid,))["cost_price"] == 8000


def test_stock_that_ran_out_carries_no_cost_to_average(db_path, product):
    pid = product(stock=0, cost=10000)
    call(services.create_restock, [{"product_id": pid, "qty": 5, "unit_price": 20000}])
    assert one("SELECT cost_price FROM products WHERE id=?", (pid,))["cost_price"] == 20000


def test_the_same_product_listed_twice_averages_line_by_line(db_path, product):
    # Each line has to average onto the result of the previous one, not onto the stock as
    # it stood before the batch started.
    pid = product(stock=0)
    call(services.create_restock, [
        {"product_id": pid, "qty": 5, "unit_price": 10000},
        {"product_id": pid, "qty": 5, "unit_price": 20000}])
    row = one("SELECT stock_qty, cost_price FROM products WHERE id=?", (pid,))
    assert row["stock_qty"] == 10
    assert row["cost_price"] == 15000


# --- What is written down ---

def test_the_batch_records_the_invoice_as_written(db_path, product):
    a = product(name="Kopi", sku="KP-1")
    b = product(name="Gula", sku="GL-1")
    result = call(services.create_restock,
                  [{"product_id": a, "qty": 10, "unit_price": 12000},
                   {"product_id": b, "qty": 5, "unit_price": 8000}],
                  discount=20000, shipping_cost=15000, admin_fee=2500)
    assert result["subtotal"] == 160000
    assert result["total_cost"] == 157500
    batch = one("SELECT * FROM restock_batches WHERE id=?", (result["batch_id"],))
    assert (batch["subtotal_cost"], batch["discount"], batch["shipping_cost"],
            batch["admin_fee"], batch["total_cost"]) == (160000, 20000, 15000, 2500, 157500)
    line = one("SELECT * FROM restock_items WHERE product_id=?", (a,))
    # unit_price is the invoice figure, unit_cost the landed one; keeping both is what
    # lets the report explain where the difference came from.
    assert line["unit_price"] == 12000
    assert line["unit_cost"] == pytest.approx(11812.5)
    assert line["allocated_cost"] == pytest.approx(118125)


def test_a_discount_larger_than_the_goods_is_refused(db_path, product):
    pid = product()
    with pytest.raises(ServiceError):
        call(services.create_restock, [{"product_id": pid, "qty": 1, "unit_price": 1000}],
             discount=5000)
    # Nothing partial left behind: no batch, and no stock added.
    assert one("SELECT COUNT(*) AS n FROM restock_batches")["n"] == 0
    assert one("SELECT stock_qty FROM products WHERE id=?", (pid,))["stock_qty"] == 0


def test_restock_cost_still_means_money_paid(db_path, product):
    # sales_summary's restock_cost and net_profit read total_cost, so the charges have to
    # be inside it or the cash view of a month would silently drift.
    pid = product()
    call(services.create_restock, [{"product_id": pid, "qty": 2, "unit_price": 10000}],
         discount=3000, shipping_cost=5000, admin_fee=1000)
    assert one("SELECT SUM(total_cost) AS t FROM restock_batches")["t"] == 23000


# --- The cost an order remembers ---

def test_an_order_snapshots_the_cost_at_the_time_of_sale(db_path, product):
    pid = product(price=20000, cost=0, stock=10)
    call(services.create_restock, [{"product_id": pid, "qty": 10, "unit_price": 12000}])
    call(services.create_order, [{"product_id": pid, "quantity": 2}])
    assert one("SELECT unit_cost FROM order_items")["unit_cost"] == 12000

    # A restock at a new price moves the product's cost but must leave the sale alone.
    call(services.create_restock, [{"product_id": pid, "qty": 18, "unit_price": 20000}])
    assert one("SELECT cost_price FROM products WHERE id=?", (pid,))["cost_price"] > 12000
    assert one("SELECT unit_cost FROM order_items")["unit_cost"] == 12000
