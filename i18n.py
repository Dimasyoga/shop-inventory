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
        'Orders': 'Pesanan',
        'Restock': 'Restok',
        'Self Use': 'Pemakaian Sendiri',
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
        'Edit': 'Edit',
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
        'Revenue ({month})': 'Pendapatan ({month})',
        'Net Profit ({month})': 'Laba Bersih ({month})',
        'Gross Profit ({month})': 'Laba Kotor ({month})',
        'Total Product Sale Value': 'Total Nilai Jual Produk',
        'Restock Cost ({month})': 'Biaya Restok ({month})',
        'Self Use ({month})': 'Pemakaian Sendiri ({month})',
        'Recent Orders': 'Pesanan Terbaru',
        'No orders yet': 'Belum ada pesanan',
        'Low Stock Alerts': 'Peringatan Stok Menipis',
        'Threshold': 'Ambang Batas',
        'All stock levels OK': 'Semua tingkat stok aman',

        # --- Products ---
        '+ Add Product': '+ Tambah Produk',
        'Search products...': 'Cari produk...',
        'SKU': 'SKU',
        'Sale Price': 'Harga Jual',
        'Add Product': 'Tambah Produk',
        'Edit Product': 'Edit Produk',
        'Price (Rp) *': 'Harga (Rp) *',
        'Cost Price': 'Harga Pokok',
        'Cost Price (Rp)': 'Harga Pokok (Rp)',
        'Cost Price (Rp) *': 'Harga Pokok (Rp) *',
        'Kept up to date by restocking; set it here for stock you already had.':
            'Diperbarui otomatis saat restok; isi di sini untuk stok yang sudah Anda miliki.',
        'What you paid per unit for this opening stock.':
            'Harga yang Anda bayar per unit untuk stok awal ini.',
        'Stock Qty': 'Jumlah Stok',
        "Stock is managed via orders and the Restock page and can't be edited here.":
            'Stok dikelola melalui pesanan dan halaman Restok, dan tidak dapat diubah di sini.',
        'Reorder Threshold': 'Ambang Pemesanan Ulang',
        'No products found': 'Tidak ada produk ditemukan',
        'Needs cost': 'Perlu harga modal',
        'Products with no cost recorded, or a cost a voided restock left in doubt':
            'Produk tanpa harga modal tercatat, atau harga modal yang diragukan karena restok dibatalkan',
        'Every product has a cost recorded': 'Semua produk sudah memiliki harga modal',
        'No cost recorded — sales of this product are left out of profit':
            'Harga modal belum tercatat — penjualan produk ini tidak dihitung dalam laba',
        'A voided restock left this cost in doubt — check it against the invoice':
            'Restok yang dibatalkan membuat harga modal ini diragukan — cocokkan dengan faktur',
        'Product saved': 'Produk tersimpan',
        'Archive this product?': 'Arsipkan produk ini?',
        'Product archived': 'Produk diarsipkan',

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
        'No orders found': 'Tidak ada pesanan ditemukan',
        'Stock: {n}': 'Stok: {n}',

        # --- Restock ---
        'New Restock': 'Restok Baru',
        'Quantity': 'Jumlah',
        '+ Add Product': '+ Tambah Produk',
        'Enter the supplier invoice: the price per unit of each product, then any discount, shipping and bank fee that apply to the whole invoice.':
            'Masukkan invoice pemasok: harga per unit setiap produk, lalu diskon, ongkos kirim, dan biaya bank yang berlaku untuk seluruh invoice.',
        'Price per unit': 'Harga per unit',
        'Discount': 'Diskon',
        'Shipping': 'Ongkos Kirim',
        'Admin Fee': 'Biaya Admin',
        'Invoice Total': 'Total Invoice',
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

        # --- Voiding a batch ---
        'Void': 'Batalkan',
        'voided': 'dibatalkan',
        'Void of #{id}': 'Pembatalan #{id}',
        'Batch #{id} voided': 'Batch #{id} dibatalkan',
        'Void batch #{id}? The stock it added comes back out and the invoice is reversed.':
            'Batalkan batch #{id}? Stok yang ditambahkan akan dikeluarkan kembali dan faktur dibalik.',
        'Void batch #{id}? The stock it took out goes back in.':
            'Batalkan batch #{id}? Stok yang dikeluarkan akan dikembalikan.',
        'Cost left in doubt for: {names} — a later restock had already averaged onto it':
            'Harga modal diragukan untuk: {names} — restok berikutnya sudah terlanjur dirata-ratakan di atasnya',
        '{n} completed sale(s) already recorded the old cost and are unchanged':
            '{n} penjualan selesai sudah mencatat harga modal lama dan tidak diubah',
        'Batch #{id} not found': 'Batch #{id} tidak ditemukan',
        'Batch #{id} is itself a void and cannot be voided':
            'Batch #{id} adalah pembatalan dan tidak dapat dibatalkan lagi',
        'Batch #{id} was already voided by batch #{void_id}':
            'Batch #{id} sudah dibatalkan oleh batch #{void_id}',
        'Cannot void: {name} no longer has the {qty} restocked by this batch in stock':
            'Tidak dapat dibatalkan: stok {name} tidak lagi memiliki {qty} yang ditambahkan batch ini',

        # --- Self use ---
        'New Self Use': 'Pemakaian Sendiri Baru',
        'Submit Self Use': 'Kirim Pemakaian Sendiri',
        'Self Use History': 'Riwayat Pemakaian Sendiri',
        'No self use history yet': 'Belum ada riwayat pemakaian sendiri',
        'Total Value': 'Total Nilai',
        'Self use saved! Total value: {value}':
            'Pemakaian sendiri tersimpan! Total nilai: {value}',

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
        'Gross Profit': 'Laba Kotor',
        'Sales Trend': 'Tren Penjualan',
        'Top 3 by Quantity': '3 Terlaris (Jumlah)',
        'Top 3 by Profit': '3 Teratas (Laba)',
        'Qty Sold': 'Jml Terjual',
        'Profit': 'Laba',
        'Margin': 'Margin',
        'Share': 'Kontribusi',
        '{n} sale(s) excluded from Profit and Gross Profit — cost not recorded yet':
            '{n} penjualan tidak dihitung dalam Laba dan Laba Kotor — biaya belum dicatat',
        '{n} sale(s) excluded — cost not recorded yet':
            '{n} penjualan tidak dihitung — biaya belum dicatat',
        'Products With No Sales': 'Produk Tanpa Penjualan',
        'Stock Value': 'Nilai Stok',
        'All products sold at least once': 'Semua produk terjual setidaknya sekali',
        'Stock value at risk: {amount}': 'Nilai stok menganggur: {amount}',
        'Showing {shown} of {total}': 'Menampilkan {shown} dari {total}',
        'No data yet': 'Belum ada data',
        'Week {n}': 'Minggu {n}',   # sales-trend x-axis label for the month view

        # --- Monthly report (reports.py, plus its web and bot triggers) ---
        'Monthly Report': 'Laporan Bulanan',
        'Generated {timestamp}': 'Dibuat {timestamp}',
        'Sales Performance': 'Kinerja Penjualan',
        'Metric': 'Metrik',
        'Value': 'Nilai',
        'Stock Value (today)': 'Nilai Stok (hari ini)',
        'Cost of Goods Sold': 'Harga Pokok Penjualan',
        'Net profit is revenue minus restock cost. Self use is reported separately and never subtracted: those goods were already paid for as restock spend.':
            'Laba bersih adalah pendapatan dikurangi biaya restok. Pemakaian sendiri dilaporkan terpisah dan tidak pernah dikurangkan: barangnya sudah dibayar sebagai biaya restok.',
        'Gross profit is revenue minus what the goods sold this month cost, so it ignores stock bought but not yet sold. A month of heavy restocking shows a thin net profit and a healthy gross one. Sales whose cost was never recorded are left out of both it and cost of goods sold.':
            'Laba kotor adalah pendapatan dikurangi biaya barang yang terjual bulan ini, jadi stok yang dibeli tapi belum terjual tidak dihitung. Bulan dengan banyak restok menunjukkan laba bersih tipis dan laba kotor sehat. Penjualan yang biayanya belum pernah dicatat tidak dihitung, baik di laba kotor maupun di harga pokok penjualan.',
        '{n} sale(s) are excluded from Gross Profit and from this ranking because no cost was recorded for the product when the order was created.':
            '{n} penjualan tidak dihitung dalam Laba Kotor maupun peringkat ini karena tidak ada biaya yang tercatat untuk produknya saat pesanan dibuat.',
        'Sales Records': 'Catatan Penjualan',
        'One row per product sold. Only completed orders are included: drafts, confirmed-but-unpaid and cancelled orders never move stock or revenue.':
            'Satu baris per produk terjual. Hanya pesanan selesai yang disertakan: pesanan draf, terkonfirmasi tapi belum dibayar, dan dibatalkan tidak pernah memengaruhi stok atau pendapatan.',
        'Restock Records': 'Catatan Restok',
        'One row per product restocked. Unit price is what the supplier invoice listed; unit cost adds that line’s share of the invoice discount, shipping and bank fee, split in proportion to line value. Landed cost is unit cost times quantity, and the lines of a batch sum to what was paid. A batch numbered 45/42 is a void: it reverses batch 42, and its negative figures cancel that entry out.':
            'Satu baris per produk direstok. Harga satuan adalah harga yang tertera di invoice pemasok; biaya satuan menambahkan bagian baris itu atas diskon, ongkos kirim, dan biaya bank invoice, dibagi sebanding dengan nilai baris. Biaya akhir adalah biaya satuan dikali kuantitas, dan baris-baris satu batch berjumlah sama dengan yang dibayarkan. Batch bernomor 45/42 adalah pembatalan: batch itu membalik batch 42, dan angka negatifnya menghapus catatan tersebut.',
        'Self Use Records': 'Catatan Pemakaian Sendiri',
        'One row per product taken by the seller, valued at the retail price at the time of entry. No revenue, and not deducted from net profit. A batch numbered 45/42 is a void: it reverses batch 42, putting that stock back.':
            'Satu baris per produk yang diambil penjual, dinilai pada harga jual saat dicatat. Tidak ada pendapatan, dan tidak dikurangkan dari laba bersih. Batch bernomor 45/42 adalah pembatalan: batch itu membalik batch 42 dan mengembalikan stoknya.',
        'Order': 'Pesanan',
        'Unit Price': 'Harga Satuan',
        'Qty Added': 'Jml Ditambah',
        'Unit Cost': 'Biaya Satuan',
        'Landed Cost': 'Biaya Akhir',
        'No records for this month': 'Tidak ada catatan untuk bulan ini',
        'Active products with no completed sale this month, most valuable idle stock first. Stock value is the current price times the quantity on hand.':
            'Produk aktif tanpa penjualan selesai bulan ini, stok menganggur termahal lebih dulu. Nilai stok adalah harga saat ini dikali jumlah yang tersedia.',
        'Products: {n} — stock value {amount}': 'Produk: {n} — nilai stok {amount}',
        'Orders: {n} — total {amount}': 'Pesanan: {n} — total {amount}',
        'Batches: {n} — total {amount}': 'Batch: {n} — total {amount}',
        'A PDF with the month summary plus every sale, restock and self-use record, for audit. Saved on the server and sendable to your whitelisted Telegram IDs.':
            'PDF berisi ringkasan bulan beserta seluruh catatan penjualan, restok, dan pemakaian sendiri, untuk audit. Tersimpan di server dan dapat dikirim ke ID Telegram yang diizinkan.',
        'Download PDF': 'Unduh PDF',
        'Send to Telegram': 'Kirim ke Telegram',
        'Sending…': 'Mengirim…',
        'Report for {month} sent to {n} recipient(s)':
            'Laporan {month} terkirim ke {n} penerima',
        'Sent to {sent} of {total} recipients.': 'Terkirim ke {sent} dari {total} penerima.',
        'Send the monthly report automatically': 'Kirim laporan bulanan otomatis',
        'When a month closes, the audit report PDF for it is saved on the server and sent to every whitelisted ID. You can also send any month by hand from the Sales page.':
            'Saat sebuah bulan berakhir, PDF laporan auditnya disimpan di server dan dikirim ke semua ID yang diizinkan. Bulan apa pun juga bisa dikirim manual dari halaman Penjualan.',

        # --- Settings ---
        'Telegram Bot': 'Bot Telegram',
        'Create a bot with {botfather}, paste its token here, and whitelist your Telegram user ID.':
            'Buat bot dengan {botfather}, tempelkan tokennya di sini, dan izinkan ID pengguna Telegram Anda.',
        'To find your ID: enable the bot, message it, and it replies with your ID.':
            'Untuk mengetahui ID Anda: aktifkan bot, kirimi bot pesan, dan bot akan membalas dengan ID Anda.',
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
        '🏠 Self use': '🏠 Pemakaian sendiri',
        '📈 Sales summary': '📈 Ringkasan penjualan',
        '📄 Monthly report': '📄 Laporan bulanan',
        'Pick a month:': 'Pilih bulan:',
        'Building the report…': 'Menyusun laporan…',
        'Could not build the report.': 'Laporan tidak dapat dibuat.',
        'Could not send the report, but it was saved on the server.':
            'Laporan tidak dapat dikirim, tetapi sudah tersimpan di server.',
        '📄 Report for {month} sent.': '📄 Laporan {month} terkirim.',
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
        'Self use: {amount}': 'Pemakaian sendiri: {amount}',
        'Gross profit: {amount}': 'Laba kotor: {amount}',
        '({n} sale(s) excluded)': '({n} penjualan tidak dihitung)',
        'Net profit: {amount}': 'Laba bersih: {amount}',
        'Week of {date}': 'Minggu tanggal {date}',
        '🆕 New order — review': '🆕 Pesanan baru — tinjau',
        '📥 Restock — review': '📥 Restok — tinjau',
        '🏠 Self use — review': '🏠 Pemakaian sendiri — tinjau',
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
        '• {name} ×{qty} — <i>price missing</i>': '• {name} ×{qty} — <i>harga belum diisi</i>',
        'Subtotal: {amount}': 'Subtotal: {amount}',
        'Discount: −{amount}': 'Diskon: −{amount}',
        'Shipping: +{amount}': 'Ongkos kirim: +{amount}',
        'Admin fee: +{amount}': 'Biaya admin: +{amount}',
        'Total paid: {amount}': 'Total dibayar: {amount}',
        '✅ Create draft order': '✅ Buat pesanan draf',
        '✅ Save restock': '✅ Simpan restok',
        '✅ Save self use': '✅ Simpan pemakaian sendiri',
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
        "Couldn't read that amount. Send the price per unit as a number, e.g. <code>12000</code>":
            'Tidak dapat membaca jumlah itu. Kirim harga per unit sebagai angka, mis. <code>12000</code>',
        "Couldn't read that amount. Send it as a number, e.g. <code>15000</code>, or tap Skip":
            'Tidak dapat membaca jumlah itu. Kirim sebagai angka, mis. <code>15000</code>, atau ketuk Lewati',
        'Send the <b>quantity</b> as a number, e.g. <code>12</code>':
            'Kirim <b>jumlah</b> sebagai angka, mis. <code>12</code>',
        'Send the <b>price per unit</b> of {name} from the invoice, e.g. <code>12000</code>':
            'Kirim <b>harga per unit</b> {name} dari invoice, mis. <code>12000</code>',
        'Last known: {amount}': 'Terakhir diketahui: {amount}',
        'Send the <b>discount</b> on this invoice, or tap Skip.':
            'Kirim <b>diskon</b> pada invoice ini, atau ketuk Lewati.',
        'Send the <b>shipping cost</b>, or tap Skip.':
            'Kirim <b>ongkos kirim</b>, atau ketuk Lewati.',
        'Send the <b>bank admin fee</b>, or tap Skip.':
            'Kirim <b>biaya admin bank</b>, atau ketuk Lewati.',
        'Skip': 'Lewati',
        'Skipped': 'Dilewati',
        'Abandoned': 'Dibatalkan',
        'Session expired — start again from the menu':
            'Sesi berakhir — mulai lagi dari menu',
        'Added': 'Ditambahkan',
        'Nothing selected yet': 'Belum ada yang dipilih',
        'Send the unit price for every product first':
            'Kirim harga satuan untuk setiap produk terlebih dahulu',
        'Order created': 'Pesanan dibuat',
        'Restock saved': 'Restok tersimpan',
        'Self use saved': 'Pemakaian sendiri tersimpan',
        '✅ Draft order <b>#{id}</b> created — total {total}':
            '✅ Pesanan draf <b>#{id}</b> dibuat — total {total}',
        '✅ Restock batch <b>#{id}</b> saved — {cost}':
            '✅ Batch restok <b>#{id}</b> tersimpan — {cost}',
        '✅ Self use <b>#{id}</b> saved — {total}':
            '✅ Pemakaian sendiri <b>#{id}</b> tersimpan — {total}',

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
        'Discount cannot exceed the invoice subtotal':
            'Diskon tidak boleh melebihi subtotal invoice',
        'invalid unit': 'unit tidak valid',

        # --- Request validation / API errors (app.py); surfaced as toasts ---
        'Invalid JSON body': 'Isi JSON tidak valid',
        'No whitelisted Telegram IDs to send to':
            'Tidak ada ID Telegram yang diizinkan untuk dikirimi',
        'Could not send to any recipient. The report was saved on the server.':
            'Tidak dapat mengirim ke penerima mana pun. Laporan tersimpan di server.',
        'Name required': 'Nama wajib diisi',
        'Price must be a number': 'Harga harus berupa angka',
        'Price must be 0 or more': 'Harga harus 0 atau lebih',
        'Cost price must be a number': 'Harga pokok harus berupa angka',
        'Cost price must be 0 or more': 'Harga pokok harus 0 atau lebih',
        'Reorder threshold must be a whole number':
            'Ambang pemesanan ulang harus berupa bilangan bulat',
        'Reorder threshold must be 0 or more': 'Ambang pemesanan ulang harus 0 atau lebih',
        'Stock must be a whole number': 'Stok harus berupa bilangan bulat',
        'Stock must be 0 or more': 'Stok harus 0 atau lebih',
        'SKU already exists': 'SKU sudah ada',
        'Product ID and a non-zero whole-number quantity required':
            'ID produk dan jumlah bilangan bulat bukan nol wajib diisi',
        'Product not found': 'Produk tidak ditemukan',
        'Insufficient stock': 'Stok tidak cukup',
        'At least one item required': 'Minimal satu item wajib diisi',
        'Each item needs a product_id and a positive whole-number quantity':
            'Setiap item memerlukan product_id dan jumlah bilangan bulat positif',
        'Unit price must be 0 or more': 'Harga satuan harus 0 atau lebih',
        'Cost price is required for stock on hand':
            'Harga pokok wajib diisi bila ada stok awal',
        'Discount, shipping and admin fee must each be 0 or more':
            'Diskon, ongkos kirim, dan biaya admin masing-masing harus 0 atau lebih',
        'Valid product and positive whole-number quantity required':
            'Produk yang valid dan jumlah bilangan bulat positif wajib diisi',
        'invalid period': 'periode tidak valid',
        'invalid offset': 'offset tidak valid',
        'Unsupported language': 'Bahasa tidak didukung',
        'Request failed ({status})': 'Permintaan gagal ({status})',

        # settings errors
        "'{token}' is not a numeric Telegram user ID":
            "'{token}' bukan ID pengguna Telegram berupa angka",
        "Unknown timezone '{name}'": "Zona waktu '{name}' tidak dikenal",
        'Alert threshold must be a number': 'Ambang peringatan harus berupa angka',
        'Alert threshold cannot be negative': 'Ambang peringatan tidak boleh negatif',
        'Bot token required to enable the bot': 'Token bot diperlukan untuk mengaktifkan bot',
        'No users whitelisted — the bot will reject everyone':
            'Belum ada pengguna yang diizinkan — bot akan menolak semua orang',
        'No bot token saved or provided': 'Tidak ada token bot yang tersimpan atau diberikan',
        'Telegram rejected the token: {error}': 'Telegram menolak token: {error}',
        'Could not reach api.telegram.org': 'Tidak dapat menghubungi api.telegram.org',
        'Current password is incorrect': 'Kata sandi saat ini salah',
        'Nothing to change': 'Tidak ada yang perlu diubah',
        'New password must be at least 6 characters': 'Kata sandi baru minimal 6 karakter',
        'Username already taken': 'Nama pengguna sudah dipakai',
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


def month_label(dt, lang):
    """'June 2026' / 'Juni 2026' for a date or datetime.

    The dashboard, the bot's monthly summary and the monthly report all title
    themselves this way; keeping one implementation stops them drifting apart.
    """
    return f'{month_name(dt.month, lang)} {dt.year}'


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
