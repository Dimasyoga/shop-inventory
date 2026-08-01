"""Every write path tells the seller when the server refuses.

These are one regression test in eight parts. `api()` rejects on a non-2xx, and eight
call sites were written as `.then(d => d.success ? ... : showToast(d.error))` with no
`.catch`. The failure branch was unreachable, so a refused save produced an unhandled
promise rejection and a completely silent UI: the modal sat there, and the seller had
no way to learn that the shop had said no.

Nothing server-side could see this -- the API returned a correct 400 with correct
translated text every time. It took a browser.
"""
from playwright.sync_api import expect


def toast_text(page):
    expect(page.locator('.toast')).to_be_visible()
    return page.locator('.toast').inner_text()


def test_a_duplicate_sku_is_reported(page, shop):
    shop.product('Kopi', 'DUPE-1')
    shop.product('Teh', 'TEH-1')
    page.goto(f'{shop.base_url}/products')

    page.get_by_role('button', name='+ Add Product').click()
    page.locator('#productName').fill('Another')
    page.locator('#productSku').fill('DUPE-1')
    page.locator('#productPrice').fill('1000')
    page.get_by_role('button', name='Save').click()

    assert 'SKU' in toast_text(page)


def test_an_order_beyond_stock_is_reported(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=3)
    page.goto(f'{shop.base_url}/orders')

    page.get_by_role('button', name='+ New Order').click()
    # A new order opens with no lines; the seller adds the first one.
    page.get_by_role('button', name='+ Add Item').click()
    page.locator('#orderItems select').first.select_option(str(kopi))
    page.locator('#orderItems input').first.fill('99')
    page.locator('#orderSubmitBtn').click()

    assert 'Kopi' in toast_text(page)
    # ...and nothing was written on the way to the refusal.
    assert shop.stock_of(kopi) == (3, 0)


def test_self_use_beyond_stock_is_reported(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=2)
    page.goto(f'{shop.base_url}/self-use')

    page.locator('#selfUseItems select').first.select_option(str(kopi))
    page.locator('#selfUseItems input').first.fill('50')
    page.get_by_role('button', name='Submit Self Use').click()

    assert 'Kopi' in toast_text(page)
    assert shop.stock_of(kopi) == (2, 0)


def test_a_successful_write_still_reports_success(page, shop):
    """The other half of the fix: replacing the dead branch must not cost the happy path
    its toast."""
    kopi = shop.product('Kopi', 'K-1', stock=10)
    page.goto(f'{shop.base_url}/orders')

    page.get_by_role('button', name='+ New Order').click()
    # A new order opens with no lines; the seller adds the first one.
    page.get_by_role('button', name='+ Add Item').click()
    page.locator('#orderItems select').first.select_option(str(kopi))
    page.locator('#orderItems input').first.fill('2')
    page.locator('#orderSubmitBtn').click()

    assert 'created' in toast_text(page)
    assert shop.stock_of(kopi) == (10, 2)


def test_an_expired_session_lands_on_the_login_page(page, shop):
    """Sessions now time out, so this is a routine morning at the shop rather than an
    edge case. The old behaviour was a toast reading like a JSON parse error, because
    fetch() followed the redirect and tried to parse the login page as JSON."""
    kopi = shop.product('Kopi', 'K-1', stock=10)
    page.goto(f'{shop.base_url}/orders')

    page.context.clear_cookies()  # the session aged out while the page sat open

    page.get_by_role('button', name='+ New Order').click()
    page.wait_for_url('**/login')
    assert page.locator('input[name="username"]').is_visible()
    assert shop.stock_of(kopi) == (10, 0)


def test_an_expired_session_does_not_toast_a_parse_error(page, shop):
    shop.product('Kopi', 'K-1', stock=10)
    page.goto(f'{shop.base_url}/orders')
    page.context.clear_cookies()

    page.get_by_role('button', name='+ New Order').click()
    page.wait_for_url('**/login')
    # goToLogin() hands back a promise that never settles precisely so nothing
    # toasts against a page already navigating away.
    expect(page.locator('.toast')).to_have_count(0)


def test_no_unhandled_rejection_reaches_the_console(page, shop):
    """The symptom that gave the bug away: the refusal surfaced as an uncaught error in
    devtools instead of a toast in the page."""
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))

    kopi = shop.product('Kopi', 'K-1', stock=1)
    page.goto(f'{shop.base_url}/orders')
    page.get_by_role('button', name='+ New Order').click()
    # A new order opens with no lines; the seller adds the first one.
    page.get_by_role('button', name='+ Add Item').click()
    page.locator('#orderItems select').first.select_option(str(kopi))
    page.locator('#orderItems input').first.fill('50')
    page.locator('#orderSubmitBtn').click()

    expect(page.locator('.toast')).to_be_visible()
    assert errors == []
