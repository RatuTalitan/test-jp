"""Environment-based configuration loader for the marketplace (Task 2.1).

Secrets and configuration are read **only** from environment variables / the
host secret manager (Req 12.5: "store credentials and secret tokens outside of
any source code and configuration files committed to version control"). Nothing
is read from a committed file; the repository ships only a ``.env.example`` that
documents key *names* (never values).

Design alignment (design.md):
  * Required boot secrets fail fast with a single, clear error listing every
    missing variable at once.
  * ``WEBHOOK_SECRET_TOKEN`` is only required when the bot runs in webhook mode.
    v1 uses long-polling ("Minimal (no setWebhook, no secret URL, no cert)"), so
    the token is optional unless ``USE_WEBHOOK`` is enabled (Phase 2 / scale).
  * ``UPI_ADDRESS``, ``UTR_PATTERN`` and ``UPI_QR_OBJECT_KEY`` are *seed* values:
    "Environment variables may seed initial defaults, but the authoritative
    source once configured is the Seller-editable ``seller_settings`` row." They
    are therefore optional, with ``UTR_PATTERN`` defaulting to exactly 12
    alphanumeric characters (Req 6.2/6.6).

Secret values are wrapped in :class:`Secret` so they are never accidentally
printed, logged, or serialized: their ``repr``/``str`` is a constant mask that
reveals nothing about the underlying value. Call :meth:`Secret.reveal` only at
the point of use (e.g. when constructing a DB engine or Telegram client).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Mapping, Optional

__all__ = [
    "Secret",
    "Config",
    "ConfigError",
    "MissingConfigError",
    "InvalidConfigError",
    "DEFAULT_UTR_PATTERN",
    "load_config",
    "get_config",
    "reset_config_cache",
]

# Default UTR format: exactly 12 alphanumeric characters (Req 6.2/6.6).
DEFAULT_UTR_PATTERN = r"^[A-Za-z0-9]{12}$"

# Constant mask emitted in place of any secret value. It is intentionally
# independent of the wrapped value so it can leak neither the content nor the
# length of the secret.
_SECRET_MASK = "***"


class Secret:
    """An opaque wrapper around a sensitive string.

    The wrapped value is accessible only via :meth:`reveal`. Every other path
    that could expose it -- ``repr``, ``str``, ``format``, logging -- yields a
    constant mask, so secrets never end up in logs, tracebacks, or error
    messages (Req 12.5: "never log secret values").
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the underlying secret value. Use only at the point of use."""
        return self._value

    def __repr__(self) -> str:  # never include the value
        return f"Secret({_SECRET_MASK})"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:  # f-strings / format() stay masked
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


class ConfigError(RuntimeError):
    """Base class for configuration loading failures."""


class MissingConfigError(ConfigError):
    """Raised when one or more required environment variables are absent.

    Lists every missing variable at once so the operator can fix them in a
    single pass (fail fast with a clear error). The exception message contains
    only variable *names*, never any value.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        joined = ", ".join(self.missing)
        super().__init__(
            "Missing required configuration. Set the following environment "
            f"variable(s) (see .env.example): {joined}"
        )


class InvalidConfigError(ConfigError):
    """Raised when a present environment variable holds an invalid value.

    The message identifies the offending variable by name and describes the
    expectation; it never echoes a secret's value.
    """


@dataclass(frozen=True)
class Config:
    """Fully-resolved, validated application configuration.

    Secret-bearing fields are :class:`Secret` instances so the dataclass'
    auto-generated ``repr`` masks them automatically. Non-sensitive fields
    (the Seller's Telegram id, the public UPI VPA, the UTR pattern, the QR
    object reference, and the webhook flag) are plain values.
    """

    # --- Required secrets / connection strings -----------------------------
    bot_token: Secret
    db_url: Secret
    object_store_key: Secret
    verified_contact_encryption_key: Secret
    seller_telegram_id: int

    # --- Optional / seed values --------------------------------------------
    webhook_secret_token: Optional[Secret] = None
    upi_address: Optional[str] = None
    upi_qr_object_key: Optional[str] = None
    utr_pattern: str = DEFAULT_UTR_PATTERN
    use_webhook: bool = False

    def __repr__(self) -> str:
        # Secret fields mask themselves; this explicit repr keeps the masking
        # guarantee obvious and stable even if field ordering changes.
        parts = []
        for f in fields(self):
            parts.append(f"{f.name}={getattr(self, f.name)!r}")
        return f"Config({', '.join(parts)})"


