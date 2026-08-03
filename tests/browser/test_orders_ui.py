"""The orders page in a real browser: paging and draft editing.

Both shipped with green server-side suites and neither could be exercised by one --
the pager is entirely client state, and the edit modal's whole job is turning a draft
into a PUT. What is checked here is behaviour the API tests cannot see: which rows are
on screen, which buttons are disabled, and what the reservation columns did afterwards.
"""
from playwright.sync_api import expect


def seed_many_orders(shop, count=16):
    """Enough orders to page at 10, in a mix of statuses so the filter has work to do."""
    kopi = shop.product('Kopi Bubuk "Gayo", 200g', 'KOPI-200', price=45000, stock=200)
    teh = shop.product('Teh Melati', 'TEH-001', price=18000, stock=200)
    ids = []
    for i in range(count):
        status = ('draft', 'confirmed', 'completed')[i % 3]
        items = [(kopi, 1)] if i % 2 else [(kopi, 1), (teh, 2)]
        ids.append(shop.order(items, status=status))
    return kopi, teh, ids


def row_ids(page):
    """The Order ID column, top to bottom."""
    return page.locator('#ordersBody tr td:first-child').all_text_contents()


# --- paging ---

def test_first_page_shows_ten_newest_with_prev_disabled(page, shop):
    _, _, ids = seed_many_orders(shop)
    page.goto(f'{shop.base_url}/orders')

    expect(page.locator('#ordersBody tr')).to_have_count(10)
    assert row_ids(page) == [str(i) for i in sorted(ids, reverse=True)[:10]]
    expect(page.get_by_role('button', name='◀ Prev')).to_be_disabled()
    expect(page.get_by_role('button', name='Next ▶')).to_be_enabled()


def test_next_reaches_a_second_page_that_does_not_repeat_or_skip(page, shop):
    _, _, ids = seed_many_orders(shop)
    page.goto(f'{shop.base_url}/orders')
    first = row_ids(page)

    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')
    second = row_ids(page)

    assert not set(first) & set(second), 'a row appeared on both pages'
    assert sorted(int(i) for i in first + second) == sorted(ids)
    expect(page.get_by_role('button', name='◀ Prev')).to_be_enabled()
    expect(page.get_by_role('button', name='Next ▶')).to_be_disabled()


def test_prev_returns_to_the_first_page(page, shop):
    seed_many_orders(shop)
    page.goto(f'{shop.base_url}/orders')
    first = row_ids(page)

    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')
    page.get_by_role('button', name='◀ Prev').click()

    expect(page.locator('.pager-label')).to_have_text('Page 1')
    assert row_ids(page) == first


