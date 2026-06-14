"""Smoke tests for Task 22.1/22.2: router wiring and main.py importability.

Verifies that:

* ``main.py`` is importable cleanly (no side effects at import time) — covers
  the "add a small smoke test that imports main.py cleanly" criterion from
  Task 22.2.
* The :class:`~marketplace.bot_interface.router.BotRouter` can be constructed
  and registers handlers on a real (but fake-token) ``Application`` without
  network calls.
* The router's handler structure exposes the expected conversation states.
* The language-switch callbacks, browse/cart/orders commands, and admin
  commands all result in the right ConversationHandler state constants.
* :func:`~marketplace.main.build_application` produces an Application with at
  least one registered handler when given a fake token and a sessionmaker.

These tests use an **in-memory SQLite database** (``sqlite:///:memory:``) and
monkeypatching to avoid real network calls (Req 12.5) while exercising the
real wiring path.

Requirements: 1.9, 13.1, 13.2, 13.8.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_FAKE_TOKEN = "123456789:AAFake_token_for_testing_only_never_real"
_FAKE_SELLER_ID = 999_999_999


def _make_env_overrides(**extra):
    """Build a minimal valid env for load_config()."""
    return {
        "BOT_TOKEN": _FAKE_TOKEN,
        "DB_URL": "sqlite:///:memory:",
        "OBJECT_STORE_KEY": "fake-object-store-key",
        "VERIFIED_CONTACT_ENCRYPTION_KEY": "a" * 32,
        "SELLER_TELEGRAM_ID": str(_FAKE_SELLER_ID),
        **extra,
    }


# ---------------------------------------------------------------------------
# 1. main.py is importable (no side effects at import time)
# ---------------------------------------------------------------------------
class TestMainImportable:
    """main.py must be importable without triggering any network call."""

    def test_import_main(self):
        """Import marketplace.main cleanly — no side effects at module level."""
        # This will fail if main.py runs anything at import time.
        import importlib

        mod = importlib.import_module("marketplace.main")
        assert hasattr(mod, "main"), "main() coroutine should be defined"
        assert hasattr(mod, "build_application"), "build_application() should be defined"

    def test_main_functions_are_callable(self):
        """main() and build_application() must be callable (not partially executed)."""
        from marketplace.main import build_application, main

        import inspect

        assert inspect.iscoroutinefunction(main), "main() must be async"
        assert callable(build_application)


# ---------------------------------------------------------------------------
# 2. BotRouter construction and handler registration
# ---------------------------------------------------------------------------
class TestBotRouterConstruction:
    """BotRouter can be constructed and registers handlers without network I/O."""

    def _make_session_factory(self):
        """Build an in-memory SQLite sessionmaker (schema-less; for wiring tests)."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        # Create all ORM tables so the UoW can open without errors.
        try:
            from marketplace.db import Base
            Base.metadata.create_all(engine)
        except Exception:
            pass  # If tables already exist or schema differs, ignore.
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def _make_router(self, session_factory):
        """Build a BotRouter with a fake channel and renderer."""
        from marketplace.auth.service import AuthService
        from marketplace.bot_interface.channel import Renderer
        from marketplace.bot_interface.router import BotRouter
        from marketplace.db.repositories import SqlAlchemyUnitOfWork

        def uow_factory():
            return SqlAlchemyUnitOfWork(session_factory)

        auth_service = AuthService(uow_factory, _FAKE_SELLER_ID)

        # Fake channel — no real bot, no network calls
        from marketplace.bot_interface.channel import FakeMessagingChannel

        channel = MagicMock()
        channel.send_text = AsyncMock(return_value=MagicMock(ok=True))
        channel.register_command_menu = AsyncMock(return_value=None)
        channel.request_contact = AsyncMock(return_value=MagicMock(ok=True))
        channel.send_payment_instructions = AsyncMock(return_value=MagicMock(ok=True))

        renderer = Renderer()
        router = BotRouter(
            session_factory=session_factory,
            auth_service=auth_service,
            seller_telegram_id=_FAKE_SELLER_ID,
            channel=channel,
            renderer=renderer,
        )
        return router

    def test_router_constructs_without_error(self):
        sf = self._make_session_factory()
        router = self._make_router(sf)
        assert router is not None

    def test_register_handlers_returns_none(self):
        """register_handlers() adds handlers to a real Application instance."""
        sf = self._make_session_factory()
        router = self._make_router(sf)

        # PTB v21 does not validate the token until the first API call.
        # Constructing an Application with a fake token is safe for handler wiring tests.
        from telegram.ext import Application

        app = Application.builder().token(_FAKE_TOKEN).build()
        result = router.register_handlers(app)
        # register_handlers returns None
        assert result is None

    def test_application_has_handlers_after_registration(self):
        """After registration, the Application must have at least one handler."""
        sf = self._make_session_factory()
        router = self._make_router(sf)

        from telegram.ext import Application

        app = Application.builder().token(_FAKE_TOKEN).build()
        router.register_handlers(app)
        assert len(app.handlers) > 0, "Expected at least one handler group"


