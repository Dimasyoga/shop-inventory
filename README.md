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
- Telegram bot with button-driven menus (see below)
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

### Prerequisites
- Python 3.14+ installed
- pip available

### Install Dependencies
```bash
pip install flask
```

### Database Setup
The database is created automatically on first run. `database.py` handles:
- Creating all tables if they don't exist
- Migrating new tables (`restock_batches`, `restock_items`) to existing databases
- Seeding default admin user (`admin` / `admin123`)

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

## 4. Workflow Explanations

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
