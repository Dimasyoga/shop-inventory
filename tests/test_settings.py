import pytest

import database


def get_setting(key):
    conn = database.get_db()
    val = database.get_setting(conn, key)
    conn.close()
    return val


# --- Telegram settings ---

def test_telegram_settings_round_trip(client):
    res = client.post("/api/settings/telegram", json={
        "enabled": True, "token": "123:ABC", "whitelist": "111, 222", "timezone": "Asia/Jakarta"})
    assert res.status_code == 200
    assert get_setting("telegram_enabled") == "1"
    assert get_setting("telegram_bot_token") == "123:ABC"
    assert get_setting("telegram_whitelist") == "111,222"
    assert get_setting("shop_timezone") == "Asia/Jakarta"


def test_blank_token_keeps_saved_value(client):
    client.post("/api/settings/telegram", json={"enabled": True, "token": "123:ABC", "whitelist": "1"})
    res = client.post("/api/settings/telegram", json={"enabled": True, "token": "", "whitelist": "1"})
    assert res.status_code == 200
    assert get_setting("telegram_bot_token") == "123:ABC"


def test_bad_whitelist_entry_rejected(client):
    res = client.post("/api/settings/telegram", json={"enabled": False, "whitelist": "111, bogus"})
    assert res.status_code == 400
    assert "bogus" in res.get_json()["error"]


def test_bad_timezone_rejected(client):
    res = client.post("/api/settings/telegram", json={"enabled": False, "whitelist": "", "timezone": "Not/AZone"})
    assert res.status_code == 400


def test_enable_without_token_rejected(client):
    res = client.post("/api/settings/telegram", json={"enabled": True, "token": "", "whitelist": "1"})
    assert res.status_code == 400
    assert "token" in res.get_json()["error"].lower()


def test_empty_whitelist_warns(client):
    res = client.post("/api/settings/telegram", json={"enabled": True, "token": "123:ABC", "whitelist": ""})
    assert res.status_code == 200
    assert res.get_json()["warning"]


def test_settings_page_never_echoes_token(client):
    client.post("/api/settings/telegram", json={"enabled": False, "token": "SECRET-TOKEN-99", "whitelist": ""})
    html = client.get("/settings").get_data(as_text=True)
    assert "SECRET-TOKEN-99" not in html
    assert "leave blank to keep" in html


def test_settings_requires_login(db_path):
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        res = c.get("/settings")
        assert res.status_code == 302  # redirect to login


# --- Test-connection endpoint (Telegram API faked) ---

class FakeTelegramAPI:
    fail = False

    def __init__(self, token, timeout=None):
        self.token = token

    def call(self, method, **params):
        from telegram_bot import TelegramError
        if self.fail:
            raise TelegramError("Unauthorized", 401)
        return {"username": "shopbot", "id": 42}


def test_connection_test_success(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "TelegramAPI", FakeTelegramAPI)
    res = client.post("/api/settings/telegram/test", json={"token": "123:ABC"})
    assert res.status_code == 200
    assert res.get_json()["bot_username"] == "shopbot"


def test_connection_test_bad_token(client, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "TelegramAPI", FakeTelegramAPI)
    monkeypatch.setattr(FakeTelegramAPI, "fail", True)
    res = client.post("/api/settings/telegram/test", json={"token": "bad"})
    assert res.status_code == 400
    assert "Unauthorized" in res.get_json()["error"]


def test_connection_test_without_any_token(client):
    res = client.post("/api/settings/telegram/test", json={"token": ""})
    assert res.status_code == 400


# --- Account management ---

def test_account_wrong_current_password(client):
    res = client.post("/api/settings/account", json={
        "current_password": "wrong", "new_password": "newpass123"})
    assert res.status_code == 400
    # password unchanged: original still logs in
    import app as app_module
    with app_module.app.test_client() as c:
        assert c.post("/login", data={"username": "admin", "password": "admin123"}).status_code == 302


def test_account_password_change(client):
    res = client.post("/api/settings/account", json={
        "current_password": "admin123", "new_password": "newpass123"})
    assert res.status_code == 200
    import app as app_module
    with app_module.app.test_client() as c:
        assert c.post("/login", data={"username": "admin", "password": "newpass123"}).status_code == 302
        c.get("/logout")
        assert c.post("/login", data={"username": "admin", "password": "admin123"}).status_code == 200  # rejected


def test_account_username_change(client):
    res = client.post("/api/settings/account", json={
        "current_password": "admin123", "new_username": "boss"})
    assert res.status_code == 200
    import app as app_module
    with app_module.app.test_client() as c:
        assert c.post("/login", data={"username": "boss", "password": "admin123"}).status_code == 302


def test_account_short_password_rejected(client):
    res = client.post("/api/settings/account", json={
        "current_password": "admin123", "new_password": "abc"})
    assert res.status_code == 400


def test_account_nothing_to_change(client):
    res = client.post("/api/settings/account", json={"current_password": "admin123"})
    assert res.status_code == 400
