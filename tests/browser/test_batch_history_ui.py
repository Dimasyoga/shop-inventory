"""The restock and self-use history tables: paging, the period filter, and the expander.

Both tables used to render every batch the shop had ever recorded into one innerHTML,
so nothing about them was observable server-side -- the endpoint returned a correct
(enormous) JSON array either way. What is worth clicking here is the interaction
between the pager and the period buttons, which share the same table.
"""
import re

from playwright.sync_api import expect

PAGE = 10  # app.HISTORY_PAGE_SIZE


def restocks(shop, count, product_id):
    import services
    conn = shop.connect()
    try:
        for n in range(count):
            services.create_restock(
                conn, [{'product_id': product_id, 'qty': 1, 'unit_price': 1000 + n}],
                actor='web:admin')
    finally:
        conn.close()


def self_uses(shop, count, product_id):
    import services
    conn = shop.connect()
    try:
        for _ in range(count):
            services.create_self_use(conn, [{'product_id': product_id, 'qty': 1}],
                                     actor='web:admin')
    finally:
        conn.close()


# --- restock history ---

def test_the_restock_history_pages_instead_of_listing_everything(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=0)
    restocks(shop, 25, kopi)

    page.goto(f'{shop.base_url}/restock')
    # Two rows per batch: the summary and its (hidden) breakdown.
    expect(page.locator('#restockHistoryBody tr.restock-batch-row')).to_have_count(PAGE)
    expect(page.get_by_role('button', name='◀ Prev')).to_be_disabled()

    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')
    expect(page.locator('#restockHistoryBody tr.restock-batch-row')).to_have_count(PAGE)

    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 3')
    expect(page.locator('#restockHistoryBody tr.restock-batch-row')).to_have_count(5)
    expect(page.get_by_role('button', name='Next ▶')).to_be_disabled()


def test_a_single_page_of_restocks_shows_no_pager(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=0)
    restocks(shop, 3, kopi)

    page.goto(f'{shop.base_url}/restock')
    expect(page.locator('#restockHistoryBody tr.restock-batch-row')).to_have_count(3)
    expect(page.locator('#restockHistoryPager')).to_be_empty()


def test_changing_the_period_returns_to_page_one(page, shop):
    """The new period holds different batches, so keeping the page number would land
    the seller somewhere arbitrary -- or on an empty page past the end."""
    kopi = shop.product('Kopi', 'K-1', stock=0)
    restocks(shop, 25, kopi)

    page.goto(f'{shop.base_url}/restock')
    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')

    page.locator('.period-selector[data-history="restock"]').get_by_role(
        'button', name='This Month').click()
    expect(page.locator('.pager-label')).to_have_text('Page 1')
    expect(page.locator('#restockHistoryBody tr.restock-batch-row')).to_have_count(PAGE)


def test_a_restock_row_still_expands_to_its_lines(page, shop):
    # The breakdown is what the page is for; paging must not cost it.
    kopi = shop.product('Kopi', 'K-1', stock=0)
    restocks(shop, 1, kopi)

    page.goto(f'{shop.base_url}/restock')
    detail = page.locator('#restockHistoryBody tr.restock-detail-row').first
    expect(detail).to_be_hidden()
    page.locator('#restockHistoryBody tr.restock-batch-row').first.click()
    expect(detail).to_be_visible()
    expect(detail).to_contain_text('Kopi')


def test_submitting_a_restock_shows_it_at_the_top_of_the_first_page(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=0)
    restocks(shop, 12, kopi)

    page.goto(f'{shop.base_url}/restock')
    page.locator('#restockItems select').first.select_option(str(kopi))
    page.locator('#restockItems input[type="number"]').first.fill('7')
    page.get_by_role('button', name='Submit Restock').click()

    first = page.locator('#restockHistoryBody tr.restock-batch-row').first
    expect(first).to_be_visible()
    first.click()
    expect(page.locator('#restockHistoryBody tr.restock-detail-row').first).to_contain_text('+7')


# --- self-use history ---

def test_the_self_use_history_pages_too(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=200)
    self_uses(shop, 25, kopi)

    page.goto(f'{shop.base_url}/self-use')
    expect(page.locator('#selfUseHistoryBody tr.self-use-batch-row')).to_have_count(PAGE)

    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')
    expect(page.locator('#selfUseHistoryBody tr.self-use-batch-row')).to_have_count(PAGE)


def test_a_void_and_its_original_are_both_visible_in_the_history(page, shop):
    """Corrections are reversing entries, so the pair has to survive paging: the void
    lands on page 1 and the batch it reverses must still carry its marker."""
    import services
    kopi = shop.product('Kopi', 'K-1', stock=200)
    conn = shop.connect()
    try:
        batch = services.create_self_use(conn, [{'product_id': kopi, 'qty': 3}],
                                         actor='web:admin')
        services.void_self_use(conn, batch['batch_id'], actor='web:admin')
    finally:
        conn.close()

    page.goto(f'{shop.base_url}/self-use')
    rows = page.locator('#selfUseHistoryBody tr.self-use-batch-row')
    expect(rows).to_have_count(2)
    # Newest first, so the void heads the page and names what it reverses.
    expect(rows.first).to_contain_text(f'Void of #{batch["batch_id"]}')
    expect(rows.first).to_have_class(re.compile('batch-void'))
