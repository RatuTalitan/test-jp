# Jan Purna (जन पूर्णा) — Cotton Seed Oil Cake Marketplace

A single-vendor ordering marketplace for cotton seed oil cake (cattle feed),
delivered as a **Telegram bot** for v1. One Seller (also the Admin) serves many
Customers, who are predominantly elderly users in India.

> This repository currently contains the **project scaffolding** only
> (Task 1.1). Business logic is implemented by subsequent tasks.

## Tech stack

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.11 |
| Bot framework | python-telegram-bot (PTB) v21+ |
| Database | PostgreSQL via SQLAlchemy + psycopg (v3) |
| Migrations | Alembic |
| Testing | pytest, pytest-asyncio, Hypothesis (property-based) |

## Project layout

```
src/marketplace/        # modular-monolith application package
  bot_interface/        # Telegram MessagingChannel + UpdateSource + i18n (presentation)
  auth/                 # Auth_Service
  catalog/              # Catalog_Service
  cart/                 # Cart_Service
  order/                # Order_Service (+ Order State Machine)
  payment/              # Payment_Service
  fulfillability/       # Fulfillability Engine
  notification/         # Notification_Service
  admin/                # Admin_Console
  domain/               # channel-neutral entities, results, Monetary_Rounding
  db/                   # SQLAlchemy models, repositories, Alembic migrations
  config/               # env-based config loader + branding strings
tests/
  properties/           # Hypothesis property-based tests (P1a-P35)
  integration/          # DB / object-storage / Telegram round-trips
  unit/                 # example / edge-case tests
  conftest.py           # Hypothesis profiles (max_examples >= 100 floor)
```

Domain packages never import the Telegram SDK; all channel-specific code lives
behind the `bot_interface` seam so future channels (WhatsApp / web) stay cheap.

## Development setup

Requires Python 3.11.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

## Running tests

```bash
# Full suite
pytest

# Only the fast smoke checks
pytest -m smoke

# Property-based tests (Hypothesis); CI profile explores more examples
HYPOTHESIS_PROFILE=ci pytest -m property
```

Pytest markers: `property`, `integration`, `smoke` (see `pyproject.toml`).
