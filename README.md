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
- Sales dashboard with period-based analytics and trend charts
- Stock audit logging on every sale and restock
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

Manage the shop from Telegram: browse products, walk through creating orders and
restocks, confirm/complete/cancel orders, and check the sales summary — all via
tap-through inline menus.

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

To enable the Flask debugger during development: `FLASK_DEBUG=1 ./start.sh`
(never on a network you don't trust — the debugger allows code execution).

### Project Structure
```
shop-inventory/
├── app.py              # Flask routes + web-layer validation
├── services.py         # Business logic shared by web routes and the bot
├── telegram_bot.py     # Telegram bot: API client, menus, flows, poller
├── i18n.py             # Translation table + helpers (web UI, JS, bot)
├── database.py         # SQLite schema, migrations, settings, DB connection
├── shop.db             # SQLite database file
├── start.sh            # Startup script
├── static/
│   ├── css/style.css   # Application styles
│   └── js/app.js       # Client-side JavaScript
└── templates/
    ├── base.html       # Base layout with sidebar navigation
    ├── login.html      # Login page
    ├── dashboard.html  # Overview with stats and alerts
    ├── categories.html # Category CRUD
    ├── products.html   # Product catalog with stock management
    ├── orders.html     # Order creation and lifecycle
    ├── restock.html    # Batch restock with cost tracking
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
- Migrating new tables (`restock_batches`, `restock_items`) to existing databases
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
- **Financials**: Net profit (revenue − restock cost), total product value (price × stock), restock cost
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

#### Sales Dashboard (`/sales`)
- Period selector: Today, This Week, This Month, This Year, All Time
- **Summary stats**: Revenue, completed orders, unique SKUs, items sold, restock cost, net profit, product value
- **Trend chart**: Daily revenue line chart (Chart.js)
- **Top 3 / Bottom 3 sellers**: By quantity sold

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

`shop.db` and `.secret_key` live in the `shop-data` Docker volume mounted at
`/data`, deliberately outside the source tree: rebuilds, `git pull` and `git clean`
cannot touch them. Deleting that volume deletes the inventory.

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
