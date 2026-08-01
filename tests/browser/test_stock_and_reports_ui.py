"""The products stock column, the stock history page, the reservation repair, and the
CSV button.

Four screens whose logic lives entirely in the browser: a chip that appears only when
a condition holds, two filtered lists, and a download whose filename and quoting are
only observable once something actually saves the file.
"""
import csv
import io

from playwright.sync_api import expect

# A name with a quote and a comma, because HTML escaping and CSV quoting fail in
# different places and both have to survive the whole round trip.
AWKWARD = 'Kopi Bubuk "Gayo", 200g'


# --- the products stock column ---

def test_available_is_shown_only_when_an_open_order_holds_units(page, shop):
    held = shop.product('Kopi', 'K-1', stock=20)
    shop.product('Teh', 'T-1', stock=30)  # nothing held against it
    shop.order([(held, 4)])

    page.goto(f'{shop.base_url}/products')

    held_row = page.locator('#productsBody tr:has-text("Kopi")')
    free_row = page.locator('#productsBody tr:has-text("Teh")')
    expect(held_row.locator('.stock-held')).to_have_text('Available: 16')
    expect(free_row.locator('.stock-held')).to_have_count(0)


def test_the_held_tooltip_names_the_quantity(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=20)
    shop.order([(kopi, 7)])
    page.goto(f'{shop.base_url}/products')

    cell = page.locator('#productsBody tr:has-text("Kopi") td[title]')
    expect(cell).to_have_attribute('title', 'Held: 7')


def test_completing_an_order_moves_the_number_out_of_stock_not_just_availability(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=20)
    shop.order([(kopi, 5)], status='completed')
    page.goto(f'{shop.base_url}/products')

    row = page.locator('#productsBody tr:has-text("Kopi")')
    expect(row).to_contain_text('15')
    expect(row.locator('.stock-held')).to_have_count(0)


def test_a_product_name_with_a_quote_is_escaped_not_executed(page, shop):
    shop.product(AWKWARD, 'K-1')
    page.goto(f'{shop.base_url}/products')
    expect(page.locator('#productsBody tr').first).to_contain_text(AWKWARD)


# --- stock history ---

def test_the_history_lists_movements_newest_first(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=50)
    first = shop.order([(kopi, 2)], status='completed')
    second = shop.order([(kopi, 3)], status='completed')

    page.goto(f'{shop.base_url}/stock-history')

    rows = page.locator('#movementsBody tr')
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text(f'sale order #{second}')
    expect(rows.nth(0)).to_contain_text('-3')
    expect(rows.nth(1)).to_contain_text(f'sale order #{first}')
    expect(rows.nth(0)).to_contain_text('web:admin')


def test_an_open_order_puts_nothing_in_the_history(page, shop):
    """The page promises this in its own help text."""
    kopi = shop.product('Kopi', 'K-1', stock=50)
    shop.order([(kopi, 4)], status='confirmed')

    page.goto(f'{shop.base_url}/stock-history')
    expect(page.locator('.empty-row')).to_have_text('No stock movements recorded')


def test_the_history_filters_by_product(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=50)
    teh = shop.product('Teh', 'T-1', stock=50)
    shop.order([(kopi, 1)], status='completed')
    shop.order([(teh, 2)], status='completed')

    page.goto(f'{shop.base_url}/stock-history')
    expect(page.locator('#movementsBody tr')).to_have_count(2)

    page.locator('#filterMovementProduct').select_option(str(teh))

    expect(page.locator('#movementsBody tr')).to_have_count(1)
    expect(page.locator('#movementsBody tr').first).to_contain_text('Teh')


def test_the_history_pages_and_the_filter_resets_to_page_one(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=200)
    for _ in range(30):
        shop.order([(kopi, 1)], status='completed')

    page.goto(f'{shop.base_url}/stock-history')
    expect(page.locator('#movementsBody tr')).to_have_count(25)
    expect(page.get_by_role('button', name='◀ Prev')).to_be_disabled()

    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')
    expect(page.locator('#movementsBody tr')).to_have_count(5)

    page.locator('#filterMovementProduct').select_option(str(kopi))
    expect(page.locator('#movementsBody tr')).to_have_count(25)
    expect(page.locator('.pager-label')).to_have_text('Page 1')


def test_a_void_shows_both_halves(page, shop):
    """Corrections are reversing entries; the history must show the pair, not an edit."""
    import services
    kopi = shop.product('Kopi', 'K-1', stock=0)
    conn = shop.connect()
    batch = services.create_restock(conn, [{'product_id': kopi, 'qty': 10, 'unit_price': 5000}],
                                    actor='web:admin')
    services.void_restock(conn, batch['batch_id'], actor='web:admin')
    conn.close()

    page.goto(f'{shop.base_url}/stock-history')
    rows = page.locator('#movementsBody tr')
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text('-10')
    expect(rows.nth(1)).to_contain_text('+10')


