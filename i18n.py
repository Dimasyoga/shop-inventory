"""Lightweight i18n for the web UI and the Telegram bot.

Translation keys are the English source strings themselves (gettext-style), so
English needs no table — a missing key falls back to the key. Only the target
languages carry a mapping. Placeholders use ``str.format`` syntax, e.g.
``t('Order #{n}', n=5)``; the same ``{name}`` tokens are understood by the
browser-side ``t()`` in app.js, which receives the active language's table.

The active language is a single shop-wide setting (``language`` in the settings
table), resolved with :func:`normalize_lang` so a stale or bogus value can never
break rendering.
"""

# code -> display name (shown verbatim in the picker; language names are not translated)
LANGUAGES = {
    'en': 'English',
    'id': 'Bahasa Indonesia',
}

DEFAULT_LANG = 'en'

# English source string -> Indonesian. English is intentionally absent (identity).
TRANSLATIONS = {
    'id': {
        # --- App / navigation ---
        'Shop Inventory': 'Inventaris Toko',
        'Dashboard': 'Dasbor',
        'Products': 'Produk',
        'Categories': 'Kategori',
        'Orders': 'Pesanan',
        'Restock': 'Restok',
        'Sales': 'Penjualan',
        'Settings': 'Pengaturan',
        'Logout': 'Keluar',

        # --- Login ---
        'Login': 'Masuk',
        'Sign in to manage your shop': 'Masuk untuk mengelola toko Anda',
        'Username': 'Nama pengguna',
        'Password': 'Kata sandi',
        'Sign In': 'Masuk',
        'Invalid credentials': 'Kredensial tidak valid',

        # --- Common ---
        'Name': 'Nama',
        'Name *': 'Nama *',
        'Category': 'Kategori',
        'Stock': 'Stok',
        'Status': 'Status',
        'Total': 'Total',
        'Date': 'Tanggal',
        'Actions': 'Aksi',
        'Product': 'Produk',
        'Price': 'Harga',
        'Revenue': 'Pendapatan',
        'Save': 'Simpan',
        'Cancel': 'Batal',
        'None': 'Tidak ada',
        'Edit': 'Edit',
        'Delete': 'Hapus',
        'Archive': 'Arsipkan',
        'View': 'Lihat',
        'Confirm': 'Konfirmasi',
        'Complete': 'Selesaikan',
        'Qty': 'Jml',
        'Subtotal': 'Subtotal',

        # --- Order status labels (lowercase values are the raw DB statuses) ---
        'draft': 'draf',
        'completed': 'selesai',
        'cancelled': 'dibatalkan',
        'Draft': 'Draf',
        'Completed': 'Selesai',
        'Cancelled': 'Dibatalkan',
        'Payment Confirmed': 'Pembayaran Dikonfirmasi',
        'Order #{n}': 'Pesanan #{n}',

        # --- Dashboard ---
        'Total Products': 'Total Produk',
        'Total Orders': 'Total Pesanan',
        'Low Stock': 'Stok Menipis',
        "This Month's Revenue": 'Pendapatan Bulan Ini',
        'Net Profit (This Month)': 'Laba Bersih (Bulan Ini)',
        'Total Product Sale Value': 'Total Nilai Jual Produk',
        'Restock Cost (This Month)': 'Biaya Restok (Bulan Ini)',
        'Recent Orders': 'Pesanan Terbaru',
        'No orders yet': 'Belum ada pesanan',
        'Low Stock Alerts': 'Peringatan Stok Menipis',
        'Threshold': 'Ambang Batas',
        'All stock levels OK': 'Semua tingkat stok aman',

        # --- Products ---
        '+ Add Product': '+ Tambah Produk',
        'Search products...': 'Cari produk...',
        'All Categories': 'Semua Kategori',
        'SKU': 'SKU',
        'Sale Price': 'Harga Jual',
        'Add Product': 'Tambah Produk',
        'Edit Product': 'Edit Produk',
        'Price (Rp) *': 'Harga (Rp) *',
        'Stock Qty': 'Jumlah Stok',
        "Stock is managed via orders and the Restock page and can't be edited here.":
            'Stok dikelola melalui pesanan dan halaman Restok, dan tidak dapat diubah di sini.',
        'Reorder Threshold': 'Ambang Pemesanan Ulang',
        'No products found': 'Tidak ada produk ditemukan',
        'Product saved': 'Produk tersimpan',
        'Archive this product?': 'Arsipkan produk ini?',
        'Product archived': 'Produk diarsipkan',

        # --- Categories ---
        '+ Add Category': '+ Tambah Kategori',
        'Created': 'Dibuat',
        'No categories yet': 'Belum ada kategori',
        'Add Category': 'Tambah Kategori',
        'Edit Category': 'Edit Kategori',
        'Category Name *': 'Nama Kategori *',
        'Delete this category?': 'Hapus kategori ini?',
        'Category deleted': 'Kategori dihapus',
        'Category saved': 'Kategori tersimpan',

        # --- Orders ---
        '+ New Order': '+ Pesanan Baru',
        'Search by Order ID...': 'Cari berdasarkan ID Pesanan...',
        'All Status': 'Semua Status',
        'Order ID': 'ID Pesanan',
        'Items': 'Item',
        'New Order': 'Pesanan Baru',
        '+ Add Item': '+ Tambah Item',
        'Create Order': 'Buat Pesanan',
        'Select product': 'Pilih produk',
        '{n} items': '{n} item',
        'Add at least one item': 'Tambahkan minimal satu item',
        'Order ID {id} created': 'Pesanan ID {id} dibuat',
        'Confirm payment for this order?': 'Konfirmasi pembayaran untuk pesanan ini?',
        'Payment confirmed': 'Pembayaran dikonfirmasi',
        'Complete this order? Stock will be deducted.':
            'Selesaikan pesanan ini? Stok akan dikurangi.',
        'Order completed': 'Pesanan selesai',
        'Cancel this order?': 'Batalkan pesanan ini?',
        'Order cancelled': 'Pesanan dibatalkan',
        'Order ID {id}': 'Pesanan ID {id}',

        # --- Restock ---
        'New Restock': 'Restok Baru',
        'Quantity': 'Jumlah',
        '+ Add Product': '+ Tambah Produk',
        'Total Restock Cost': 'Total Biaya Restok',
        'Total cost for this batch': 'Total biaya untuk batch ini',
        'Submit Restock': 'Kirim Restok',
        'Restock History': 'Riwayat Restok',
        'All Time': 'Sepanjang Waktu',
        'Today': 'Hari Ini',
        'This Week': 'Minggu Ini',
        'This Month': 'Bulan Ini',
        'Batch': 'Batch',
        'Total Cost': 'Total Biaya',
        'No restock history yet': 'Belum ada riwayat restok',
        'Add at least one product': 'Tambahkan minimal satu produk',
        'Restock saved! Total cost: {cost}': 'Restok tersimpan! Total biaya: {cost}',
        'Batch #{id}': 'Batch #{id}',
        '{n} products': '{n} produk',

        # --- Sales dashboard ---
        'Sales Dashboard': 'Dasbor Penjualan',
        'Day': 'Hari',
        'Week': 'Minggu',
        'Month': 'Bulan',
        'Year': 'Tahun',
        'Reset': 'Atur Ulang',
        'Total Revenue': 'Total Pendapatan',
        'Completed Orders': 'Pesanan Selesai',
        'Unique SKUs Sold': 'SKU Unik Terjual',
        'Total Items Sold': 'Total Item Terjual',
        'Restock Cost': 'Biaya Restok',
        'Net Profit': 'Laba Bersih',
        'Sales Trend': 'Tren Penjualan',
        'Top 3 Sellers': '3 Terlaris',
        'Bottom 3 Sellers': '3 Terbawah',
        'Qty Sold': 'Jml Terjual',
        'No data yet': 'Belum ada data',

        # --- Settings ---
        'Telegram Bot': 'Bot Telegram',
        'Enable bot': 'Aktifkan bot',
        'Bot token': 'Token bot',
        'Saved — leave blank to keep': 'Tersimpan — kosongkan untuk mempertahankan',
        'Whitelisted Telegram user IDs (comma-separated)':
            'ID pengguna Telegram yang diizinkan (dipisahkan koma)',
        'Shop timezone (for bot sales summaries)':
            'Zona waktu toko (untuk ringkasan penjualan bot)',
        'Stale order alert threshold (hours)':
            'Ambang peringatan pesanan tertahan (jam)',
        'Alert whitelisted users when a draft or payment-confirmed order stays in that state longer than this. 0 disables.':
            'Beri tahu pengguna yang diizinkan saat pesanan draf atau pembayaran-dikonfirmasi bertahan di status itu lebih lama dari ini. 0 menonaktifkan.',
        'Test Connection': 'Uji Koneksi',
        'Account': 'Akun',
        'Current password': 'Kata sandi saat ini',
        'New password (leave blank to keep)': 'Kata sandi baru (kosongkan untuk mempertahankan)',
        'Confirm new password': 'Konfirmasi kata sandi baru',
        'Update Account': 'Perbarui Akun',
        'Language': 'Bahasa',
        'Interface language': 'Bahasa antarmuka',
        'This changes the language of the web interface and the Telegram bot.':
            'Ini mengubah bahasa antarmuka web dan bot Telegram.',
        'Language updated': 'Bahasa diperbarui',
        'Telegram settings saved': 'Pengaturan Telegram tersimpan',
        'Testing…': 'Menguji…',
        'Connected as @{name}': 'Terhubung sebagai @{name}',
        'New passwords do not match': 'Kata sandi baru tidak cocok',
        'Account updated': 'Akun diperbarui',

        # --- Telegram bot screens ---
        'What do you want to do?': 'Apa yang ingin Anda lakukan?',
        '📦 Products': '📦 Produk',
        '🛒 Orders': '🛒 Pesanan',
        '🆕 New order': '🆕 Pesanan baru',
        '📥 Restock': '📥 Restok',
        '📈 Sales summary': '📈 Ringkasan penjualan',
        '« Menu': '« Menu',
        '« Orders': '« Pesanan',
        '« Back': '« Kembali',
        '« No': '« Tidak',
        '◀ Prev': '◀ Sebelumnya',
        'Next ▶': 'Berikutnya ▶',
        'No products yet.': 'Belum ada produk.',
        'stock {n}': 'stok {n}',
        'Pick a status:': 'Pilih status:',
        '📝 Draft': '📝 Draf',
        '💳 Confirmed': '💳 Dikonfirmasi',
        '✅ Completed': '✅ Selesai',
        '❌ Cancelled': '❌ Dibatalkan',
        'All': 'Semua',
        'All orders': 'Semua pesanan',
        '{label}: nothing here.': '{label}: kosong.',
        '💳 Payment Confirmed': '💳 Pembayaran Dikonfirmasi',
        '✅ Confirm payment': '✅ Konfirmasi pembayaran',
        '❌ Cancel': '❌ Batal',
        '💰 Complete': '💰 Selesaikan',
        '✅ Yes': '✅ Ya',
        'Complete order #{id}? Stock will be deducted.':
            'Selesaikan pesanan #{id}? Stok akan dikurangi.',
        'Cancel order #{id}?': 'Batalkan pesanan #{id}?',
        'Order completed': 'Pesanan selesai',
        '📈 Sales — {label}': '📈 Penjualan — {label}',
        'Revenue: {amount}': 'Pendapatan: {amount}',
        'Orders: {orders}   Items sold: {items}': 'Pesanan: {orders}   Item terjual: {items}',
        'Restock cost: {amount}': 'Biaya restok: {amount}',
        'Net profit: {amount}': 'Laba bersih: {amount}',
        'Week of {date}': 'Minggu tanggal {date}',
        '🆕 New order — review': '🆕 Pesanan baru — tinjau',
        '📥 Restock — review': '📥 Restok — tinjau',
        'Selected:': 'Dipilih:',
        'Pick a product:': 'Pilih produk:',
        '✔ Done': '✔ Selesai',
        '✖ Abandon': '✖ Batalkan',
        '✏️ Custom': '✏️ Kustom',
        '+ Add more': '+ Tambah lagi',
        'How many <b>{name}</b>?': 'Berapa banyak <b>{name}</b>?',
        ' (stock: {n})': ' (stok: {n})',
        'Tap a number, or ✏️ Custom to type any amount.':
            'Ketuk angka, atau ✏️ Kustom untuk mengetik jumlah apa pun.',
        'Total cost: <b>{cost}</b>': 'Total biaya: <b>{cost}</b>',
        '✅ Create draft order': '✅ Buat pesanan draf',
        '✅ Save restock': '✅ Simpan restok',
        'View order': 'Lihat pesanan',
        'Total: {amount}': 'Total: {amount}',
        # stale-order alerts (pushed by the bot poller)
        'Order needs attention': 'Pesanan perlu perhatian',
        'Order #{n} — {status}': 'Pesanan #{n} — {status}',
        'Stuck in this state for over {hours}h.':
            'Tertahan di status ini lebih dari {hours} jam.',
        # bot prompts / acks
        'Not authorized. Your Telegram ID: <code>{id}</code>':
            'Tidak diizinkan. ID Telegram Anda: <code>{id}</code>',
        'Not authorized': 'Tidak diizinkan',
        "Couldn't read that number. Send the quantity as a whole number, e.g. <code>12</code>":
            'Tidak dapat membaca angka itu. Kirim jumlah sebagai bilangan bulat, mis. <code>12</code>',
        "Couldn't read that amount. Send the total cost as a number, e.g. <code>150000</code>":
            'Tidak dapat membaca jumlah itu. Kirim total biaya sebagai angka, mis. <code>150000</code>',
        'Send the <b>quantity</b> as a number, e.g. <code>12</code>':
            'Kirim <b>jumlah</b> sebagai angka, mis. <code>12</code>',
        'Send the <b>total cost</b> of this restock as a message, e.g. <code>150000</code>':
            'Kirim <b>total biaya</b> restok ini sebagai pesan, mis. <code>150000</code>',
        'Abandoned': 'Dibatalkan',
        'Session expired — start again from the menu':
            'Sesi berakhir — mulai lagi dari menu',
        'Added': 'Ditambahkan',
        'Nothing selected yet': 'Belum ada yang dipilih',
        'Send the total cost first': 'Kirim total biaya terlebih dahulu',
        'Order created': 'Pesanan dibuat',
        'Restock saved': 'Restok tersimpan',
        '✅ Draft order <b>#{id}</b> created — total {total}':
            '✅ Pesanan draf <b>#{id}</b> dibuat — total {total}',
        '✅ Restock batch <b>#{id}</b> saved — {cost}':
            '✅ Batch restok <b>#{id}</b> tersimpan — {cost}',

        # --- Service / business-rule errors (services.py) ---
        'Order not found': 'Pesanan tidak ditemukan',
        'Product {id} not found': 'Produk {id} tidak ditemukan',
        'Insufficient stock for {name}': 'Stok tidak cukup untuk {name}',
        'Insufficient stock for product #{id}': 'Stok tidak cukup untuk produk #{id}',
        'Only draft orders can be confirmed': 'Hanya pesanan draf yang dapat dikonfirmasi',
        'Only confirmed orders can be completed':
            'Hanya pesanan yang dikonfirmasi yang dapat diselesaikan',
        'Cannot cancel completed orders': 'Tidak dapat membatalkan pesanan yang sudah selesai',
        'Order already cancelled': 'Pesanan sudah dibatalkan',
        'invalid unit': 'unit tidak valid',
    },
}


