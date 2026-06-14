"""Modern Telegram presentation building blocks (Task 19.6 / Req 1, 18).

These are **presentation-layer concerns that live entirely inside the Telegram
adapter seam** (design.md -> "Modern Telegram Presentation"). Domain services
(Auth, Catalog, Cart, Order, Payment, Notification, Admin) remain
channel-agnostic and never reference command menus, menu buttons, or keyboard
types -- preserving the seam that makes WhatsApp / web / Mini App additions
cheap (Phase 2).

What this module provides (channel-neutral; the Telegram adapter turns these
into Bot API calls):

* **Persistent command menu** (`setMyCommands`): :func:`command_menu_for`
  builds the localized command list for a user. Customer commands (browse,
  cart, my orders, help, **/language**) are always present; **Seller-only**
  commands are appended **only when** ``Auth_Service.require_admin`` authorizes
  the actor (Req 1.7). The language-switch command is always included so the
  toggle is always reachable (Req 18.3, 18.8).
* **Language-switch control** (:class:`LanguageSwitch`): available to both
  Customers and the Seller, it presents Hindi (हिंदी) and English as large,
  clearly labeled buttons; on selection it calls
  ``Auth_Service.set_language_preference`` and lets the caller re-render in the
  selected language (Req 18.3, 18.4, 18.8). It never blocks a flow (Req 18.9).
* **Branded onboarding** (:class:`Onboarding`): the ``/start`` welcome
  introduces "Jan Purna (जन पूर्णा)" from the branding module before presenting
  the Share Contact action (Req 1.1/1.4/1.5).
* **Navigation keyboards**: callback-driven inline keyboards as the primary
  navigation, plus large-label reply keyboards as the elderly-friendly option.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from marketplace.bot_interface.channel import (
    Button,
    DeliveryResult,
    Keyboard,
    MessagingChannel,
    Renderer,
    coerce_language,
)
from marketplace.bot_interface.i18n import Language
from marketplace.domain.results import is_failure

__all__ = [
    "CommandSpec",
    "ResolvedCommand",
    "CUSTOMER_COMMANDS",
    "SELLER_COMMANDS",
    "LANGUAGE_COMMAND",
    "command_menu_for",
    "LANGUAGE_CALLBACK_PREFIX",
    "language_callback_data",
    "parse_language_callback",
    "LanguageSwitch",
    "Onboarding",
    "main_menu_keyboard",
    "main_menu_reply_keyboard",
]


# ---------------------------------------------------------------------------
# Persistent command menu (setMyCommands).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CommandSpec:
    """A command and the catalog key for its (localized) menu description."""

    command: str
    description_key: str


@dataclass(frozen=True)
class ResolvedCommand:
    """A command with its description already resolved for a language."""

    command: str
    description: str


#: The `/language` switch command -- always present so the toggle is reachable
#: from Telegram's command list at any time (Req 18.3, 18.8).
LANGUAGE_COMMAND = CommandSpec("language", "CMD_LANGUAGE")

#: Standard customer commands (always available, any role).
CUSTOMER_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("browse", "CMD_BROWSE"),
    CommandSpec("cart", "CMD_CART"),
    CommandSpec("orders", "CMD_ORDERS"),
    CommandSpec("help", "CMD_HELP"),
    LANGUAGE_COMMAND,
)

#: Seller-only commands, appended **only** when require_admin authorizes
#: the actor (Req 1.7/12.1). Domain services never see these.
SELLER_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("catalog", "CMD_CATALOG"),
    CommandSpec("verify", "CMD_VERIFY"),
    CommandSpec("active_orders", "CMD_ACTIVE_ORDERS"),
    CommandSpec("settings", "CMD_SETTINGS"),
)


def command_menu_for(
    auth: object,
    telegram_user_id: int,
    language: object = None,
    renderer: Optional[Renderer] = None,
) -> list[ResolvedCommand]:
    """Build the localized persistent command menu for ``telegram_user_id``.

    Customer commands (including ``/language``) are always present. Seller-only
    commands are appended **only when** ``auth.require_admin(telegram_user_id)``
    does not return a failure result (Req 1.7). Descriptions are resolved for
    the active language (Hindi default, Req 18.2).
    """
    renderer = renderer if renderer is not None else Renderer()
    specs: list[CommandSpec] = list(CUSTOMER_COMMANDS)

    # Gate the Seller-only commands behind the Auth_Service admin check.
    if auth is not None:
        verdict = auth.require_admin(telegram_user_id)
        if not is_failure(verdict):
            specs.extend(SELLER_COMMANDS)

    return [
        ResolvedCommand(
            command=spec.command,
            description=renderer.text(spec.description_key, language),
        )
        for spec in specs
    ]


# ---------------------------------------------------------------------------
# Language-switch control (Req 18.3, 18.4, 18.8).
# ---------------------------------------------------------------------------
LANGUAGE_CALLBACK_PREFIX = "lang"


def language_callback_data(code: str) -> str:
    """Build the callback payload for a language choice (e.g. ``'lang:EN'``)."""
    return f"{LANGUAGE_CALLBACK_PREFIX}:{code}"


def parse_language_callback(data: Optional[str]) -> Optional[str]:
    """Parse a language callback payload into ``'HI'`` / ``'EN'`` or ``None``.

    Returns ``None`` for any payload that is not a recognized language choice,
    so a malformed callback never raises or blocks the flow (Req 18.9).
    """
    if not data or not isinstance(data, str):
        return None
    prefix, sep, raw = data.partition(":")
    if sep == "" or prefix != LANGUAGE_CALLBACK_PREFIX:
        return None
    code = raw.strip().upper()
    if code in (Language.HINDI.value, Language.ENGLISH.value):
        return code
    return None


class LanguageSwitch:
    """The role-independent Hindi/English language-switch control (Req 18).

    Construction takes the :class:`~marketplace.auth.AuthService` (for the small
    ``set_language_preference`` write), a :class:`MessagingChannel`, and a
    :class:`Renderer`. The control reads only the catalog and writes only the
    per-user ``language_preference``, so it is exposed identically to Customers
    and the Seller (Req 18.8) and never blocks any flow (Req 18.9).
    """

    def __init__(
        self,
        auth: object,
        channel: MessagingChannel,
        renderer: Optional[Renderer] = None,
    ) -> None:
        self._auth = auth
        self._channel = channel
        self._renderer = renderer if renderer is not None else Renderer()

    def keyboard(self, language: object = None) -> Keyboard:
        """Two large, clearly labeled inline buttons: हिंदी and English."""
        hindi = self._renderer.button(
            "BTN_LANG_HINDI",
            language,
            callback_data=language_callback_data(Language.HINDI.value),
        )
        english = self._renderer.button(
            "BTN_LANG_ENGLISH",
            language,
            callback_data=language_callback_data(Language.ENGLISH.value),
        )
        # One per row for big, easy tap targets (elderly-friendly).
        return Keyboard.inline_single_column([hindi, english])

    async def present(
        self, recipient_id: int, language: object = None
    ) -> DeliveryResult:
        """Present the language choices with the switch keyboard."""
        prompt = self._renderer.text("CHOOSE_LANGUAGE", language)
        return await self._channel.send_text(
            recipient_id, prompt, buttons=self.keyboard(language)
        )

    async def apply_selection(
        self,
        user_ref: object,
        callback_data: str,
        recipient_id: Optional[int] = None,
    ) -> tuple[object, Optional[object]]:
        """Persist the chosen language and (optionally) confirm in that language.

        Parses ``callback_data`` into a language code, persists it via
        ``Auth_Service.set_language_preference``, and, when ``recipient_id`` is
        given and the write succeeded, sends the ``LANGUAGE_SET`` confirmation
        **already rendered in the newly selected language** so subsequent
        content flips immediately (Req 18.4/18.5).

        Returns ``(result, active_language)`` where ``result`` is the
        Auth_Service result (a ``User`` on success, or a typed failure) and
        ``active_language`` is the now-active language on success (else
        ``None``). Never raises.
        """
        code = parse_language_callback(callback_data)
        if code is None:
            # Unrecognized selection: do not block; report nothing changed.
            return None, None

        result = self._auth.set_language_preference(user_ref, code)

        if is_failure(result):
            return result, None

        # Success: result is the updated User carrying the stored preference.
        active_language = getattr(result, "language_preference", None) or code
        if recipient_id is not None:
            confirmation = self._renderer.text("LANGUAGE_SET", active_language)
            await self._channel.send_text(recipient_id, confirmation)
        return result, active_language


# ---------------------------------------------------------------------------
# Branded onboarding (/start) -- Req 1.1/1.4/1.5.
# ---------------------------------------------------------------------------
class Onboarding:
    """The branded ``/start`` welcome that precedes the Share Contact action."""

    def __init__(
        self, channel: MessagingChannel, renderer: Optional[Renderer] = None
    ) -> None:
        self._channel = channel
        self._renderer = renderer if renderer is not None else Renderer()

    async def start(
        self, recipient_id: int, language: object = None
    ) -> DeliveryResult:
        """Send the brand-first welcome, then present Share Contact (Req 1.1).

        The welcome introduces "Jan Purna (जन पूर्णा)" (language-independent
        brand string, Req 18.7) before the Share Contact action is presented
        with its localized explanation (Req 1.4/1.5).
        """
        brand = self._renderer.brand_display_name()
        welcome = self._renderer.text("WELCOME", language, brand=brand)
        await self._channel.send_text(recipient_id, welcome)

        explanation = self._renderer.text("SHARE_CONTACT_PROMPT", language)
        return await self._channel.request_contact(recipient_id, explanation)


# ---------------------------------------------------------------------------
# Navigation keyboards (primary = inline; elderly-friendly = large reply).
# ---------------------------------------------------------------------------
def _main_menu_buttons(
    renderer: Renderer, language: object, is_admin: bool, inline: bool
) -> list[Button]:
    """The primary navigation buttons, optionally including Seller entries."""

    def make(key: str, callback: str) -> Button:
        # Inline navigation is callback-driven; reply keyboards carry labels only.
        return renderer.button(
            key, language, callback_data=callback if inline else None
        )

    buttons = [
        make("BTN_BROWSE", "nav:browse"),
        make("BTN_VIEW_CART", "nav:cart"),
        make("BTN_MY_ORDERS", "nav:orders"),
        make("BTN_HELP", "nav:help"),
        make("BTN_SWITCH_LANGUAGE", "nav:language"),
    ]
    return buttons


def main_menu_keyboard(
    renderer: Renderer,
    language: object = None,
    is_admin: bool = False,
) -> Keyboard:
    """Callback-driven inline keyboard -- the **primary** navigation (design)."""
    buttons = _main_menu_buttons(renderer, language, is_admin, inline=True)
    return Keyboard.inline_single_column(buttons)


def main_menu_reply_keyboard(
    renderer: Renderer,
    language: object = None,
    is_admin: bool = False,
) -> Keyboard:
    """Large-label reply keyboard -- the **elderly-friendly** option (design)."""
    buttons = _main_menu_buttons(renderer, language, is_admin, inline=False)
    return Keyboard.reply_large(buttons)
