import sqlite3
import logging
import os
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import generate_password_hash

log = logging.getLogger('database')

DEFAULT_ADMIN_USERNAME = 'admin'
DEFAULT_ADMIN_PASSWORD = 'admin123'

# Marks a settings value as encrypted, mirroring how hashed passwords are
# detected by their method prefix. Anything without it is a pre-encryption row.
SECRET_PREFIX = 'enc:v1:'

# Settings rows held encrypted at rest. Read them with get_secret_setting and
# write them with set_secret_setting; adding a key here also migrates any
# existing plaintext value on the next start.
ENCRYPTED_SETTINGS = ('telegram_bot_token',)

# Deliberately a sibling of the DB rather than inside it: backup.sh copies only
# shop.db, so a leaked backup carries no way to decrypt the bot token.
ENCRYPTION_KEY_PATH = (os.environ.get('SHOP_ENCRYPTION_KEY_PATH')
                       or os.path.join(os.path.dirname(__file__), '.encryption_key'))

# Module-level so a deployment can point at a mounted volume and tests can
# monkeypatch it (tests/conftest.py). get_db() must keep reading it at call
# time rather than caching the value.
DB_PATH = os.environ.get('SHOP_DB_PATH') or os.path.join(os.path.dirname(__file__), 'shop.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else default

def set_setting(db, key, value):
    """Upsert one setting. Does not commit; the caller owns the transaction."""
    db.execute("""
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))


def _load_encryption_key():
    """Fernet key for settings encrypted at rest, from the environment or a
    generated 0600 file next to the database."""
    env_key = os.environ.get('SHOP_ENCRYPTION_KEY')
    if env_key:
        try:
            Fernet(env_key.encode())
        except (ValueError, TypeError) as e:
            # A typo'd key is a deployment mistake made seconds ago: fail now,
            # rather than silently losing access to the stored token.
            raise RuntimeError(
                'SHOP_ENCRYPTION_KEY is not a valid Fernet key. Generate one with:\n'
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"') from e
        return env_key.encode()
    try:
        with open(ENCRYPTION_KEY_PATH, 'rb') as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(ENCRYPTION_KEY_PATH) or '.', exist_ok=True)
    fd = os.open(ENCRYPTION_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key)
    return key

def encrypt_secret(value):
    """Wrap a plaintext setting value for storage. Blank stays blank so callers
    can keep treating '' as 'not configured'."""
    if not value:
        return value
    return SECRET_PREFIX + Fernet(_load_encryption_key()).encrypt(value.encode()).decode()

def decrypt_secret(stored, key='value'):
    """Unwrap a stored secret. Values without the prefix predate encryption and
    are returned as-is; init_db() rewrites them on the next start.

    A key that no longer matches (a backup restored without .encryption_key)
    yields '' plus a loud log line, so the bot idles as if unconfigured and the
    owner can just re-enter the token -- losing the key must not take the shop's
    web UI down with it."""
    if not stored:
        return ''
    if not stored.startswith(SECRET_PREFIX):
        return stored
    try:
        return Fernet(_load_encryption_key()).decrypt(stored[len(SECRET_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        log.error("cannot decrypt setting %r: wrong or missing encryption key "
                  "(expected at %s). Re-enter the value in Settings.",
                  key, ENCRYPTION_KEY_PATH)
        return ''

def get_secret_setting(db, key, default=''):
    """get_setting for a value held encrypted at rest."""
    raw = get_setting(db, key)
    return default if raw is None else decrypt_secret(raw, key)

def set_secret_setting(db, key, value):
    """set_setting for a value held encrypted at rest. Does not commit."""
    set_setting(db, key, encrypt_secret(value))

def init_db():
    conn = get_db()
    c = conn.cursor()
    # WAL is a persistent property of the file, so setting it once here is enough.
    # It lets the Telegram poller thread write while a web request reads, instead
    # of both queueing on the busy_timeout above.
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT UNIQUE,
            price REAL NOT NULL DEFAULT 0,
            cost_price REAL NOT NULL DEFAULT 0,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            reorder_threshold INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            cost_review_needed INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS stock_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            change_qty INTEGER NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'draft',
            total_amount REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            alerted_status TEXT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            unit_cost REAL NOT NULL DEFAULT 0,
            subtotal REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS restock_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subtotal_cost REAL NOT NULL DEFAULT 0,
            discount REAL NOT NULL DEFAULT 0,
            shipping_cost REAL NOT NULL DEFAULT 0,
            admin_fee REAL NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS restock_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty_added INTEGER NOT NULL,
            unit_price REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            allocated_cost REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES restock_batches(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS self_use_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_value REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS self_use_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            subtotal REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES self_use_batches(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
    ''')

    # Migrate: add new tables if not exists
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='restock_batches'")
    if not c.fetchone():
        c.execute('''CREATE TABLE restock_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subtotal_cost REAL NOT NULL DEFAULT 0,
            discount REAL NOT NULL DEFAULT 0,
            shipping_cost REAL NOT NULL DEFAULT 0,
            admin_fee REAL NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='restock_items'")
    if not c.fetchone():
        c.execute('''CREATE TABLE restock_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty_added INTEGER NOT NULL,
            unit_price REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            allocated_cost REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES restock_batches(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )''')

    # Migrate: drop the categories feature. It earned nothing over plain SKUs and
    # only added a step to product creation.
    #
    # products.category_id is named in a table-level FOREIGN KEY, and SQLite refuses
    # ALTER TABLE DROP COLUMN for such a column, so products must be rebuilt. The old
    # table is renamed aside and the new one created under the real name, rather than
    # building `products_new` and renaming it into place: order_items, restock_items,
    # self_use_items and stock_logs all carry foreign keys onto products, and
    # legacy_alter_table keeps this rename from rewriting them to follow the old
    # table. They keep pointing at `products`, which exists again by the time
    # enforcement comes back on.
    product_cols = [r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()]
    if 'category_id' in product_cols:
        conn.commit()  # neither PRAGMA below can change inside a transaction
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("PRAGMA legacy_alter_table=ON")
        c.execute("ALTER TABLE products RENAME TO products_pre_categories_drop")
        c.execute('''CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT UNIQUE,
            price REAL NOT NULL DEFAULT 0,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            reorder_threshold INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        # Explicit column list, and ids are carried over: every other table
        # references products(id), so the values must survive the rebuild.
        c.execute('''INSERT INTO products
            (id, name, sku, price, stock_qty, reorder_threshold, is_archived,
             created_at, updated_at)
            SELECT id, name, sku, price, stock_qty, reorder_threshold, is_archived,
                   created_at, updated_at
            FROM products_pre_categories_drop''')
        c.execute("DROP TABLE products_pre_categories_drop")
        conn.commit()
        c.execute("PRAGMA legacy_alter_table=OFF")
        c.execute("PRAGMA foreign_keys=ON")
        orphans = c.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            # Never leave the shop running on a store whose references went stale.
            raise RuntimeError(
                f'dropping categories left {len(orphans)} broken foreign key(s); '
                'restore the pre-upgrade backup and report this')
        log.warning('rebuilt products without category_id and dropped the categories table')
    c.execute("DROP TABLE IF EXISTS categories")

    # Migrate: track which order status a stale-order alert was last sent for, so
    # the Telegram bot notifies once per stalling status instead of every cycle.
    order_cols = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if 'alerted_status' not in order_cols:
        c.execute("ALTER TABLE orders ADD COLUMN alerted_status TEXT")

    # Migrate: per-product cost, so profit margin becomes computable. Restock used to
    # capture a single batch total and split it across lines in proportion to quantity,
    # which cannot express a supplier invoice: every line carries its own price, while
    # the discount voucher, shipping and bank admin fee apply to the invoice as a whole.
    # These columns record the invoice as written and the landed unit cost it implies.
    #
    # Each block is guarded on its own table, and the backfills below run only in the
    # same pass that adds the columns, so a re-run never overwrites captured data. They
    # cascade -- restock lines seed products, which seed order lines -- so the order of
    # the blocks matters.
    restock_item_cols = [r[1] for r in c.execute("PRAGMA table_info(restock_items)").fetchall()]
    if 'unit_cost' not in restock_item_cols:
        c.execute("ALTER TABLE restock_items ADD COLUMN unit_price REAL NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE restock_items ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0")
        # The quantity split is the only cost history that exists, so it seeds both the
        # landed cost and the invoice price we never got to record.
        c.execute("UPDATE restock_items SET unit_cost = allocated_cost / qty_added,"
                  " unit_price = allocated_cost / qty_added WHERE qty_added > 0")

    batch_cols = [r[1] for r in c.execute("PRAGMA table_info(restock_batches)").fetchall()]
    if 'subtotal_cost' not in batch_cols:
        c.execute("ALTER TABLE restock_batches ADD COLUMN subtotal_cost REAL NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE restock_batches ADD COLUMN discount REAL NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE restock_batches ADD COLUMN shipping_cost REAL NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE restock_batches ADD COLUMN admin_fee REAL NOT NULL DEFAULT 0")
        # No charge history exists, so the whole of an old batch total is goods.
        # total_cost keeps its meaning -- money actually paid -- and stays untouched,
        # which is what leaves sales_summary's restock_cost and net_profit unaffected.
        c.execute("UPDATE restock_batches SET subtotal_cost = total_cost")

    product_cols = [r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()]
    if 'cost_price' not in product_cols:
        c.execute("ALTER TABLE products ADD COLUMN cost_price REAL NOT NULL DEFAULT 0")
        # Seeded only from batches that restocked ONE product. The old quantity split
        # divides a batch total evenly per unit, so in a mixed batch a Rp 5.000 item and a
        # Rp 30.000 item come out at the same cost -- a figure that yields margins of
        # several hundred percent and poisons the weighted average of the next restock.
        # A single-line batch has nothing to split across, so its total genuinely is that
        # product's cost. Everything else stays 0, meaning "unknown": the margin reports
        # leave those products out and say so, and the next restock records the truth.
        c.execute("""UPDATE products SET cost_price = COALESCE((
                         SELECT ri.unit_cost FROM restock_items ri
                         WHERE ri.product_id = products.id
                           AND (SELECT COUNT(*) FROM restock_items sib
                                WHERE sib.batch_id = ri.batch_id) = 1
                         ORDER BY ri.id DESC LIMIT 1), 0)""")
        seeded = c.execute("SELECT COUNT(*) FROM products WHERE cost_price > 0").fetchone()[0]
        log.warning('seeded cost_price for %d product(s) from single-product restock batches; '
                    'products still at 0 need a cost from their next restock or the product '
                    'form before they appear in the profit ranking', seeded)

    order_item_cols = [r[1] for r in c.execute("PRAGMA table_info(order_items)").fetchall()]
    if 'unit_cost' not in order_item_cols:
        c.execute("ALTER TABLE order_items ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0")
        # An estimate, and knowingly so: the cost at the time of each historical sale was
        # never recorded. It inherits the restriction above, so a line only gets a cost
        # when that cost was defensible in the first place.
        c.execute("""UPDATE order_items SET unit_cost = COALESCE((
                         SELECT p.cost_price FROM products p
                         WHERE p.id = order_items.product_id), 0)""")
        backfilled = c.execute("SELECT COUNT(*) FROM order_items WHERE unit_cost > 0").fetchone()[0]
        log.warning('backfilled unit_cost on %d historical order line(s) from the seeded product '
                    'cost; margins from before this upgrade are estimates', backfilled)

    # Migrate: mark products whose recorded cost is suspect rather than merely absent.
    # A cost of 0 already means "unknown" and is visible as such, but a cost that a
    # voided restock left standing looks perfectly ordinary while being wrong. The flag
    # is what tells the two apart on the products page; nothing sets it until a void
    # cannot restore the previous cost.
    product_cols = [r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()]
    if 'cost_review_needed' not in product_cols:
        c.execute("ALTER TABLE products ADD COLUMN cost_review_needed INTEGER NOT NULL DEFAULT 0")

    # Seed the first user. Credentials come from the environment so a deployment
    # never has to ship with the documented default; changing them later has no
    # effect, since this only runs against an empty users table.
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        username = os.environ.get('SHOP_ADMIN_USERNAME') or DEFAULT_ADMIN_USERNAME
        password = os.environ.get('SHOP_ADMIN_PASSWORD') or DEFAULT_ADMIN_PASSWORD
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                  (username, generate_password_hash(password)))
        if password == DEFAULT_ADMIN_PASSWORD:
            log.warning('seeded user %r with the default password; change it in Settings '
                        'or set SHOP_ADMIN_PASSWORD before first start', username)

    # Migrate: hash any plaintext passwords in place (idempotent; hashed values
    # carry a method prefix and are skipped on re-run).
    users = c.execute("SELECT id, password FROM users").fetchall()
    for user_id, password in users:
        if not password.startswith(('pbkdf2:', 'scrypt:')):
            c.execute("UPDATE users SET password = ? WHERE id = ?",
                      (generate_password_hash(password), user_id))

    # Migrate: encrypt secrets stored in plaintext by older versions (idempotent;
    # encrypted values carry SECRET_PREFIX and are skipped on re-run).
    for key in ENCRYPTED_SETTINGS:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row and row[0] and not row[0].startswith(SECRET_PREFIX):
            c.execute("UPDATE settings SET value = ? WHERE key = ?",
                      (encrypt_secret(row[0]), key))
            log.info('encrypted setting %r at rest', key)

    conn.commit()
    conn.close()
