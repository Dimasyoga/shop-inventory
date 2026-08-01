# AGENTS.md

Orientation for the next agent working on this repo. Read this, then skim
`README.md` for the product-level tour.

## What this is

A Flask + SQLite shop-inventory app (Python 3.13) with a Jinja/vanilla-JS web UI
and a long-polling Telegram bot. Currency is Indonesian Rupiah. UI + bot are
bilingual (English / Bahasa Indonesia).

## Architecture & where things live

| File | Responsibility |
|---|---|
| `app.py` | Flask routes, request validation, session/auth. `bootstrap()` does logging + `init_db()` + `BotPoller`. |
| `wsgi.py` | Gunicorn entrypoint. Calls `bootstrap()`; importing `app:app` directly would skip it. |
| `services.py` | Business logic shared by web routes **and** the bot. Each function takes an open sqlite3 connection, owns its transaction, and raises `ServiceError`/`NotFoundError`. |
| `reports.py` | Monthly audit report: collects a month's records and renders the PDF (fpdf2). Same connection/no-Flask rules as `services.py`; imports fpdf lazily so importing the module never requires it. |
| `telegram_bot.py` | Bot API client, screen renderers, stateful order/restock flows, monthly-report delivery, and the `BotPoller` daemon thread. Must **not** import `app.py`. |
| `database.py` | Schema (`init_db`), idempotent migrations, `get_setting`/`set_setting`, DB connection. |
| `i18n.py` | `TRANSLATIONS` table (English source string → translation), `make_t(lang)`, calendar names. Shared by templates, `app.js`, and the bot. |
| `templates/*.html` | Jinja templates; `settings.html` holds Telegram/language/account config. |
| `static/js/app.js` | Client JS; `t(...)` mirrors the server translator. |
| `tests/` | pytest. `conftest.py` has `db_path` (temp DB), `client`, `insert` fixtures. |

## Key conventions (follow these)

- **Settings** are string key/value rows in the `settings` table. Read with
  `get_setting(db, key, default)`, write with `set_setting(db, key, value)`
  (does **not** commit — caller owns the transaction). Keys listed in
  `database.ENCRYPTED_SETTINGS` (currently just `telegram_bot_token`) are held
  encrypted at rest as `enc:v1:…` and must go through `get_secret_setting` /
  `set_secret_setting` instead; adding a key to that tuple also migrates any
  existing plaintext value on the next `init_db()`. Reads tolerate un-prefixed
  legacy values, and a key mismatch returns `''` with an ERROR log rather than
  raising — losing the key must never take the web UI down.
- **Timestamps** are stored as UTC `'YYYY-MM-DD HH:MM:SS'` strings (SQLite
  `CURRENT_TIMESTAMP`). Compare against `services._to_utc_str(dt)` output — fixed
  width, so lexical string comparison is chronological.
- **i18n:** any user-facing string is an English literal wrapped in `t(...)` —
  including API error/warning text, which the browser shows verbatim in a toast
  (use `app._err('English source', **params)` instead of `jsonify({'error': ...})`).
  Add the Indonesian value to `TRANSLATIONS['id']` in `i18n.py`; missing keys
  fall back to English, and `tests/test_i18n_coverage.py` scans every call site
  to fail the suite when one is absent. Date/month labels come from
  `i18n.month_name` / `weekday_abbr`, never `strftime('%b')`. Bot config
  (language, whitelist, token, timezone, thresholds) is re-read every poll cycle,
  so web-UI changes apply with no restart.
- **Migrations** live in `init_db()` and must be idempotent (guard with
  `PRAGMA table_info` / `sqlite_master` checks). New columns go both in the
  `CREATE TABLE` block *and* a guarded `ALTER TABLE` for existing DBs. *Removing* a
  foreign-keyed column means rebuilding the table — SQLite rejects `DROP COLUMN` on
  one named in a constraint. Follow the categories-removal migration: rename the old
  table aside under `legacy_alter_table=ON` so the foreign keys in `order_items`,
  `stock_logs` and friends are not rewritten to follow it, recreate the table under
  its real name, copy rows **with their ids**, then verify with
  `PRAGMA foreign_key_check` and refuse to continue if anything dangles.
  `tests/test_migrations.py` builds an old-shaped database and asserts the outcome;
  extend it rather than trusting a destructive migration by inspection.
- **Corrections are reversing entries, never edits.** A mistyped restock or self-use
  batch is voided by `services.void_restock` / `void_self_use`, which write a second
  batch holding the negated figures and linked by `voids_batch_id`. That one column
  carries both halves: a batch *is* a void when its own is set, and *has been* voided
  when another points at it. Nothing is deleted or rewritten, so `sales_summary` and the
  monthly report net the pair out on their own and a month already reported keeps the
  totals it printed. Any new query over batches must skip rows whose batch has been
  voided — `services._surviving_restock_lines` is the predicate.
