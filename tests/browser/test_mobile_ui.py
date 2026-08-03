"""The phone layout, and the text-size setting that scales it.

None of this is reachable from a Flask test client: whether the menu is off-screen,
whether a seven-column table has stopped being a table, and whether a button is big
enough to hit are all questions about computed layout. They are asked at a real phone
viewport, because every one of them passes at desktop width by doing nothing.

The screen is a 390x844 iPhone-class viewport. The stylesheet's breakpoint is 768px,
so any width below that exercises the same rules.
"""
import re

import pytest
from playwright.sync_api import expect

PHONE = {'width': 390, 'height': 844}
# The touch target the layout promises: 2.75rem, which is 44px at the default scale.
MIN_TAP = 44


@pytest.fixture
def phone(page):
    page.set_viewport_size(PHONE)
    return page


def box(locator):
    b = locator.bounding_box()
    assert b is not None, 'element is not visible, so it has no box'
    return b


def drawer_settled(page, want_open):
    """The drawer slides in over 250ms, so a rectangle read the instant after the tap
    is a rectangle mid-transition -- which is what the first draft of these tests
    asserted on, and it failed at -303px of a -320px slide. Polling the real geometry
    waits exactly as long as the animation takes and no longer."""
    page.wait_for_function(
        """(open) => {
            const r = document.getElementById('mainNav').getBoundingClientRect();
            return open ? r.x >= 0 : r.x + r.width <= 0;
        }""", arg=want_open)


# --- the navigation drawer ---

def test_the_menu_is_off_screen_until_the_button_opens_it(phone, shop):
    phone.goto(f'{shop.base_url}/products')
    nav = phone.locator('#mainNav')

    # Present in the DOM (so its links are still crawlable and focusable in order),
    # but translated off the left edge rather than covering the page.
    assert box(nav)['x'] + box(nav)['width'] <= 0

    phone.locator('#navToggle').click()
    drawer_settled(phone, True)
    assert box(nav)['x'] >= 0
    expect(phone.locator('#navToggle')).to_have_attribute('aria-expanded', 'true')


def test_the_drawer_shows_words_not_only_icons(phone, shop):
    """The old mobile layout was a 60px rail of emoji. Naming each destination is the
    whole reason this screen was rebuilt."""
    phone.goto(f'{shop.base_url}/products')
    phone.locator('#navToggle').click()

    for label in ('Dashboard', 'Products', 'Orders', 'Restock', 'Self Use',
                  'Sales', 'Stock History', 'Settings'):
        expect(phone.locator('#mainNav').get_by_role('link', name=label)).to_be_visible()


def test_every_menu_row_is_a_full_size_target(phone, shop):
    phone.goto(f'{shop.base_url}/products')
    phone.locator('#navToggle').click()

    links = phone.locator('#mainNav .nav-links a')
    for i in range(links.count()):
        assert box(links.nth(i))['height'] >= MIN_TAP


def test_the_scrim_closes_the_drawer(phone, shop):
    """Tapping beside an open menu is what people try before they look for the ✕."""
    phone.goto(f'{shop.base_url}/products')
    phone.locator('#navToggle').click()
    drawer_settled(phone, True)

    # Tapped where a thumb would land -- the strip of page still showing beside the
    # open menu. Clicking the scrim's own centre would land on the drawer, which sits
    # above it and covers the middle of the screen.
    nav = box(phone.locator('#mainNav'))
    phone.mouse.click(nav['width'] + (PHONE['width'] - nav['width']) / 2, PHONE['height'] / 2)
    drawer_settled(phone, False)

    expect(phone.locator('#navToggle')).to_have_attribute('aria-expanded', 'false')


def test_following_a_link_leaves_the_menu_closed_on_the_next_page(phone, shop):
    phone.goto(f'{shop.base_url}/products')
    phone.locator('#navToggle').click()
    phone.locator('#mainNav').get_by_role('link', name='Orders').click()

    expect(phone).to_have_url(f'{shop.base_url}/orders')
    nav = phone.locator('#mainNav')
    assert box(nav)['x'] + box(nav)['width'] <= 0


def test_the_desktop_sidebar_is_untouched(page, shop):
    """The drawer rules must not leak above the breakpoint -- the shop is also run
    from a laptop, where the sidebar is always visible and there is no ☰."""
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(f'{shop.base_url}/products')

    expect(page.locator('#mainNav')).to_be_visible()
    assert box(page.locator('#mainNav'))['x'] == 0
    expect(page.locator('#navToggle')).to_be_hidden()


# --- tables become cards ---

