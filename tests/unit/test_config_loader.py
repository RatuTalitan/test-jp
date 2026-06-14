"""Unit tests for the environment-based configuration loader (Task 2.1).

Covers the behaviour required by the task and Req 12.5:
  * a clear, single error when required secrets are missing;
  * values load correctly from the environment (incl. optional/seed defaults);
  * secret values are masked in repr / str / format / logging and never leak.
"""

import logging

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marketplace.config import (
    DEFAULT_UTR_PATTERN,
    Config,
    InvalidConfigError,
    MissingConfigError,
    Secret,
    load_config,
)
from marketplace.config.loader import (
    ENV_BOT_TOKEN,
    ENV_DB_URL,
    ENV_OBJECT_STORE_KEY,
    ENV_SELLER_TELEGRAM_ID,
    ENV_VERIFIED_CONTACT_ENCRYPTION_KEY,
    ENV_WEBHOOK_SECRET_TOKEN,
)

# A complete, valid environment with distinctive secret values so we can assert
# they never appear in any string representation.
SECRET_BOT_TOKEN = "BOTTOKEN-supersecret-123456"
SECRET_DB_URL = "postgresql://user:dbpassword123@host:5432/db"
SECRET_OBJECT_KEY = "OBJSTOREKEY-abcdef-secret"
SECRET_ENC_KEY = "ENCRYPTIONKEY-zzz-secret-key"

_ALL_SECRET_VALUES = [
    SECRET_BOT_TOKEN,
    SECRET_DB_URL,
    SECRET_OBJECT_KEY,
    SECRET_ENC_KEY,
]


def _valid_env(**overrides: str) -> dict[str, str]:
    env = {
        ENV_BOT_TOKEN: SECRET_BOT_TOKEN,
        ENV_DB_URL: SECRET_DB_URL,
        ENV_OBJECT_STORE_KEY: SECRET_OBJECT_KEY,
        ENV_VERIFIED_CONTACT_ENCRYPTION_KEY: SECRET_ENC_KEY,
        ENV_SELLER_TELEGRAM_ID: "987654321",
    }
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Loading values from the environment
# ---------------------------------------------------------------------------


def test_loads_required_values_and_optional_defaults():
    cfg = load_config(_valid_env())

    assert isinstance(cfg, Config)
    assert cfg.bot_token.reveal() == SECRET_BOT_TOKEN
    assert cfg.db_url.reveal() == SECRET_DB_URL
    assert cfg.object_store_key.reveal() == SECRET_OBJECT_KEY
    assert cfg.verified_contact_encryption_key.reveal() == SECRET_ENC_KEY
    assert cfg.seller_telegram_id == 987654321

    # Optional/seed values default sensibly when not provided.
    assert cfg.webhook_secret_token is None
    assert cfg.use_webhook is False
    assert cfg.upi_address is None
    assert cfg.upi_qr_object_key is None
    assert cfg.utr_pattern == DEFAULT_UTR_PATTERN


def test_loads_optional_seed_values_when_present():
    cfg = load_config(
        _valid_env(
            UPI_ADDRESS="seller@upi",
            UPI_QR_OBJECT_KEY="qr/seller.png",
            UTR_PATTERN=r"^\d{16}$",
        )
    )
    assert cfg.upi_address == "seller@upi"
    assert cfg.upi_qr_object_key == "qr/seller.png"
    assert cfg.utr_pattern == r"^\d{16}$"


def test_blank_utr_pattern_falls_back_to_default():
    cfg = load_config(_valid_env(UTR_PATTERN="   "))
    assert cfg.utr_pattern == DEFAULT_UTR_PATTERN


def test_whitespace_only_required_value_is_treated_as_missing():
    env = _valid_env()
    env[ENV_BOT_TOKEN] = "   "
    with pytest.raises(MissingConfigError) as exc_info:
        load_config(env)
    assert ENV_BOT_TOKEN in exc_info.value.missing


# ---------------------------------------------------------------------------
# Fail-fast on missing required secrets
# ---------------------------------------------------------------------------


def test_missing_single_required_secret_raises_clear_error():
    env = _valid_env()
    del env[ENV_DB_URL]
    with pytest.raises(MissingConfigError) as exc_info:
        load_config(env)
    assert exc_info.value.missing == [ENV_DB_URL]
    assert ENV_DB_URL in str(exc_info.value)
    # The error references .env.example to guide the operator.
    assert ".env.example" in str(exc_info.value)


