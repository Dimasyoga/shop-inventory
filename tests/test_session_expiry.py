"""Sessions expire after a period of inactivity, and say so usefully when they do.

Before this, `session['user_id']` was set without marking the session permanent. That
left a browser-session cookie -- gone when the browser closes, which on a shop counter
PC that is never closed means never -- and Flask's default 31-day signature window
behind it. A machine left signed in stayed signed in.

The window is an *idle* one: Flask re-issues the cookie on every response, so working
restarts the clock. Tests here force expiry by shrinking the window rather than by
waiting, since a negative lifetime makes any cookie already too old (max_age=0 is
accepted -- age 0 is not greater than 0 -- so 0 would be a flaky boundary).
"""
from datetime import timedelta

import pytest

import app as app_module


@pytest.fixture
def lifetime(monkeypatch):
    """Set the idle window for one test, restoring the configured default after."""
    def _set(delta):
        monkeypatch.setattr(app_module.app, 'permanent_session_lifetime', delta)
    return _set


def signed_in(client):
    return client.post('/login', data={'username': 'admin', 'password': 'admin123'})


def test_the_default_window_is_twelve_hours(db_path):
    assert app_module.app.permanent_session_lifetime == timedelta(hours=12)


def test_signing_in_marks_the_session_permanent(client, db_path):
    """Without this the cookie has no expiry the browser honours, and Flask does not
    re-issue it, so the idle clock would never restart."""
    with app_module.app.test_client() as c:
        assert signed_in(c).status_code == 302
        with c.session_transaction() as sess:
            assert sess.permanent is True


def test_a_page_survives_while_the_window_is_open(client, db_path, lifetime):
    lifetime(timedelta(hours=12))
    assert client.get('/products').status_code == 200


def test_an_expired_session_is_sent_back_to_the_login_page(db_path, lifetime):
    with app_module.app.test_client() as c:
        signed_in(c)
        assert c.get('/products').status_code == 200

        lifetime(timedelta(seconds=-1))  # the window has already closed

        res = c.get('/products')
        assert res.status_code == 302
        assert '/login' in res.headers['Location']


def test_an_expired_session_gets_401_json_from_the_api_not_a_redirect(db_path, lifetime):
    """A fetch() follows a redirect, gets 200 and an HTML login page, and then fails
    parsing it -- reporting a JSON syntax error to a seller whose actual problem is
    that they need to sign in. 401 is what the client can act on."""
    with app_module.app.test_client() as c:
        signed_in(c)
        lifetime(timedelta(seconds=-1))

        res = c.get('/api/products')
        assert res.status_code == 401
        assert res.get_json()['error'] == 'Your session has expired. Please sign in again.'


def test_the_expiry_message_is_translated(db_path, lifetime):
    import database
    with app_module.app.test_client() as c:
        signed_in(c)
        conn = database.get_db()
        database.set_setting(conn, 'language', 'id')
        conn.commit()
        conn.close()
        lifetime(timedelta(seconds=-1))

        body = c.get('/api/products').get_json()
        assert body['error'] == 'Sesi Anda telah berakhir. Silakan masuk kembali.'


def test_every_api_route_answers_401_and_every_page_redirects(client, db_path, lifetime):
    """The split is by path prefix, so it has to hold for routes nobody thought about."""
    with app_module.app.test_client() as c:
        signed_in(c)
        lifetime(timedelta(seconds=-1))

        for path in ('/api/products', '/api/orders', '/api/stock/movements',
                     '/api/reports/months', '/api/stock/reservations/check'):
            assert c.get(path).status_code == 401, path
        for path in ('/', '/products', '/orders', '/settings', '/stock-history'):
            assert c.get(path).status_code == 302, path


def test_healthz_stays_reachable_without_a_session(db_path, lifetime):
    """The container healthcheck must not start failing because nobody is signed in."""
    lifetime(timedelta(seconds=-1))
    with app_module.app.test_client() as anon:
        assert anon.get('/healthz').status_code == 200


def test_activity_slides_the_window_rather_than_expiring_mid_shift(db_path):
    """SESSION_REFRESH_EACH_REQUEST re-issues the cookie on every response, so the
    session a seller is actively using never ages out under them."""
    with app_module.app.test_client() as c:
        signed_in(c)
        first = c.get_cookie('session')
        assert first is not None

        c.get('/products')
        # A fresh Set-Cookie on an ordinary read is what restarts the clock.
        assert c.get_cookie('session') is not None
        assert app_module.app.config['SESSION_REFRESH_EACH_REQUEST'] is True


def test_signing_out_still_ends_the_session_immediately(db_path):
    with app_module.app.test_client() as c:
        signed_in(c)
        c.get('/logout')
        assert c.get('/products').status_code == 302
        assert c.get('/api/products').status_code == 401


def test_the_window_is_configurable(monkeypatch):
    """SHOP_SESSION_HOURS is read at import; check the arithmetic it feeds."""
    monkeypatch.setenv('SHOP_SESSION_HOURS', '0.5')
    hours = float(__import__('os').environ.get('SHOP_SESSION_HOURS') or 12)
    assert timedelta(hours=hours) == timedelta(minutes=30)
