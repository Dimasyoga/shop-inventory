"""Harness for the browser suite: a real server, a real Chromium, a real session.

These exist because 500-odd server-side tests can all pass while the page is broken.
Draft editing, the orders pager and the CSV button each shipped green and still had to
be clicked by hand before anyone knew they worked -- static/js/app.js is a thousand
lines that nothing in CI could reach.

Two decisions worth knowing before adding to this directory:

* The server runs in a **subprocess**, not a thread. tests/conftest.py's ``db_path``
  monkeypatches ``database.DB_PATH``, and a threaded server would share that module
  global -- a unit test running between two browser tests could repoint the live
  server's database mid-suite. A subprocess has its own, fixed by the environment.
* Sign-in is done by minting the session cookie rather than driving the login form.
  It keeps the browser tests off the login throttle (five failures per address, and
  every test here shares one), and the form itself is server-rendered and already
  covered by tests/test_login_throttle.py.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

# When playwright is not installed, ignore this directory rather than failing
# collection: `pip install -r requirements-dev.txt` is the fix, and the rest of the
# suite should still run for someone who has not done it yet.
collect_ignore_glob = []
try:
    import playwright.sync_api  # noqa: F401
except ImportError:  # pragma: no cover - depends on the developer's environment
    collect_ignore_glob = ['test_*.py']

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_collection_modifyitems(items):
    """Mark everything here `browser`, so `-m "not browser"` skips the lot."""
    for item in items:
        if os.path.dirname(str(item.fspath)) == os.path.dirname(os.path.abspath(__file__)):
            item.add_marker(pytest.mark.browser)

# Fixed so this process can sign a cookie the server will accept. A literal is right
# here and nowhere else: the server is a subprocess on loopback holding throwaway data.
TEST_SECRET = 'browser-tests-only-not-a-deployment-key'


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='session')
def live_server(tmp_path_factory):
    """A real app process on loopback. Yields (base_url, db_path).

    Session-scoped because starting Flask costs about a second and the tests reset the
    data between themselves instead.
    """
    tmp = tmp_path_factory.mktemp('browser')
    db_file = tmp / 'browser.db'
    port = _free_port()
    env = {
        **os.environ,
        'SHOP_DB_PATH': str(db_file),
        'SHOP_SECRET_KEY': TEST_SECRET,
        'SHOP_ENCRYPTION_KEY_PATH': str(tmp / 'browser.key'),
        # No bot: it would long-poll api.telegram.org from a test run.
        'SHOP_ENABLE_BOT': '0',
        'PORT': str(port),
        'HOST': '127.0.0.1',
        'FLASK_DEBUG': '',
    }
    proc = subprocess.Popen([sys.executable, 'app.py'], cwd=REPO_ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base_url = f'http://127.0.0.1:{port}'
    try:
        _wait_until_healthy(proc, base_url)
        yield base_url, str(db_file)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _wait_until_healthy(proc, base_url, timeout=30):
    import urllib.error
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # pragma: no cover - only on a broken app.py
            raise RuntimeError(f'server exited early:\n{proc.stdout.read()}')
        try:
            with urllib.request.urlopen(f'{base_url}/healthz', timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.1)
    proc.terminate()  # pragma: no cover
    raise RuntimeError('server never became healthy')  # pragma: no cover


@pytest.fixture
def shop(live_server, monkeypatch):
    """Empty the shop and hand back a helper that seeds it through the real services.

    Points this process's ``database.DB_PATH`` at the server's file so the seeding
    helpers can call services.* directly rather than reimplementing them in SQL.
    monkeypatch puts it back afterwards, which matters because the unit tests in the
    parent directory share this interpreter.
    """
    import database

    base_url, db_file = live_server
    monkeypatch.setattr(database, 'DB_PATH', db_file)

    conn = database.get_db()
    # Children before parents: order_items references both orders and products.
    for table in ('stock_logs', 'order_items', 'orders', 'restock_items',
                  'restock_batches', 'self_use_items', 'self_use_batches', 'products'):
        conn.execute(f'DELETE FROM {table}')
    # Reset the counters too, so ids are predictable and a test can assert on "order #1".
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()

    return Shop(base_url, db_file)


class Shop:
    """Seeding and assertion helpers, all going through the same services the app uses."""

    def __init__(self, base_url, db_file):
        self.base_url = base_url
        self.db_file = db_file

    def connect(self):
        import database
        return database.get_db()

    def product(self, name, sku=None, price=10000, cost=6000, stock=50, threshold=0):
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO products (name, sku, price, cost_price, stock_qty, reorder_threshold)"
            " VALUES (?, ?, ?, ?, ?, ?)", (name, sku, price, cost, stock, threshold))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid

    def order(self, items, status='draft'):
        """items = [(product_id, qty)]. Advances the order to ``status``."""
        import services
        conn = self.connect()
        try:
            res = services.create_order(
                conn, [{'product_id': p, 'quantity': q} for p, q in items])
            if status in ('confirmed', 'completed'):
                services.confirm_order(conn, res['order_id'])
            if status == 'completed':
                services.complete_order(conn, res['order_id'], actor='web:admin')
            if status == 'cancelled':
                services.cancel_order(conn, res['order_id'])
            return res['order_id']
        finally:
            conn.close()

    def stock_of(self, product_id):
        """(physical, reserved) -- the pair every reservation assertion is about."""
        conn = self.connect()
        row = conn.execute("SELECT stock_qty, reserved_qty FROM products WHERE id = ?",
                           (product_id,)).fetchone()
        conn.close()
        return row['stock_qty'], row['reserved_qty']

    def set_reserved(self, product_id, qty):
        """Corrupt the hold counter behind the services' back, to test the reconciler."""
        conn = self.connect()
        conn.execute("UPDATE products SET reserved_qty = ? WHERE id = ?", (qty, product_id))
        conn.commit()
        conn.close()

    def order_lines(self, order_id):
        conn = self.connect()
        rows = conn.execute(
            "SELECT product_id, quantity FROM order_items WHERE order_id = ? ORDER BY id",
            (order_id,)).fetchall()
        conn.close()
        return [(r['product_id'], r['quantity']) for r in rows]

    def order_total(self, order_id):
        conn = self.connect()
        row = conn.execute("SELECT total_amount FROM orders WHERE id = ?", (order_id,)).fetchone()
        conn.close()
        return row['total_amount']


@pytest.fixture
def page(page, shop):
    """A signed-in page on the live server.

    Shadows pytest-playwright's ``page`` so every test in this directory gets a session
    without repeating the setup, and so nobody is tempted to type a password.
    """
    from flask.sessions import SecureCookieSessionInterface

    import app as app_module

    app_module.app.secret_key = TEST_SECRET
    serializer = SecureCookieSessionInterface().get_signing_serializer(app_module.app)
    cookie = serializer.dumps({'user_id': 1, 'username': 'admin'})

    host = shop.base_url.replace('http://', '')
    page.context.add_cookies([{
        'name': 'session', 'value': cookie,
        'domain': host.split(':')[0], 'path': '/',
    }])
    page.set_default_timeout(10_000)
    return page
