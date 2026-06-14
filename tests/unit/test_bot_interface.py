"""Focused unit tests for the Bot_Interface building blocks (Tasks 19.1/2/3/6).

These exercise the presentation-layer seams with **fakes only** -- no real
network call is ever made:

* catalog-driven rendering picks the active language, with the Hindi default
  for an unset preference (Req 18.1, 18.2, 18.5);
* webhook update authenticity: a matching secret-token header is accepted and a
  missing/non-matching one is discarded with **no state change** (Req 12.3/12.4);
* the voice baseline acknowledges receipt, says voice is not interpreted in v1,
  re-presents the current step's options, and retains the step (Req 14.1-14.4);
* the persistent command menu (``setMyCommands``) always includes ``/language``
  and gates Seller-only commands behind ``require_admin`` (Req 1.7, 18.3, 18.8);
* the language switch persists the choice via the Auth_Service and flips the
  rendered language immediately (Req 18.3, 18.4, 18.8);
* branded onboarding introduces "Jan Purna (जन पूर्णा)" before Share Contact
  (Req 1.1/1.4/1.5).
"""

import pytest

from marketplace.auth import AuthService
from marketplace.bot_interface import (
    Button,
    ConversationStep,
    FakeMessagingChannel,
    Keyboard,
    LanguageSwitch,
    LongPollSource,
    Onboarding,
    Renderer,
    VoiceMessageHandler,
    WebhookSource,
    build_update_source,
    command_menu_for,
    language_callback_data,
    parse_language_callback,
)
from marketplace.bot_interface.channel import DownloadError
from marketplace.bot_interface.i18n import Language as CatalogLanguage
from marketplace.bot_interface.telegram_channel import TelegramMessagingChannel
from marketplace.config.branding import DISPLAY_NAME
from marketplace.domain import (
    InMemoryDatabase,
    InMemoryUnitOfWork,
    Language,
    User,
)
from marketplace.auth import SharedContact

SELLER_TELEGRAM_ID = 999_000_111
CUSTOMER_TELEGRAM_ID = 555_222_333


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def db() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def auth(db: InMemoryDatabase) -> AuthService:
    return AuthService(
        uow_factory=lambda: InMemoryUnitOfWork(db),
        seller_telegram_id=SELLER_TELEGRAM_ID,
    )


@pytest.fixture
def channel() -> FakeMessagingChannel:
    return FakeMessagingChannel()


@pytest.fixture
def renderer() -> Renderer:
    return Renderer()


# --------------------------------------------------------------------------- #
# A tiny fake python-telegram-bot Bot for the adapter tests (no network).
# --------------------------------------------------------------------------- #
class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeFile:
    def __init__(self, blob: bytes, file_size=None) -> None:
        self._blob = blob
        self.file_size = file_size if file_size is not None else len(blob)

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._blob)


class FakeBot:
    """Records Bot API calls so the Telegram adapter can be tested offline."""

    def __init__(self, files=None) -> None:
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []
        self.commands_calls: list[dict] = []
        self.menu_button_calls: list[dict] = []
        self._files = dict(files or {})
        self._mid = 1

    def _next(self) -> _FakeMessage:
        m = _FakeMessage(self._mid)
        self._mid += 1
        return m

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return self._next()

    async def send_photo(self, chat_id, photo, caption=None):
        self.sent_photos.append(
            {"chat_id": chat_id, "photo": photo, "caption": caption}
        )
        return self._next()

    async def get_file(self, file_id):
        if file_id not in self._files:
            from telegram.error import TelegramError

            raise TelegramError(f"no such file {file_id}")
        blob, size = self._files[file_id]
        return _FakeFile(blob, file_size=size)

    async def set_my_commands(self, commands, scope=None):
        self.commands_calls.append({"commands": commands, "scope": scope})

    async def set_chat_menu_button(self, chat_id=None, menu_button=None):
        self.menu_button_calls.append({"chat_id": chat_id, "menu_button": menu_button})


def _register_customer(auth: AuthService) -> User:
    return auth.register_contact(
        CUSTOMER_TELEGRAM_ID,
        SharedContact(phone_number="+919812345678", user_id=CUSTOMER_TELEGRAM_ID),
    )


