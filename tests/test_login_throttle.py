"""Failed sign-ins are rate limited per client address.

The shop has one admin account on a LAN, so an unthrottled login form is an
open invitation to guess the password. These cover the lock going on, the wait
expiring, and the two ways the throttle could be worse than useless: locking a
guesser out of nothing (a successful sign-in must clear the count) and locking
the shop owner out of their own machine (a guesser elsewhere on the LAN must
not spend the owner's budget).
"""
import pytest

import app as app_module


LOCKOUT_MSG = b"Too many failed sign-in attempts"


@pytest.fixture
def clock(monkeypatch):
    """Hand-cranked monotonic clock, so a 15-minute wait costs no wall time."""
    class Clock:
        now = 1000.0

        def advance(self, seconds):
            self.now += seconds

    c = Clock()
    monkeypatch.setattr(app_module, "_login_now", lambda: c.now)
    return c


@pytest.fixture
def anon(db_path):
    """A logged-out client. ``client`` from conftest arrives with a session."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _attempt(c, password="wrong", username="admin", addr=None):
    kwargs = {"environ_base": {"REMOTE_ADDR": addr}} if addr else {}
    return c.post("/login", data={"username": username, "password": password}, **kwargs)


def test_guessing_is_refused_after_the_limit(anon, clock):
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        res = _attempt(anon)
        assert res.status_code == 200
        assert b"Invalid credentials" in res.data

    res = _attempt(anon)
    assert res.status_code == 429
    assert LOCKOUT_MSG in res.data
    assert b"15 minute" in res.data


def test_lockout_refuses_the_right_password_too(anon, clock):
    """Otherwise the throttle is an oracle: a locked-out guesser could tell a
    correct password from a wrong one by which rejection came back."""
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        _attempt(anon)

    res = _attempt(anon, password="admin123")
    assert res.status_code == 429
    assert LOCKOUT_MSG in res.data


def test_lockout_lifts_once_the_wait_passes(anon, clock):
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        _attempt(anon)
    assert _attempt(anon).status_code == 429

    clock.advance(app_module.LOGIN_LOCKOUT + 1)
    res = _attempt(anon, password="admin123")
    assert res.status_code == 302  # signed in


def test_countdown_shrinks_as_the_wait_runs_down(anon, clock):
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        _attempt(anon)

    clock.advance(13 * 60)
    res = _attempt(anon)
    assert res.status_code == 429
    # 2 minutes left, rounded up -- never down, which would invite a retry that
    # is refused on arrival.
    assert b"2 minute" in res.data


def test_a_successful_sign_in_clears_the_count(anon, clock):
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS - 1):
        _attempt(anon)
    assert _attempt(anon, password="admin123").status_code == 302
    anon.get("/logout")

    # Starting from zero again, not one attempt away from a lockout.
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS - 1):
        assert _attempt(anon).status_code == 200


def test_old_failures_stop_counting(anon, clock):
    """A slow trickle of typos over a day must not add up to a lockout."""
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS * 3):
        assert _attempt(anon).status_code == 200
        clock.advance(app_module.LOGIN_LOCKOUT + 1)


def test_one_client_cannot_lock_out_another(anon, clock):
    for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
        _attempt(anon, addr="10.0.0.9")
    assert _attempt(anon, addr="10.0.0.9").status_code == 429

    # The owner, on a different device, is unaffected.
    assert _attempt(anon, password="admin123", addr="10.0.0.4").status_code == 302


def test_guessing_a_different_username_does_not_buy_a_fresh_budget(anon, clock):
    """The bucket is the address, so rotating usernames does not reset it."""
    for i in range(app_module.LOGIN_MAX_ATTEMPTS):
        assert _attempt(anon, username=f"user{i}").status_code == 200
    assert _attempt(anon, username="someone-else").status_code == 429


def test_stale_buckets_are_dropped(anon, clock):
    """Every address that ever mistypes leaves an entry behind; they have to be
    reaped somewhere, or the map is an unbounded leak on a long-running process."""
    _attempt(anon, addr="10.0.0.9")
    assert "10.0.0.9" in app_module._login_failures

    clock.advance(app_module.LOGIN_LOCKOUT + 1)
    _attempt(anon, addr="10.0.0.4")
    assert "10.0.0.9" not in app_module._login_failures
    assert "10.0.0.4" in app_module._login_failures