# Names of the environment variables, centralized so .env.example and any
# secret-scanning tooling (Task 2.2) can reference a single source of truth.
ENV_BOT_TOKEN = "BOT_TOKEN"
ENV_DB_URL = "DB_URL"
ENV_OBJECT_STORE_KEY = "OBJECT_STORE_KEY"
ENV_SELLER_TELEGRAM_ID = "SELLER_TELEGRAM_ID"
ENV_VERIFIED_CONTACT_ENCRYPTION_KEY = "VERIFIED_CONTACT_ENCRYPTION_KEY"
ENV_WEBHOOK_SECRET_TOKEN = "WEBHOOK_SECRET_TOKEN"
ENV_UPI_ADDRESS = "UPI_ADDRESS"
ENV_UPI_QR_OBJECT_KEY = "UPI_QR_OBJECT_KEY"
ENV_UTR_PATTERN = "UTR_PATTERN"
ENV_USE_WEBHOOK = "USE_WEBHOOK"

# Required for the application to boot at all (long-polling v1).
_REQUIRED_SECRET_VARS = (
    ENV_BOT_TOKEN,
    ENV_DB_URL,
    ENV_OBJECT_STORE_KEY,
    ENV_VERIFIED_CONTACT_ENCRYPTION_KEY,
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


def _clean(raw: Optional[str]) -> Optional[str]:
    """Normalize an env value: strip surrounding whitespace; '' -> None."""
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_bool(raw: Optional[str], var_name: str) -> bool:
    value = _clean(raw)
    if value is None:
        return False
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise InvalidConfigError(
        f"{var_name} must be a boolean (one of: 1/0, true/false, yes/no, on/off)."
    )


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """Load and validate configuration from ``env`` (defaults to ``os.environ``).

    Raises:
        MissingConfigError: if any required variable is missing/blank. All
            missing names are reported together.
        InvalidConfigError: if a present variable holds an invalid value
            (e.g. a non-integer Seller id, or an enabled webhook mode without
            a secret token).
    """
    source: Mapping[str, str] = os.environ if env is None else env

    use_webhook = _parse_bool(source.get(ENV_USE_WEBHOOK), ENV_USE_WEBHOOK)

    # Collect every missing required variable before raising, so the operator
    # sees the complete list in one error.
    missing: list[str] = []
    for var in _REQUIRED_SECRET_VARS:
        if _clean(source.get(var)) is None:
            missing.append(var)

    seller_raw = _clean(source.get(ENV_SELLER_TELEGRAM_ID))
    if seller_raw is None:
        missing.append(ENV_SELLER_TELEGRAM_ID)

    webhook_raw = _clean(source.get(ENV_WEBHOOK_SECRET_TOKEN))
    # The webhook secret token is required *only* in webhook mode (Phase 2);
    # v1 long-polling does not need it.
    if use_webhook and webhook_raw is None:
        missing.append(ENV_WEBHOOK_SECRET_TOKEN)

    if missing:
        raise MissingConfigError(missing)

    # All required values are present; validate typed fields.
    assert seller_raw is not None  # for type-checkers; guaranteed by checks above
    try:
        seller_telegram_id = int(seller_raw)
    except ValueError as exc:
        raise InvalidConfigError(
            f"{ENV_SELLER_TELEGRAM_ID} must be an integer Telegram user id."
        ) from exc

    return Config(
        bot_token=Secret(_clean(source.get(ENV_BOT_TOKEN))),  # type: ignore[arg-type]
        db_url=Secret(_clean(source.get(ENV_DB_URL))),  # type: ignore[arg-type]
        object_store_key=Secret(_clean(source.get(ENV_OBJECT_STORE_KEY))),  # type: ignore[arg-type]
        verified_contact_encryption_key=Secret(
            _clean(source.get(ENV_VERIFIED_CONTACT_ENCRYPTION_KEY))  # type: ignore[arg-type]
        ),
        seller_telegram_id=seller_telegram_id,
        webhook_secret_token=Secret(webhook_raw) if webhook_raw is not None else None,
        upi_address=_clean(source.get(ENV_UPI_ADDRESS)),
        upi_qr_object_key=_clean(source.get(ENV_UPI_QR_OBJECT_KEY)),
        utr_pattern=_clean(source.get(ENV_UTR_PATTERN)) or DEFAULT_UTR_PATTERN,
        use_webhook=use_webhook,
    )


# --- Process-wide cached accessor ------------------------------------------
_cached_config: Optional[Config] = None


def get_config() -> Config:
    """Return a process-wide cached :class:`Config`, loading it on first use."""
    global _cached_config
    if _cached_config is None:
        _cached_config = load_config()
    return _cached_config


def reset_config_cache() -> None:
    """Clear the cached config (primarily for tests)."""
    global _cached_config
    _cached_config = None