# =========================================================================== #
# 1. Catalog-driven rendering picks the active language (Hindi default).
# =========================================================================== #
def test_renderer_resolves_active_language_english(renderer):
    assert renderer.text("NOT_AUTHORIZED", Language.EN) == (
        "You are not authorized to perform this action."
    )


def test_renderer_unset_preference_defaults_to_hindi(renderer):
    # None preference -> Hindi (Req 18.2).
    assert renderer.text("NOT_AUTHORIZED", None) == renderer.text(
        "NOT_AUTHORIZED", Language.HI
    )
    assert renderer.text("NOT_AUTHORIZED", None) == "इस कार्य के लिए आपको अनुमति नहीं है।"


def test_renderer_accepts_domain_and_catalog_language(renderer):
    # Both the domain Language enum and the i18n Language enum resolve the same.
    assert renderer.text("BTN_BROWSE", Language.EN) == renderer.text(
        "BTN_BROWSE", CatalogLanguage.ENGLISH
    )


def test_renderer_emits_brand_strings_language_independently(renderer):
    assert renderer.text("BRAND_NAME", Language.HI) == "Jan Purna"
    assert renderer.text("BRAND_NAME", Language.EN) == "Jan Purna"
    assert renderer.brand_display_name() == DISPLAY_NAME


def test_renderer_builds_button_with_resolved_label(renderer):
    btn = renderer.button("BTN_VIEW_CART", Language.EN, callback_data="nav:cart")
    assert btn.label == "View cart"
    assert btn.callback_data == "nav:cart"


# =========================================================================== #
# 2. Webhook authenticity: accept matching, discard non-matching (no state).
# =========================================================================== #
SECRET = "s3cr3t-token-value"


def test_webhook_accepts_matching_secret_header():
    source = WebhookSource(SECRET)
    request = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    assert source.verify_authenticity(request) is True


def test_webhook_accepts_case_insensitive_header_name():
    source = WebhookSource(SECRET)
    request = {"x-telegram-bot-api-secret-token": SECRET}
    assert source.verify_authenticity(request) is True


@pytest.mark.parametrize(
    "request_obj",
    [
        {"X-Telegram-Bot-Api-Secret-Token": "wrong-token"},
        {"X-Telegram-Bot-Api-Secret-Token": ""},
        {"Some-Other-Header": SECRET},  # header absent
        {},  # no headers at all
        None,
    ],
)
def test_webhook_rejects_non_matching_or_missing_secret(request_obj):
    source = WebhookSource(SECRET)
    assert source.verify_authenticity(request_obj) is False


def test_webhook_with_no_configured_secret_rejects_everything():
    source = WebhookSource(None)
    assert source.verify_authenticity({"X-Telegram-Bot-Api-Secret-Token": "anything"}) is False


def test_guard_runs_handler_only_for_authentic_update_no_state_change():
    """A rejected update must never invoke the state-changing callback."""
    source = WebhookSource(SECRET)
    state = {"writes": 0}

    def mutate():
        state["writes"] += 1
        return "did-work"

    # Authentic -> handler runs.
    accepted = source.guard({"X-Telegram-Bot-Api-Secret-Token": SECRET}, mutate)
    assert accepted == "did-work"
    assert state["writes"] == 1

    # Inauthentic -> discarded with NO state change (Req 12.4).
    rejected = source.guard({"X-Telegram-Bot-Api-Secret-Token": "nope"}, mutate)
    assert rejected is None
    assert state["writes"] == 1  # unchanged


def test_object_with_headers_attribute_is_supported():
    class _Req:
        def __init__(self, headers):
            self.headers = headers

    source = WebhookSource(SECRET)
    assert source.verify_authenticity(_Req({"X-Telegram-Bot-Api-Secret-Token": SECRET})) is True
    assert source.verify_authenticity(_Req({"X-Telegram-Bot-Api-Secret-Token": "x"})) is False


def test_long_poll_source_is_always_authentic():
    # v1 long-polling pulls from Telegram directly -> intrinsically authentic.
    assert LongPollSource().verify_authenticity() is True
    assert LongPollSource().verify_authenticity({"anything": "here"}) is True


class _Cfg:
    def __init__(self, use_webhook, token=None):
        self.use_webhook = use_webhook
        self.webhook_secret_token = token