def test_the_pager_is_hidden_when_everything_fits(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    shop.order([(kopi, 1)])
    page.goto(f'{shop.base_url}/orders')

    expect(page.locator('#ordersBody tr')).to_have_count(1)
    expect(page.locator('#ordersPager')).to_be_empty()


def test_changing_the_status_filter_returns_to_page_one(page, shop):
    """The regression this guards: filtering from page 2 without resetting leaves the
    seller on a page that no longer exists, staring at an empty table."""
    seed_many_orders(shop)
    page.goto(f'{shop.base_url}/orders')
    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')

    page.locator('#filterStatus').select_option('draft')

    expect(page.locator('#ordersBody tr')).to_have_count(6)
    # The DOM carries the raw status; the capital D on screen is CSS.
    assert set(page.locator('#ordersBody .badge').all_text_contents()) == {'draft'}


def test_searching_returns_to_page_one(page, shop):
    seed_many_orders(shop)
    page.goto(f'{shop.base_url}/orders')
    page.get_by_role('button', name='Next ▶').click()
    expect(page.locator('.pager-label')).to_have_text('Page 2')

    page.locator('#searchOrder').fill('1')

    # Every id containing a "1", which is page-one content by definition.
    expect(page.locator('#ordersBody tr')).to_have_count(8)
    assert all('1' in i for i in row_ids(page))


def test_a_filter_matching_nothing_says_so(page, shop):
    seed_many_orders(shop)
    page.goto(f'{shop.base_url}/orders')
    page.locator('#searchOrder').fill('9999')
    expect(page.locator('.empty-row')).to_have_text('No orders found')


# --- draft editing ---

def open_editor(page, shop, order_id):
    page.goto(f'{shop.base_url}/orders')
    page.locator(f'#ordersBody tr:has(td:text-is("{order_id}")) button[title="Edit"]').click()
    expect(page.locator('#orderModal')).to_have_class('modal active')


def test_only_drafts_offer_an_edit_button(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    draft = shop.order([(kopi, 1)], status='draft')
    done = shop.order([(kopi, 1)], status='completed')
    page.goto(f'{shop.base_url}/orders')

    assert page.locator(f'#ordersBody tr:has(td:text-is("{draft}")) button[title="Edit"]').count() == 1
    assert page.locator(f'#ordersBody tr:has(td:text-is("{done}")) button[title="Edit"]').count() == 0


def test_the_editor_opens_titled_and_prefilled(page, shop):
    kopi = shop.product('Kopi', 'K-1', price=45000)
    teh = shop.product('Teh', 'T-1', price=18000)
    order_id = shop.order([(kopi, 2), (teh, 1)])

    open_editor(page, shop, order_id)

    expect(page.locator('#orderModalTitle')).to_have_text('Edit Order')
    expect(page.locator('#orderSubmitBtn')).to_have_text('Save Changes')
    rows = page.locator('#orderItems .form-row')
    expect(rows).to_have_count(2)
    assert rows.nth(0).locator('input').input_value() == '2'
    assert rows.nth(1).locator('input').input_value() == '1'
    expect(page.locator('#orderTotal')).to_have_text('Rp 108.000')


def test_the_editor_credits_back_the_units_this_order_holds(page, shop):
    """Without this the draft cannot keep its own quantity: the products page counts
    those units as unavailable, and they are unavailable to everyone except this order.
    """
    kopi = shop.product('Kopi', 'K-1', stock=10)
    order_id = shop.order([(kopi, 4)])
    assert shop.stock_of(kopi) == (10, 4)

    open_editor(page, shop, order_id)

    # 6 available to anyone else, plus the 4 this draft is holding.
    selected = page.locator('#orderItems .form-row select').first
    assert 'Available: 10' in selected.locator('option:checked').text_content()


def test_removing_a_line_updates_the_total_before_saving(page, shop):
    kopi = shop.product('Kopi', 'K-1', price=45000)
    teh = shop.product('Teh', 'T-1', price=18000)
    order_id = shop.order([(kopi, 1), (teh, 1)])

    open_editor(page, shop, order_id)
    expect(page.locator('#orderTotal')).to_have_text('Rp 63.000')

    page.locator('#orderItems .form-row').nth(1).get_by_role('button').click()

    expect(page.locator('#orderItems .form-row')).to_have_count(1)
    expect(page.locator('#orderTotal')).to_have_text('Rp 45.000')
    # Nothing is written until Save: the draft still has both lines.
    assert shop.order_lines(order_id) == [(kopi, 1), (teh, 1)]


def test_saving_an_edit_moves_the_holds_and_leaves_physical_stock_alone(page, shop):
    """The invariant the whole reservation design rests on. An edited draft re-holds
    what it now needs and gives back what it dropped; stock_qty has not moved, because
    nothing has physically left the shelf."""
    kopi = shop.product('Kopi', 'K-1', price=45000, stock=20)
    teh = shop.product('Teh', 'T-1', price=18000, stock=30)
    order_id = shop.order([(kopi, 1), (teh, 1)])
    assert shop.stock_of(kopi) == (20, 1)
    assert shop.stock_of(teh) == (30, 1)

    open_editor(page, shop, order_id)
    page.locator('#orderItems .form-row').nth(1).get_by_role('button').click()
    page.locator('#orderItems .form-row').first.locator('input').fill('3')
    page.locator('#orderSubmitBtn').click()

    expect(page.locator('.toast')).to_contain_text(f'Order ID {order_id} updated')
    assert shop.order_lines(order_id) == [(kopi, 3)]
    assert shop.order_total(order_id) == 135000
    assert shop.stock_of(kopi) == (20, 3), 'the raised line should hold 3'
    assert shop.stock_of(teh) == (30, 0), 'the dropped line should hold nothing'


def test_an_edit_may_spend_the_units_it_is_itself_giving_up(page, shop):
    """Releasing before re-taking is what makes this work. Correcting 4 down to 3 and
    moving the freed unit onto the same product would fail under a delta calculation
    that took the new lines before giving the old ones back."""
    kopi = shop.product('Kopi', 'K-1', stock=4)
    order_id = shop.order([(kopi, 4)])
    assert shop.stock_of(kopi) == (4, 4)

    open_editor(page, shop, order_id)
    page.locator('#orderItems .form-row').first.locator('input').fill('4')
    page.locator('#orderSubmitBtn').click()

    expect(page.locator('.toast')).to_contain_text(f'Order ID {order_id} updated')
    assert shop.stock_of(kopi) == (4, 4)


def test_an_edit_beyond_availability_is_refused_and_changes_nothing(page, shop):
    """The rollback path: update_order releases every old hold before taking the new
    ones, so a refusal has to put the released ones back or the draft quietly loses
    its claim on stock it still needs."""
    kopi = shop.product('Kopi', 'K-1', stock=10)
    mine = shop.order([(kopi, 2)])
    shop.order([(kopi, 5)], status='confirmed')
    assert shop.stock_of(kopi) == (10, 7)

    open_editor(page, shop, mine)
    page.locator('#orderItems .form-row').first.locator('input').fill('9')
    page.locator('#orderSubmitBtn').click()

    # The message names what is left and what the other order is sitting on.
    expect(page.locator('.toast')).to_contain_text('Only 5 of Kopi available, 5 held by other orders')
    assert shop.order_lines(mine) == [(kopi, 2)]
    assert shop.stock_of(kopi) == (10, 7), 'the released holds must be restored'


def test_cancelling_the_editor_writes_nothing(page, shop):
    kopi = shop.product('Kopi', 'K-1', stock=10)
    order_id = shop.order([(kopi, 2)])

    open_editor(page, shop, order_id)
    page.locator('#orderItems .form-row').first.locator('input').fill('7')
    page.locator('#orderModal').get_by_role('button', name='Cancel').click()

    expect(page.locator('#orderModal')).not_to_have_class('modal active')
    assert shop.order_lines(order_id) == [(kopi, 2)]
    assert shop.stock_of(kopi) == (10, 2)


# --- buyer & payment ---
#
# The whole feature is client-side plumbing the API tests cannot reach: a multipart
# form built by hand, a file input that means "leave it" when empty, and a modal that
# has to prefill from an order it fetches. apiForm() is also the first fetch in the
# app that is not api()/fetchJson(), so the 401-and-toast contract has to be shown to
# hold for it too.

JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64


def open_payment(page, shop, order_id):
    page.goto(f'{shop.base_url}/orders')
    page.locator(
        f'#ordersBody tr:has(td:text-is("{order_id}")) button[title="Buyer & Payment"]').click()
    expect(page.locator('#paymentModal')).to_have_class('modal active')


def attach_proof(page, data=JPEG, name='receipt.jpg', mime='image/jpeg'):
    page.locator('#payProofFile').set_input_files(
        {'name': name, 'mimeType': mime, 'buffer': data})


def test_a_cancelled_order_offers_no_payment_button(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    live = shop.order([(kopi, 1)], status='completed')
    dead = shop.order([(kopi, 1)], status='cancelled')
    page.goto(f'{shop.base_url}/orders')

    sel = 'button[title="Buyer & Payment"]'
    assert page.locator(f'#ordersBody tr:has(td:text-is("{live}")) {sel}').count() == 1
    assert page.locator(f'#ordersBody tr:has(td:text-is("{dead}")) {sel}').count() == 0


def test_saving_a_buyer_and_method_shows_them_on_the_row(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)])

    open_payment(page, shop, order_id)
    page.locator('#payBuyerName').fill('Bu Rina')
    page.locator('#payMethod').select_option('bank_transfer')
    page.locator('#paymentModal').get_by_role('button', name='Save').click()

    expect(page.locator('.toast')).to_contain_text('Payment details saved')
    expect(page.locator('#paymentModal')).not_to_have_class('modal active')
    assert shop.payment_of(order_id) == ('Bu Rina', 'bank_transfer', None)
    expect(page.locator(f'#ordersBody tr:has(td:text-is("{order_id}"))')).to_contain_text('Bu Rina')


def test_a_completed_order_still_accepts_a_receipt(page, shop):
    """The reason this is not part of the order editor: the lines of a completed
    order are frozen, but the transfer receipt often arrives after the sale."""
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)], status='completed')

    open_payment(page, shop, order_id)
    page.locator('#payBuyerName').fill('Pak Budi')
    attach_proof(page)
    page.locator('#paymentModal').get_by_role('button', name='Save').click()

    expect(page.locator('.toast')).to_contain_text('Payment details saved')
    assert shop.payment_of(order_id) == ('Pak Budi', None, JPEG)
    # The paperclip is how the list says a proof exists without shipping the bytes.
    expect(page.locator(f'#ordersBody tr:has(td:text-is("{order_id}"))')).to_contain_text('📎')