# --- reservation repair, from Settings ---

def test_drift_is_reported_and_repaired_from_settings(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=20)
    teh = shop.product('Teh', 'T-1', stock=20)
    shop.order([(kopi, 3)])
    shop.order([(teh, 4)])
    shop.set_reserved(kopi, 9)   # phantom hold: units nothing can sell
    shop.set_reserved(teh, 0)    # lost hold: units promised twice

    page.goto(f'{shop.base_url}/settings')
    page.get_by_role('button', name='Check held stock').click()

    rows = page.locator('#reservationResult tbody tr')
    expect(rows).to_have_count(2)
    expect(page.locator('#reservationResult')).to_contain_text('2 product(s) hold stock')
    expect(rows.filter(has_text='Kopi')).to_contain_text('+6')
    expect(rows.filter(has_text='Teh')).to_contain_text('-4')

    # Checking must not change anything -- the two steps are separate on purpose.
    assert shop.stock_of(kopi) == (20, 9)

    page.get_by_role('button', name='Correct held stock').click()

    expect(page.locator('#reservationResult')).to_contain_text('Held stock matches the open orders')
    assert shop.stock_of(kopi) == (20, 3)
    assert shop.stock_of(teh) == (20, 4)


def test_a_healthy_shop_reports_nothing_to_fix(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=20)
    shop.order([(kopi, 3)])

    page.goto(f'{shop.base_url}/settings')
    page.get_by_role('button', name='Check held stock').click()

    expect(page.locator('#reservationResult')).to_contain_text('Nothing to fix')
    expect(page.get_by_role('button', name='Correct held stock')).to_have_count(0)


def test_repaired_stock_becomes_sellable_again(page, shop):
    """The point of the feature: units stuck behind a phantom hold come back."""
    kopi = shop.product('Kopi', 'K-1', stock=5)
    shop.set_reserved(kopi, 5)

    page.goto(f'{shop.base_url}/settings')
    page.get_by_role('button', name='Check held stock').click()
    page.get_by_role('button', name='Correct held stock').click()
    expect(page.locator('#reservationResult')).to_contain_text('Nothing to fix')

    page.goto(f'{shop.base_url}/orders')
    page.get_by_role('button', name='+ New Order').click()
    page.get_by_role('button', name='+ Add Item').click()
    page.locator('#orderItems select').first.select_option(str(kopi))
    page.locator('#orderItems input').first.fill('5')
    page.locator('#orderSubmitBtn').click()

    expect(page.locator('.toast')).to_contain_text('created')
    assert shop.stock_of(kopi) == (5, 5)


# --- the CSV download ---

def test_download_csv_saves_the_months_sales_with_the_right_name(page, shop):
    kopi = shop.product(AWKWARD, 'KOPI-200', price=45000, cost=30000, stock=50)
    shop.order([(kopi, 2)], status='completed')

    page.goto(f'{shop.base_url}/sales')
    # The selector defaults to last month; this month is where the sales are.
    page.locator('#reportMonth').select_option('0')
    with page.expect_download() as info:
        page.get_by_role('button', name='Download CSV').click()
    download = info.value

    assert download.suggested_filename.startswith('shop-report-')
    assert download.suggested_filename.endswith('.csv')

    text = open(download.path(), encoding='utf-8-sig').read()
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == 'Order'
    # The quote and the comma survive a real parser rather than splitting the row.
    assert rows[1][2] == AWKWARD
    assert rows[1][4] == '2'


def test_the_csv_carries_a_bom_for_excel(page, shop):
    """Without it Excel reads the file as the local codepage and mangles every name."""
    kopi = shop.product('Kopi', 'K-1', stock=10)
    shop.order([(kopi, 1)], status='completed')

    page.goto(f'{shop.base_url}/sales')
    page.locator('#reportMonth').select_option('0')
    with page.expect_download() as info:
        page.get_by_role('button', name='Download CSV').click()

    assert open(info.value.path(), 'rb').read(3) == b'\xef\xbb\xbf'


def test_download_csv_sits_beside_download_pdf(page, shop):
    page.goto(f'{shop.base_url}/sales')
    expect(page.get_by_role('button', name='Download PDF')).to_be_visible()
    expect(page.get_by_role('button', name='Download CSV')).to_be_visible()