def test_build_update_source_switches_on_config():
    assert isinstance(build_update_source(_Cfg(False)), LongPollSource)
    src = build_update_source(_Cfg(True, SECRET))
    assert isinstance(src, WebhookSource)
    assert src.verify_authenticity({"X-Telegram-Bot-Api-Secret-Token": SECRET}) is True


def test_build_update_source_unwraps_secret_wrapper():
    class _Secret:
        def __init__(self, v):
            self._v = v

        def reveal(self):
            return self._v

    src = build_update_source(_Cfg(True, _Secret(SECRET)))
    assert src.verify_authenticity({"X-Telegram-Bot-Api-Secret-Token": SECRET}) is True


# =========================================================================== #
# 3. Voice baseline: acknowledge, re-present current step, retain step.
# =========================================================================== #
def _quantity_step() -> ConversationStep:
    kb = Keyboard.inline_single_column(
        [
            Button(label="MOQ", callback_data="qty:moq"),
            Button(label="MOQ x2", callback_data="qty:moq2"),
        ]
    )
    return ConversationStep(
        step_id="ENTER_QUANTITY",
        prompt_key="ENTER_QUANTITY",
        keyboard=kb,
        placeholders={"unit": "क्विंटल"},
    )


async def test_voice_acknowledges_and_represents_current_step(channel):
    handler = VoiceMessageHandler(channel)
    step = _quantity_step()

    ack = await handler.handle(CUSTOMER_TELEGRAM_ID, step, language=None)

    sent = channel.last_text()
    assert sent is not None and sent.recipient_id == CUSTOMER_TELEGRAM_ID
    # Confirms receipt (Req 14.1) and says voice is not interpreted in v1 (14.3).
    assert "हमें आपका वॉइस संदेश मिल गया है।" in sent.text
    assert "वॉइस संदेश समझे नहीं जाते" in sent.text
    # Re-presents the current step prompt + its buttons (Req 14.2).
    assert "क्विंटल" in sent.text
    assert sent.buttons is step.keyboard
    assert sent.buttons.labels() == ["MOQ", "MOQ x2"]
    # The step is retained, never advanced (Req 14.2/14.4).
    assert ack.retained_step is step
    assert ack.processable is True


async def test_voice_unprocessable_fallback_retains_step(channel):
    handler = VoiceMessageHandler(channel)
    step = _quantity_step()

    ack = await handler.handle(
        CUSTOMER_TELEGRAM_ID, step, language=Language.EN, processable=False
    )

    sent = channel.last_text()
    assert "We have received your voice message." in sent.text
    assert "could not handle this voice message" in sent.text
    # Options re-presented and the same step retained (Req 14.4).
    assert sent.buttons is step.keyboard
    assert ack.retained_step is step
    assert ack.processable is False


async def test_voice_step_without_keyboard_still_acknowledges(channel):
    handler = VoiceMessageHandler(channel)
    ack = await handler.handle(CUSTOMER_TELEGRAM_ID, ConversationStep(), language=None)
    sent = channel.last_text()
    assert "हमें आपका वॉइस संदेश मिल गया है।" in sent.text
    assert sent.buttons is None
    assert ack.processable is True


# =========================================================================== #
# 4. setMyCommands includes /language and gates Seller-only commands.
# =========================================================================== #
def test_command_menu_for_customer_has_language_no_seller_commands(auth, renderer):
    menu = command_menu_for(auth, CUSTOMER_TELEGRAM_ID, Language.EN, renderer)
    names = [c.command for c in menu]
    assert "language" in names  # always reachable (Req 18.3/18.8)
    for customer_cmd in ("browse", "cart", "orders", "help"):
        assert customer_cmd in names
    # Seller-only commands are NOT exposed to a Customer (Req 1.7).
    for seller_cmd in ("catalog", "verify", "active_orders", "settings"):
        assert seller_cmd not in names


def test_command_menu_for_seller_includes_seller_commands(auth, renderer):
    menu = command_menu_for(auth, SELLER_TELEGRAM_ID, Language.EN, renderer)
    names = [c.command for c in menu]
    assert "language" in names
    for seller_cmd in ("catalog", "verify", "active_orders", "settings"):
        assert seller_cmd in names


