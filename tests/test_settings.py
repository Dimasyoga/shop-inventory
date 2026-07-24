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


def test_alert_hours_round_trip(client):
    res = client.post("/api/settings/telegram", json={
        "enabled": False, "whitelist": "1", "alert_hours": "12"})
    assert res.status_code == 200
    assert get_setting("order_alert_hours") == "12"


def test_blank_alert_hours_disables(client):
    client.post("/api/settings/telegram", json={"enabled": False, "whitelist": "1", "alert_hours": "24"})
    res = client.post("/api/settings/telegram", json={"enabled": False, "whitelist": "1", "alert_hours": ""})
    assert res.status_code == 200
    assert get_setting("order_alert_hours") == "0"


def test_negative_alert_hours_rejected(client):
    res = client.post("/api/settings/telegram", json={
        "enabled": False, "whitelist": "1", "alert_hours": "-5"})
    assert res.status_code == 400


def test_non_numeric_alert_hours_rejected(client):
    res = client.post("/api/settings/telegram", json={
        "enabled": False, "whitelist": "1", "alert_hours": "soon"})
    assert res.status_code == 400


def test_alert_hours_omitted_leaves_setting_unchanged(client):
    client.post("/api/settings/telegram", json={"enabled": False, "whitelist": "1", "alert_hours": "6"})
    client.post("/api/settings/telegram", json={"enabled": False, "whitelist": "1"})
    assert get_setting("order_alert_hours") == "6"


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


# --- Language ---

def test_language_defaults_to_english(client):
    # No setting stored yet: the picker shows English selected and English labels.
    html = client.get("/settings").get_data(as_text=True)
    assert 'value="en" selected' in html
    assert "Settings" in html and "Pengaturan" not in html


def test_language_round_trip_and_rerender(client):
    res = client.post("/api/settings/language", json={"language": "id"})
    assert res.status_code == 200
    assert get_setting("language") == "id"
    # Subsequent renders come back translated.
    html = client.get("/settings").get_data(as_text=True)
    assert 'value="id" selected' in html
    assert "Pengaturan" in html  # "Settings" heading, translated


def test_language_switch_translates_dashboard(client):
    client.post("/api/settings/language", json={"language": "id"})
    html = client.get("/").get_data(as_text=True)
    assert "Dasbor" in html          # nav + header
    assert "Total Produk" in html    # a stat label
    assert 'lang="id"' in html


def test_unsupported_language_rejected(client):
    res = client.post("/api/settings/language", json={"language": "fr"})
    assert res.status_code == 400
    assert get_setting("language") is None  # nothing persisted


def test_login_page_respects_language(client, db_path):
    # Set language while logged in, then hit the (logged-out) login page.
    client.post("/api/settings/language", json={"language": "id"})
    import app as app_module
    with app_module.app.test_client() as c:
        html = c.get("/login").get_data(as_text=True)
    assert "Masuk untuk mengelola toko Anda" in html


def test_service_error_translated_in_api_english(client):
    # Ordering a nonexistent product raises NotFoundError -> translated to English (identity).
    res = client.post("/api/orders", json={"items": [{"product_id": 999, "quantity": 1}]})
    assert res.status_code == 404
    assert res.get_json()["error"] == "Product 999 not found"


def test_service_error_translated_in_api_indonesian(client):
    client.post("/api/settings/language", json={"language": "id"})
    res = client.post("/api/orders", json={"items": [{"product_id": 999, "quantity": 1}]})
    assert res.status_code == 404
    assert res.get_json()["error"] == "Produk 999 tidak ditemukan"
