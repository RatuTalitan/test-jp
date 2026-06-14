"""BotRouter: routes Telegram updates to domain services (Task 22.1).

The router opens **one** :class:`~marketplace.db.repositories.SqlAlchemyUnitOfWork`
per inbound Telegram update, routes the update to the correct domain service,
renders the channel-agnostic result back as text + inline keyboard, and rolls
back atomically on any error — the ``UnitOfWork.__exit__`` handles rollback
automatically if ``commit()`` was never called (Req 1.9, 12.3, 12.4, 12.6).

Implemented customer flows (fully wired):
  * /start → branded onboarding (Share Contact if unauthenticated)
  * contact share → Auth_Service.register_contact
  * /language → language switch (Hindi / English)
  * /browse (+ nav:browse callback) → Catalog browse by category → product detail → add to cart
  * /cart (+ nav:cart callback) → CartService.view, remove item, clear, checkout
  * /checkout → OrderService.place_order → PaymentService.present_instructions
  * UTR text input → PaymentService.submit_utr
  * /orders (+ nav:orders callback) → OrderService list → order detail → cancel prompt
  * /cancel → cancel order

Implemented seller flows (fully wired, gated via require_admin):
  * /verify → AdminConsole.list_pending_verifications → approve/reject buttons
  * Approve callback → OrderService.verify_payment (cascades to approve)
  * Reject callback → WAITING_REJECT state → rejection reason text → OrderService.reject_payment
  * /active_orders → AdminConsole.list_active_orders → mark-ready / mark-collected buttons
  * Mark-ready callback → AdminConsole.mark_ready
  * Mark-collected callback → OrderService.mark_collected
  * Offline-approve callback → OrderService.offline_verify
  * /settings → settings menu → set pickup location / set UPI address text flows

Stubbed (documented, not yet wired):
  * UPI QR image upload (requires Telegram file download + object store)
  * Seller order modification (add/remove/change line)
  * Catalog management (create/update product, category)
  * UTR screenshot attachment

Conversation state in-process (memory; no Redis, Req cost-zero):
  PTB Application's default in-memory context.user_data / ConversationHandler state.

Requirements: 1.9, 12.3, 12.4, 12.6, 13.1, 13.8 (via UpdateSource abstraction).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from marketplace.admin.service import AdminConsole
from marketplace.auth.service import AuthService, SharedContact
from marketplace.bot_interface.channel import Button, Keyboard, Renderer
from marketplace.bot_interface.i18n import Language
from marketplace.bot_interface.presentation import (
    LanguageSwitch,
    Onboarding,
    main_menu_keyboard,
    parse_language_callback,
)
from marketplace.bot_interface.telegram_channel import TelegramMessagingChannel
from marketplace.bot_interface.voice import ConversationStep, VoiceMessageHandler
from marketplace.cart.service import CartService
from marketplace.catalog.service import (
    CatalogService,
    EmptyCatalog,
    EmptyCategory,
    IN_STOCK,
    OUT_OF_STOCK,
    ProductDetail,
)
from marketplace.db.repositories import SqlAlchemyUnitOfWork
from marketplace.domain.entities import Order, OrderState, Role, SellerSettings, User, new_id
from marketplace.domain.results import (
    Conflict,
    NotAuthorized,
    NotFound,
    Rejected,
    Unauthenticated,
)
from marketplace.fulfillability.engine import is_fulfillable, shortfalls as get_shortfalls
from marketplace.order.service import (
    EMPTY_CART,
    NO_ORDERS,
    ORDER_NOT_AVAILABLE,
    PAYMENT_REFERENCE_NOT_PROVIDED,
    CustomerOrderDetail,
    OrderService,
)
from marketplace.order.state_machine import Actor
from marketplace.payment.service import (
    UPI_NOT_CONFIGURED,
    PaymentService,
    UpiInstructions,
)

__all__ = ["BotRouter", "ONBOARDING", "MAIN", "WAITING_QTY", "WAITING_UTR",
           "WAITING_REJECT", "WAITING_PICKUP", "WAITING_UPI_ADDR"]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
ONBOARDING = 0      # user has no verified contact yet
MAIN = 1            # normal callback-driven navigation
WAITING_QTY = 2     # user entered a product, waiting for quantity text
WAITING_UTR = 3     # user viewing payment instructions, waiting for UTR
WAITING_REJECT = 4  # admin initiated rejection, waiting for reason text
WAITING_PICKUP = 5  # admin setting pickup location, waiting for text
WAITING_UPI_ADDR = 6  # admin setting UPI address, waiting for text

# context.user_data keys
_CTX_PRODUCT_ID = "_pid"
_CTX_PRODUCT_NAME = "_pname"
_CTX_PRODUCT_MOQ = "_pmoq"
_CTX_PRODUCT_UNIT = "_punit"
_CTX_ORDER_ID = "_oid"
_CTX_REJECT_ORDER_ID = "_reid"


# ---------------------------------------------------------------------------
# Callback data helpers (all prefixes ≤ 5 chars so UUID payloads stay < 64B)
# ---------------------------------------------------------------------------
def _cb_cat(category_id: str) -> str:
    return f"cat:{category_id}"


def _cb_prod(product_id: str) -> str:
    return f"p:{product_id}"


def _cb_add(product_id: str) -> str:
    return f"add:{product_id}"


def _cb_qpreset(product_id: str, qty: str) -> str:
    return f"qp:{product_id}:{qty}"


def _cb_remove(product_id: str) -> str:
    return f"rm:{product_id}"


def _cb_order(order_id: str) -> str:
    return f"ord:{order_id}"


def _cb_pay(order_id: str) -> str:
    return f"pay:{order_id}"


def _cb_cancel(order_id: str) -> str:
    return f"can:{order_id}"


def _cb_cancel_confirm(order_id: str, yes: bool) -> str:
    return f"cc:{order_id}:{'y' if yes else 'n'}"


def _cb_approve(order_id: str) -> str:
    return f"apr:{order_id}"


def _cb_reject(order_id: str) -> str:
    return f"rej:{order_id}"


def _cb_ready(order_id: str) -> str:
    return f"rdy:{order_id}"


def _cb_collected(order_id: str) -> str:
    return f"col:{order_id}"


def _cb_offline(order_id: str) -> str:
    return f"off:{order_id}"


def _parse_order_number(order: Order) -> str:
    """Return the human-readable order number, falling back to short id."""
    return str(order.order_number) if order.order_number is not None else str(order.order_id)[:8]


def _state_label(state: OrderState) -> str:
    """Map an OrderState to a short English/human-readable label."""
    _MAP = {
        OrderState.PLACED: "Placed",
        OrderState.PAYMENT_PENDING: "Awaiting payment",
        OrderState.PAYMENT_SUBMITTED: "Payment submitted",
        OrderState.PAYMENT_VERIFIED: "Payment verified",
        OrderState.APPROVED: "Approved",
        OrderState.READY_FOR_PICKUP: "Ready for pickup",
        OrderState.COMPLETED: "Completed",
        OrderState.CANCELLED: "Cancelled",
        OrderState.REJECTED: "Rejected",
    }
    return _MAP.get(state, state.value)


# ---------------------------------------------------------------------------
# BotRouter
# ---------------------------------------------------------------------------
class BotRouter:
    """Routes Telegram updates to domain services (one UoW per update).

    Args:
        session_factory: A ``sessionmaker`` bound to the application's engine.
        auth_service:    A persistent :class:`~marketplace.auth.service.AuthService`
                         (owns the ``uow_factory`` and the Seller config).
        seller_telegram_id: The Seller's Telegram user id (for creating ``Actor``).
        channel:         The :class:`TelegramMessagingChannel` adapter.
        renderer:        The :class:`Renderer` (catalog + branding).
        object_store:    Optional object store (for QR upload — stubbed until wired).
    """

    def __init__(
        self,
        session_factory,
        auth_service: AuthService,
        seller_telegram_id: int,
        channel: TelegramMessagingChannel,
        renderer: Renderer,
        object_store=None,
    ) -> None:
        self._sf = session_factory
        self._auth = auth_service
        self._seller_tid = int(seller_telegram_id)
        self._channel = channel
        self._renderer = renderer
        self._object_store = object_store
        self._voice_handler = VoiceMessageHandler(channel, renderer)
        self._onboarding = Onboarding(channel, renderer)
        self._language_switch = LanguageSwitch(auth_service, channel, renderer)

    # ------------------------------------------------------------------
    # UoW factory
    # ------------------------------------------------------------------
    def _uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._sf)

    # ------------------------------------------------------------------
    # Auth helpers (each opens its own UoW)
    # ------------------------------------------------------------------
    def _get_user_lang(self, telegram_user_id: int) -> Optional[Language]:
        with self._uow() as uow:
            user = uow.users.get_by_telegram_id(telegram_user_id)
            return user.language_preference if user else None

    def _get_user(self, telegram_user_id: int) -> Optional[User]:
        with self._uow() as uow:
            return uow.users.get_by_telegram_id(telegram_user_id)

    def _is_seller(self, telegram_user_id: int) -> bool:
        return self._auth.role_of(telegram_user_id) == Role.ADMIN

    def _seller_actor(self, user: Optional[User] = None) -> Actor:
        uid = user.user_id if user is not None else None
        return Actor.seller(uid)

    def _customer_actor(self, user: User) -> Actor:
        return Actor.customer(user.user_id)

    # ------------------------------------------------------------------
    # Auth gate helper: returns (user, lang) or None and sends the onboarding
    # prompt if the user has not shared their contact yet.
    # ------------------------------------------------------------------
    async def _require_auth(
        self, chat_id: int
    ) -> Optional[tuple[User, Optional[Language]]]:
        user = self._get_user(chat_id)
        lang = user.language_preference if user else None
        if user is None or not user.is_authenticated:
            await self._onboarding.start(chat_id, lang)
            return None
        return user, lang

    async def _require_seller(
        self, chat_id: int
    ) -> Optional[tuple[User, Optional[Language]]]:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return None
        user, lang = auth
        if not self._is_seller(chat_id):
            await self._channel.send_text(
                chat_id, self._renderer.text("SELLER_REQUIRED", lang)
            )
            return None
        return user, lang

    # ------------------------------------------------------------------
    # Main keyboard helper
    # ------------------------------------------------------------------
    def _main_kb(self, lang: Optional[Language], is_admin: bool) -> Keyboard:
        return main_menu_keyboard(self._renderer, lang, is_admin)

    # ------------------------------------------------------------------
    # register_handlers
    # ------------------------------------------------------------------
    def register_handlers(self, application: Application) -> None:
        """Attach all handlers to ``application``."""

        # ── callback pattern handlers (language switch usable in all states) ──
        lang_cqh = CallbackQueryHandler(self._handle_lang_cb, pattern=r"^lang:")

        # The main ConversationHandler handles every flow
        conv = ConversationHandler(
            entry_points=[
                CommandHandler("start", self._cmd_start),
                CommandHandler("help", self._cmd_help),
                CommandHandler("browse", self._cmd_browse),
                CommandHandler("cart", self._cmd_cart),
                CommandHandler("checkout", self._cmd_checkout),
                CommandHandler("orders", self._cmd_orders),
                CommandHandler("language", self._cmd_language),
                CommandHandler("cancel", self._cmd_cancel),
                # Seller-only commands (auth is checked inside handlers)
                CommandHandler("admin", self._cmd_admin),
                CommandHandler("verify", self._cmd_verify),
                CommandHandler("active_orders", self._cmd_active_orders),
                CommandHandler("settings", self._cmd_settings),
                # Contact share at any time (handles re-sharing)
                MessageHandler(filters.CONTACT, self._handle_contact_share),
                # Inline keyboard entry (catches callbacks that start a flow)
                CallbackQueryHandler(self._handle_cb),
            ],
            states={
                ONBOARDING: [
                    MessageHandler(filters.CONTACT, self._handle_contact_share),
                    CallbackQueryHandler(self._handle_lang_cb, pattern=r"^lang:"),
                    CommandHandler("language", self._cmd_language_any),
                    CommandHandler("start", self._cmd_start),
                    MessageHandler(filters.VOICE, self._handle_voice_onboarding),
                    MessageHandler(filters.ALL & ~filters.COMMAND, self._re_prompt_contact),
                ],
                MAIN: [
                    CallbackQueryHandler(self._handle_cb),
                    MessageHandler(filters.VOICE, self._handle_voice_main),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_unknown_text),
                ],
                WAITING_QTY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_qty_input),
                    MessageHandler(filters.VOICE, self._handle_voice_qty),
                    CommandHandler("cancel", self._cmd_cancel_input),
                    CallbackQueryHandler(self._handle_cb),
                ],
                WAITING_UTR: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_utr_input),
                    MessageHandler(filters.VOICE, self._handle_voice_utr),
                    CommandHandler("cancel", self._cmd_cancel_input),
                    CallbackQueryHandler(self._handle_cb),
                ],
                WAITING_REJECT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_reject_reason),
                    MessageHandler(filters.VOICE, self._handle_voice_main),
                    CommandHandler("cancel", self._cmd_cancel_input),
                    CallbackQueryHandler(self._handle_cb),
                ],
                WAITING_PICKUP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_pickup_text),
                    MessageHandler(filters.VOICE, self._handle_voice_main),
                    CommandHandler("cancel", self._cmd_cancel_input),
                    CallbackQueryHandler(self._handle_cb),
                ],
                WAITING_UPI_ADDR: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_upi_addr_text),
                    MessageHandler(filters.VOICE, self._handle_voice_main),
                    CommandHandler("cancel", self._cmd_cancel_input),
                    CallbackQueryHandler(self._handle_cb),
                ],
            },
            fallbacks=[
                CommandHandler("start", self._cmd_start),
                MessageHandler(filters.VOICE, self._handle_voice_main),
                MessageHandler(filters.ALL, self._handle_fallback),
            ],
            allow_reentry=True,
            name="main_conv",
            persistent=False,  # in-memory, no Redis
            per_message=False,  # track conversation per user/chat
        )

        application.add_handler(conv)

    # ==================================================================
    # /start
    # ==================================================================
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        try:
            user = self._get_user(chat_id)
            lang = user.language_preference if user else None
            if user is None or not user.is_authenticated:
                await self._onboarding.start(chat_id, lang)
                return ONBOARDING
            # Attempt to register command menu (non-blocking)
            try:
                await self._channel.register_command_menu(self._auth, chat_id, lang)
            except Exception:
                pass
            is_admin = self._is_seller(chat_id)
            brand = self._renderer.brand_display_name()
            welcome = self._renderer.text("WELCOME", lang, brand=brand)
            await self._channel.send_text(
                chat_id, welcome, buttons=self._main_kb(lang, is_admin)
            )
            return MAIN
        except Exception:
            log.exception("Error in /start for chat_id=%s", chat_id)
            return MAIN

    # ==================================================================
    # /help
    # ==================================================================
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        user = self._get_user(chat_id)
        lang = user.language_preference if user else None
        is_admin = self._is_seller(chat_id)
        prompt = self._renderer.text("HELP_PROMPT", lang)
        await self._channel.send_text(
            chat_id, prompt, buttons=self._main_kb(lang, is_admin)
        )
        return MAIN

    # ==================================================================
    # Contact sharing
    # ==================================================================
    async def _handle_contact_share(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        contact = update.message.contact
        if contact is None:
            await self._onboarding.start(chat_id, None)
            return ONBOARDING

        shared = SharedContact(
            phone_number=contact.phone_number,
            user_id=contact.user_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
        )
        result = self._auth.register_contact(chat_id, shared)
        if isinstance(result, User):
            lang = result.language_preference
            try:
                await self._channel.register_command_menu(self._auth, chat_id, lang)
            except Exception:
                pass
            is_admin = self._is_seller(chat_id)
            msg = self._renderer.text("CONTACT_SHARED_OK", lang)
            await self._channel.send_text(
                chat_id, msg, buttons=self._main_kb(lang, is_admin)
            )
            return MAIN
        # Rejected: re-prompt
        msg = self._renderer.text("CONTACT_REQUIRED", None)
        await self._channel.request_contact(chat_id, msg)
        return ONBOARDING

    async def _re_prompt_contact(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        msg = self._renderer.text("CONTACT_REQUIRED", lang)
        await self._channel.request_contact(chat_id, msg)
        return ONBOARDING

    # ==================================================================
    # /language (works in any state)
    # ==================================================================
    async def _cmd_language(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        await self._language_switch.present(chat_id, lang)
        return MAIN

    async def _cmd_language_any(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Language command usable even in ONBOARDING."""
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        await self._language_switch.present(chat_id, lang)
        user = self._get_user(chat_id)
        if user is None or not user.is_authenticated:
            return ONBOARDING
        return MAIN

    async def _handle_lang_cb(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle lang:HI / lang:EN callback (usable in any state)."""
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        code = parse_language_callback(query.data)
        if code:
            result = self._auth.set_language_preference(chat_id, code)
            if isinstance(result, User):
                lang = result.language_preference
                confirmation = self._renderer.text("LANGUAGE_SET", lang)
                await self._channel.send_text(chat_id, confirmation)
        user = self._get_user(chat_id)
        if user is None or not user.is_authenticated:
            return ONBOARDING
        return MAIN

    # ==================================================================
    # Main callback dispatcher
    # ==================================================================
    async def _handle_cb(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Route any callback_data to the matching flow handler."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        chat_id = update.effective_chat.id

        # Language switch (anywhere)
        if data.startswith("lang:"):
            return await self._handle_lang_cb(update, context)

        # Navigation
        if data in ("nav:browse", "browse"):
            return await self._do_browse(chat_id, context)
        if data in ("nav:cart", "cart"):
            return await self._do_cart(chat_id, context)
        if data in ("nav:orders", "orders"):
            return await self._do_orders(chat_id, context)
        if data in ("nav:checkout", "checkout"):
            return await self._do_checkout(chat_id, context)
        if data in ("nav:language",):
            lang = self._get_user_lang(chat_id)
            await self._language_switch.present(chat_id, lang)
            return MAIN
        if data in ("nav:help", "help"):
            return await self._cmd_help(update, context)

        # Browse
        if data.startswith("cat:"):
            return await self._do_category_products(chat_id, data[4:], context)
        if data.startswith("p:"):
            return await self._do_product_detail(chat_id, data[2:], context)
        if data.startswith("add:"):
            return await self._do_prompt_qty(chat_id, data[4:], context)
        if data.startswith("qp:"):  # qty_preset: qp:{product_id}:{qty}
            parts = data.split(":", 2)
            if len(parts) == 3:
                return await self._do_add_to_cart(chat_id, parts[1], parts[2], context)

        # Cart
        if data.startswith("rm:"):
            return await self._do_remove_from_cart(chat_id, data[3:], context)
        if data == "cart:clear":
            return await self._do_clear_cart(chat_id, context)

        # Orders
        if data.startswith("ord:"):
            return await self._do_order_detail(chat_id, data[4:], context)
        if data.startswith("pay:"):
            oid = data[4:]
            context.user_data[_CTX_ORDER_ID] = oid
            return await self._do_show_payment_instructions(chat_id, oid, context)
        if data.startswith("can:"):
            return await self._do_cancel_confirm_prompt(chat_id, data[4:], context)
        if data.startswith("cc:"):  # cancel_confirm: cc:{oid}:{y/n}
            parts = data.split(":", 2)
            if len(parts) == 3:
                return await self._do_cancel_confirm(chat_id, parts[1], parts[2], context)

        # Admin
        if data == "verify_list":
            return await self._do_admin_verify_list(chat_id, context)
        if data.startswith("apr:"):
            return await self._do_admin_approve(chat_id, data[4:], context)
        if data.startswith("rej:"):
            oid = data[4:]
            context.user_data[_CTX_REJECT_ORDER_ID] = oid
            lang = self._get_user_lang(chat_id)
            prompt = self._renderer.text("REJECT_REASON_PROMPT", lang)
            await self._channel.send_text(chat_id, prompt)
            return WAITING_REJECT
        if data == "active":
            return await self._do_admin_active_orders(chat_id, context)
        if data.startswith("rdy:"):
            return await self._do_admin_mark_ready(chat_id, data[4:], context)
        if data.startswith("col:"):
            return await self._do_admin_mark_collected(chat_id, data[4:], context)
        if data.startswith("off:"):
            return await self._do_admin_offline_approve(chat_id, data[4:], context)
        if data == "settings":
            return await self._do_admin_settings(chat_id, context)
        if data == "set_pickup":
            lang = self._get_user_lang(chat_id)
            prompt = self._renderer.text("PICKUP_PROMPT", lang)
            await self._channel.send_text(chat_id, prompt)
            return WAITING_PICKUP
        if data == "set_upi_addr":
            lang = self._get_user_lang(chat_id)
            prompt = self._renderer.text("UPI_ADDR_PROMPT", lang)
            await self._channel.send_text(chat_id, prompt)
            return WAITING_UPI_ADDR

        # Fallback
        return await self._send_main_menu(chat_id)

    # ==================================================================
    # /browse command
    # ==================================================================
    async def _cmd_browse(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        return await self._do_browse(chat_id, context)

    async def _do_browse(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        with self._uow() as uow:
            svc = CatalogService(uow)
            result = svc.list_available_grouped_by_category()
            uow.commit()

        if isinstance(result, EmptyCatalog):
            text = self._renderer.text("NO_CATEGORIES", lang)
            is_admin = self._is_seller(chat_id)
            await self._channel.send_text(
                chat_id, text, buttons=self._main_kb(lang, is_admin)
            )
            return MAIN

        prompt = self._renderer.text("BROWSE_INTRO", lang)
        buttons = []
        for category, products in result:
            if products:
                label = category.name
                buttons.append(
                    Button(label=label, callback_data=_cb_cat(str(category.category_id)))
                )
        kb = Keyboard.inline_single_column(buttons) if buttons else None
        await self._channel.send_text(chat_id, prompt, buttons=kb)
        return MAIN

    async def _do_category_products(
        self, chat_id: int, category_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            category_id = uuid.UUID(category_id_str)
        except ValueError:
            return await self._do_browse(chat_id, context)

        with self._uow() as uow:
            svc = CatalogService(uow)
            result = svc.list_available_in_category(category_id)
            category = uow.catalog.get_category(category_id)
            uow.commit()

        cat_name = category.name if category else "?"
        if isinstance(result, EmptyCategory):
            text = self._renderer.text(
                "CATEGORY_PRODUCTS_INTRO", lang, category=cat_name
            ) + "\n" + self._renderer.text("NO_CATEGORIES", lang)
            await self._channel.send_text(chat_id, text)
            return MAIN

        intro = self._renderer.text("CATEGORY_PRODUCTS_INTRO", lang, category=cat_name)
        buttons = [
            Button(label=p.name, callback_data=_cb_prod(str(p.product_id)))
            for p in result
        ]
        back = Button(
            label=self._renderer.text("BTN_BACK_TO_CATEGORIES", lang),
            callback_data="nav:browse",
        )
        buttons.append(back)
        kb = Keyboard.inline_single_column(buttons)
        await self._channel.send_text(chat_id, intro, buttons=kb)
        return MAIN

    async def _do_product_detail(
        self, chat_id: int, product_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            product_id = uuid.UUID(product_id_str)
        except ValueError:
            return await self._do_browse(chat_id, context)

        with self._uow() as uow:
            svc = CatalogService(uow)
            detail = svc.get_product(product_id)
            uow.commit()

        if isinstance(detail, NotFound):
            text = self._renderer.text("PRODUCT_NOT_FOUND", lang)
            await self._channel.send_text(chat_id, text)
            return MAIN

        assert isinstance(detail, ProductDetail)
        stock_label = self._renderer.text(
            "IN_STOCK_LABEL" if detail.stock_status == IN_STOCK else "OUT_OF_STOCK_LABEL",
            lang,
        )
        description = detail.description or ""
        text = self._renderer.text(
            "PRODUCT_DETAIL_INTRO",
            lang,
            name=detail.name,
            category=detail.category_name or "",
            price=str(detail.price_per_unit),
            unit=detail.unit.value.lower(),
            moq=str(detail.min_order_quantity),
            stock_status=stock_label,
            description=description,
        )

        buttons = []
        if detail.stock_status == IN_STOCK:
            add_label = self._renderer.text("BTN_ADD_TO_CART", lang)
            buttons.append(Button(label=add_label, callback_data=_cb_add(product_id_str)))
        back_label = self._renderer.text("BTN_BACK_TO_CATEGORIES", lang)
        buttons.append(Button(label=back_label, callback_data="nav:browse"))
        kb = Keyboard.inline_single_column(buttons)
        await self._channel.send_text(chat_id, text, buttons=kb)
        return MAIN

    # ==================================================================
    # Add-to-cart flow: prompt for quantity, wait for text input
    # ==================================================================
    async def _do_prompt_qty(
        self, chat_id: int, product_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            product_id = uuid.UUID(product_id_str)
        except ValueError:
            return MAIN

        with self._uow() as uow:
            product = uow.catalog.get_product(product_id)
            uow.commit()

        if product is None:
            text = self._renderer.text("PRODUCT_NOT_FOUND", lang)
            await self._channel.send_text(chat_id, text)
            return MAIN

        context.user_data[_CTX_PRODUCT_ID] = product_id_str
        context.user_data[_CTX_PRODUCT_NAME] = product.name
        context.user_data[_CTX_PRODUCT_MOQ] = str(product.min_order_quantity)
        context.user_data[_CTX_PRODUCT_UNIT] = product.unit.value.lower()

        prompt = self._renderer.text(
            "ENTER_QUANTITY_PROMPT",
            lang,
            name=product.name,
            unit=product.unit.value.lower(),
            moq=str(product.min_order_quantity),
        )
        # Offer preset buttons: MOQ and MOQ×2
        moq = product.min_order_quantity
        unit_str = product.unit.value.lower()
        moq_label = self._renderer.text(
            "QTY_PRESET_MOQ", lang, moq=str(moq), unit=unit_str
        )
        double_moq = moq * 2
        double_label = self._renderer.text(
            "QTY_PRESET_DOUBLE_MOQ", lang, qty=str(double_moq), unit=unit_str
        )
        cancel_label = self._renderer.text("BTN_CANCEL", lang)
        preset_buttons = [
            Button(label=moq_label, callback_data=_cb_qpreset(product_id_str, str(moq))),
            Button(label=double_label, callback_data=_cb_qpreset(product_id_str, str(double_moq))),
            Button(label=cancel_label, callback_data="nav:browse"),
        ]
        kb = Keyboard.inline_single_column(preset_buttons)
        await self._channel.send_text(chat_id, prompt, buttons=kb)
        return WAITING_QTY

    async def _handle_qty_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        product_id_str = context.user_data.get(_CTX_PRODUCT_ID)
        if not product_id_str:
            return await self._send_main_menu(chat_id, lang)

        try:
            qty = Decimal(update.message.text.strip().replace(",", "."))
        except InvalidOperation:
            err = self._renderer.text("QTY_INVALID", lang)
            await self._channel.send_text(chat_id, err)
            return WAITING_QTY

        return await self._do_add_to_cart(chat_id, product_id_str, str(qty), context)

    async def _do_add_to_cart(
        self,
        chat_id: int,
        product_id_str: str,
        qty_str: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            product_id = uuid.UUID(product_id_str)
            qty = Decimal(qty_str)
        except (ValueError, InvalidOperation):
            err = self._renderer.text("QTY_INVALID", lang)
            await self._channel.send_text(chat_id, err)
            return MAIN

        with self._uow() as uow:
            svc = CartService(uow)
            result = svc.add_item(user.user_id, product_id, qty)
            if not isinstance(result, (Rejected, NotFound)):
                uow.commit()

        if isinstance(result, Rejected):
            code = result.code
            details = result.details or {}
            if code == "QTY_BELOW_MOQ":
                msg = self._renderer.text(
                    "QTY_BELOW_MOQ", lang,
                    moq=str(details.get("moq", "")),
                    unit=context.user_data.get(_CTX_PRODUCT_UNIT, ""),
                )
            elif code == "QTY_EXCEEDS_STOCK":
                msg = self._renderer.text(
                    "QTY_EXCEEDS_STOCK", lang, stock=str(details.get("stock", ""))
                )
            elif code == "PRODUCT_OUT_OF_STOCK":
                msg = self._renderer.text("PRODUCT_OUT_OF_STOCK", lang)
            else:
                msg = self._renderer.text("QTY_INVALID", lang)
            await self._channel.send_text(chat_id, msg)
            return WAITING_QTY if context.user_data.get(_CTX_PRODUCT_ID) else MAIN
        elif isinstance(result, NotFound):
            msg = self._renderer.text("PRODUCT_NOT_FOUND", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        # Success
        product_name = context.user_data.pop(_CTX_PRODUCT_NAME, "Product")
        context.user_data.pop(_CTX_PRODUCT_ID, None)
        context.user_data.pop(_CTX_PRODUCT_MOQ, None)
        context.user_data.pop(_CTX_PRODUCT_UNIT, None)
        msg = self._renderer.text("ADDED_TO_CART", lang, name=product_name)
        # Show cart view button and continue shopping button
        kb = Keyboard.inline_single_column([
            Button(
                label=self._renderer.text("BTN_VIEW_CART", lang),
                callback_data="nav:cart",
            ),
            Button(
                label=self._renderer.text("BTN_BROWSE", lang),
                callback_data="nav:browse",
            ),
        ])
        await self._channel.send_text(chat_id, msg, buttons=kb)
        return MAIN

    # ==================================================================
    # /cart command
    # ==================================================================
    async def _cmd_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        return await self._do_cart(chat_id, context)

    async def _do_cart(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        with self._uow() as uow:
            svc = CartService(uow)
            view = svc.view(user.user_id)
            uow.commit()

        if not view.line_items:
            text = self._renderer.text("EMPTY_CART", lang)
            await self._channel.send_text(
                chat_id, text, buttons=self._main_kb(lang, self._is_seller(chat_id))
            )
            return MAIN

        # Build cart display text
        lines = []
        for line in view.line_items:
            line_text = self._renderer.text(
                "CART_LINE", lang,
                name=line.name,
                qty=str(line.quantity),
                unit=line.unit.value.lower(),
                price=str(line.unit_price),
                total=str(line.line_total),
            )
            lines.append(line_text)
        body = self._renderer.text(
            "CART_CONTENTS", lang,
            lines="\n".join(lines),
            total=str(view.total),
        )

        # Keyboard: remove buttons + checkout + clear
        buttons = []
        for line in view.line_items:
            rm_label = self._renderer.text("BTN_REMOVE_ITEM", lang, name=line.name[:20])
            buttons.append(
                Button(label=rm_label, callback_data=_cb_remove(str(line.product_id)))
            )
        checkout_label = self._renderer.text("BTN_CHECKOUT", lang)
        clear_label = self._renderer.text("BTN_CLEAR_CART", lang)
        buttons.append(Button(label=checkout_label, callback_data="nav:checkout"))
        buttons.append(Button(label=clear_label, callback_data="cart:clear"))
        kb = Keyboard.inline_single_column(buttons)
        await self._channel.send_text(chat_id, body, buttons=kb)
        return MAIN

    async def _do_remove_from_cart(
        self, chat_id: int, product_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            product_id = uuid.UUID(product_id_str)
        except ValueError:
            return await self._do_cart(chat_id, context)

        with self._uow() as uow:
            svc = CartService(uow)
            svc.remove_item(user.user_id, product_id)
            uow.commit()

        return await self._do_cart(chat_id, context)

    async def _do_clear_cart(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        with self._uow() as uow:
            svc = CartService(uow)
            svc.clear(user.user_id)
            uow.commit()

        msg = self._renderer.text("CART_CLEARED", lang)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    # ==================================================================
    # /checkout → place order → payment instructions
    # ==================================================================
    async def _cmd_checkout(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        return await self._do_checkout(chat_id, context)

    async def _do_checkout(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        with self._uow() as uow:
            order_svc = OrderService(uow)
            result = order_svc.place_order(user.user_id)
            if isinstance(result, Order):
                # Present UPI instructions in the same transaction
                pay_svc = PaymentService(uow, self._object_store)
                instructions = pay_svc.present_instructions(result.order_id)
                uow.commit()
            else:
                instructions = None

        if isinstance(result, Rejected) and result.code == EMPTY_CART:
            text = self._renderer.text("EMPTY_CART", lang)
            await self._channel.send_text(chat_id, text)
            return MAIN
        if isinstance(result, (Rejected, Conflict, Unauthenticated, NotFound)):
            reason = getattr(result, "reason", "") or ""
            text = self._renderer.text("TRANSITION_FAILED", lang, reason=reason)
            await self._channel.send_text(chat_id, text)
            return MAIN

        assert isinstance(result, Order)
        order_num = _parse_order_number(result)
        placed_msg = self._renderer.text(
            "ORDER_PLACED_DETAIL", lang, order_number=order_num, total=str(result.total_amount)
        )
        await self._channel.send_text(chat_id, placed_msg)

        # Show payment instructions
        if isinstance(instructions, UpiInstructions):
            await self._channel.send_payment_instructions(
                recipient_id=chat_id,
                upi_address=instructions.upi_address,
                qr_image=instructions.upi_qr_object_key,
                amount=instructions.amount_due,
                caption=self._renderer.text(
                    "PAYMENT_INSTRUCTIONS", lang,
                    upi_address=instructions.upi_address,
                    amount=str(instructions.amount_due),
                ),
            )
            # Offer UTR entry
            context.user_data[_CTX_ORDER_ID] = str(result.order_id)
            utr_prompt = self._renderer.text("ENTER_UTR_PROMPT", lang)
            await self._channel.send_text(
                chat_id,
                utr_prompt,
                buttons=Keyboard.inline_single_column([
                    Button(
                        label=self._renderer.text("BTN_MY_ORDERS", lang),
                        callback_data="nav:orders",
                    ),
                ]),
            )
            return WAITING_UTR
        elif isinstance(instructions, Rejected) and getattr(instructions, "code", "") == UPI_NOT_CONFIGURED:
            text = self._renderer.text("UPI_NOT_CONFIGURED_MSG", lang)
            await self._channel.send_text(chat_id, text)

        return MAIN

    # ==================================================================
    # UTR submission
    # ==================================================================
    async def _do_show_payment_instructions(
        self, chat_id: int, order_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Show payment instructions for an existing PAYMENT_PENDING order."""
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        with self._uow() as uow:
            pay_svc = PaymentService(uow, self._object_store)
            instructions = pay_svc.present_instructions(order_id)
            uow.commit()

        if isinstance(instructions, UpiInstructions):
            await self._channel.send_payment_instructions(
                recipient_id=chat_id,
                upi_address=instructions.upi_address,
                qr_image=instructions.upi_qr_object_key,
                amount=instructions.amount_due,
                caption=self._renderer.text(
                    "PAYMENT_INSTRUCTIONS", lang,
                    upi_address=instructions.upi_address,
                    amount=str(instructions.amount_due),
                ),
            )
            context.user_data[_CTX_ORDER_ID] = order_id_str
            utr_prompt = self._renderer.text("ENTER_UTR_PROMPT", lang)
            await self._channel.send_text(chat_id, utr_prompt)
            return WAITING_UTR
        else:
            reason = getattr(instructions, "code", "")
            if reason == UPI_NOT_CONFIGURED:
                msg = self._renderer.text("UPI_NOT_CONFIGURED_MSG", lang)
            else:
                msg = self._renderer.text("TRANSITION_FAILED", lang, reason=reason)
            await self._channel.send_text(chat_id, msg)
            return MAIN

    async def _handle_utr_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        order_id_str = context.user_data.get(_CTX_ORDER_ID)
        if not order_id_str:
            return await self._send_main_menu(chat_id, lang)

        utr = update.message.text.strip()

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        actor = self._customer_actor(user)
        with self._uow() as uow:
            pay_svc = PaymentService(uow, self._object_store)
            result = pay_svc.submit_utr(order_id, utr, actor)
            if isinstance(result, Order):
                uow.commit()

        if isinstance(result, Order):
            context.user_data.pop(_CTX_ORDER_ID, None)
            msg = self._renderer.text("UTR_SUBMITTED_OK", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        if isinstance(result, Rejected):
            code = result.code
            if code == "INVALID_UTR_FORMAT":
                msg = self._renderer.text("INVALID_UTR_FORMAT", lang)
            elif code == "PAYMENT_NOT_PENDING":
                msg = self._renderer.text("TRANSITION_FAILED", lang, reason="Order is not awaiting payment")
                context.user_data.pop(_CTX_ORDER_ID, None)
                await self._channel.send_text(chat_id, msg)
                return MAIN
            else:
                msg = self._renderer.text("INVALID_UTR_FORMAT", lang)
        elif isinstance(result, Conflict):
            msg = self._renderer.text("DUPLICATE_UTR", lang)
        else:
            msg = self._renderer.text("TRANSITION_FAILED", lang, reason=str(result))
        await self._channel.send_text(chat_id, msg)
        return WAITING_UTR

    # ==================================================================
    # /orders
    # ==================================================================
    async def _cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        return await self._do_orders(chat_id, context)

    async def _do_orders(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        with self._uow() as uow:
            order_svc = OrderService(uow)
            orders = order_svc.list_customer_orders(user.user_id)
            uow.commit()

        if not orders:
            msg = self._renderer.text("NO_ORDERS_MSG", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        intro = self._renderer.text("ORDERS_LIST_INTRO", lang)
        buttons = []
        for order in orders:
            num = _parse_order_number(order)
            state_lbl = _state_label(order.state)
            btn_label = self._renderer.text(
                "ORDER_BUTTON_LABEL", lang,
                number=num, state=state_lbl, total=str(order.total_amount)
            )
            buttons.append(Button(label=btn_label, callback_data=_cb_order(str(order.order_id))))
        kb = Keyboard.inline_single_column(buttons)
        await self._channel.send_text(chat_id, intro, buttons=kb)
        return MAIN

    async def _do_order_detail(
        self, chat_id: int, order_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return await self._do_orders(chat_id, context)

        with self._uow() as uow:
            order_svc = OrderService(uow)
            result = order_svc.get_order_for_customer(order_id, user.user_id)
            # Also get payment reference
            payment = uow.payments.get_by_order(order_id) if isinstance(result, CustomerOrderDetail) else None
            uow.commit()

        if isinstance(result, (NotFound, NotAuthorized)):
            msg = self._renderer.text("ORDER_NOT_YOURS_MSG", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        assert isinstance(result, CustomerOrderDetail)
        order = result.order
        num = _parse_order_number(order)
        state_lbl = _state_label(order.state)
        payment_ref = result.payment_reference or self._renderer.text("PAYMENT_REF_NOT_PROVIDED", lang)

        lines_parts = []
        for item in order.items:
            lines_parts.append(
                f"  {str(item.ordered_quantity)} × ₹{str(item.unit_price)} = ₹{str(item.line_amount)}"
            )
        lines_text = "\n".join(lines_parts) if lines_parts else "-"

        text = self._renderer.text(
            "ORDER_DETAIL", lang,
            number=num,
            state=state_lbl,
            total=str(order.total_amount),
            payment_ref=payment_ref,
            lines=lines_text,
        )

        buttons = []
        # Allow UTR submission if payment pending
        if order.state == OrderState.PAYMENT_PENDING:
            pay_label = self._renderer.text("BTN_SUBMIT_PAYMENT", lang)
            buttons.append(
                Button(label=pay_label, callback_data=_cb_pay(order_id_str))
            )
        # Allow cancellation in cancellable states
        CANCELLABLE = {OrderState.PLACED, OrderState.PAYMENT_PENDING, OrderState.PAYMENT_SUBMITTED}
        if order.state in CANCELLABLE:
            cancel_label = self._renderer.text("BTN_CANCEL", lang)
            buttons.append(
                Button(label=cancel_label, callback_data=_cb_cancel(order_id_str))
            )
        kb = Keyboard.inline_single_column(buttons) if buttons else None
        await self._channel.send_text(chat_id, text, buttons=kb)
        return MAIN

    # ==================================================================
    # /cancel command and cancel confirm flow
    # ==================================================================
    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth
        # Show orders so user can pick which to cancel
        msg = self._renderer.text("CANCEL_ORDER_PROMPT", lang)
        await self._channel.send_text(chat_id, msg)
        return await self._do_orders(chat_id, context)

    async def _do_cancel_confirm_prompt(
        self, chat_id: int, order_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        with self._uow() as uow:
            order = uow.orders.get(order_id)
            uow.commit()

        if order is None:
            msg = self._renderer.text("ORDER_NOT_YOURS_MSG", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        num = _parse_order_number(order)
        prompt = self._renderer.text("CANCEL_CONFIRM_PROMPT", lang, number=num)
        kb = Keyboard.inline_single_column([
            Button(
                label=self._renderer.text("BTN_YES_CANCEL", lang),
                callback_data=_cb_cancel_confirm(order_id_str, True),
            ),
            Button(
                label=self._renderer.text("BTN_NO_KEEP", lang),
                callback_data=_cb_cancel_confirm(order_id_str, False),
            ),
        ])
        await self._channel.send_text(chat_id, prompt, buttons=kb)
        return MAIN

    async def _do_cancel_confirm(
        self,
        chat_id: int,
        order_id_str: str,
        decision: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> int:
        auth = await self._require_auth(chat_id)
        if auth is None:
            return ONBOARDING
        user, lang = auth

        if decision != "y":
            msg = self._renderer.text("CANCEL_ABORTED", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        actor = self._customer_actor(user)
        with self._uow() as uow:
            order_svc = OrderService(uow)
            result = order_svc.cancel(order_id, actor)
            if isinstance(result, Order):
                uow.commit()

        if isinstance(result, Order):
            num = _parse_order_number(result)
            msg = self._renderer.text("ORDER_CANCELLED", lang, order_number=num)
            await self._channel.send_text(chat_id, msg)
        elif isinstance(result, NotAuthorized):
            msg = self._renderer.text("CANCEL_NOT_ALLOWED", lang)
            await self._channel.send_text(chat_id, msg)
        else:
            msg = self._renderer.text("CANCEL_NOT_ALLOWED", lang)
            await self._channel.send_text(chat_id, msg)
        return MAIN

    # ==================================================================
    # Admin — /verify (list pending verifications)
    # ==================================================================
    async def _cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Alias for /verify (shows admin panel)."""
        chat_id = update.effective_chat.id
        return await self._do_admin_verify_list(chat_id, context)

    async def _cmd_verify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        chat_id = update.effective_chat.id
        return await self._do_admin_verify_list(chat_id, context)

    async def _do_admin_verify_list(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        with self._uow() as uow:
            admin_svc = AdminConsole(uow, self._auth, self._object_store)
            result = admin_svc.list_pending_verifications(chat_id)
            uow.commit()

        if isinstance(result, NotAuthorized):
            msg = self._renderer.text("SELLER_REQUIRED", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        if not result:
            msg = self._renderer.text("ADMIN_NO_PENDING", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        intro = self._renderer.text("ADMIN_VERIFY_INTRO", lang)
        await self._channel.send_text(chat_id, intro)

        for view in result:
            order = view.order
            num = _parse_order_number(order)
            utr_str = view.utr or "—"
            flag = self._renderer.text("ADMIN_STOCK_FLAG", lang) if not view.fulfillable else ""
            line = self._renderer.text(
                "ADMIN_ORDER_VERIFY_LINE", lang,
                number=num, total=str(order.total_amount), utr=utr_str, flag=flag
            )
            approve_label = self._renderer.text("BTN_APPROVE", lang, number=num)
            reject_label = self._renderer.text("BTN_REJECT", lang, number=num)
            kb = Keyboard.inline_single_column([
                Button(label=approve_label, callback_data=_cb_approve(str(order.order_id))),
                Button(label=reject_label, callback_data=_cb_reject(str(order.order_id))),
            ])
            await self._channel.send_text(chat_id, line, buttons=kb)

        return MAIN

    async def _do_admin_approve(
        self, chat_id: int, order_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        actor = self._seller_actor(user)
        with self._uow() as uow:
            order_svc = OrderService(uow)
            result = order_svc.verify_payment(order_id, actor)
            if isinstance(result, Order):
                uow.commit()

        if isinstance(result, Order):
            num = _parse_order_number(result)
            msg = self._renderer.text("APPROVE_SUCCESS", lang, number=num)
        else:
            reason = getattr(result, "reason", str(result))
            msg = self._renderer.text("APPROVE_FAILED", lang, reason=reason)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    async def _handle_reject_reason(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        order_id_str = context.user_data.pop(_CTX_REJECT_ORDER_ID, None)
        if not order_id_str:
            return MAIN

        reason = update.message.text.strip()
        if not (1 <= len(reason) <= 500):
            prompt = self._renderer.text("REJECT_REASON_PROMPT", lang)
            await self._channel.send_text(chat_id, prompt)
            context.user_data[_CTX_REJECT_ORDER_ID] = order_id_str
            return WAITING_REJECT

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        actor = self._seller_actor(user)
        with self._uow() as uow:
            order_svc = OrderService(uow)
            result = order_svc.reject_payment(order_id, actor, reason)
            if isinstance(result, Order):
                uow.commit()

        if isinstance(result, Order):
            msg = self._renderer.text("REJECT_SUCCESS", lang)
        else:
            err = getattr(result, "reason", str(result))
            msg = self._renderer.text("REJECT_FAILED", lang, reason=err)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    # ==================================================================
    # Admin — /active_orders
    # ==================================================================
    async def _cmd_active_orders(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        return await self._do_admin_active_orders(chat_id, context)

    async def _do_admin_active_orders(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        with self._uow() as uow:
            admin_svc = AdminConsole(uow, self._auth, self._object_store)
            result = admin_svc.list_active_orders(chat_id)
            uow.commit()

        if isinstance(result, NotAuthorized):
            msg = self._renderer.text("SELLER_REQUIRED", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        if not result:
            msg = self._renderer.text("ADMIN_NO_ACTIVE", lang)
            await self._channel.send_text(chat_id, msg)
            return MAIN

        intro = self._renderer.text("ADMIN_ACTIVE_INTRO", lang)
        await self._channel.send_text(chat_id, intro)

        for view in result:
            order = view.order
            num = _parse_order_number(order)
            state_lbl = _state_label(order.state)
            flag = self._renderer.text("ADMIN_STOCK_FLAG", lang) if not view.fulfillable else ""
            line = f"#{num} — {state_lbl} — ₹{order.total_amount}{flag}"

            buttons = []
            if order.state == OrderState.PAYMENT_SUBMITTED:
                approve_label = self._renderer.text("BTN_APPROVE", lang, number=num)
                reject_label = self._renderer.text("BTN_REJECT", lang, number=num)
                buttons.append(Button(label=approve_label, callback_data=_cb_approve(str(order.order_id))))
                buttons.append(Button(label=reject_label, callback_data=_cb_reject(str(order.order_id))))
            elif order.state == OrderState.APPROVED:
                ready_label = self._renderer.text("BTN_MARK_READY", lang, number=num)
                buttons.append(Button(label=ready_label, callback_data=_cb_ready(str(order.order_id))))
            elif order.state == OrderState.READY_FOR_PICKUP:
                collected_label = self._renderer.text("BTN_MARK_COLLECTED", lang, number=num)
                buttons.append(Button(label=collected_label, callback_data=_cb_collected(str(order.order_id))))
            # Offline approve if in PAYMENT_PENDING
            if order.state == OrderState.PAYMENT_PENDING:
                offline_label = self._renderer.text("BTN_OFFLINE_APPROVE", lang, number=num)
                buttons.append(Button(label=offline_label, callback_data=_cb_offline(str(order.order_id))))

            kb = Keyboard.inline_single_column(buttons) if buttons else None
            await self._channel.send_text(chat_id, line, buttons=kb)

        return MAIN

    async def _do_admin_mark_ready(
        self, chat_id: int, order_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        with self._uow() as uow:
            admin_svc = AdminConsole(uow, self._auth, self._object_store)
            result = admin_svc.mark_ready(order_id, chat_id)
            if not isinstance(result, (NotAuthorized, NotFound, Rejected, Conflict)):
                from marketplace.admin.service import ReadyResult
                if isinstance(result, ReadyResult):
                    uow.commit()
                elif isinstance(result, Order):
                    uow.commit()

        from marketplace.admin.service import ReadyResult
        if isinstance(result, ReadyResult):
            order = result.order
            num = _parse_order_number(order)
            if result.pickup_location_warning:
                msg = self._renderer.text("MARK_READY_NO_PICKUP", lang, number=num)
            else:
                msg = self._renderer.text("MARK_READY_SUCCESS", lang, number=num)
        elif isinstance(result, Order):
            num = _parse_order_number(result)
            msg = self._renderer.text("MARK_READY_SUCCESS", lang, number=num)
        else:
            reason = getattr(result, "reason", str(result))
            msg = self._renderer.text("TRANSITION_FAILED", lang, reason=reason)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    async def _do_admin_mark_collected(
        self, chat_id: int, order_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        actor = self._seller_actor(user)
        with self._uow() as uow:
            order_svc = OrderService(uow)
            result = order_svc.mark_collected(order_id, actor)
            if isinstance(result, Order):
                uow.commit()

        if isinstance(result, Order):
            num = _parse_order_number(result)
            msg = self._renderer.text("MARK_COLLECTED_SUCCESS", lang, number=num)
        else:
            reason = getattr(result, "reason", str(result))
            msg = self._renderer.text("TRANSITION_FAILED", lang, reason=reason)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    async def _do_admin_offline_approve(
        self, chat_id: int, order_id_str: str, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        try:
            order_id = uuid.UUID(order_id_str)
        except ValueError:
            return MAIN

        actor = self._seller_actor(user)
        with self._uow() as uow:
            order_svc = OrderService(uow)
            result = order_svc.offline_verify(order_id, actor)
            if isinstance(result, Order):
                uow.commit()

        if isinstance(result, Order):
            msg = self._renderer.text("OFFLINE_APPROVE_SUCCESS", lang)
        else:
            reason = getattr(result, "reason", str(result))
            msg = self._renderer.text("OFFLINE_APPROVE_FAILED", lang, reason=reason)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    # ==================================================================
    # Admin — /settings
    # ==================================================================
    async def _cmd_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        return await self._do_admin_settings(chat_id, context)

    async def _do_admin_settings(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        with self._uow() as uow:
            settings = uow.seller_settings.get()
            uow.commit()

        pickup = settings.pickup_location if settings else "—"
        upi = settings.upi_address if settings else "—"
        intro = self._renderer.text("ADMIN_SETTINGS_INTRO", lang)
        body = f"{intro}\nPickup: {pickup}\nUPI: {upi}"

        kb = Keyboard.inline_single_column([
            Button(
                label=self._renderer.text("BTN_SET_PICKUP", lang),
                callback_data="set_pickup",
            ),
            Button(
                label=self._renderer.text("BTN_SET_UPI_ADDR", lang),
                callback_data="set_upi_addr",
            ),
            # NOTE: UPI QR image upload is stubbed — requires file download wiring
        ])
        await self._channel.send_text(chat_id, body, buttons=kb)
        return MAIN

    async def _handle_pickup_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        text = update.message.text.strip()
        with self._uow() as uow:
            admin_svc = AdminConsole(uow, self._auth, self._object_store)
            result = admin_svc.set_pickup_location(text, chat_id)
            if not isinstance(result, (Rejected, NotAuthorized)):
                uow.commit()

        if isinstance(result, (Rejected, NotAuthorized)):
            msg = self._renderer.text("PICKUP_SET_FAILED", lang)
            await self._channel.send_text(chat_id, msg)
            return WAITING_PICKUP
        msg = self._renderer.text("PICKUP_SET_SUCCESS", lang)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    async def _handle_upi_addr_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        auth = await self._require_seller(chat_id)
        if auth is None:
            return MAIN
        user, lang = auth

        text = update.message.text.strip()
        with self._uow() as uow:
            admin_svc = AdminConsole(uow, self._auth, self._object_store)
            result = admin_svc.set_upi_address(text, chat_id)
            if not isinstance(result, (Rejected, NotAuthorized)):
                uow.commit()

        if isinstance(result, (Rejected, NotAuthorized)):
            msg = self._renderer.text("UPI_ADDR_SET_FAILED", lang)
            await self._channel.send_text(chat_id, msg)
            return WAITING_UPI_ADDR
        msg = self._renderer.text("UPI_ADDR_SET_SUCCESS", lang)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    # ==================================================================
    # Cancel multi-step input flows
    # ==================================================================
    async def _cmd_cancel_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        context.user_data.pop(_CTX_PRODUCT_ID, None)
        context.user_data.pop(_CTX_ORDER_ID, None)
        context.user_data.pop(_CTX_REJECT_ORDER_ID, None)
        msg = self._renderer.text("ADMIN_INPUT_CANCELLED", lang)
        await self._channel.send_text(chat_id, msg)
        return MAIN

    # ==================================================================
    # Voice message handlers (Req 14)
    # ==================================================================
    async def _handle_voice_onboarding(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        step = ConversationStep(
            step_id="ONBOARDING",
            prompt_key="SHARE_CONTACT_PROMPT",
        )
        await self._voice_handler.handle(chat_id, step, lang)
        return ONBOARDING

    async def _handle_voice_main(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        is_admin = self._is_seller(chat_id)
        step = ConversationStep(
            step_id="MAIN",
            prompt_key="MAIN_MENU_PROMPT",
            keyboard=self._main_kb(lang, is_admin),
        )
        await self._voice_handler.handle(chat_id, step, lang)
        return MAIN

    async def _handle_voice_qty(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        product_name = context.user_data.get(_CTX_PRODUCT_NAME, "")
        moq = context.user_data.get(_CTX_PRODUCT_MOQ, "")
        unit = context.user_data.get(_CTX_PRODUCT_UNIT, "")
        step = ConversationStep(
            step_id="WAITING_QTY",
            prompt_key="ENTER_QUANTITY_PROMPT",
            placeholders={"name": product_name, "moq": moq, "unit": unit},
        )
        await self._voice_handler.handle(chat_id, step, lang)
        return WAITING_QTY

    async def _handle_voice_utr(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        step = ConversationStep(
            step_id="WAITING_UTR",
            prompt_key="ENTER_UTR_PROMPT",
        )
        await self._voice_handler.handle(chat_id, step, lang)
        return WAITING_UTR

    # ==================================================================
    # Unknown text / fallback
    # ==================================================================
    async def _handle_unknown_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        is_admin = self._is_seller(chat_id)
        msg = self._renderer.text("UNKNOWN_INPUT", lang)
        await self._channel.send_text(
            chat_id, msg, buttons=self._main_kb(lang, is_admin)
        )
        return MAIN

    async def _handle_fallback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        chat_id = update.effective_chat.id
        return await self._send_main_menu(chat_id)

    async def _handle_voice_admin_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Voice fallback for admin text-input states."""
        chat_id = update.effective_chat.id
        lang = self._get_user_lang(chat_id)
        step = ConversationStep(step_id="ADMIN_INPUT", prompt_key="UNKNOWN_INPUT")
        await self._voice_handler.handle(chat_id, step, lang)
        return WAITING_REJECT  # placeholder; real state determined by caller

    # ==================================================================
    # Shared helpers
    # ==================================================================
    async def _send_main_menu(
        self, chat_id: int, lang: Optional[Language] = None
    ) -> int:
        if lang is None:
            lang = self._get_user_lang(chat_id)
        is_admin = self._is_seller(chat_id)
        prompt = self._renderer.text("MAIN_MENU_PROMPT", lang)
        await self._channel.send_text(
            chat_id, prompt, buttons=self._main_kb(lang, is_admin)
        )
        return MAIN