- **`cost_price` can only be un-blended from a snapshot.** `create_restock` records
  `restock_items.cost_before` per line; voiding restores it, but *only* when no later
  surviving batch has averaged onto it, since rebuilding the weighted average would need
  stock levels that sales have since moved. When it cannot, the product gets
  `products.cost_review_needed = 1` rather than an invented figure, and the Products page
  *Needs cost* chip (`services.NEEDS_COST`) asks for a human. Resist any change that
  makes the flag clear itself — only an explicit cost on the product form does, because
  a later restock blends onto the suspect base and inherits the doubt.
- **An open order holds its stock; `stock_qty` never moves until it is completed.**
  `products.reserved_qty` counts units promised to draft and confirmed orders, and what
  a new order may draw on is `services.AVAILABLE` (`stock_qty - reserved_qty`), never
  `stock_qty`. Keep the two apart: `stock_qty` is physical stock, which is what the
  dashboard, low-stock alerts, the restock weighted average and the monthly report all
  read, and none of them should shift because an order was typed. Hold with
  `_hold_stock` — the condition and the increment are one statement, because checking
  first and claiming after is the exact race this replaced. Completion decrements both
  columns together; cancelling releases. Releases clamp at zero, since a negative
  reservation would read as extra availability and hand out stock that isn't there.
- **Editing a draft releases every old line before taking the new ones**
  (`services.update_order`), rather than computing per-product deltas. That ordering is
  what lets an edit spend units it is itself giving up — dropping one line to add
  another of the last item, or just correcting 3 to 2 — and `db.rollback()` restores the
  old holds exactly when a new line cannot be met. Drafts only: a confirmed order has
  been paid for. Self use and stock adjustment still work on `stock_qty` directly and
  are *not* checked against reservations — they record something that already physically
  happened, so refusing them would only make the database wrong. A hold is a claim on
  stock, not a lock on the shelf, and `complete_order` keeps its `stock_qty` guard for
  exactly that case.
- **The login throttle buckets by client address, not username.** Five failures in
  15 minutes lock the bucket (`app._login_failures`), and a lockout refuses the
  *correct* password too — checking credentials first would make the throttle an
  oracle for whether a guess was right. Keying it on the username instead would
  hand anyone a way to lock the shop owner out of their own shop, which is why it
  is not. The map is process memory, justified by the one-worker rule below;
  `tests/conftest.py` clears it around every test, since the process outlives them
  and every test client shares one address.
- **`ServiceError`** carries an English `template` + `params` for translation via
  `i18n.translate_error`; `str(e)` still yields English for logs.
- **The bot poller** advances its update offset even when handling an update
  throws, so a poison update never loops.
- **Recurring bot work** hangs off `_cycle` behind a monotonic-deadline check
  (`_maybe_check_alerts`, `_maybe_send_report`), each with its own DB connection
  and a blanket `except Exception: log.exception(...)` so a failing job cannot kill
  the poller. Anything firing less often than the process restarts needs its
  progress **persisted** — the monthly report keys off a `last_report_period`
  settings row, because an in-memory deadline re-fires on every restart. A marker
  that is absent means "no history", and must plant itself without acting rather
  than backfilling.

## Running things

```bash
bash start.sh            # or: python3 app.py  (serves http://localhost:5000)
source venv/bin/activate && python -m pytest -q   # full suite
docker compose up -d --build   # deployment path; ./deploy.sh to update
```

Default login `admin` / `admin123` (override with `SHOP_ADMIN_USERNAME` /
`SHOP_ADMIN_PASSWORD` before first start). `FLASK_DEBUG=1 ./start.sh` enables the
Werkzeug debugger (never on an untrusted network).

## Deployment constraints (don't break these)

- **One worker, always.** `BotPoller` is an in-process thread holding a single
  `getUpdates` offset and the store is one SQLite file, so a second worker or
  replica duplicates every bot message and doubles stale-order alerts. Scale with
  `--threads`. This is why the Dockerfile pins `--workers 1`.
- **State lives outside the source tree.** `SHOP_DB_PATH` and
  `SHOP_SECRET_KEY_PATH` point into the `/data` volume. Keep `database.DB_PATH` a
  module-level name read at call time inside `get_db()` — `tests/conftest.py`
  monkeypatches it, and moving the lookup into a config object breaks the suite.
- **Anything a deployment must set goes in `.env.example`**; anything the shop
  owner should change while running stays a `settings` row (re-read every poll
  cycle, so no restart).
- `/healthz` is unauthenticated and returns untranslated JSON on purpose — it is
  the container healthcheck, and `t(...)` there would trip the i18n coverage test.
- New runtime assets must be vendored into `static/`, not pulled from a CDN: the
  shop's network may be offline. That covers the PDF fonts in `static/fonts/` as
  much as Chart.js — and they must stay Unicode-capable, since product names are
  user data and a Latin-1 font raises on the first curly quote.
- **Generated files never go in the source tree.** `reports.REPORT_DIR` follows the
  `database.DB_PATH` idiom (module-level, env-overridable, read at call time) and
  points into `/data` in Docker; `tests/conftest.py` redirects it to `tmp_path` so
  no test can leave a PDF behind.