def test_command_menu_descriptions_are_localized(auth, renderer):
    en = {c.command: c.description for c in command_menu_for(auth, CUSTOMER_TELEGRAM_ID, Language.EN, renderer)}
    hi = {c.command: c.description for c in command_menu_for(auth, CUSTOMER_TELEGRAM_ID, Language.HI, renderer)}
    assert en["language"] == "Change language"
    assert hi["language"] == "भाषा बदलें"


async def test_adapter_registers_command_menu_and_menu_button(auth):
    bot = FakeBot()
    adapter = TelegramMessagingChannel(bot)

    await adapter.register_command_menu(auth, SELLER_TELEGRAM_ID, Language.EN)

    assert len(bot.commands_calls) == 1
    registered = {c.command for c in bot.commands_calls[0]["commands"]}
    assert "language" in registered
    assert {"catalog", "verify", "active_orders", "settings"} <= registered
    # The command set is scoped to the user's chat.
    assert bot.commands_calls[0]["scope"] is not None
    # The chat Menu Button was configured for one-tap access.
    assert len(bot.menu_button_calls) == 1
    assert bot.menu_button_calls[0]["chat_id"] == SELLER_TELEGRAM_ID


async def test_adapter_command_menu_gates_seller_commands_for_customer(auth):
    bot = FakeBot()
    adapter = TelegramMessagingChannel(bot)
    await adapter.register_command_menu(auth, CUSTOMER_TELEGRAM_ID, Language.HI)
    registered = {c.command for c in bot.commands_calls[0]["commands"]}
    assert "language" in registered
    assert not ({"catalog", "verify", "active_orders", "settings"} & registered)


# =========================================================================== #
# 5. Language switch persists via Auth and flips the rendered language.
# =========================================================================== #
def test_parse_language_callback_roundtrip():
    assert parse_language_callback(language_callback_data("EN")) == "EN"
    assert parse_language_callback(language_callback_data("HI")) == "HI"
    assert parse_language_callback("lang:FR") is None
    assert parse_language_callback("nav:browse") is None
    assert parse_language_callback(None) is None


async def test_language_switch_persists_and_flips_language(auth, channel, db):
    _register_customer(auth)
    switch = LanguageSwitch(auth, channel, Renderer())

    result, active = await switch.apply_selection(
        CUSTOMER_TELEGRAM_ID,
        language_callback_data("EN"),
        recipient_id=CUSTOMER_TELEGRAM_ID,
    )

    # Persisted via Auth_Service (Req 18.4).
    assert isinstance(result, User)
    assert result.language_preference is Language.EN
    assert active is Language.EN
    # Subsequent identify confirms the stored preference round-trips.
    again = auth.identify(CUSTOMER_TELEGRAM_ID)
    assert again.language_preference is Language.EN
    # The confirmation was rendered immediately in the NEW language (Req 18.5).
    assert channel.last_text().text == "Language has been set to English."


async def test_language_switch_available_to_seller(auth, channel):
    auth.register_contact(
        SELLER_TELEGRAM_ID,
        SharedContact(phone_number="+919800000000", user_id=SELLER_TELEGRAM_ID),
    )
    switch = LanguageSwitch(auth, channel, Renderer())
    result, active = await switch.apply_selection(
        SELLER_TELEGRAM_ID, language_callback_data("HI"), recipient_id=SELLER_TELEGRAM_ID
    )
    assert isinstance(result, User)
    assert result.language_preference is Language.HI
    assert active is Language.HI


async def test_language_switch_present_offers_both_choices(auth, channel):
    switch = LanguageSwitch(auth, channel, Renderer())
    await switch.present(CUSTOMER_TELEGRAM_ID, language=None)
    sent = channel.last_text()
    assert sent.buttons is not None
    assert sent.buttons.labels() == ["हिंदी", "English"]
    # Callback payloads carry the language codes.
    datas = [b.callback_data for row in sent.buttons.rows for b in row]
    assert datas == [language_callback_data("HI"), language_callback_data("EN")]


async def test_language_switch_ignores_unrecognized_callback(auth, channel):
    switch = LanguageSwitch(auth, channel, Renderer())
    result, active = await switch.apply_selection(CUSTOMER_TELEGRAM_ID, "nav:browse")
    # Never blocks; nothing changed.
    assert result is None and active is None
    assert channel.sent_texts == []


