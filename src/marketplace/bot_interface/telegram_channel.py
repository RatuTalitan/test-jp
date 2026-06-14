"""The Telegram implementation of :class:`MessagingChannel` (Tasks 19.1, 19.6).

This is the **only** module that imports the python-telegram-bot SDK. It turns
the channel-neutral presentation types (:class:`~marketplace.bot_interface.channel.Keyboard`,
:class:`~marketplace.bot_interface.channel.Button`) into Telegram Bot API calls
and exposes the modern presentation features (persistent command menu via
``setMyCommands``, the chat Menu Button, inline + reply keyboards) behind the
abstraction so domain services never see them (design.md -> "Modern Telegram
Presentation").

The adapter receives a python-telegram-bot ``Bot`` (or any duck-typed object
exposing the same async methods) at construction. Production code builds a real
``telegram.Bot`` from the configured token via :meth:`from_token`; tests inject
a lightweight fake that records calls, so **no real network call is made in the
test suite**.
"""

from __future__ import annotations

from typing import Optional

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
)
from telegram.error import TelegramError

from marketplace.bot_interface.channel import (
    DeliveryResult,
    DownloadError,
    Keyboard,
    MessagingChannel,
    Renderer,
)
from marketplace.bot_interface.presentation import command_menu_for

__all__ = ["TelegramMessagingChannel"]


class TelegramMessagingChannel(MessagingChannel):
    """A :class:`MessagingChannel` backed by python-telegram-bot v21+."""

    def __init__(self, bot: Bot, renderer: Optional[Renderer] = None) -> None:
        self._bot = bot
        self._renderer = renderer if renderer is not None else Renderer()

    @classmethod
    def from_token(cls, token: str, renderer: Optional[Renderer] = None) -> "TelegramMessagingChannel":
        """Build an adapter from a bot token (production wiring, Task 22)."""
        return cls(Bot(token=token), renderer=renderer)

    # -- Keyboard conversion -----------------------------------------------
    @staticmethod
    def _to_markup(keyboard: Optional[Keyboard]):
        """Convert a channel-neutral :class:`Keyboard` to a Telegram markup."""
        if keyboard is None:
            return None
        if keyboard.inline:
            rows = [
                [
                    InlineKeyboardButton(
                        text=b.label, callback_data=b.callback_data or b.label
                    )
                    for b in row
                ]
                for row in keyboard.rows
            ]
            return InlineKeyboardMarkup(rows)
        # Reply keyboard: large labels, optional Share Contact request.
        rows = [
            [
                KeyboardButton(text=b.label, request_contact=b.request_contact)
                for b in row
            ]
            for row in keyboard.rows
        ]
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True,
            one_time_keyboard=keyboard.one_time,
        )

    # -- MessagingChannel interface ----------------------------------------
    async def send_text(
        self,
        recipient_id: int,
        text: str,
        buttons: Optional[Keyboard] = None,
    ) -> DeliveryResult:
        try:
            message = await self._bot.send_message(
                chat_id=recipient_id,
                text=text,
                reply_markup=self._to_markup(buttons),
            )
        except TelegramError as exc:
            return DeliveryResult.failed(str(exc))
        return DeliveryResult.delivered(getattr(message, "message_id", None))

    async def send_payment_instructions(
        self,
        recipient_id: int,
        upi_address: str,
        qr_image: Optional[object],
        amount: object,
        caption: Optional[str] = None,
    ) -> DeliveryResult:
        # The localized framing copy is composed by the caller/renderer; this
        # adapter only delivers it. When a QR image/reference is supplied it is
        # sent as a photo with the instructions as the caption; otherwise the
        # instructions are sent as text.
        body = caption if caption is not None else (
            f"{upi_address}\n{amount}"
        )
        try:
            if qr_image is not None:
                message = await self._bot.send_photo(
                    chat_id=recipient_id, photo=qr_image, caption=body
                )
            else:
                message = await self._bot.send_message(chat_id=recipient_id, text=body)
        except TelegramError as exc:
            return DeliveryResult.failed(str(exc))
        return DeliveryResult.delivered(getattr(message, "message_id", None))

    async def request_contact(
        self, recipient_id: int, explanation: str
    ) -> DeliveryResult:
        # The Share Contact action is a reply keyboard with a single big button.
        share_label = self._renderer.text("BTN_SHARE_CONTACT")
        markup = ReplyKeyboardMarkup(
            [[KeyboardButton(text=share_label, request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        try:
            message = await self._bot.send_message(
                chat_id=recipient_id, text=explanation, reply_markup=markup
            )
        except TelegramError as exc:
            return DeliveryResult.failed(str(exc))
        return DeliveryResult.delivered(getattr(message, "message_id", None))

    async def download_file(self, file_id: str, max_bytes: int) -> bytes:
        try:
            tg_file = await self._bot.get_file(file_id)
            # Reject early if Telegram reports a size over the budget.
            size = getattr(tg_file, "file_size", None)
            if size is not None and size > max_bytes:
                raise DownloadError(
                    f"file {file_id!r} ({size} bytes) exceeds the {max_bytes}-byte budget"
                )
            data = await tg_file.download_as_bytearray()
        except DownloadError:
            raise
        except TelegramError as exc:
            raise DownloadError(f"could not download {file_id!r}: {exc}") from exc
        blob = bytes(data)
        if len(blob) > max_bytes:
            raise DownloadError(
                f"file {file_id!r} ({len(blob)} bytes) exceeds the {max_bytes}-byte budget"
            )
        return blob

    # -- Modern presentation (Task 19.6) -----------------------------------
    async def register_command_menu(
        self,
        auth: object,
        telegram_user_id: int,
        language: object = None,
    ) -> None:
        """Register the persistent command menu + chat Menu Button for a user.

        The command list is built by :func:`command_menu_for`, which gates the
        Seller-only commands behind ``Auth_Service.require_admin`` (Req 1.7) and
        always includes ``/language`` (Req 18.3, 18.8). Commands are scoped to
        the user's chat so each role sees only its own menu.
        """
        commands = command_menu_for(auth, telegram_user_id, language, self._renderer)
        bot_commands = [BotCommand(c.command, c.description) for c in commands]
        scope = BotCommandScopeChat(chat_id=telegram_user_id)
        await self._bot.set_my_commands(bot_commands, scope=scope)
        # One-tap access to the bot's primary entry point via the Menu Button.
        await self._bot.set_chat_menu_button(
            chat_id=telegram_user_id, menu_button=MenuButtonCommands()
        )
