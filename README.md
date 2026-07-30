# Shop Inventory Management System

## 1. Repository Summary

A Flask-based shop inventory management system built with Python 3.14, SQLite, and Chart.js. It provides a complete solution for managing products, orders, stock, and sales analytics for small retail businesses.

### Tech Stack
- **Backend**: Python 3.14 + Flask 3.1.3
- **Database**: SQLite (file: `shop.db`)
- **Frontend**: Jinja2 templates + vanilla JavaScript
- **Charts**: Chart.js 4.4.7 (CDN)
- **Currency**: Indonesian Rupiah (Rp)

### Features
- User authentication (default: `admin` / `admin123`, hashed at rest; change it in Settings)
- Product catalog with categories, SKU, pricing, and stock tracking
- Order management with lifecycle: draft → confirmed → completed (or cancelled)
- Batch-level restock system with cost allocation
- Self-use tracking for stock the seller takes for themself (no revenue)
- Sales dashboard with period-based analytics and trend charts
- Monthly PDF audit report, archived on the server and pushed to Telegram (see below)
- Stock audit logging on every sale, restock, and self use
- Low stock alerts and reorder thresholds
- Telegram bot with button-driven menus and stale-order alerts (see below)
- Bilingual interface — English and Bahasa Indonesia (see below)

### Language

The web interface and the Telegram bot are available in **English** and
**Bahasa Indonesia**. Choose the language under **Settings → Language**; the
choice is a single shop-wide setting that applies to every user and to the bot
(bot changes take effect within one poll cycle, no restart needed). English is
the default.

Translations live in `i18n.py` as a single `English source string → translation`
table shared by the templates, the browser JavaScript (`app.js`), and the bot.
Adding a language means adding one entry to `LANGUAGES`, a mapping in
`TRANSLATIONS`, and its month/weekday names (`MONTHS`, `MONTHS_ABBR`,
`WEEKDAYS_ABBR`); any missing translation key falls back to the English source,
so a partial translation degrades gracefully. Date labels are built from those
calendar tables (bot) and the browser's `Intl` locale (web) rather than the
server locale, so month and weekday names are localized too.

### Telegram Bot

Manage the shop from Telegram: browse products, walk through creating orders,
restocks and self-use entries, confirm/complete/cancel orders, check the sales
summary, and pull the monthly report — all via tap-through inline menus.

Setup:
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. In the web UI, open **Settings → Telegram Bot**, paste the token, tick
   *Enable bot*, and Save (use *Test Connection* to verify the token).
3. Message your bot on Telegram. It replies "Not authorized" with your numeric
   Telegram ID — paste that ID into the **whitelist** field in Settings and Save.
4. Message the bot again: the main menu appears.

