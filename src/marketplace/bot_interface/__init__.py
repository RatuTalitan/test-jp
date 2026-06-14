"""Bot_Interface subsystem (presentation layer).

Houses the ``MessagingChannel`` adapter, the ``UpdateSource`` abstraction
(long-poll for v1, webhook for Phase 2), the Message_Catalog (i18n) rendering,
and all Telegram-specific code. This is the ONLY layer that turns domain
results/codes into user-facing words. No business logic lives here.

Public seams (Tasks 19.1-19.6):

* ``MessagingChannel`` / ``FakeMessagingChannel`` / ``Renderer`` and the
  channel-neutral ``Button`` / ``Keyboard`` / ``DeliveryResult`` types
  (``channel``).
* ``UpdateSource`` / ``LongPollSource`` / ``WebhookSource`` with bot-origin
  authenticity verification (``update_source``).
* The voice-message baseline handler (``voice``).
* The modern Telegram presentation building blocks -- command menu, language
  switch, branded onboarding, navigation keyboards (``presentation``).

The Telegram SDK implementation (``TelegramMessagingChannel``) lives in
``telegram_channel`` and is imported lazily by callers that need it, so the rest
of the package (and the test suite) never requires the SDK to be importable.
"""

from marketplace.bot_interface.channel import (
    Button,
    DeliveryResult,
    DownloadError,
    FakeMessagingChannel,
    Keyboard,
    MessagingChannel,
    Renderer,
    coerce_language,
)
from marketplace.bot_interface.i18n import (
    DEFAULT_CATALOG,
    DEFAULT_LANGUAGE,
    Language,
    MessageCatalog,
    resolve,
)
from marketplace.bot_interface.presentation import (
    CUSTOMER_COMMANDS,
    LANGUAGE_COMMAND,
    SELLER_COMMANDS,
    CommandSpec,
    LanguageSwitch,
    Onboarding,
    ResolvedCommand,
    command_menu_for,
    language_callback_data,
    main_menu_keyboard,
    main_menu_reply_keyboard,
    parse_language_callback,
)
from marketplace.bot_interface.update_source import (
    WEBHOOK_SECRET_HEADER,
    LongPollSource,
    UpdateSource,
    WebhookSource,
    build_update_source,
)
from marketplace.bot_interface.voice import (
    ConversationStep,
    VoiceAck,
    VoiceMessageHandler,
)

__all__ = [
    # channel
    "Button",
    "Keyboard",
    "DeliveryResult",
    "DownloadError",
    "MessagingChannel",
    "FakeMessagingChannel",
    "Renderer",
    "coerce_language",
    # i18n
    "Language",
    "MessageCatalog",
    "DEFAULT_CATALOG",
    "DEFAULT_LANGUAGE",
    "resolve",
    # update source
    "UpdateSource",
    "LongPollSource",
    "WebhookSource",
    "WEBHOOK_SECRET_HEADER",
    "build_update_source",
    # voice
    "ConversationStep",
    "VoiceAck",
    "VoiceMessageHandler",
    # presentation
    "CommandSpec",
    "ResolvedCommand",
    "CUSTOMER_COMMANDS",
    "SELLER_COMMANDS",
    "LANGUAGE_COMMAND",
    "command_menu_for",
    "language_callback_data",
    "parse_language_callback",
    "LanguageSwitch",
    "Onboarding",
    "main_menu_keyboard",
    "main_menu_reply_keyboard",
]
