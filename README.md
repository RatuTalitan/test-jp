# Jan Purna (जन पूर्णा) — Cotton Seed Oil Cake Marketplace

> **JP** — A Telegram-bot-based single-vendor ordering marketplace for cotton seed oil cake (cattle feed). Built for elderly rural users in India with bilingual Hindi + English support.

[![Tests](https://img.shields.io/badge/tests-632%20passing-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/)
[![PTB](https://img.shields.io/badge/python--telegram--bot-v21%2B-blue)](https://python-telegram-bot.org/)

---

## Table of Contents

1. [Overview](#overview)
2. [Features](#features)
3. [Architecture](#architecture)
4. [Tech Stack](#tech-stack)
5. [Project Layout](#project-layout)
6. [Prerequisites](#prerequisites)
7. [Local Development Setup](#local-development-setup)
8. [Configuration Reference](#configuration-reference)
9. [Running the Bot](#running-the-bot)
10. [Bot Commands & Flows](#bot-commands--flows)
11. [Running Tests](#running-tests)
12. [Backup & Restore](#backup--restore)
13. [Deployment](#deployment)
14. [Phase 2 Roadmap](#phase-2-roadmap)
15. [Spec Docs](#spec-docs)

---

## Overview

Jan Purna (जन पूर्णा) is a single-vendor marketplace delivered as a **Telegram bot**. It was built for a cotton seed oil cake business where:

- The **Seller** (Admin) lists products, manages inventory, processes payments, and fulfills orders.
- **Customers** (primarily elderly, rural, Hindi-speaking) browse products, build a cart, place orders, pay via UPI, and track pickup status — all inside Telegram.

**Why Telegram?** — Telegram's free messaging eliminates SMS/OTP costs, elderly users can send voice messages natively, and notifications are free. No payment gateway fees either — manual UPI + QR code with the Seller verifying the transaction reference.

---

## Features

### Customer features
- 🛍️ Browse products by category with availability (in stock / out of stock)
- 🛒 Add to cart with MOQ validation, combine-on-add, and stock checks
- 📦 Place orders → receive UPI QR + address → submit UTR (transaction reference)
- 📋 View order history and real-time status
- ❌ Cancel pre-approval orders
- 🌐 Switch between **Hindi** and **English** — preference saved per user
- 🎤 Voice message support (acknowledged with current-step re-prompt)

### Seller / Admin features
- 📂 Manage product catalog (categories, products, pricing, stock, availability)
- ✅ Review submitted payments (UTR + optional screenshot)
- 🔒 Approve/reject payments with fulfillability warnings (stock conflict flags, never blocking)
- 📍 Mark orders Ready for Pickup (with pickup location notification) and Completed
- ⚡ Offline-payment bypass: trust selected customers to pay outside the app
- ✏️ Edit order contents post-placement (pre-approval), with stock-delta adjustments and audit trail
- ⚙️ Set/update pickup location, UPI address, and UPI QR image at runtime
- 👥 Manage customer offline-payment flags

### Platform features
- 🔁 Bilingual message catalog (Hindi default, English available, easily extensible)
- 🔐 All secrets stored in environment variables — nothing committed
- 📊 635+ automated tests (unit, integration, Hypothesis property-based)
- 🗄️ PostgreSQL with Alembic migrations (SQLite for local dev/tests)
- 🔔 Notification retry worker (up to 3 retries, 30s apart, idempotent)
- 💾 Backup/restore scripts with cron scheduling
- 🔗 Webhook-ready (switch from long-poll to webhook with one env var)

---

## Architecture

```
Telegram Cloud (free)
        │
        ▼ long-poll (v1) / webhook (Phase 2)
┌──────────────────────────────────────────────────────────┐
│  Application Process (modular monolith)                  │
│                                                          │
│  Bot_Interface ──► Auth_Service                          │
│  (router.py)   ──► Catalog_Service                       │
│  ConversHndlr  ──► Cart_Service                          │
│                ──► Order_Service (+ State Machine)        │
│                ──► Payment_Service                        │
│                ──► Admin_Console                          │
│                ──► Fulfillability_Engine                  │
│                ──► Notification_Service (retry worker)    │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────┐
│  PostgreSQL (free tier)         │
│  + Object Storage (screenshots) │
└─────────────────────────────────┘
```

**One DB transaction per inbound Telegram update** — all service calls within one update commit or roll back atomically. Domain services are channel-agnostic; only `bot_interface/` touches the Telegram SDK.

---

## Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11 | |
| Bot framework | python-telegram-bot v21+ | Long-poll + webhook, ConversationHandler |
| Database | PostgreSQL (runtime) / SQLite (dev) | SQLAlchemy 2 + psycopg v3 |
| Migrations | Alembic | Additive-only schema evolution |
| Testing | pytest + Hypothesis | 635+ tests, property-based invariants |
| Hosting (v1) | Oracle Always-Free VM / Fly.io / Railway | ₹0–₹420/mo |
| Object storage | Cloudflare R2 / Supabase / Backblaze B2 | Free tiers, no egress fees |
| Cost (v1) | ≈ ₹0 – ₹420/mo | Telegram is free; DB and compute have free tiers |

---

## Project Layout

```
jan-purna/
├── src/marketplace/
│   ├── main.py               # ← Application entrypoint (run this)
│   ├── bot_interface/
│   │   ├── router.py         # ConversationHandler — all Telegram flows
│   │   ├── channel.py        # MessagingChannel interface + Renderer + FakeChannel
│   │   ├── telegram_channel.py  # Real Telegram adapter (PTB)
│   │   ├── update_source.py  # LongPollSource / WebhookSource
│   │   ├── voice.py          # Voice message baseline handler
│   │   ├── presentation.py   # setMyCommands, language switch, onboarding
│   │   └── i18n.py           # Bilingual Message_Catalog (Hindi + English)
│   ├── auth/                 # Auth_Service (contact, roles, lang pref, offline flag)
│   ├── catalog/              # Catalog_Service (products, categories, browsing)
│   ├── cart/                 # Cart_Service (add/change/remove/view/clear)
│   ├── order/
│   │   ├── service.py        # OrderService (placement, approval, cancel, modify, status)
│   │   └── state_machine.py  # Guarded transition() function + allowed-transition table
│   ├── payment/
│   │   ├── service.py        # PaymentService (UTR submission, screenshot, UPI instructions)
│   │   └── object_store.py   # ObjectStore port + InMemoryObjectStore
│   ├── fulfillability/
│   │   └── engine.py         # is_fulfillable(), shortfalls(), recompute_for_product_change()
│   ├── notification/
│   │   ├── service.py        # NotificationService (enqueue)
│   │   └── sender.py         # MessageSender port + retry worker
│   ├── admin/
│   │   └── service.py        # AdminConsole (settings, pending verifications, active orders)
│   ├── domain/
│   │   ├── entities.py       # Channel-neutral domain dataclasses
│   │   ├── results.py        # Typed result objects (Rejected, NotFound, etc.)
│   │   ├── repositories.py   # Repository Protocol interfaces + UnitOfWork
│   │   ├── memory.py         # In-memory repository implementations (for tests)
│   │   ├── money.py          # Monetary_Rounding utility (Decimal, ROUND_HALF_UP)
│   │   └── serialization.py  # Versioned domain serialization (format_version)
│   ├── db/
│   │   ├── models.py         # SQLAlchemy ORM models (single source of truth)
│   │   ├── repositories.py   # SQLAlchemy repository implementations
│   │   └── session.py        # make_engine / make_session_factory
│   └── config/
│       ├── loader.py         # Environment-based config (fails fast, masks secrets)
│       └── branding.py       # Brand strings: "Jan Purna", "जन पूर्णा", "JP"
├── migrations/               # Alembic migration files
│   ├── env.py
│   └── versions/
│       ├── 0001_initial_schema.py        # users, categories, products
│       ├── 0002_carts_orders.py          # carts, orders, order_items
│       ├── 0003_payments_audit_notifications.py
│       └── 0004_seller_settings.py
├── scripts/
│   ├── backup.py             # Database backup script (pg_dump / shutil.copy)
│   ├── restore.py            # Database restore + timing assertion
│   ├── backup.cron           # Cron/systemd timer config (every 12h)
│   └── README.md             # Backup/restore operator docs
├── tests/
│   ├── unit/                 # Fast unit + example tests (in-memory UoW)
│   ├── integration/          # DB migration tests + router wiring tests
│   ├── properties/           # Hypothesis property-based tests
│   ├── test_backup_restore.py
│   └── conftest.py           # Hypothesis profiles (max_examples=100 floor)
├── .env.example              # Config key names (no values — copy to .env)
├── alembic.ini               # Alembic config (URL injected at runtime)
└── pyproject.toml            # Dependencies + pytest/black/isort config
```

---

## Prerequisites

- **Python 3.11** (`pyenv install 3.11.15` or your OS package manager)
- **PostgreSQL 14+** (for production; SQLite is used automatically for local dev/tests)
- **A Telegram Bot** — create one via [@BotFather](https://t.me/BotFather) and get the `BOT_TOKEN`
- **Your Telegram User ID** — send a message to [@userinfobot](https://t.me/userinfobot) to find your `SELLER_TELEGRAM_ID`
- (Optional) An **object storage bucket** for payment screenshots (Cloudflare R2 free tier recommended)

---

## Local Development Setup

```bash
# 1. Clone the repository
git clone https://github.com/RatuTalitan/test-jp.git
cd test-jp

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows

# 3. Install dependencies (includes test tools)
pip install -e ".[test]"

# 4. Copy the example environment file and fill in your values
cp .env.example .env
# Edit .env — at minimum set BOT_TOKEN, SELLER_TELEGRAM_ID
# For local dev with SQLite: DB_URL=sqlite:///./local-dev.db

# 5. Run all tests to verify everything is working
pytest
```

The test suite uses an **in-memory SQLite database** — no Postgres needed for tests.

---

## Configuration Reference

Copy `.env.example` to `.env` and fill in the values. **Never commit `.env`**.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ Yes | Telegram bot token from BotFather |
| `DB_URL` | ✅ Yes | Database URL, e.g. `postgresql://user:pass@host:5432/db` or `sqlite:///./local-dev.db` |
| `OBJECT_STORE_KEY` | ✅ Yes | Object storage credential (for payment screenshots) |
| `VERIFIED_CONTACT_ENCRYPTION_KEY` | ✅ Yes | 32-character key for encrypting phone numbers at rest |
| `SELLER_TELEGRAM_ID` | ✅ Yes | Your Telegram user ID (integer) — this account gets Admin privileges |
| `USE_WEBHOOK` | No | Set to `true` to use webhooks instead of long-polling (default: `false`) |
| `WEBHOOK_SECRET_TOKEN` | Only if `USE_WEBHOOK=true` | Secret token for webhook authentication |
| `WEBHOOK_URL` | Only if `USE_WEBHOOK=true` | Public HTTPS URL for your webhook endpoint |
| `PORT` | No | Port for webhook server (default: `8080`) |
| `UPI_ADDRESS` | No | Seed value for UPI VPA (Seller can update via `/settings`) |
| `UPI_QR_OBJECT_KEY` | No | Seed value for UPI QR object storage key |
| `UTR_PATTERN` | No | Regex for valid UTR (default: `^[A-Za-z0-9]{12}$`) |
| `BACKUP_DIR` | No | Directory for database backups (default: `./backups/`) |
| `BACKUP_RETENTION_DAYS` | No | Days to keep backups (default: `30`) |

---

## Running the Bot

### Option 1: Long-polling (recommended for v1 / local dev)

```bash
# Make sure .env is filled in
source .venv/bin/activate
python -m marketplace.main
```

On startup the bot will:
1. Load and validate all config (fails fast with a clear error if anything is missing)
2. Run any pending Alembic migrations automatically
3. Seed the `seller_settings` row if it doesn't exist yet
4. Register the command menu and start long-polling

### Option 2: Webhook (for production scale)

```bash
export USE_WEBHOOK=true
export WEBHOOK_URL=https://your-domain.com/bot
export WEBHOOK_SECRET_TOKEN=your-secret-token
export PORT=8080
python -m marketplace.main
```

**Switch between modes with a single env var — no code changes required.**

---

## Bot Commands & Flows

### Customer commands

| Command | Description |
|---|---|
| `/start` | Onboarding — introduces Jan Purna brand, requests Telegram contact |
| `/browse` | Browse product catalog by category |
| `/cart` | View cart, remove items, go to checkout |
| `/checkout` | Place order from cart → receive UPI payment instructions |
| `/orders` | View order history and order details |
| `/cancel` | Cancel a pre-approval order |
| `/language` | Switch between Hindi (हिंदी) and English |
| `/help` | Show available commands |

### Customer order flow

```
/browse → select category → select product → add to cart (preset qty buttons)
       ↓
/cart → review items → /checkout → order placed → UPI QR shown
       ↓
type UTR (transaction reference) → payment submitted → wait for Seller approval
       ↓
Approved → Telegram notification → pickup location shown → collect order
```

### Seller / Admin commands

| Command | Description |
|---|---|
| `/verify` or `/admin` | List orders awaiting payment verification (with fulfillability flags) |
| `/active_orders` | List all active (non-terminal) orders |
| `/settings` | Update pickup location and UPI payment details |

### Seller order flow

```
Customer places order → /verify shows pending orders with UTR + fulfillability flag
       ↓
Tap Approve → stock decremented atomically → Customer notified
OR Tap Reject → enter reason → Customer notified with reason
       ↓
/active_orders → tap Mark Ready → Customer notified with pickup location
       ↓
Customer collects → tap Mark Collected → order completed
```

### Language switch

Customers and the Seller can switch languages at any time using `/language` or the inline language button. The preference is saved per user — subsequent sessions use the same language automatically. Hindi is the default for new users.

---

## Running Tests

```bash
# Full test suite (635+ tests, fast — no real DB or network calls)
pytest

# Only smoke checks (quickest feedback)
pytest -m smoke

# Only unit tests
pytest tests/unit/

# Only integration tests (runs Alembic migrations against SQLite)
pytest tests/integration/ -m integration

# Hypothesis property tests
HYPOTHESIS_PROFILE=ci pytest -m property

# Run with coverage
pytest --cov=src/marketplace --cov-report=term-missing
```

### Test markers

| Marker | Description |
|---|---|
| `smoke` | Fast sanity checks (imports, branding, config) |
| `integration` | Tests that run real DB migrations (SQLite) |
| `property` | Hypothesis property-based tests (max_examples ≥ 100) |

### Hypothesis profiles

| Profile | `max_examples` | Usage |
|---|---|---|
| `default` | 100 | Local development |
| `dev` | 100 | Same as default, more verbose |
| `ci` | 500 | CI / stronger coverage |

Set via `HYPOTHESIS_PROFILE=ci pytest`.

---

## Backup & Restore

### Take a backup

```bash
export DB_URL="postgresql://user:pass@host/db"
export BACKUP_DIR="/var/backups/jan-purna"

python -m scripts.backup
# → Backup completed: backup_20240101_120000.sql
```

Backups older than `BACKUP_RETENTION_DAYS` (default 30) are automatically pruned.

### Restore from a backup

```bash
export DB_URL="postgresql://user:pass@host/db"
export BACKUP_FILE="/var/backups/jan-purna/backup_20240101_120000.sql"

python -m scripts.restore
# → Restore completed in 12.34 seconds.
```

A warning is printed if the restore takes longer than 60 minutes.

### Automated scheduling (cron)

```bash
# Edit crontab
crontab -e

# Add this line (every 12 hours — satisfies ≤24h requirement):
0 */12 * * * cd /app && python -m scripts.backup >> /var/log/backup.log 2>&1
```

See `scripts/backup.cron` for the full cron line and a systemd timer alternative.

---

## Deployment

### Minimum viable deployment (free, ~₹0/mo)

| Component | Recommended | Cost |
|---|---|---|
| Compute | Oracle Cloud Always-Free VM (1 OCPU, 1 GB RAM) | Free |
| Database | Neon.tech free tier or Supabase free tier (500 MB) | Free |
| Object storage | Cloudflare R2 (10 GB free, no egress) | Free |
| Telegram | Bot API | Free |

### Steps

```bash
# On your server / VM:

# 1. Clone and install
git clone https://github.com/RatuTalitan/test-jp.git
cd test-jp
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Create .env with production values
cp .env.example .env
vim .env   # Fill in BOT_TOKEN, DB_URL (Postgres), OBJECT_STORE_KEY, etc.

# 3. Run the bot (keep alive with systemd or screen/tmux)
python -m marketplace.main

# With systemd (recommended for production):
# Create /etc/systemd/system/jan-purna.service (see below)
sudo systemctl enable --now jan-purna
```

### Systemd service unit

```ini
[Unit]
Description=Jan Purna Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/jan-purna
EnvironmentFile=/opt/jan-purna/.env
ExecStart=/opt/jan-purna/.venv/bin/python -m marketplace.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Before going live checklist

- [ ] `BOT_TOKEN` is set from BotFather
- [ ] `SELLER_TELEGRAM_ID` is your Telegram user ID
- [ ] `DB_URL` points to a real Postgres instance
- [ ] `UPI_ADDRESS` and UPI QR are configured (or set via `/settings` after first run)
- [ ] Pickup location is configured via `/settings`
- [ ] `.env` is NOT committed to git
- [ ] Backups are scheduled with cron

---

## Phase 2 Roadmap

The architecture is designed so all of these are **additive changes** — no existing data needs to be migrated or rewritten.

| Feature | Status | Notes |
|---|---|---|
| **WhatsApp Business channel** | 🗂️ Planned | Add a `WhatsAppChannel` adapter alongside `TelegramChannel`; same domain services, same DB |
| **Telegram Mini App (Web App)** | 🗂️ Planned | Richer catalog/cart UI launched from the Menu Button; reuses the same backend via HTTP/JSON API |
| **Voice / AI assistant** | 🗂️ Planned | Transcribe voice messages (pay-per-use STT) and route to the same service interfaces |
| **Automated payment gateway** | 🗂️ Planned | Replace manual UTR with a gateway; slot into the existing Payment_Service |
| **Seller order modification UI** | 🚧 Stubbed | Service is complete; Telegram conversation UI is not yet wired |
| **Catalog management bot UI** | 🚧 Stubbed | Services are complete; Telegram CRUD flows are not yet wired |
| **UTR screenshot upload** | 🚧 Stubbed | `PaymentService.attach_screenshot` is implemented; Telegram file download wiring pending |
| **Multi-language (beyond Hi/EN)** | 🗂️ Planned | Add new keys to the Message_Catalog — no domain changes |

---

## Spec Docs

Full specification lives in `.kiro/specs/cottonseed-oilcake-marketplace/`:

| File | Description |
|---|---|
| `requirements.md` | 19 requirements in EARS format covering all functional and non-functional aspects |
| `design.md` | Full architecture, data models, order state machine, 35 correctness properties, cost estimate |
| `tasks.md` | Dependency-ordered implementation plan (635+ tests, all core tasks complete) |

---

## License

Proprietary — built for Jan Purna (जन पूर्णा) cotton seed oil cake business.

---

*Jan Purna (जन पूर्णा) — JP*