# Localized calendar names. Used to build date labels ourselves instead of
# strftime %a/%b/%B, which follow the C locale and can't be translated portably.
MONTHS = {
    'en': ['January', 'February', 'March', 'April', 'May', 'June', 'July',
           'August', 'September', 'October', 'November', 'December'],
    'id': ['Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli',
           'Agustus', 'September', 'Oktober', 'November', 'Desember'],
}
MONTHS_ABBR = {
    'en': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
    'id': ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'],
}
# Monday-first, matching datetime.weekday().
WEEKDAYS_ABBR = {
    'en': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
    'id': ['Sen', 'Sel', 'Rab', 'Kam', 'Jum', 'Sab', 'Min'],
}


def normalize_lang(lang):
    """Coerce an arbitrary value to a supported language code."""
    return lang if lang in LANGUAGES else DEFAULT_LANG


def make_t(lang):
    """Return a translator ``t(source, **params)`` for ``lang``.

    Missing keys fall back to the source string, so partially-translated
    languages degrade to English rather than showing blanks. The resolved
    language code is exposed as ``t.lang`` for callers that also need to format
    dates (see :func:`month_name` / :func:`weekday_abbr`).
    """
    lang = normalize_lang(lang)
    table = TRANSLATIONS.get(lang, {})

    def t(source, **params):
        out = table.get(source, source)
        return out.format(**params) if params else out

    t.lang = lang
    return t


def month_name(month, lang, abbr=False):
    """Localized name for a 1..12 month number."""
    return (MONTHS_ABBR if abbr else MONTHS)[normalize_lang(lang)][month - 1]


def weekday_abbr(weekday, lang):
    """Localized abbreviated weekday for a 0..6 index (Monday = 0)."""
    return WEEKDAYS_ABBR[normalize_lang(lang)][weekday]


def translate_error(err, t):
    """Translate a ServiceError raised by services.py.

    The exception carries an English ``template`` plus ``params`` (see
    ``services.ServiceError``); older/plain exceptions without them fall back to
    ``str(err)``.
    """
    template = getattr(err, 'template', None)
    if template is None:
        return str(err)
    return t(template, **getattr(err, 'params', {}))


def js_table(lang):
    """The active language's mapping, for embedding into the page for app.js.

    English resolves to an empty table (identity), keeping the payload tiny.
    """
    return TRANSLATIONS.get(normalize_lang(lang), {})
