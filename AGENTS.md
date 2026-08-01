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
| `tests/browser/` | Playwright against a real server. `conftest.py` has `live_server`, `shop` (seeding + stock assertions) and a signed-in `page`. |

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
- **Every stock movement records who caused it.** `stock_logs.actor` is
  `web:<username>` from a route (`app._actor()`), `telegram:<chat_id>` from the bot
  (`telegram_bot._actor(chat_id)`), or `services.ACTOR_SYSTEM` for anything with no
  request behind it. The five service functions that write a movement take a
  keyword-only `actor=ACTOR_SYSTEM`; the default is deliberately honest rather than
  convenient, because crediting an unattributed call to the admin would make the
  column worse than useless the one time somebody reads it. Rows from before the
  column stay `NULL` and are **not** backfilled, so the history shows those as unknown
  rather than inventing an actor. The Stock History page (`/stock-history`,
  `services.list_stock_movements`) is what reads the table back. `reason` is stored
  English written at the time of the movement and is displayed **as recorded, never
  translated** — it is a record of what happened, and it is not a `t(...)` call site.
- **Lists are paged, and a page costs a fixed number of queries.** `list_orders`
  returns `(orders, has_more)` where each order is a dict already carrying its `items`,
  fetched for the whole page in one `IN` query — never one per row. `GET /api/orders`
  returns `{orders, has_more, page}`, and a single order comes from
  `GET /api/orders/<id>`: pulling the list and searching it client-side breaks the
  moment the order is not on the page being held. Order by `created_at DESC, id DESC`,
  not `created_at` alone — the column is second-resolution, and an unstable tiebreak
  lets a row cross the page boundary between requests and be shown twice or skipped.
  `tests/test_orders_pagination.py` pins the query count with sqlite3's trace hook.
- **Indexes live in one `executescript` at the very end of `init_db()`**, after every
  migration, because several cover columns the `ALTER TABLE` blocks above add — build
  them earlier and an upgrade of an old database fails on a column that does not exist
  yet. `CREATE INDEX IF NOT EXISTS` makes the block idempotent on its own, so it needs
  no `PRAGMA` guard. SQLite indexes primary keys and UNIQUE columns by itself but *not*
  foreign keys, which is what every one of these is for. `tests/test_indexes.py` asserts
  on `EXPLAIN QUERY PLAN`, not on timings: the shop's own database is small enough that
  a scan is invisible, so an index the planner declines to use would cost writes and buy
  nothing with no test noticing. Adding one means adding the plan assertion that
  justifies it. `stock_logs` carries exactly one index, `(product_id)`, and that is a
  ceiling rather than a starting point: it is the table every sale, restock and self use
  writes to. It stays that narrow because an index entry carries the rowid and
  `stock_logs.id` *is* the rowid, so `(product_id)` alone already orders a product's
  movements the way the history page reads them — `ORDER BY id DESC`, never
  `created_at DESC`, which would buy a sort and a wider index for nothing. The
  unfiltered history has no index at all on purpose: descending rowid walks the table
  backwards and stops at the page size.
- **`api()` in `app.js` rejects on any non-2xx, so every caller needs a `.catch`.**
  Write `api(...).then(d => ...).catch(err => showToast(err.message, 'error'))`.
  `.then(d => d.success ? ... : showToast(d.error))` is the trap: `api()` throws rather
  than resolving with the error body, so that failure branch is unreachable and the
  rejection goes nowhere. Eight write paths were written that way and silently swallowed
  every refusal the server made — a rejected order edit left the modal sitting open with
  no explanation. `tests/browser/test_error_reporting_ui.py` pins the behaviour; the
  server-side tests cannot see it, because the API was returning a correct 400 the whole
  time. Anything rendering into the page rather than acting on the result should use
  `fetchJson`, which toasts on failure by itself.
- **UI behaviour is tested in `tests/browser/`, in a real browser.** `static/js/app.js`
  is a thousand lines that no Flask test client can reach: paging, modal state and
  downloads are all client-side, and three features once shipped with a green suite and
  had to be clicked by hand before anyone knew they worked. The server there is a
  **subprocess**, because `tests/conftest.py` monkeypatches `database.DB_PATH` and a
  threaded server would share that global. Sign-in mints a session cookie rather than
  driving the login form — it keeps the suite off the login throttle, which buckets by
  address and would lock out after five tests. Reach for these whenever a change lands
  in `app.js` or a template; assert on what the reservation columns did afterwards
  (`shop.stock_of`), not just on what the page says.
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
  exactly that case. Because `reserved_qty` is a counter and the open order lines are
  what justify it, `services.reservation_drift` recomputes and compares the two;
  `repair_reservations` resets the counter and returns the *before* state so the caller
  can report what changed. Startup logs drift and never repairs it, and the two are
  separate actions in Settings on purpose — the figure says what customers were
  promised, so it gets shown before anything rewrites it. Resist making repair
  automatic for the same reason `cost_review_needed` does not clear itself.
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
ruff check .             # config in ruff.toml; CI fails on a finding
./backup.sh              # backups/shop-<stamp>.db; ./restore.sh <file> puts one back
source venv/bin/activate && python -m pytest -q   # full suite, browser tests included
playwright install chromium    # one-off, before the browser tests can run
python -m pytest -q -m "not browser"   # skip them if you have not
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
