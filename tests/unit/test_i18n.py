"""Example/unit tests for the Message_Catalog (i18n) module — Task 2.4.

These cover the concrete behaviors required by Requirement 18:

* active-language resolution (English when English is the active language);
* Hindi default when the preference is unset/``NULL`` (Req 18.2);
* graceful fallback to the other language *preferring Hindi* when a key is
  missing/untranslated for the active language (Req 18.9);
* brand-string language-independence (Req 18.7);
* placeholder interpolation.

The universal property covering these (Property 35) is implemented separately
in Task 2.5; this file holds representative example coverage only.
"""

import pytest

from marketplace.bot_interface.i18n import (
    DEFAULT_CATALOG,
    DEFAULT_LANGUAGE,
    FORMAT_VERSION,
    SUPPORTED_LANGUAGES,
    Language,
    MessageCatalog,
    resolve,
)


# ---------------------------------------------------------------------------
# Supported languages / constants.
# ---------------------------------------------------------------------------
def test_supported_languages_are_hindi_and_english():
    assert set(SUPPORTED_LANGUAGES) == {Language.HINDI, Language.ENGLISH}
    assert Language.HINDI.value == "HI"
    assert Language.ENGLISH.value == "EN"


def test_default_language_is_hindi():
    assert DEFAULT_LANGUAGE is Language.HINDI


def test_catalog_carries_a_format_version():
    assert DEFAULT_CATALOG.format_version == FORMAT_VERSION
    assert FORMAT_VERSION >= 1


def test_every_seed_key_has_both_languages():
    """Complete Hindi AND English coverage for every catalog key (Req 18.1/18.6)."""
    for key in (
        "WELCOME",
        "ORDER_PLACED",
        "QTY_BELOW_MOQ",
        "DUPLICATE_UTR",
        "NOT_AUTHORIZED",
        "NOTIFY_NEW_ORDER",
        "BTN_BROWSE",
        "BTN_SWITCH_LANGUAGE",
    ):
        hi = resolve(key, Language.HINDI)
        en = resolve(key, Language.ENGLISH)
        assert hi and en, f"{key} missing a language entry"


# ---------------------------------------------------------------------------
# Active-language resolution (Req 18.5).
# ---------------------------------------------------------------------------
def test_english_active_language_resolves_in_english():
    assert resolve("NOT_AUTHORIZED", Language.ENGLISH) == (
        "You are not authorized to perform this action."
    )


def test_hindi_active_language_resolves_in_hindi():
    assert resolve("NOT_AUTHORIZED", Language.HINDI) == (
        "इस कार्य के लिए आपको अनुमति नहीं है।"
    )


def test_raw_string_code_preference_resolves():
    """A raw persisted code string (e.g. 'EN') resolves like the enum."""
    assert resolve("BTN_BROWSE", "EN") == resolve("BTN_BROWSE", Language.ENGLISH)
    assert resolve("BTN_BROWSE", "en") == resolve("BTN_BROWSE", Language.ENGLISH)


# ---------------------------------------------------------------------------
# Hindi default for an unset/NULL preference (Req 18.2).
# ---------------------------------------------------------------------------
def test_unset_preference_defaults_to_hindi():
    assert resolve("NOT_AUTHORIZED", None) == resolve("NOT_AUTHORIZED", Language.HINDI)


def test_unrecognized_preference_defaults_to_hindi():
    assert Language.from_preference("FR") is Language.HINDI
    assert Language.from_preference(None) is Language.HINDI
    assert resolve("BTN_CONFIRM", "FR") == resolve("BTN_CONFIRM", Language.HINDI)


# ---------------------------------------------------------------------------
# Graceful fallback preferring Hindi (Req 18.9).
# ---------------------------------------------------------------------------
def test_missing_active_language_falls_back_to_hindi():
    """English missing for a key -> resolve to Hindi (the preferred fallback)."""
    catalog = MessageCatalog(
        entries={"ONLY_HINDI": {Language.HINDI: "केवल हिंदी"}},
        brand_strings={},
    )
    # English is the active language but only a Hindi entry exists.
    assert catalog.resolve("ONLY_HINDI", Language.ENGLISH) == "केवल हिंदी"


def test_missing_hindi_falls_back_to_english():
    """Hindi missing for a key -> resolve to the other supported language."""
    catalog = MessageCatalog(
        entries={"ONLY_ENGLISH": {Language.ENGLISH: "English only"}},
        brand_strings={},
    )
    assert catalog.resolve("ONLY_ENGLISH", Language.HINDI) == "English only"


def test_unknown_key_never_raises_and_returns_token():
    """An entirely unknown key must not block the flow (Req 18.9)."""
    assert resolve("THIS_KEY_DOES_NOT_EXIST", Language.HINDI) == "THIS_KEY_DOES_NOT_EXIST"
    assert resolve("THIS_KEY_DOES_NOT_EXIST", Language.ENGLISH) == "THIS_KEY_DOES_NOT_EXIST"


def test_empty_entry_does_not_raise():
    catalog = MessageCatalog(entries={"EMPTY": {}}, brand_strings={})
    # No language available at all -> empty string, but no exception.
    assert catalog.resolve("EMPTY", Language.ENGLISH) == ""


# ---------------------------------------------------------------------------
# Brand-string language-independence (Req 18.7).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key,expected",
    [
        ("BRAND_NAME", "Jan Purna"),
        ("BRAND_NAME_DEVANAGARI", "जन पूर्णा"),
        ("BRAND_SHORT_NAME", "JP"),
    ],
)
def test_brand_strings_are_language_independent(key, expected):
    hi = resolve(key, Language.HINDI)
    en = resolve(key, Language.ENGLISH)
    unset = resolve(key, None)
    assert hi == en == unset == expected


def test_brand_key_is_flagged_as_brand():
    assert DEFAULT_CATALOG.is_brand_key("BRAND_NAME")
    assert not DEFAULT_CATALOG.is_brand_key("WELCOME")


def test_brand_full_lockup_includes_both_forms():
    full = resolve("BRAND_FULL", Language.ENGLISH)
    assert "Jan Purna" in full and "जन पूर्णा" in full


# ---------------------------------------------------------------------------
# Placeholder interpolation.
# ---------------------------------------------------------------------------
def test_placeholder_interpolation_english():
    assert resolve("ORDER_PLACED", Language.ENGLISH, order_number="JP-1001") == (
        "Your order JP-1001 has been placed."
    )


def test_placeholder_interpolation_hindi():
    assert resolve("QTY_BELOW_MOQ", Language.HINDI, moq=5, unit="क्विंटल") == (
        "मात्रा न्यूनतम ऑर्डर मात्रा 5 क्विंटल से कम है।"
    )


def test_brand_can_be_embedded_via_placeholder():
    brand = resolve("BRAND_FULL", Language.HINDI)
    welcome = resolve("WELCOME", Language.HINDI, brand=brand)
    assert brand in welcome


def test_missing_placeholder_does_not_raise():
    """A missing placeholder leaves the token intact instead of raising."""
    # order_number intentionally omitted.
    out = resolve("ORDER_PLACED", Language.ENGLISH)
    assert out == "Your order {order_number} has been placed."
