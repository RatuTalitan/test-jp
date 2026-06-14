"""Voice-message baseline handler (Task 19.3 / Req 14).

v1 does **not** interpret voice input (the assistant feature is disabled), but
the bot must never leave a Customer who speaks instead of types without a
response. On any voice message the Bot_Interface:

* replies promptly confirming the voice message was received (Req 14.1);
* states that voice is not interpreted in v1 (Req 14.3);
* **re-presents, in the same reply, the button/text options for the Customer's
  current conversation step** (Req 14.2);
* on an unprocessable voice message (e.g. it could not be downloaded), responds
  with a fallback and **retains the current step** so the flow is not lost
  (Req 14.4).

Everything here is presentation-layer and catalog-driven (Hindi default): the
copy comes from the :class:`~marketplace.bot_interface.i18n.MessageCatalog` via
the :class:`~marketplace.bot_interface.channel.Renderer`, and the current step's
keyboard is re-sent unchanged. Domain services are never involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from marketplace.bot_interface.channel import (
    DeliveryResult,
    Keyboard,
    MessagingChannel,
    Renderer,
)

__all__ = ["ConversationStep", "VoiceAck", "VoiceMessageHandler"]


@dataclass(frozen=True)
class ConversationStep:
    """A snapshot of the user's current conversation step's options.

    ``prompt_key`` is the catalog key for the step's prompt; ``keyboard`` is the
    button/text options to re-present; ``placeholders`` interpolate the prompt;
    ``step_id`` identifies the step so it can be retained verbatim on a voice
    fallback (Req 14.4). All fields are optional so a stepless context (e.g. the
    very first interaction) still yields a sensible acknowledgement.
    """

    step_id: str = ""
    prompt_key: Optional[str] = None
    keyboard: Optional[Keyboard] = None
    placeholders: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceAck:
    """The outcome of handling a voice message.

    ``retained_step`` is always the same :class:`ConversationStep` that was
    passed in -- the step is never advanced by a voice message (Req 14.2/14.4).
    ``processable`` records whether the voice was a normal (unsupported-in-v1)
    message or one that could not be handled at all.
    """

    delivery: DeliveryResult
    retained_step: ConversationStep
    processable: bool


class VoiceMessageHandler:
    """Builds and sends the catalog-driven voice acknowledgement (Req 14)."""

    def __init__(self, channel: MessagingChannel, renderer: Optional[Renderer] = None) -> None:
        self._channel = channel
        self._renderer = renderer if renderer is not None else Renderer()

    async def handle(
        self,
        recipient_id: int,
        step: ConversationStep,
        language: object = None,
        *,
        processable: bool = True,
    ) -> VoiceAck:
        """Acknowledge a voice message and re-present the current step.

        Args:
            recipient_id: the Telegram chat/user id to reply to.
            step: the user's current conversation step (re-presented, retained).
            language: the active language (``None`` -> Hindi default, Req 18.2).
            processable: ``True`` for a normal voice message (not interpreted in
                v1, Req 14.3); ``False`` when the voice could not be handled
                (Req 14.4). Either way the step is retained.

        Returns:
            A :class:`VoiceAck` carrying the delivery result and the (unchanged)
            retained step.
        """
        lines = [self._renderer.text("VOICE_RECEIVED", language)]

        # State that voice is not interpreted in v1 (processable) or that the
        # message could not be handled (unprocessable fallback) -- both direct
        # the user to the on-screen options.
        status_key = "VOICE_NOT_INTERPRETED" if processable else "VOICE_UNPROCESSABLE"
        lines.append(self._renderer.text(status_key, language))

        # Re-present the current step's prompt text, if any.
        if step.prompt_key:
            lines.append(
                self._renderer.text(step.prompt_key, language, **dict(step.placeholders))
            )

        text = "\n".join(line for line in lines if line)

        # Re-present the current step's buttons/options in the same reply.
        delivery = await self._channel.send_text(
            recipient_id, text, buttons=step.keyboard
        )
        return VoiceAck(delivery=delivery, retained_step=step, processable=processable)
