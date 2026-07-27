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

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT UNIQUE,
            category_id INTEGER,
            price REAL NOT NULL DEFAULT 0,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            reorder_threshold INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id)
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
            total_cost REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS restock_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty_added INTEGER NOT NULL,
            allocated_cost REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES restock_batches(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
    ''')

    # Migrate: add new tables if not exists
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='restock_batches'")
    if not c.fetchone():
        c.execute('''CREATE TABLE restock_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            allocated_cost REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES restock_batches(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )''')

    # Migrate: track which order status a stale-order alert was last sent for, so
    # the Telegram bot notifies once per stalling status instead of every cycle.
    order_cols = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    if 'alerted_status' not in order_cols:
        c.execute("ALTER TABLE orders ADD COLUMN alerted_status TEXT")

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