def test_a_wide_table_stacks_and_labels_itself(phone, shop):
    """Six columns do not fit 390px. Each cell is labelled from the table's own
    <thead>, which is what makes a stacked row readable."""
    shop.product('Kopi', 'K-1', price=12000, stock=40)
    phone.goto(f'{shop.base_url}/products')

    row = phone.locator('#productsBody tr').first
    expect(row.locator('td').first).to_have_attribute('data-label', 'SKU')
    expect(row.locator('td').nth(1)).to_have_attribute('data-label', 'Name')
    expect(row.locator('td').nth(4)).to_have_attribute('data-label', 'Stock')
    # The header itself is gone; its text now sits on the cells.
    expect(phone.locator('#productsTable thead')).to_be_hidden()


def test_labels_are_applied_to_rows_drawn_after_load(phone, shop):
    """Every list on this app is rendered by app.js well after DOMContentLoaded, so
    labelling once at startup would catch nothing that matters."""
    kopi = shop.product('Kopi', 'K-1', stock=20)
    shop.order([(kopi, 3)])
    phone.goto(f'{shop.base_url}/orders')

    row = phone.locator('#ordersBody tr').first
    expect(row.locator('td').first).to_have_attribute('data-label', 'Order ID')
    expect(row.locator('td').nth(5)).to_have_attribute('data-label', 'Status')


def test_labels_follow_the_ui_language(phone, shop):
    """The labels are copied from the rendered header, so they are translated for
    free -- and must not be hardcoded English by some later change."""
    conn = shop.connect()
    import database
    database.set_setting(conn, 'language', 'id')
    conn.commit()
    conn.close()
    try:
        shop.product('Kopi', 'K-1', stock=40)
        phone.goto(f'{shop.base_url}/products')
        cell = phone.locator('#productsBody tr').first.locator('td').nth(1)
        expect(cell).to_have_attribute('data-label', 'Nama')
    finally:
        conn = shop.connect()
        database.set_setting(conn, 'language', 'en')
        conn.commit()
        conn.close()


def test_the_empty_row_notice_is_not_given_a_column_label(phone, shop):
    """It spans the table with a colspan and is a sentence, not a field."""
    phone.goto(f'{shop.base_url}/orders')
    cell = phone.locator('#ordersBody .empty-row')
    expect(cell).to_be_visible()
    expect(cell).not_to_have_attribute('data-label', value=None)


def test_nothing_scrolls_sideways(phone, shop):
    """The one symptom that makes a page feel broken on a phone.

    Seeded with figures long enough to be the problem. The first version of this test
    used a single cheap product, and the dashboard passed it while overflowing by 34px
    in real use: the stat cards were laid out on `1fr` tracks, which will not shrink
    below their content, so it took a seven-digit rupiah total to push a column wide
    enough to notice. Keep the numbers big here.
    """
    kopi = shop.product('Kopi Bubuk Gayo Premium', 'KOPI-GAYO-200',
                        price=4_800_000, cost=3_100_000, stock=940)
    shop.product('Teh Melati Wangi Super', 'TEH-MEL-100', price=1_500_000, stock=8, threshold=10)
    shop.order([(kopi, 3)], status='completed')
    shop.order([(kopi, 2)])
    for path in ('/', '/products', '/orders', '/restock', '/self-use',
                 '/stock-history', '/settings'):
        phone.goto(f'{shop.base_url}{path}')
        overflow = phone.evaluate(
            'document.documentElement.scrollWidth - document.documentElement.clientWidth')
        assert overflow <= 1, f'{path} scrolls sideways by {overflow}px'


def test_a_modal_fits_the_screen(phone, shop):
    """It was a flat 500px wide, which ran off the side of every phone made."""
    shop.product('Kopi', 'K-1', stock=40)
    phone.goto(f'{shop.base_url}/products')
    phone.get_by_role('button', name='+ Add Product').click()

    content = box(phone.locator('#productModal .modal-content'))
    assert content['x'] >= 0
    assert content['x'] + content['width'] <= PHONE['width'] + 1


# --- touch targets and the iOS zoom trap ---

def test_buttons_are_big_enough_to_hit(phone, shop):
    shop.product('Kopi', 'K-1', stock=40)
    phone.goto(f'{shop.base_url}/products')

    buttons = phone.locator('button:visible')
    for i in range(buttons.count()):
        b = buttons.nth(i)
        assert box(b)['height'] >= MIN_TAP, b.inner_text()


def test_no_input_is_small_enough_to_make_safari_zoom(phone, shop):
    """Safari zooms the whole page in when a field under 16px takes focus, and the
    shop owner then has to pinch back out to see the rest of the form. Every text
    field on every form-bearing screen, not a sample."""
    shop.product('Kopi', 'K-1', stock=40)
    for path in ('/login', '/products', '/orders', '/restock', '/self-use', '/settings'):
        phone.goto(f'{shop.base_url}{path}')
        if path == '/products':
            phone.get_by_role('button', name='+ Add Product').click()
        fields = phone.locator('input:visible, select:visible, textarea:visible')
        for i in range(fields.count()):
            size = phone.evaluate(
                '(el) => parseFloat(getComputedStyle(el).fontSize)', fields.nth(i).element_handle())
            assert size >= 16, f'{path} field {i} is {size}px'


