"""The ``UpdateSource`` abstraction and bot-origin authenticity check (Task 19.2).

The Bot_Interface receives Telegram updates through an internal ``UpdateSource``
abstraction with two implementations so the v1 long-polling deployment can flip
to webhooks at scale as a **configuration change with no data-format impact**
(design.md -> "Long-Polling vs Webhook"; Req 13.8):

* :class:`LongPollSource` (**v1**): pulls updates with ``getUpdates`` over the
  bot's authenticated TLS connection to the Telegram Bot API. There is **no
  public endpoint**, so an update obtained this way is already known to come
  from Telegram -- authenticity is intrinsic and :meth:`verify_authenticity`
  is always ``True``.
* :class:`WebhookSource` (**Phase 2**): receives updates as inbound HTTPS
  requests. Telegram is configured (via ``setWebhook``) to send a secret token
  in the ``X-Telegram-Bot-Api-Secret-Token`` header; :meth:`verify_authenticity`
  validates that header against the configured secret. An update whose header
  is **missing or does not match** fails verification (Req 12.3).

Both verify the update **before any state change or data write** (Req 12.3), and
an update that fails verification is **discarded with no state change** (Req
12.4). :meth:`UpdateSource.guard` expresses exactly that: it runs an authentic
update's handler and, for an inauthentic one, does nothing -- so a rejected
update can never mutate state.

This module imports **no** Telegram SDK symbols and performs no I/O, so it is
fully unit-testable; the concrete poll/serve plumbing is wired in Task 22.
"""

from __future__ import annotations

import abc
import hmac
from typing import Any, Callable, Mapping, Optional, TypeVar

__all__ = [
    "WEBHOOK_SECRET_HEADER",
    "UpdateSource",
    "LongPollSource",
    "WebhookSource",
    "build_update_source",
]

#: The HTTP header Telegram sends with each webhook delivery (case-insensitive).
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

_T = TypeVar("_T")


def _extract_secret_header(request: Any) -> Optional[str]:
    """Best-effort, case-insensitive extraction of the secret-token header.

    Accepts a variety of "request" shapes so the check is easy to exercise and
    to wire to any web framework later:

    * a plain mapping of headers (``{"X-Telegram-Bot-Api-Secret-Token": ...}``);
    * an object exposing a ``headers`` mapping attribute;
    * a raw string (treated as the token value itself).

    Returns the header value, or ``None`` if it is absent.
    """
    if request is None:
        return None
    if isinstance(request, str):
        return request
    headers = request
    if not isinstance(request, Mapping):
        headers = getattr(request, "headers", None)
    if headers is None:
        return None
    # Direct hit first, then a case-insensitive scan (HTTP headers are
    # case-insensitive and frameworks normalize them differently).
    try:
        if WEBHOOK_SECRET_HEADER in headers:
            return headers[WEBHOOK_SECRET_HEADER]
    except TypeError:  # pragma: no cover - non-mapping headers
        return None
    target = WEBHOOK_SECRET_HEADER.lower()
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - unexpected headers type
        return None
    for name, value in items:
        if isinstance(name, str) and name.lower() == target:
            return value
    return None


class UpdateSource(abc.ABC):
    """Source of inbound Telegram updates with a bot-origin authenticity gate."""

    @abc.abstractmethod
    def verify_authenticity(self, request_or_update: Any) -> bool:
        """Return ``True`` iff the update verifiably originates from the bot.

        Called **before** any state change or data write (Req 12.3).
        """

    def guard(
        self,
        request_or_update: Any,
        on_authentic: Callable[[], _T],
    ) -> Optional[_T]:
        """Run ``on_authentic`` only for a verified update; else discard it.

        This encodes Req 12.4: an update failing verification is discarded
        **without processing and with no state change** -- the callback (which
        performs the state change) is simply never invoked, and ``None`` is
        returned.
        """
        if self.verify_authenticity(request_or_update):
            return on_authentic()
        return None


class LongPollSource(UpdateSource):
    """v1 long-polling source (``getUpdates``); no public endpoint (Req 13.8).

    Updates are pulled directly from the Telegram Bot API over the bot's own
    authenticated connection, so they are intrinsically bot-origin: there is no
    untrusted inbound request to forge. :meth:`verify_authenticity` therefore
    always returns ``True``.
    """

    def verify_authenticity(self, request_or_update: Any = None) -> bool:
        return True


class WebhookSource(UpdateSource):
    """Phase 2 webhook source validating the secret-token header (Req 12.3/12.4).

    Telegram is registered with a secret token via ``setWebhook`` and echoes it
    in the ``X-Telegram-Bot-Api-Secret-Token`` header on every delivery. An
    update is authentic only when that header is present and matches the
    configured secret (compared in constant time). A misconfigured source with
    no secret rejects everything rather than accept unverified input.
    """

    def __init__(self, secret_token: Optional[str]) -> None:
        # Normalize empty/blank to None so a misconfigured token rejects all.
        token = secret_token.strip() if isinstance(secret_token, str) else secret_token
        self._secret_token: Optional[str] = token or None

    def verify_authenticity(self, request_or_update: Any) -> bool:
        if not self._secret_token:
            # No secret configured: cannot verify -> reject (no state change).
            return False
        provided = _extract_secret_header(request_or_update)
        if not provided:
            return False
        # Constant-time comparison to avoid leaking the secret via timing.
        return hmac.compare_digest(str(provided), str(self._secret_token))


def build_update_source(config: Any) -> UpdateSource:
    """Select the source from configuration with no data-format impact (Req 13.8).

    ``config.use_webhook`` chooses :class:`WebhookSource` (using
    ``config.webhook_secret_token``) or the default :class:`LongPollSource`.
    The webhook secret may be a plain string or a ``Secret`` wrapper (its
    ``reveal()`` is used when present).
    """
    if getattr(config, "use_webhook", False):
        raw = getattr(config, "webhook_secret_token", None)
        secret = raw.reveal() if hasattr(raw, "reveal") else raw
        return WebhookSource(secret)
    return LongPollSource()