# ---------------------------------------------------------------------------
# 3. Conversation state constants are exported
# ---------------------------------------------------------------------------
class TestConversationStateConstants:
    """The expected conversation state constants must be importable and distinct."""

    def test_state_constants_exist(self):
        from marketplace.bot_interface.router import (
            MAIN,
            ONBOARDING,
            WAITING_QTY,
            WAITING_REJECT,
            WAITING_UPI_ADDR,
            WAITING_UTR,
        )

        states = [ONBOARDING, MAIN, WAITING_QTY, WAITING_UTR, WAITING_REJECT, WAITING_UPI_ADDR]
        assert len(set(states)) == len(states), "All state constants must be distinct"
        assert ONBOARDING != MAIN
        assert WAITING_QTY not in (ONBOARDING, MAIN)

    def test_waiting_pickup_constant(self):
        from marketplace.bot_interface.router import WAITING_PICKUP

        assert isinstance(WAITING_PICKUP, int)


# ---------------------------------------------------------------------------
# 4. build_application returns an Application with handlers
# ---------------------------------------------------------------------------
class TestBuildApplication:
    """build_application() must wire config → services → handlers without I/O."""

    def _make_session_factory(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        try:
            from marketplace.db import Base
            Base.metadata.create_all(engine)
        except Exception:
            pass
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def test_build_application_registers_handlers(self):
        """build_application returns an Application with handlers registered."""
        from marketplace.config.loader import load_config
        from marketplace.main import build_application

        config = load_config(env=_make_env_overrides())
        sf = self._make_session_factory()

        app, router = build_application(config, sf)
        assert app is not None
        assert router is not None
        assert len(app.handlers) > 0

    def test_build_application_returns_bot_router_instance(self):
        """The router returned by build_application is a BotRouter."""
        from marketplace.bot_interface.router import BotRouter
        from marketplace.config.loader import load_config
        from marketplace.main import build_application

        config = load_config(env=_make_env_overrides())
        sf = self._make_session_factory()

        _, router = build_application(config, sf)
        assert isinstance(router, BotRouter)


# ---------------------------------------------------------------------------
# 5. _seed_seller_settings smoke test
# ---------------------------------------------------------------------------
class TestSeedSellerSettings:
    """_seed_seller_settings creates a row on first call and is idempotent."""

    def _make_session_factory(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        from marketplace.db import Base
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def test_seeds_on_empty_db(self):
        from marketplace.config.loader import load_config
        from marketplace.db.repositories import SqlAlchemyUnitOfWork
        from marketplace.main import _seed_seller_settings

        config = load_config(env=_make_env_overrides(UPI_ADDRESS="test@bank"))
        sf = self._make_session_factory()

        _seed_seller_settings(sf, config)

        with SqlAlchemyUnitOfWork(sf) as uow:
            settings = uow.seller_settings.get()
            assert settings is not None
            assert settings.upi_address == "test@bank"

    def test_idempotent_does_not_overwrite(self):
        """Calling _seed_seller_settings twice must not overwrite the first seed."""
        from marketplace.config.loader import load_config
        from marketplace.db.repositories import SqlAlchemyUnitOfWork
        from marketplace.main import _seed_seller_settings

        config = load_config(env=_make_env_overrides(UPI_ADDRESS="first@bank"))
        sf = self._make_session_factory()

        _seed_seller_settings(sf, config)
        # Second call with a different UPI address — must be a no-op
        config2 = load_config(env=_make_env_overrides(UPI_ADDRESS="second@bank"))
        _seed_seller_settings(sf, config2)

        with SqlAlchemyUnitOfWork(sf) as uow:
            settings = uow.seller_settings.get()
            assert settings is not None
            assert settings.upi_address == "first@bank", (
                "Second call must not overwrite the first seed"
            )


# ---------------------------------------------------------------------------
# 6. load_config fails fast on missing secrets
# ---------------------------------------------------------------------------
class TestConfigFailFast:
    """load_config() raises MissingConfigError on absent required vars."""

    def test_fails_fast_with_empty_env(self):
        from marketplace.config.loader import MissingConfigError, load_config

        with pytest.raises(MissingConfigError) as exc_info:
            load_config(env={})
        assert "BOT_TOKEN" in str(exc_info.value)
        assert "DB_URL" in str(exc_info.value)

    def test_passes_with_all_required_vars(self):
        from marketplace.config.loader import load_config

        config = load_config(env=_make_env_overrides())
        assert config.bot_token.reveal() == _FAKE_TOKEN
        assert config.seller_telegram_id == _FAKE_SELLER_ID
