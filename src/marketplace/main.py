"""Application entrypoint: wires everything together and runs the bot (Task 22.2).

Startup sequence (Req 12.5, 13.1, 13.2, 13.8):

1.  **Load config** via :func:`~marketplace.config.loader.load_config` — fails
    fast on any missing required secret with a clear, single-pass error message
    that names every absent variable (Req 12.5).

2.  **Run Alembic migrations to head** — ensures the schema is current on every
    startup. All migrations are additive (no destructive renames, Req 13.7), so
    running to head on an already-migrated database is a safe no-op.

3.  **Build engine + sessionmaker** from ``DB_URL``.

4.  **Seed ``seller_settings``** if no row exists — applies the optional
    ``UPI_ADDRESS``, ``UPI_QR_OBJECT_KEY``, and ``UTR_PATTERN`` seed values from
    config into the singleton ``seller_settings`` row. Existing rows are left
    unchanged (the Seller may have updated them via the Admin_Console).

5.  **Build the Telegram ``Application``** from ``BOT_TOKEN``.

6.  **Register the router's handlers** on the Application.

7.  **Start long-polling** (v1 default, Req 13.1) or **webhook** when
    ``USE_WEBHOOK=true`` (Req 13.8). The distinction is a configuration switch
    with no data-format impact (design.md -> Webhook vs Long-Polling).

8.  **Run until interrupted** (SIGINT/SIGTERM).

The module is also importable cleanly for the smoke test (no side effects at
import time; the wiring runs only when ``main()`` is awaited or the module is
run as ``__main__``).

Requirements: 1.9, 13.1, 13.2, 13.8.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from marketplace.auth.service import AuthService
from marketplace.bot_interface.channel import Renderer
from marketplace.bot_interface.router import BotRouter
from marketplace.bot_interface.update_source import build_update_source
from marketplace.config.loader import Config, load_config
from marketplace.db.repositories import SqlAlchemyUnitOfWork
from marketplace.db.session import make_engine, make_session_factory
from marketplace.domain.entities import SellerSettings, new_id

__all__ = ["main", "build_application"]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Config loader (re-exported; main() calls it)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. Migrations
# ---------------------------------------------------------------------------
def _run_migrations(db_url: str) -> None:
    """Apply all pending Alembic migrations to ``head``.

    Safe to call on every startup: Alembic detects the current revision and only
    runs migrations that have not yet been applied. All migrations are additive
    (new nullable columns / new tables, never destructive renames), so this is
    a no-op when the schema is current (Req 13.7).

    The ``alembic.ini`` is expected at the project root (the same directory that
    contains the ``migrations/`` folder). We resolve the path relative to this
    source file so the entrypoint works from any working directory.
    """
    # Resolve alembic.ini relative to this module (src/marketplace/main.py →
    # go up two levels: marketplace → src → project root).
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.join(here, "..", "..")
    ini_path = os.path.normpath(os.path.join(project_root, "alembic.ini"))

    cfg = AlembicConfig(ini_path)
    # Inject the resolved DB URL so the migration env picks it up without
    # requiring the DB_URL env var to be set (the caller has already resolved it
    # via the config loader and passed it in, Req 12.5).
    cfg.set_main_option("sqlalchemy.url", db_url)
    # script_location is relative to the project root; make it absolute.
    cfg.set_main_option(
        "script_location",
        os.path.normpath(os.path.join(project_root, "migrations")),
    )
    log.info("Running Alembic migrations to head...")
    alembic_command.upgrade(cfg, "head")
    log.info("Migrations complete.")


# ---------------------------------------------------------------------------
# 4. Seed seller_settings
# ---------------------------------------------------------------------------
def _seed_seller_settings(session_factory, config: Config) -> None:
    """Seed the singleton ``seller_settings`` row if it does not exist yet.

    On first boot the ``seller_settings`` table is empty. This seeds it with any
    optional ``UPI_ADDRESS``, ``UPI_QR_OBJECT_KEY``, and ``UTR_PATTERN`` values
    from environment config (Req 6.2/6.6, 19 from the config loader). Existing
    rows are left completely unchanged — the Seller may have configured them via
    the Admin_Console, and we must never overwrite those values.

    If ``seller_settings`` already has a row (``uow.seller_settings.get()`` is
    not ``None``), this function is a no-op.
    """
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        existing = uow.seller_settings.get()
        if existing is not None:
            # Row already exists — do not overwrite Seller-configured values.
            return

        # No row yet: seed with any config-provided defaults.
        from marketplace.config.loader import DEFAULT_UTR_PATTERN
        settings = SellerSettings(
            seller_settings_id=new_id(),
            upi_address=config.upi_address,
            upi_qr_object_key=config.upi_qr_object_key,
            utr_pattern=config.utr_pattern or DEFAULT_UTR_PATTERN,
            updated_at=datetime.now(timezone.utc),
        )
        uow.seller_settings.upsert(settings)
        uow.commit()
        log.info("Seeded seller_settings with config defaults.")


# ---------------------------------------------------------------------------
# 5–7. Build the Telegram Application (importable, no side effects)
# ---------------------------------------------------------------------------
def build_application(config: Config, session_factory):
    """Build and configure the Telegram Application without starting it.

    Returns ``(application, router)`` so tests and the main loop can share the
    setup without triggering any network calls. Does **not** call
    ``application.initialize()`` or ``run_polling()``; those happen in
    :func:`main`.
    """
    from telegram.ext import Application

    renderer = Renderer()

    # Build a UoW factory for the AuthService (persistent; one factory per
    # process, not one factory per request).
    def _uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    auth_service = AuthService(_uow_factory, config.seller_telegram_id)

    # Build the Application.  ``Application.builder()`` configures the updater
    # (long-poll) and the job queue; the actual bot token is set here.
    app = (
        Application.builder()
        .token(config.bot_token.reveal())
        .build()
    )

    from marketplace.bot_interface.telegram_channel import TelegramMessagingChannel

    channel = TelegramMessagingChannel(app.bot, renderer)

    router = BotRouter(
        session_factory=session_factory,
        auth_service=auth_service,
        seller_telegram_id=config.seller_telegram_id,
        channel=channel,
        renderer=renderer,
    )
    router.register_handlers(app)
    return app, router


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    """Wire config, schema, services, and bot into a running process.

    Runs until the process is interrupted (SIGINT / SIGTERM).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # 1. Load config — fails fast on missing secrets (Req 12.5).
    config = load_config()
    log.info("Configuration loaded (seller_telegram_id=%s)", config.seller_telegram_id)

    # 2. Run Alembic migrations to head.
    _run_migrations(config.db_url.reveal())

    # 3. Build engine + sessionmaker.
    engine = make_engine(config.db_url.reveal())
    session_factory = make_session_factory(engine)

    # 4. Seed seller_settings if no row exists yet.
    _seed_seller_settings(session_factory, config)

    # 5–6. Build the Telegram Application and register handlers.
    app, router = build_application(config, session_factory)
    log.info("Telegram Application built and handlers registered.")

    # 7. Determine update source (long-poll v1 or webhook Phase 2).
    update_source = build_update_source(config)
    log.info("Update source: %s", type(update_source).__name__)

    if config.use_webhook:
        # Webhook mode (Req 13.8): requires a public HTTPS URL and the secret
        # token. WEBHOOK_URL must be set in the environment.
        webhook_url = os.environ.get("WEBHOOK_URL", "")
        port = int(os.environ.get("PORT", "8080"))
        secret_token = (
            config.webhook_secret_token.reveal()
            if config.webhook_secret_token is not None
            else None
        )
        log.info("Starting in webhook mode on port %s, URL=%s", port, webhook_url)
        await app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            secret_token=secret_token,
            allowed_updates=["message", "callback_query", "chat_member"],
        )
    else:
        # Long-polling mode (v1 default, Req 13.1). No public endpoint needed.
        log.info("Starting in long-polling mode.")
        await app.run_polling(
            allowed_updates=["message", "callback_query", "chat_member"],
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