# =========================================================================== #
# 6. Branded onboarding: brand intro before Share Contact.
# =========================================================================== #
async def test_onboarding_welcome_is_brand_first_then_share_contact(channel):
    onboarding = Onboarding(channel, Renderer())

    await onboarding.start(CUSTOMER_TELEGRAM_ID, language=None)

    # First message is the branded welcome introducing Jan Purna (जन पूर्णा).
    assert len(channel.sent_texts) == 1
    welcome = channel.sent_texts[0].text
    assert "Jan Purna (जन पूर्णा)" in welcome
    # Then the Share Contact action is presented (Req 1.1/1.4/1.5).
    assert len(channel.contact_requests) == 1
    assert channel.contact_requests[0].recipient_id == CUSTOMER_TELEGRAM_ID
    assert "संपर्क" in channel.contact_requests[0].explanation


async def test_onboarding_welcome_localized_english(channel):
    onboarding = Onboarding(channel, Renderer())
    await onboarding.start(CUSTOMER_TELEGRAM_ID, language=Language.EN)
    welcome = channel.sent_texts[0].text
    assert welcome == "Welcome to Jan Purna (जन पूर्णा)."


# =========================================================================== #
# 7. Fake channel download_file budget + Telegram adapter wiring.
# =========================================================================== #
async def test_fake_channel_download_respects_budget():
    channel = FakeMessagingChannel(files={"voice-1": b"x" * 100})
    assert await channel.download_file("voice-1", max_bytes=1000) == b"x" * 100
    with pytest.raises(DownloadError):
        await channel.download_file("voice-1", max_bytes=10)  # too large
    with pytest.raises(DownloadError):
        await channel.download_file("missing", max_bytes=1000)  # unknown


async def test_telegram_adapter_send_text_and_keyboard():
    bot = FakeBot()
    adapter = TelegramMessagingChannel(bot)
    kb = Keyboard.inline_single_column([Button("Browse", callback_data="nav:browse")])

    result = await adapter.send_text(CUSTOMER_TELEGRAM_ID, "hello", buttons=kb)

    assert result.ok is True
    assert bot.sent_messages[0]["chat_id"] == CUSTOMER_TELEGRAM_ID
    assert bot.sent_messages[0]["text"] == "hello"
    assert bot.sent_messages[0]["reply_markup"] is not None


async def test_telegram_adapter_request_contact_uses_reply_keyboard():
    bot = FakeBot()
    adapter = TelegramMessagingChannel(bot, Renderer())
    result = await adapter.request_contact(CUSTOMER_TELEGRAM_ID, "share please")
    assert result.ok is True
    markup = bot.sent_messages[0]["reply_markup"]
    # The single button requests the contact (Share Contact action).
    assert markup.keyboard[0][0].request_contact is True


async def test_telegram_adapter_download_file_enforces_budget():
    bot = FakeBot(files={"f1": (b"a" * 50, 50), "big": (b"b" * 5000, 5000)})
    adapter = TelegramMessagingChannel(bot)
    assert await adapter.download_file("f1", max_bytes=100) == b"a" * 50
    with pytest.raises(DownloadError):
        await adapter.download_file("big", max_bytes=100)
    with pytest.raises(DownloadError):
        await adapter.download_file("does-not-exist", max_bytes=100)


async def test_telegram_adapter_payment_instructions_photo_vs_text():
    bot = FakeBot()
    adapter = TelegramMessagingChannel(bot)
    # With a QR image -> sent as a photo with caption.
    await adapter.send_payment_instructions(
        CUSTOMER_TELEGRAM_ID, "seller@upi", qr_image=b"qr", amount="100.00", caption="pay"
    )
    assert bot.sent_photos and bot.sent_photos[0]["caption"] == "pay"
    # Without a QR image -> sent as text.
    await adapter.send_payment_instructions(
        CUSTOMER_TELEGRAM_ID, "seller@upi", qr_image=None, amount="100.00", caption="pay-text"
    )
    assert bot.sent_messages and bot.sent_messages[-1]["text"] == "pay-text"