def pseudo_text(page, locator, pseudo='::after'):
    """The rendered text of a CSS pseudo-element. It is not in the DOM, so inner_text
    cannot see it -- but it is what the reader sees, which is what is being asserted."""
    return page.evaluate(
        f"(el) => getComputedStyle(el, '{pseudo}').content", locator.element_handle())


def test_row_actions_name_themselves_on_a_phone(phone, shop):
    """On a desktop an icon button explains itself through its title, on hover. A
    phone has no hover, so an orders row offered five unlabelled emoji and no way to
    find out what any of them did. The label is drawn beside the icon instead."""
    kopi = shop.product('Kopi', 'K-1', stock=20)
    shop.order([(kopi, 3)])
    phone.goto(f'{shop.base_url}/orders')

    cell = phone.locator('#ordersBody tr').first.locator('.action-cell')
    shown = {pseudo_text(phone, cell.locator('button').nth(i))
             for i in range(cell.locator('button').count())}
    for label in ('View', 'Edit', 'Buyer & Payment', 'Confirm', 'Cancel'):
        assert any(label in s for s in shown), f'{label} is not shown, only {shown}'


def test_row_actions_stay_icon_only_on_a_desktop(page, shop):
    """Where the tooltip works, the compact row is the better one."""
    page.set_viewport_size({'width': 1280, 'height': 900})
    kopi = shop.product('Kopi', 'K-1', stock=20)
    shop.order([(kopi, 3)])
    page.goto(f'{shop.base_url}/orders')

    button = page.locator('#ordersBody tr').first.locator('.action-cell button').first
    assert pseudo_text(page, button) in ('none', 'normal')


def test_a_restock_line_is_labelled_when_the_header_row_is_dropped(phone, shop):
    """The header row above the lines is hidden on a phone, and the placeholders that
    were standing in for labels are never seen -- both number fields start with a
    value, so the placeholder is painted over before the page is drawn."""
    shop.product('Kopi', 'K-1', stock=40)
    phone.goto(f'{shop.base_url}/restock')

    row = phone.locator('.restock-item-row').first
    expect(phone.locator('.restock-header-row')).to_be_hidden()
    for label in ('Product', 'Qty', 'Price per unit'):
        expect(row.get_by_text(label, exact=True)).to_be_visible()
    # And the one control that was a bare × on the loudest button of the form.
    assert 'Remove item' in pseudo_text(phone, row.locator('.btn-remove-item'))


# --- the text size setting ---

def test_choosing_a_size_takes_effect_without_a_reload(phone, shop):
    phone.goto(f'{shop.base_url}/settings')
    before = phone.evaluate('parseFloat(getComputedStyle(document.body).fontSize)')

    phone.locator('.text-size-option[data-scale="150"]').click()
    expect(phone.locator('.toast')).to_be_visible()

    after = phone.evaluate('parseFloat(getComputedStyle(document.body).fontSize)')
    assert after > before * 1.4


def test_the_chosen_size_survives_a_reload_and_a_different_page(phone, shop):
    phone.goto(f'{shop.base_url}/settings')
    phone.locator('.text-size-option[data-scale="130"]').click()
    expect(phone.locator('.text-size-option[data-scale="130"]')).to_have_class(
        re.compile(r'\bactive\b'))

    phone.goto(f'{shop.base_url}/products')
    scaled = phone.evaluate('parseFloat(getComputedStyle(document.body).fontSize)')
    assert round(scaled) == round(16 * 1.30)


def test_the_scale_grows_the_buttons_too_not_only_the_words(phone, shop):
    """Sizes are in rem throughout, so a larger scale must move the whole interface.
    A stray px in a control is exactly the regression this catches."""
    shop.product('Kopi', 'K-1', stock=40)
    phone.goto(f'{shop.base_url}/settings')
    phone.locator('.text-size-option[data-scale="150"]').click()
    expect(phone.locator('.toast')).to_be_visible()

    phone.goto(f'{shop.base_url}/products')
    button = phone.get_by_role('button', name='+ Add Product')
    assert box(button)['height'] >= MIN_TAP * 1.4


def test_the_size_is_applied_before_the_page_paints(phone, shop):
    """Written into the <html> tag by the server rather than applied by script, so
    there is no flash of small text on every navigation."""
    phone.goto(f'{shop.base_url}/settings')
    phone.locator('.text-size-option[data-scale="115"]').click()
    expect(phone.locator('.toast')).to_be_visible()

    phone.goto(f'{shop.base_url}/orders')
    assert '115%' in phone.locator('html').get_attribute('style')
