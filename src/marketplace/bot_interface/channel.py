"""The ``MessagingChannel`` abstraction and channel-agnostic presentation types
(Task 19.1).

This module defines the seam through which the rest of the Bot_Interface speaks
to the user. **All Telegram-specific code lives behind this interface** so that
no domain service ever imports the Telegram SDK (design.md -> Architectural
Principles -> "Channel abstraction"); a WhatsApp / web / Mini App channel is a
new implementation of :class:`MessagingChannel` with no change to any service.

What lives here:

* :class:`Button` / :class:`Keyboard` -- channel-neutral keyboard descriptions.
  A button carries *already-resolved* label text plus either ``callback_data``
  (an inline, callback-driven tap), a ``request_contact`` flag (the Share
  Contact action), or neither (a plain reply-keyboard label). A :class:`Keyboard`
  is either an **inline** keyboard (callback-driven primary navigation) or a
  **reply** keyboard (large-label, elderly-friendly option) -- see design.md ->
  "Modern Telegram Presentation".
* :class:`DeliveryResult` -- the channel-neutral outcome of a send.
* :class:`DownloadError` -- raised by ``download_file`` when a file cannot be
  retrieved or exceeds the byte budget.
* :class:`MessagingChannel` -- the abstract interface from design.md.
* :class:`FakeMessagingChannel` -- an in-memory implementation used by tests so
  no real network call is ever made.
* :class:`Renderer` -- the catalog-driven renderer that turns stable string keys
  into localized copy for the active language (Hindi default), emitting brand
  strings language-independently (Req 18).

The methods that touch the wire are ``async`` because python-telegram-bot v21+
is async-only; the abstraction mirrors that so the Telegram adapter can satisfy
it directly.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from marketplace.bot_interface.i18n import (
    DEFAULT_CATALOG,
    Language,
    MessageCatalog,
)
from marketplace.config.branding import BRANDING, Branding

__all__ = [
    "Button",
    "Keyboard",
    "DeliveryResult",
    "DownloadError",
    "MessagingChannel",
    "FakeMessagingChannel",
    "Renderer",
    "coerce_language",
]


# ---------------------------------------------------------------------------
# Language coercion helper.
# ---------------------------------------------------------------------------
def coerce_language(language: object) -> Optional[object]:
    """Normalize any language representation to something the catalog accepts.

    Accepts the i18n :class:`Language`, the domain ``Language`` enum (whose
    ``.value`` is ``'HI'`` / ``'EN'``), a raw ``'HI'`` / ``'EN'`` string, or
    ``None`` (an unset preference -> Hindi default). Returns the catalog-facing
    value; ``None`` is passed through so the catalog applies its Hindi default.
    """
    if language is None:
        return None
    if isinstance(language, Language):
        return language
    # Domain Language / any enum-like: prefer its stable value ('HI'/'EN').
    return getattr(language, "value", language)


# ---------------------------------------------------------------------------
# Channel-neutral keyboard description.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Button:
    """A single tappable button with already-resolved label text.

    * ``callback_data`` set -> an **inline** button producing a callback query
      (the primary, minimal-typing navigation mechanism).
    * ``request_contact`` True -> the Telegram Share Contact action (reply
      keyboard only).
    * neither set -> a plain large-label reply-keyboard button.
    """

    label: str
    callback_data: Optional[str] = None
    request_contact: bool = False


@dataclass(frozen=True)
class Keyboard:
    """A channel-neutral keyboard: rows of :class:`Button`.

    ``inline=True`` renders as a callback-driven inline keyboard (primary
    navigation). ``inline=False`` renders as a reply keyboard with large tap
    targets -- the elderly-friendly option (design.md -> Modern Telegram
    Presentation). ``one_time`` applies to reply keyboards only.
    """

    rows: tuple[tuple[Button, ...], ...]
    inline: bool = True
    one_time: bool = False

    @classmethod
    def inline_single_column(cls, buttons: Sequence[Button]) -> "Keyboard":
        """Build a one-button-per-row inline keyboard (big, easy targets)."""
        return cls(rows=tuple((b,) for b in buttons), inline=True)

    @classmethod
    def reply_large(
        cls, buttons: Sequence[Button], one_time: bool = True
    ) -> "Keyboard":
        """Build a single-column large-label reply keyboard (elderly-friendly)."""
        return cls(rows=tuple((b,) for b in buttons), inline=False, one_time=one_time)

    def labels(self) -> list[str]:
        """All button labels, flattened (handy for assertions/tests)."""
        return [b.label for row in self.rows for b in row]


@dataclass(frozen=True)
class DeliveryResult:
    """The channel-neutral outcome of a send operation."""

    ok: bool
    message_id: Optional[int] = None
    error: Optional[str] = None

    @classmethod
    def delivered(cls, message_id: Optional[int] = None) -> "DeliveryResult":
        return cls(ok=True, message_id=message_id)

    @classmethod
    def failed(cls, error: str) -> "DeliveryResult":
        return cls(ok=False, error=error)


class DownloadError(Exception):
    """Raised when ``download_file`` cannot retrieve a file or it is too large.

    The voice/image handlers catch this to present the unprocessable-input
    fallback while retaining the user's current step (Req 14.4).
    """


# ---------------------------------------------------------------------------
# The abstraction.
# ---------------------------------------------------------------------------
class MessagingChannel(abc.ABC):
    """The single seam through which user-facing messages are sent.

    Mirrors design.md -> Bot_Interface (MessagingChannel adapter). Concrete
    implementations: :class:`~marketplace.bot_interface.telegram_channel.TelegramMessagingChannel`
    (production) and :class:`FakeMessagingChannel` (tests).
    """

    @abc.abstractmethod
    async def send_text(
        self,
        recipient_id: int,
        text: str,
        buttons: Optional[Keyboard] = None,
    ) -> DeliveryResult:
        """Send a text message, optionally with a keyboard."""

    @abc.abstractmethod
    async def send_payment_instructions(
        self,
        recipient_id: int,
        upi_address: str,
        qr_image: Optional[object],
        amount: object,
        caption: Optional[str] = None,
    ) -> DeliveryResult:
        """Send UPI payment instructions: VPA, QR image/reference, amount due."""

    @abc.abstractmethod
    async def request_contact(
        self, recipient_id: int, explanation: str
    ) -> DeliveryResult:
        """Present the Share Contact action with an explanation (Req 1.1/1.4/1.5)."""

    @abc.abstractmethod
    async def download_file(self, file_id: str, max_bytes: int) -> bytes:
        """Download a file by id, enforcing a ``max_bytes`` budget.

        Raises :class:`DownloadError` if the file cannot be retrieved or exceeds
        ``max_bytes``.
        """


# ---------------------------------------------------------------------------
# In-memory fake for tests (no network).
# ---------------------------------------------------------------------------
@dataclass
class _SentText:
    recipient_id: int
    text: str
    buttons: Optional[Keyboard]


@dataclass
class _SentPayment:
    recipient_id: int
    upi_address: str
    qr_image: Optional[object]
    amount: object
    caption: Optional[str]


@dataclass
class _SentContactRequest:
    recipient_id: int
    explanation: str


class FakeMessagingChannel(MessagingChannel):
    """An in-memory :class:`MessagingChannel` that records every send.

    Tests inspect :attr:`sent_texts`, :attr:`sent_payments`,
    :attr:`contact_requests`, and the convenience :meth:`last_text` /
    :meth:`all_text` without ever performing I/O. ``download_file`` returns
    bytes from a preconfigured :attr:`files` map and enforces the byte budget,
    raising :class:`DownloadError` for unknown/oversized files.
    """

    def __init__(self, files: Optional[Mapping[str, bytes]] = None) -> None:
        self.sent_texts: list[_SentText] = []
        self.sent_payments: list[_SentPayment] = []
        self.contact_requests: list[_SentContactRequest] = []
        self.files: dict[str, bytes] = dict(files or {})
        self._next_message_id = 1

    def _alloc_id(self) -> int:
        mid = self._next_message_id
        self._next_message_id += 1
        return mid

    async def send_text(
        self,
        recipient_id: int,
        text: str,
        buttons: Optional[Keyboard] = None,
    ) -> DeliveryResult:
        self.sent_texts.append(_SentText(recipient_id, text, buttons))
        return DeliveryResult.delivered(self._alloc_id())

    async def send_payment_instructions(
        self,
        recipient_id: int,
        upi_address: str,
        qr_image: Optional[object],
        amount: object,
        caption: Optional[str] = None,
    ) -> DeliveryResult:
        self.sent_payments.append(
            _SentPayment(recipient_id, upi_address, qr_image, amount, caption)
        )
        return DeliveryResult.delivered(self._alloc_id())

    async def request_contact(
        self, recipient_id: int, explanation: str
    ) -> DeliveryResult:
        self.contact_requests.append(_SentContactRequest(recipient_id, explanation))
        return DeliveryResult.delivered(self._alloc_id())

    async def download_file(self, file_id: str, max_bytes: int) -> bytes:
        if file_id not in self.files:
            raise DownloadError(f"file {file_id!r} not available")
        blob = self.files[file_id]
        if len(blob) > max_bytes:
            raise DownloadError(
                f"file {file_id!r} exceeds the {max_bytes}-byte budget"
            )
        return blob

    # -- Test conveniences --------------------------------------------------
    def last_text(self) -> Optional[_SentText]:
        return self.sent_texts[-1] if self.sent_texts else None

    def all_text(self) -> str:
        """Concatenate every text body sent (handy for substring assertions)."""
        return "\n".join(s.text for s in self.sent_texts)


# ---------------------------------------------------------------------------
# Catalog-driven renderer (Req 18).
# ---------------------------------------------------------------------------
class Renderer:
    """Turns stable string keys into localized copy for the active language.

    The Bot_Interface is the only layer that turns results into words. The
    renderer resolves every prompt, confirmation, error, notification, and
    button label through the :class:`MessageCatalog` for the user's active
    language -- defaulting to **Hindi** when no preference is stored (Req 18.1,
    18.2, 18.5) -- and emits brand strings language-independently (Req 18.7).
    """

    def __init__(
        self,
        catalog: Optional[MessageCatalog] = None,
        branding: Branding = BRANDING,
    ) -> None:
        self._catalog = catalog if catalog is not None else DEFAULT_CATALOG
        self._branding = branding

    @property
    def catalog(self) -> MessageCatalog:
        return self._catalog

    @property
    def branding(self) -> Branding:
        return self._branding

    def text(self, key: str, language: object = None, **placeholders: object) -> str:
        """Resolve ``key`` for ``language`` (Hindi default) with interpolation."""
        return self._catalog.resolve(key, coerce_language(language), **placeholders)

    def button(
        self,
        key: str,
        language: object = None,
        *,
        callback_data: Optional[str] = None,
        request_contact: bool = False,
        **placeholders: object,
    ) -> Button:
        """Build a :class:`Button` whose label is the resolved ``key``."""
        return Button(
            label=self.text(key, language, **placeholders),
            callback_data=callback_data,
            request_contact=request_contact,
        )

    def brand_display_name(self) -> str:
        """The language-independent lockup ``Jan Purna (जन पूर्णा)`` (Req 18.7)."""
        return self._branding.display_name