def test_the_editor_prefills_from_the_order(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)])

    open_payment(page, shop, order_id)
    page.locator('#payBuyerName').fill('Bu Rina')
    page.locator('#payMethod').select_option('cash')
    page.locator('#paymentModal').get_by_role('button', name='Save').click()
    expect(page.locator('.toast')).to_contain_text('Payment details saved')

    open_payment(page, shop, order_id)
    expect(page.locator('#payBuyerName')).to_have_value('Bu Rina')
    expect(page.locator('#payMethod')).to_have_value('cash')


def test_reopening_and_saving_without_touching_the_file_keeps_the_proof(page, shop):
    """A file input cannot be pre-filled with what is stored, so an untouched one has
    to mean "leave it" -- otherwise correcting a buyer's name drops their receipt."""
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)])

    open_payment(page, shop, order_id)
    attach_proof(page)
    page.locator('#paymentModal').get_by_role('button', name='Save').click()
    expect(page.locator('.toast')).to_contain_text('Payment details saved')

    open_payment(page, shop, order_id)
    page.locator('#payBuyerName').fill('Bu Rina')
    page.locator('#paymentModal').get_by_role('button', name='Save').click()
    expect(page.locator('.toast')).to_contain_text('Payment details saved')

    assert shop.payment_of(order_id) == ('Bu Rina', None, JPEG)