def test_missing_multiple_required_secrets_are_all_reported():
    env = {ENV_SELLER_TELEGRAM_ID: "1"}  # only the seller id present
    with pytest.raises(MissingConfigError) as exc_info:
        load_config(env)
    for var in (
        ENV_BOT_TOKEN,
        ENV_DB_URL,
        ENV_OBJECT_STORE_KEY,
        ENV_VERIFIED_CONTACT_ENCRYPTION_KEY,
    ):
        assert var in exc_info.value.missing


def test_missing_seller_id_is_reported():
    env = _valid_env()
    del env[ENV_SELLER_TELEGRAM_ID]
    with pytest.raises(MissingConfigError) as exc_info:
        load_config(env)
    assert ENV_SELLER_TELEGRAM_ID in exc_info.value.missing


def test_non_integer_seller_id_raises_invalid_config_error():
    with pytest.raises(InvalidConfigError) as exc_info:
        load_config(_valid_env(SELLER_TELEGRAM_ID="not-a-number"))
    assert ENV_SELLER_TELEGRAM_ID in str(exc_info.value)


# ---------------------------------------------------------------------------
# Webhook mode: secret token required only when enabled
# ---------------------------------------------------------------------------


def test_webhook_secret_required_only_when_webhook_enabled():
    # Enabled but token missing -> reported as missing.
    with pytest.raises(MissingConfigError) as exc_info:
        load_config(_valid_env(USE_WEBHOOK="true"))
    assert ENV_WEBHOOK_SECRET_TOKEN in exc_info.value.missing


def test_webhook_secret_loaded_when_webhook_enabled():
    cfg = load_config(
        _valid_env(USE_WEBHOOK="true", WEBHOOK_SECRET_TOKEN="hooksecret-xyz")
    )
    assert cfg.use_webhook is True
    assert cfg.webhook_secret_token is not None
    assert cfg.webhook_secret_token.reveal() == "hooksecret-xyz"


def test_invalid_use_webhook_value_raises():
    with pytest.raises(InvalidConfigError):
        load_config(_valid_env(USE_WEBHOOK="maybe"))


# ---------------------------------------------------------------------------
# Secret masking: values never leak via repr / str / format / logging
# ---------------------------------------------------------------------------


def test_secret_repr_and_str_do_not_reveal_value():
    s = Secret("topsecret-value")
    assert "topsecret-value" not in repr(s)
    assert "topsecret-value" not in str(s)
    assert "topsecret-value" not in f"{s}"
    assert "topsecret-value" not in "{}".format(s)
    # reveal() is the only path to the real value.
    assert s.reveal() == "topsecret-value"


def test_config_repr_masks_all_secret_values():
    cfg = load_config(
        _valid_env(USE_WEBHOOK="true", WEBHOOK_SECRET_TOKEN="hooksecret-xyz")
    )
    rendered = repr(cfg)
    for secret_value in _ALL_SECRET_VALUES + ["hooksecret-xyz"]:
        assert secret_value not in rendered
    # Non-secret fields remain visible for debuggability.
    assert "987654321" in rendered


def test_secrets_are_not_emitted_when_config_is_logged(caplog):
    cfg = load_config(_valid_env())
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").debug("loaded config: %s", cfg)
        logging.getLogger("test").debug("loaded config: %r", cfg)
    for secret_value in _ALL_SECRET_VALUES:
        assert secret_value not in caplog.text


# ---------------------------------------------------------------------------
# Property: masking reveals nothing about the wrapped value
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(a=st.text(min_size=0), b=st.text(min_size=0))
def test_secret_repr_is_independent_of_value(a, b):
    """repr/str of a Secret is constant regardless of contents.

    If two arbitrary secrets always render identically, the rendering can leak
    neither the content nor the length of the underlying value.
    """
    sa, sb = Secret(a), Secret(b)
    assert repr(sa) == repr(sb)
    assert str(sa) == str(sb)
    assert f"{sa}" == f"{sb}"
    # The real value is always recoverable via reveal().
    assert sa.reveal() == a
    assert sb.reveal() == b
