# AGENTS.md

Orientation for the next agent working on this repo. Read this, then skim
`README.md` for the product-level tour.

## What this is

A Flask + SQLite shop-inventory app (Python 3.14) with a Jinja/vanilla-JS web UI
and a long-polling Telegram bot. Currency is Indonesian Rupiah. UI + bot are
bilingual (English / Bahasa Indonesia).

## Architecture & where things live

| File | Responsibility |
|---|---|
| `app.py` | Flask routes, request validation, session/auth. Starts `BotPoller` in `__main__`. |
| `services.py` | Business logic shared by web routes **and** the bot. Each function takes an open sqlite3 connection, owns its transaction, and raises `ServiceError`/`NotFoundError`. |
| `telegram_bot.py` | Bot API client, screen renderers, stateful order/restock flows, and the `BotPoller` daemon thread. Must **not** import `app.py`. |
| `database.py` | Schema (`init_db`), idempotent migrations, `get_setting`/`set_setting`, DB connection. |
| `i18n.py` | `TRANSLATIONS` table (English source string → translation), `make_t(lang)`, calendar names. Shared by templates, `app.js`, and the bot. |
| `templates/*.html` | Jinja templates; `settings.html` holds Telegram/language/account config. |
| `static/js/app.js` | Client JS; `t(...)` mirrors the server translator. |
| `tests/` | pytest. `conftest.py` has `db_path` (temp DB), `client`, `insert` fixtures. |

## Key conventions (follow these)

- **Settings** are string key/value rows in the `settings` table. Read with
  `get_setting(db, key, default)`, write with `set_setting(db, key, value)`
  (does **not** commit — caller owns the transaction).
- **Timestamps** are stored as UTC `'YYYY-MM-DD HH:MM:SS'` strings (SQLite
  `CURRENT_TIMESTAMP`). Compare against `services._to_utc_str(dt)` output — fixed
  width, so lexical string comparison is chronological.
- **i18n:** any user-facing string is an English literal wrapped in `t(...)`.
  Add the Indonesian value to `TRANSLATIONS['id']` in `i18n.py`; missing keys
  fall back to English. Bot config (language, whitelist, token, timezone,
  thresholds) is re-read every poll cycle, so web-UI changes apply with no
  restart.
- **Migrations** live in `init_db()` and must be idempotent (guard with
  `PRAGMA table_info` / `sqlite_master` checks). New columns go both in the
  `CREATE TABLE` block *and* a guarded `ALTER TABLE` for existing DBs.
- **`ServiceError`** carries an English `template` + `params` for translation via
  `i18n.translate_error`; `str(e)` still yields English for logs.
- **The bot poller** advances its update offset even when handling an update
  throws, so a poison update never loops.

## Running things

```bash
bash start.sh            # or: python3 app.py  (serves http://localhost:5000)
source venv/bin/activate && python -m pytest -q   # full suite
```

Default login `admin` / `admin123`. `FLASK_DEBUG=1 ./start.sh` enables the
Werkzeug debugger (never on an untrusted network).

## Current work in progress: stale-order Telegram alerts

**Status: feature complete, all 152 tests pass, NOT yet committed.** `git status`
shows the modified files plus new `tests/test_alerts.py`.

The bot notifies every whitelisted user when an order sits in `draft` or
`confirmed` (payment confirmed) longer than a configurable threshold.

Design decisions (already implemented — do not re-litigate):
- **Threshold** is the `order_alert_hours` setting (default `24`; blank/`0`
  disables). Configured in Settings → Telegram Bot.
- **Staleness measured from `orders.updated_at`** (time in current state), so
  confirming a stale draft resets its clock.
- **Re-alert at most once per state:** once while draft, once more if it later
  stalls after confirmation. Tracked by the new `orders.alerted_status` column
  (`find_stale_orders` filters `alerted_status IS NULL OR != status`).
- **Delivery:** all whitelisted IDs, always on when the bot is enabled. An order
  is marked alerted only after ≥1 recipient received it, so transient send
  failures retry next scan.
- The poller scans every ~5 min (`BotPoller.alert_interval`, gated by
  `_maybe_check_alerts`), independent of the 25 s message poll.

Touch points: `database.py` (column + migration), `services.py`
(`find_stale_orders`, `mark_order_alerted`), `telegram_bot.py`
(`parse_alert_hours`, `BotConfig.alert_hours`, `send_stale_order_alerts`,
`BotPoller._maybe_check_alerts`), `app.py` (settings read/validate/save),
`settings.html` + `app.js` (the hours field), `i18n.py` (label + alert strings),
`README.md`.

Known constraint: Telegram bots cannot message a user who has never opened a chat
with the bot; such sends fail, are logged, and retry — the user gets the next
alert once they start the bot.

### Suggested next steps
- Commit on a feature branch (the user hadn't asked to commit yet — confirm first).
- Possible follow-ups if requested: a "repeat every threshold" reminder mode; a
  per-user opt-out; surfacing stale orders on the web dashboard too.