def test_removing_a_proof_needs_the_checkbox(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)])

    open_payment(page, shop, order_id)
    attach_proof(page)
    page.locator('#paymentModal').get_by_role('button', name='Save').click()
    expect(page.locator('.toast')).to_contain_text('Payment details saved')

    open_payment(page, shop, order_id)
    page.locator('#payRemoveProof').check()
    page.locator('#paymentModal').get_by_role('button', name='Save').click()
    expect(page.locator('.toast')).to_contain_text('Payment details saved')

    assert shop.payment_of(order_id)[2] is None
    expect(page.locator(f'#ordersBody tr:has(td:text-is("{order_id}"))')).not_to_contain_text('📎')


def test_a_refused_upload_is_reported_and_stores_nothing(page, shop):
    """apiForm() rejects on any non-2xx exactly as api() does, and its caller has the
    .catch that turns the refusal into a toast. Without it the modal would sit open
    with no explanation -- the failure eight write paths shipped with."""
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)])

    open_payment(page, shop, order_id)
    # A file the server will not accept, announcing itself as a JPEG. What it is
    # judged on is its bytes.
    attach_proof(page, data=b'<svg onload="alert(1)"/>', name='receipt.jpg')
    page.locator('#paymentModal').get_by_role('button', name='Save').click()

    expect(page.locator('.toast')).to_contain_text('JPEG, PNG, WebP or PDF')
    assert shop.payment_of(order_id) == (None, None, None)


def test_the_detail_view_links_to_the_proof(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)])

    open_payment(page, shop, order_id)
    page.locator('#payBuyerName').fill('Bu Rina')
    page.locator('#payMethod').select_option('cash')
    attach_proof(page)
    page.locator('#paymentModal').get_by_role('button', name='Save').click()
    expect(page.locator('.toast')).to_contain_text('Payment details saved')

    page.locator(f'#ordersBody tr:has(td:text-is("{order_id}")) button[title="View"]').click()
    detail = page.locator('#orderDetailContent')
    expect(detail).to_contain_text('Bu Rina')
    expect(detail).to_contain_text('Cash')
    expect(detail.get_by_role('link', name='View proof')).to_have_attribute(
        'href', f'/api/orders/{order_id}/proof')


def test_an_order_with_no_buyer_says_so_rather_than_showing_a_blank(page, shop):
    kopi = shop.product('Kopi', 'K-1')
    order_id = shop.order([(kopi, 1)])
    page.goto(f'{shop.base_url}/orders')

    expect(page.locator(f'#ordersBody tr:has(td:text-is("{order_id}"))')).to_contain_text(
        'Not recorded')


def test_searching_by_buyer_finds_the_order(page, shop):
    """The question the name was recorded to answer, asked by someone who does not
    have the order number."""
    kopi = shop.product('Kopi', 'K-1', stock=50)
    mine = shop.order([(kopi, 1)])
    shop.order([(kopi, 1)])

    open_payment(page, shop, mine)
    page.locator('#payBuyerName').fill('Bu Rina')
    page.locator('#paymentModal').get_by_role('button', name='Save').click()
    expect(page.locator('.toast')).to_contain_text('Payment details saved')

    page.locator('#searchOrder').fill('rina')
    expect(page.locator('#ordersBody tr')).to_have_count(1)
    assert row_ids(page) == [str(mine)]
