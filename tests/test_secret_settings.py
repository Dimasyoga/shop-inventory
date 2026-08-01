"""Encryption at rest for settings listed in database.ENCRYPTED_SETTINGS."""

import pytest
from cryptography.fernet import Fernet

import database


def raw_value(key):
    """The value as it actually sits on disk, bypassing decryption."""
    conn = database.get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def test_token_is_not_stored_in_plaintext(client, db_path):
    client.post("/api/settings/telegram", json={
        "enabled": True, "token": "123:SECRET-TOKEN", "whitelist": "1"})

    stored = raw_value("telegram_bot_token")
    assert stored.startswith(database.SECRET_PREFIX)
    assert "123:SECRET-TOKEN" not in stored


def test_round_trip_through_helpers(db_path):
    conn = database.get_db()
    database.set_secret_setting(conn, "telegram_bot_token", "123:ABC")
    conn.commit()
    assert database.get_secret_setting(conn, "telegram_bot_token") == "123:ABC"
    conn.close()


def test_blank_stays_blank(db_path):
    """'' means 'not configured' everywhere; encrypting it would make it truthy."""
    conn = database.get_db()
    database.set_secret_setting(conn, "telegram_bot_token", "")
    conn.commit()
    assert raw_value("telegram_bot_token") == ""
    assert database.get_secret_setting(conn, "telegram_bot_token") == ""
    conn.close()


def test_missing_key_returns_default(db_path):
    conn = database.get_db()
    assert database.get_secret_setting(conn, "telegram_bot_token") == ""
    assert database.get_secret_setting(conn, "nope", "fallback") == "fallback"
    conn.close()


def test_plaintext_value_is_migrated_on_init(db_path):
    """A database written by a pre-encryption version upgrades in place."""
    conn = database.get_db()
    database.set_setting(conn, "telegram_bot_token", "123:LEGACY")
    conn.commit()
    conn.close()

    database.init_db()

    assert raw_value("telegram_bot_token").startswith(database.SECRET_PREFIX)
    conn = database.get_db()
    assert database.get_secret_setting(conn, "telegram_bot_token") == "123:LEGACY"
    conn.close()


def test_migration_is_idempotent(db_path):
    conn = database.get_db()
    database.set_setting(conn, "telegram_bot_token", "123:LEGACY")
    conn.commit()
    conn.close()

    database.init_db()
    once = raw_value("telegram_bot_token")
    database.init_db()

    # Re-encrypting would still decrypt correctly but proves the guard failed.
    assert raw_value("telegram_bot_token") == once


def test_legacy_plaintext_still_readable_before_migration(db_path):
    """Reads must not break in the window before init_db() rewrites the row."""
    conn = database.get_db()
    database.set_setting(conn, "telegram_bot_token", "123:LEGACY")
    conn.commit()
    assert database.get_secret_setting(conn, "telegram_bot_token") == "123:LEGACY"
    conn.close()


def test_wrong_key_reads_as_unset_and_logs(db_path, monkeypatch, tmp_path, caplog):
    """A backup restored without .encryption_key must idle the bot, not crash the app."""
    conn = database.get_db()
    database.set_secret_setting(conn, "telegram_bot_token", "123:ABC")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "ENCRYPTION_KEY_PATH", str(tmp_path / "other.key"))

    conn = database.get_db()
    with caplog.at_level("ERROR"):
        assert database.get_secret_setting(conn, "telegram_bot_token") == ""
    conn.close()
    assert "cannot decrypt" in caplog.text


def test_bot_config_sees_decrypted_token(db_path):
    from telegram_bot import load_bot_config

    conn = database.get_db()
    database.set_setting(conn, "telegram_enabled", "1")
    database.set_secret_setting(conn, "telegram_bot_token", "123:ABC")
    conn.commit()

    cfg = load_bot_config(conn)
    conn.close()
    assert cfg.token == "123:ABC"
    assert cfg.enabled is True


def test_env_key_takes_precedence(db_path, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SHOP_ENCRYPTION_KEY", key)
    # Point the file elsewhere to prove the env value is what gets used.
    monkeypatch.setattr(database, "ENCRYPTION_KEY_PATH", str(tmp_path / "unused.key"))

    conn = database.get_db()
    database.set_secret_setting(conn, "telegram_bot_token", "123:ABC")
    conn.commit()
    assert database.get_secret_setting(conn, "telegram_bot_token") == "123:ABC"
    conn.close()

    assert not (tmp_path / "unused.key").exists()


def test_invalid_env_key_fails_loudly(db_path, monkeypatch):
    """A typo'd key is a fresh deployment mistake -- surface it immediately."""
    monkeypatch.setenv("SHOP_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        database._load_encryption_key()


def test_key_file_is_owner_only(db_path, monkeypatch, tmp_path):
    import os
    import stat

    path = tmp_path / "generated.key"
    monkeypatch.setattr(database, "ENCRYPTION_KEY_PATH", str(path))
    database._load_encryption_key()

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