Only whitelisted Telegram user IDs can interact with the bot. Settings changes
apply within one poll cycle (~25 s) — no restart needed. The bot uses long
polling, so it works on a LAN with no public URL. Bot sales summaries use the
**shop timezone** configured in Settings (web-page summaries follow the
browser's timezone).

#### Stale-order alerts

The bot proactively messages every whitelisted user when an order stays stuck in
**draft** or **payment-confirmed** longer than a configurable threshold — an
early warning for orders that were never completed or cancelled. Set the
threshold under **Settings → Telegram Bot → Stale order alert threshold
(hours)** (default 24; `0` or blank disables it).

Staleness is counted from the last status change, so confirming a stale draft
resets its clock. Each order is alerted at most once per state: once while it
sits in draft, and once more if it later stalls after payment confirmation. The
poller scans for stale orders about every 5 minutes, independent of the message
poll cycle.

#### Monthly report delivery

When a calendar month closes, the bot renders that month's audit report, archives
it, and uploads the PDF to every whitelisted user with a caption carrying the
headline figures. Toggle it under **Settings → Telegram Bot → Send the monthly
report automatically**. Tap **📄 Monthly report** in the bot menu to pull any of
the last six closed months on demand.

The schedule is driven by a `last_report_period` settings row rather than a timer,
so it behaves sensibly around restarts:

- **A restart never resends.** The marker records the last month delivered; an
  in-memory deadline would reset and re-fire.
- **A fresh install never backfills.** With no marker yet, the last closed month
  is recorded as already handled, so the first report you receive covers the first
  month the shop actually ran with the feature.
- **Downtime is caught up in order.** A shop offline across several month
  boundaries gets each missed month, oldest first, capped at 12 per check.
- **A Telegram outage retries.** The marker only advances once at least one
  recipient has the file, so a failed send is re-attempted on the next check
  (hourly) rather than silently skipping the month. The archive copy is written
  either way.

To enable the Flask debugger during development: `FLASK_DEBUG=1 ./start.sh`
(never on a network you don't trust — the debugger allows code execution).

### Project Structure
```
shop-inventory/
├── app.py              # Flask routes + web-layer validation
├── services.py         # Business logic shared by web routes and the bot
├── reports.py          # Monthly audit report: data collection + PDF rendering
├── telegram_bot.py     # Telegram bot: API client, menus, flows, poller
├── i18n.py             # Translation table + helpers (web UI, JS, bot)
├── database.py         # SQLite schema, migrations, settings, DB connection
├── shop.db             # SQLite database file
├── reports/            # Archived monthly report PDFs (gitignored)
├── start.sh            # Startup script
├── static/
│   ├── css/style.css   # Application styles
│   ├── fonts/          # Vendored DejaVu TTFs for PDF rendering
│   └── js/app.js       # Client-side JavaScript
└── templates/
    ├── base.html       # Base layout with sidebar navigation
    ├── login.html      # Login page
    ├── dashboard.html  # Overview with stats and alerts
    ├── categories.html # Category CRUD
    ├── products.html   # Product catalog with stock management
    ├── orders.html     # Order creation and lifecycle
    ├── restock.html    # Batch restock with cost tracking
    ├── selfuse.html    # Seller's own consumption (stock out, no revenue)
    ├── sales.html      # Sales analytics dashboard
    └── settings.html   # Language, Telegram bot config + account management
```

---

## 2. Setup

> Deploying this for real? Skip to [section 4, Deployment](#4-deployment). This
> section covers local development only.

### Prerequisites
- Python 3.13+ installed
- pip available

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Database Setup
The database is created automatically on first run. `database.py` handles:
- Creating all tables if they don't exist
- Migrating new tables (`restock_batches`, `restock_items`, `self_use_batches`,
  `self_use_items`) to existing databases
- Seeding the first user (`admin` / `admin123`, or `SHOP_ADMIN_USERNAME` /
  `SHOP_ADMIN_PASSWORD` if set — only ever on an empty database)

### Database Schema
| Table | Purpose |
|---|---|
| `users` | Authentication (username, password) |
| `categories` | Product categories |
| `products` | Product catalog (SKU, price, stock, threshold) |
| `stock_logs` | Stock adjustment audit trail |
| `orders` | Order header (status, total) |
| `order_items` | Order line items |
| `restock_batches` | Restock batch header (total cost per batch) |
| `restock_items` | Restock line items (product, qty, allocated cost) |
| `self_use_batches` | Self-use batch header (total retail value per batch) |
| `self_use_items` | Self-use line items (product, qty, price snapshot, subtotal) |

---

## 3. Running the Application

### Start the Server
```bash
bash start.sh
```
Or directly:
```bash
python3 app.py
```

The server starts at `http://localhost:5000`. Default login: **admin** / **admin123**.

### Page Guide

#### Dashboard (`/`)
- **Stats**: Total products, total orders, low stock count, this month's revenue
- **Financials**: Net profit (revenue − restock cost), total product value (price × stock), restock cost, self use
- **Recent Orders**: Last 5 orders with status and amount
- **Low Stock Alerts**: Products at or below reorder threshold

#### Categories (`/categories`)
- List, create, edit, and delete product categories
- Cannot delete a category that has products assigned

#### Products (`/products`)
- Search by name/SKU, filter by category
- Add/edit products with name, SKU, category, price, stock qty, reorder threshold
- Stock adjustment modal for manual corrections (shows warning about cost accuracy)
- Archive products (soft delete) instead of permanent deletion

#### Orders (`/orders`)
- Search by order number, filter by status
- Create orders by selecting products and quantities
- View order details in a modal
- 3-step lifecycle: draft → confirmed (payment) → completed (stock deducted)
- Cancel orders (except completed ones)

#### Restock (`/restock`)
- Add multiple products per restock batch
- Single total cost per batch, allocated proportionally by quantity
- Expandable history: click a batch row to see product breakdown
- Period filter: Today, This Week, This Month, All Time

#### Self Use (`/self-use`)
- Records stock the seller takes for themself: one product or several per batch
- Reduces stock and writes a `stock_logs` row, exactly like a sale — but creates
  no order and no revenue
- Each line is valued at the product's retail price **at the time of entry**, so
  later price edits never move historical figures
- **Self use does not affect revenue or net profit.** The money was already
  booked as restock spend when the goods were bought, so deducting their value
  from profit again would double-count. It is reported as its own metric on the
  dashboard, the sales page, and the bot summary
- Expandable history with the same period filters as Restock

#### Sales Dashboard (`/sales`)
- Period selector: Today, This Week, This Month, This Year, All Time
- **Summary stats**: Revenue, completed orders, unique SKUs, items sold, restock cost, net profit, product value, self use
- **Trend chart**: Daily revenue line chart (Chart.js)
- **Top 3 / Bottom 3 sellers**: By quantity sold
- **Monthly Report**: Pick a month, then **Download PDF** or **Send to Telegram**

#### Monthly Report

A PDF for one calendar month, for audit:

- **Page 1** — the month's sales performance: revenue, completed orders, unique
  SKUs, items sold, restock cost, net profit, self use, current stock value, plus
  top 3 / bottom 3 sellers
- **Sales Records** — every completed order, one row per product sold, with unit
  price and subtotal
- **Restock Records** — every batch, one row per product, with allocated cost
- **Self Use Records** — every batch, one row per product, at the recorded price

Notes:

- Month boundaries follow the **shop timezone** from Settings, not the browser's,
  so the archived file and the copy the bot sends always describe the same period
- Written to `SHOP_REPORT_DIR` (`/data/reports` in Docker) as
  `shop-report-YYYY-MM.pdf`, **overwritten** on regeneration rather than duplicated
- The picker offers the current month too, as a month-to-date snapshot; the
  scheduled report only ever covers closed months
- Text is rendered with vendored DejaVu TTFs (`static/fonts/`) rather than a
  built-in Latin-1 font, so a product name with a curly quote or an emoji cannot
  break the report

---

## 4. Deployment

Docker is the supported deployment path. `start.sh` and `python3 app.py` run the
Werkzeug development server and are for local development only.

### First-time setup

```bash
cp .env.example .env
# Edit .env and set SHOP_ADMIN_PASSWORD before the first start.
docker compose up -d --build
```

The app is then on `http://<machine-ip>:5000` (change with `HOST_PORT` in `.env`).
It restarts automatically after a reboot.

Configure the Telegram bot afterwards in the web UI at **Settings** — the token,
chat whitelist, timezone and stale-order threshold are database settings, re-read
every poll cycle, so they apply without a restart.

### Updating after you push a new feature

```bash
./deploy.sh
```

That backs up the database, pulls, rebuilds, restarts, and waits for the health
check — failing loudly with logs if the new version doesn't come up.

### Where the data lives

`shop.db`, `.secret_key` and the `reports/` archive live in the `shop-data` Docker
volume mounted at `/data`, deliberately outside the source tree: rebuilds,
`git pull` and `git clean` cannot touch them. Deleting that volume deletes the
inventory.

### Backups

```bash
./backup.sh              # writes backups/shop-<timestamp>.db
```

Uses SQLite's online backup API, so it is safe to run while the shop is using the
app — unlike `cp`, which can capture a torn file. `backup.sh` prints the restore
command. The Telegram bot token inside is encrypted and the key is not included,
so a backup that goes astray does not leak the bot (see
[Secrets at rest](#secrets-at-rest)). Worth putting in cron:

```
0 22 * * * cd /path/to/shop-inventory && ./backup.sh >> backups/backup.log 2>&1
```

`backup.sh` copies **only the database**. Monthly report PDFs are regenerable from
it at any time, so they are not backed up — but if a signed-off PDF is your audit
record of record, copy `/data/reports` separately.

### Secrets at rest

The Telegram bot token is encrypted (Fernet) before it is written to the
`settings` table, stored as `enc:v1:…`. An existing database with a plaintext
token is upgraded in place on the next start — nothing to do by hand.

The key comes from `SHOP_ENCRYPTION_KEY`, or is generated once into
`/data/.encryption_key` (mode `0600`). **`backup.sh` copies only `shop.db`, so
the key is never inside a backup** — that is the point: a `.db` file that ends up
on a laptop or in cloud storage cannot be decrypted.

This protects copies of the database. It cannot protect against someone who can
already read the server or the container, since the app itself must be able to
decrypt — that is inherent, not a gap in the setup.

**Losing the key is recoverable, not fatal.** If the database is restored without
its key, the app logs `cannot decrypt setting 'telegram_bot_token'`, the bot
idles as though unconfigured, and the web UI keeps working normally — re-enter
the token in Settings. To avoid that entirely, set `SHOP_ENCRYPTION_KEY` in
`.env` and keep a copy in a password manager:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Configuration

All variables are documented in `.env.example`. Summary:

| Variable | Default | Purpose |
|---|---|---|
| `SHOP_ADMIN_USERNAME` / `SHOP_ADMIN_PASSWORD` | `admin` / `admin123` | Seeds the first user; only applies to an empty database |
| `SHOP_SECRET_KEY` | auto-generated | Session signing key; persisted to `/data` if unset |
| `SHOP_ENCRYPTION_KEY` | auto-generated | Encrypts the bot token at rest; persisted to `/data` if unset |
| `SHOP_DB_PATH` / `SHOP_SECRET_KEY_PATH` / `SHOP_ENCRYPTION_KEY_PATH` | `/data/...` | State locations |
| `SHOP_REPORT_DIR` | `/data/reports` | Where monthly report PDFs are archived |
| `HOST_PORT` | `5000` | Host port to publish |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `SHOP_ENABLE_BOT` | `1` | Set `0` to run the web UI without the Telegram poller |

### Operational notes

- **Health check:** `GET /healthz` (unauthenticated) returns `{"status":"ok"}`.
  Compose uses it, and `docker compose ps` will show `(healthy)`.
- **Exactly one worker, by design.** The Telegram poller is an in-process thread
  holding a single `getUpdates` offset, and the store is one SQLite file. A second
  worker or replica duplicates every bot message and doubles the stale-order
  alerts. Scale with `--threads`, never `--workers`.
- **This is a LAN deployment.** There is no TLS and session cookies are not marked
  `Secure`. Don't port-forward it to the internet without putting a reverse proxy
  with HTTPS in front and setting `SESSION_COOKIE_SECURE`.
- **Never set `FLASK_DEBUG`** on a deployed instance — it exposes the Werkzeug
  console, which is remote code execution for anyone who can reach the port.

### Logs

```bash
docker compose logs -f app
```

---

## 5. Workflow Explanations

### Creating an Order

1. Navigate to **Orders** page, click **+ New Order**
2. Click **+ Add Item** for each product
3. Select product from dropdown (shows current stock), enter quantity
4. Subtotal and grand total update automatically
5. Click **Create Order** → order saved as **draft**
6. When payment is received, click ✅ → order becomes **confirmed** (payment confirmed, no stock deducted)
7. When items are delivered, click 💰 → order becomes **completed** (stock deducted from inventory)
8. Only **completed** orders count toward sales revenue and dashboard stats

### Adding Products

1. Navigate to **Products** page, click **+ Add Product**
2. Fill in:
   - **Name** (required): Product display name
   - **SKU** (optional): Unique stock-keeping unit identifier
   - **Category**: Assign to an existing category
   - **Price** (required): Selling price in Rupiah
   - **Stock Qty**: Initial inventory count
   - **Reorder Threshold**: Stock level that triggers low-stock alert
3. Click **Save** → product appears in catalog
4. To edit, click ✏️ icon; to adjust stock directly, click 📊 icon (use Restock page for normal additions to maintain cost accuracy)
5. To archive, click 🗑️ → product is hidden but data preserved

### Restocking Inventory

1. Navigate to **Restock** page
2. Click **+ Add Product** for each product to restock
3. For each row: select product, enter quantity
4. After all products are added, enter **Total Restock Cost** (one value for the entire batch)
5. Click **Submit Restock**
6. The system:
   - Creates a batch record with the total cost
   - Allocates cost proportionally: `allocated_cost = (qty / total_qty) × total_cost`
   - Updates each product's stock quantity
   - Records one row per product in `restock_items`
7. Restock cost appears in dashboard and sales dashboard, used for net profit calculation
8. History shows batches; click any row to expand and see product-level breakdown

### Recording Self Use

1. Navigate to **Self Use** page
2. Click **+ Add Product** for each product taken; select product and quantity
3. **Total Value** updates live from the products' retail prices — there is
   nothing to type, since no money changes hands
4. Click **Submit Self Use**
5. The system:
   - Creates a batch record with the total retail value
   - Decrements each product's stock (rejecting the whole batch if any line
     would go below zero)
   - Records one row per product in `self_use_items` with the price snapshot
   - Writes a negative `stock_logs` row with reason `self use batch #N`
6. The value appears as its own dashboard card and never changes revenue,
   restock cost, or net profit
7. History shows batches; click any row to expand and see product-level breakdown

### Generating the Monthly Report

Automatically, once a month closes:

1. The bot poller checks hourly whether the last closed month has been reported
2. If not, it renders the PDF, writes it to `SHOP_REPORT_DIR`, and uploads it to
   every whitelisted Telegram ID with a summary caption
3. `last_report_period` advances only once at least one recipient has the file, so
   a Telegram outage is retried rather than skipped

By hand, for any month:

1. Go to **Sales → Monthly Report**, pick a month
2. **Download PDF** streams it to your browser; **Send to Telegram** pushes it to
   the whitelist. Either way the archive copy is written
3. Or in the bot: **📄 Monthly report**, then pick one of the last six months

Every path calls the same `reports.build()`, so a report is identical no matter
who asked for it or how.

### Order Lifecycle Diagram

```
Draft ──[Confirm Payment]──> Confirmed ──[Complete/Deliver]──> Completed
  │                              │
  └────[Cancel]─────────────────> Deleted
                                  │
                                  └─> (No stock impact)

Confirmed ──[Cancel]──────────> Deleted
                                │
                                └─> (No stock impact)

Completed ──[Cannot Cancel]──> (Final state)
```

### Restock Cost Flow

```
Batch Restock (total: Rp 500,000)
├── Product A: 10 units → allocated Rp 200,000
├── Product B: 15 units → allocated Rp 300,000
└── Product C:  5 units → allocated Rp 100,000

Net Profit = Revenue − Restock Cost (from restock_batches)
```

### Stock Movements at a Glance

| Action | Stock | Money recorded | In net profit? |
|---|---|---|---|
| Order completed | ↓ | Revenue (`orders.total_amount`) | Yes, as revenue |
| Restock | ↑ | Cost (`restock_batches.total_cost`) | Yes, subtracted |
| Self use | ↓ | Retail value (`self_use_batches.total_value`) | **No** — reported separately |
| Stock adjust | ↕ | none | No |
