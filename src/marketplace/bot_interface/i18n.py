"""Message_Catalog (i18n) — the centralized presentation-layer string store.

This module is the single source of every user-facing string the Bot_Interface
renders: prompts, confirmations, errors, notifications, and button labels. It
lives entirely in the presentation layer (inside the Bot_Interface seam) so that
domain services (Auth, Catalog, Cart, Order, Payment, Notification, Admin) stay
language-agnostic — they return stable status/error *codes* and data
placeholders, and this catalog maps those codes to localized templates.

Design references (design.md -> "Language and Localization (Req 18)"):

* Two presentation languages, **Hindi and English**, with *complete* coverage
  in both — every key has a Hindi and an English entry (Req 18.1, 18.6).
* The active language is the user's persisted ``Language_Preference``; an unset
  / ``NULL`` preference resolves to **Hindi by default** (Req 18.2).
* **Graceful fallback:** if a key is ever missing/untranslated for the active
  language, the lookup resolves to the other supported language *preferring
  Hindi*, and never raises, blocks, aborts, or suspends the flow (Req 18.9).
* **Brand strings** ("Jan Purna", "जन पूर्णा", "JP") are *language-independent*:
  they render identically regardless of the active language (Req 18.7). They are
  pulled from the centralized branding module (Task 2.3) when available and are
  referenced here rather than duplicated.
* The catalog is a **versioned asset** carrying a ``FORMAT_VERSION`` and is
  *additive-only* — new keys/languages never break older payloads (Req 13.7).

Telegram wiring (mapping codes -> catalog lookups in handlers) is intentionally
NOT done here; that happens in the Bot_Interface adapter (Task 19).
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Mapping, Optional

# ---------------------------------------------------------------------------
# Versioning (additive-only asset, mirrors the stable-versioned-format rule).
# ---------------------------------------------------------------------------
FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# Supported languages.
# ---------------------------------------------------------------------------
class Language(str, Enum):
    """The presentation languages the System supports.

    Values are the stable two-letter codes persisted in
    ``users.language_preference`` (migration 3.1): ``HI`` and ``EN``.
    """

    HINDI = "HI"
    ENGLISH = "EN"

    @classmethod
    def from_preference(cls, preference: Optional["Language | str"]) -> "Language":
        """Resolve a stored/raw ``Language_Preference`` to an active language.

        An unset/``NULL`` preference — or any unrecognized value — resolves to
        the Hindi default (Req 18.2). This never raises.
        """
        if preference is None:
            return DEFAULT_LANGUAGE
        if isinstance(preference, Language):
            return preference
        try:
            return cls(str(preference).strip().upper())
        except ValueError:
            return DEFAULT_LANGUAGE


# The default language for any user with no stored preference (Req 18.2).
DEFAULT_LANGUAGE: Language = Language.HINDI

# All supported languages, in fallback priority order (Hindi preferred).
SUPPORTED_LANGUAGES = (Language.HINDI, Language.ENGLISH)

# Fallback order applied when a key is missing for the active language.
# Hindi is tried first, satisfying the "preferring Hindi" rule (Req 18.9).
_FALLBACK_ORDER = (Language.HINDI, Language.ENGLISH)


# ---------------------------------------------------------------------------
# Brand strings — language-independent (Req 18.7).
#
# These are pulled from the centralized branding module (Task 2.3) when it is
# available so they are defined in exactly one place. If that module is not yet
# present, we reference the canonical values here without duplicating them into
# the per-language catalog entries.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised once Task 2.3 lands a branding module
    from marketplace.config.branding import (  # type: ignore
        BRAND_NAME as _BRAND_NAME,
        BRAND_NAME_DEVANAGARI as _BRAND_NAME_DEVANAGARI,
        BRAND_SHORT_NAME as _BRAND_SHORT_NAME,
    )
except Exception:  # ImportError / AttributeError until Task 2.3 is implemented
    _BRAND_NAME = "Jan Purna"
    _BRAND_NAME_DEVANAGARI = "जन पूर्णा"
    _BRAND_SHORT_NAME = "JP"

# Brand keys resolve to the same value regardless of the active language.
_BRAND_STRINGS: Dict[str, str] = {
    "BRAND_NAME": _BRAND_NAME,
    "BRAND_NAME_DEVANAGARI": _BRAND_NAME_DEVANAGARI,
    "BRAND_SHORT_NAME": _BRAND_SHORT_NAME,
    # Composed lockup used in the /start welcome, e.g. "Jan Purna (जन पूर्णा)".
    "BRAND_FULL": f"{_BRAND_NAME} ({_BRAND_NAME_DEVANAGARI})",
}


# ---------------------------------------------------------------------------
# The seed catalog. COMPLETE Hindi AND English entries for every key.
#
# Keys are stable string identifiers (including the domain status/error codes
# the services emit, e.g. QTY_BELOW_MOQ, DUPLICATE_UTR, NOT_AUTHORIZED).
# Placeholders use Python str.format syntax ({name}) and are interpolated at
# lookup time. This is an initial keyset; later tasks extend it additively.
# ---------------------------------------------------------------------------
_CATALOG: Dict[str, Dict[Language, str]] = {
    # --- Prompts -----------------------------------------------------------
    "WELCOME": {
        Language.HINDI: "{brand} में आपका स्वागत है।",
        Language.ENGLISH: "Welcome to {brand}.",
    },
    "SHARE_CONTACT_PROMPT": {
        Language.HINDI: "जारी रखने के लिए कृपया अपना संपर्क साझा करें।",
        Language.ENGLISH: "Please share your contact to continue.",
    },
    "CHOOSE_CATEGORY": {
        Language.HINDI: "कृपया एक श्रेणी चुनें।",
        Language.ENGLISH: "Please choose a category.",
    },
    "ENTER_QUANTITY": {
        Language.HINDI: "कृपया मात्रा दर्ज करें ({unit})।",
        Language.ENGLISH: "Please enter the quantity (in {unit}).",
    },
    "CHOOSE_LANGUAGE": {
        Language.HINDI: "कृपया अपनी भाषा चुनें।",
        Language.ENGLISH: "Please choose your language.",
    },
    "HELP_PROMPT": {
        Language.HINDI: "आप नीचे दिए गए विकल्पों में से चुन सकते हैं।",
        Language.ENGLISH: "You can choose from the options below.",
    },
    # --- Voice-message baseline (Req 14.1-14.4) ---------------------------
    "VOICE_RECEIVED": {
        Language.HINDI: "हमें आपका वॉइस संदेश मिल गया है।",
        Language.ENGLISH: "We have received your voice message.",
    },
    "VOICE_NOT_INTERPRETED": {
        Language.HINDI: "अभी (v1 में) वॉइस संदेश समझे नहीं जाते। कृपया नीचे दिए गए विकल्पों का उपयोग करें।",
        Language.ENGLISH: "Voice messages are not interpreted yet (v1). Please use the options below.",
    },
    "VOICE_UNPROCESSABLE": {
        Language.HINDI: "हम इस वॉइस संदेश को संभाल नहीं सके। कृपया नीचे दिए गए विकल्पों का उपयोग करें।",
        Language.ENGLISH: "We could not handle this voice message. Please use the options below.",
    },
    # --- Confirmations -----------------------------------------------------
    "ORDER_PLACED": {
        Language.HINDI: "आपका ऑर्डर {order_number} दर्ज हो गया है।",
        Language.ENGLISH: "Your order {order_number} has been placed.",
    },
    "PAYMENT_SUBMITTED": {
        Language.HINDI: "आपका भुगतान विवरण प्राप्त हो गया है।",
        Language.ENGLISH: "Your payment details have been received.",
    },
    "ORDER_CANCELLED": {
        Language.HINDI: "ऑर्डर {order_number} रद्द कर दिया गया है।",
        Language.ENGLISH: "Order {order_number} has been cancelled.",
    },
    "LANGUAGE_SET": {
        Language.HINDI: "भाषा हिंदी में सेट कर दी गई है।",
        Language.ENGLISH: "Language has been set to English.",
    },
    # --- Errors / domain status codes -------------------------------------
    "QTY_BELOW_MOQ": {
        Language.HINDI: "मात्रा न्यूनतम ऑर्डर मात्रा {moq} {unit} से कम है।",
        Language.ENGLISH: "Quantity is below the minimum order quantity of {moq} {unit}.",
    },
    "QTY_EXCEEDS_STOCK": {
        Language.HINDI: "मात्रा उपलब्ध स्टॉक {stock} से अधिक है।",
        Language.ENGLISH: "Quantity exceeds the available stock of {stock}.",
    },
    "PRODUCT_OUT_OF_STOCK": {
        Language.HINDI: "यह उत्पाद अभी स्टॉक में नहीं है।",
        Language.ENGLISH: "This product is out of stock.",
    },
    "EMPTY_CART": {
        Language.HINDI: "आपकी टोकरी खाली है।",
        Language.ENGLISH: "Your cart is empty.",
    },
    "INVALID_UTR_FORMAT": {
        Language.HINDI: "यूटीआर का प्रारूप मान्य नहीं है।",
        Language.ENGLISH: "The UTR format is not valid.",
    },
    "DUPLICATE_UTR": {
        Language.HINDI: "यह यूटीआर पहले ही उपयोग किया जा चुका है।",
        Language.ENGLISH: "This UTR has already been used.",
    },
    "NOT_AUTHORIZED": {
        Language.HINDI: "इस कार्य के लिए आपको अनुमति नहीं है।",
        Language.ENGLISH: "You are not authorized to perform this action.",
    },
    # --- Notifications -----------------------------------------------------
    "NOTIFY_NEW_ORDER": {
        Language.HINDI: "नया ऑर्डर {order_number} प्राप्त हुआ है।",
        Language.ENGLISH: "A new order {order_number} has been received.",
    },
    "NOTIFY_ORDER_APPROVED": {
        Language.HINDI: "आपका ऑर्डर {order_number} स्वीकृत हो गया है।",
        Language.ENGLISH: "Your order {order_number} has been approved.",
    },
    "NOTIFY_ORDER_REJECTED": {
        Language.HINDI: "आपका ऑर्डर {order_number} अस्वीकृत कर दिया गया: {reason}",
        Language.ENGLISH: "Your order {order_number} was rejected: {reason}",
    },
    "NOTIFY_READY_FOR_PICKUP": {
        Language.HINDI: "ऑर्डर {order_number} पिकअप के लिए तैयार है: {pickup_location}",
        Language.ENGLISH: "Order {order_number} is ready for pickup at: {pickup_location}",
    },
    # --- Button labels -----------------------------------------------------
    "BTN_BROWSE": {
        Language.HINDI: "उत्पाद देखें",
        Language.ENGLISH: "Browse products",
    },
    "BTN_VIEW_CART": {
        Language.HINDI: "टोकरी देखें",
        Language.ENGLISH: "View cart",
    },
    "BTN_CHECKOUT": {
        Language.HINDI: "ऑर्डर करें",
        Language.ENGLISH: "Checkout",
    },
    "BTN_MY_ORDERS": {
        Language.HINDI: "मेरे ऑर्डर",
        Language.ENGLISH: "My orders",
    },
    "BTN_SHARE_CONTACT": {
        Language.HINDI: "संपर्क साझा करें",
        Language.ENGLISH: "Share contact",
    },
    "BTN_CONFIRM": {
        Language.HINDI: "पुष्टि करें",
        Language.ENGLISH: "Confirm",
    },
    "BTN_CANCEL": {
        Language.HINDI: "रद्द करें",
        Language.ENGLISH: "Cancel",
    },
    "BTN_SWITCH_LANGUAGE": {
        Language.HINDI: "भाषा बदलें",
        Language.ENGLISH: "Switch language",
    },
    # Language choices — the script name is shown identically in both languages
    # so each option is recognizable to a speaker of that language (Req 18.3).
    "BTN_LANG_HINDI": {
        Language.HINDI: "हिंदी",
        Language.ENGLISH: "हिंदी",
    },
    "BTN_LANG_ENGLISH": {
        Language.HINDI: "English",
        Language.ENGLISH: "English",
    },
    "BTN_HELP": {
        Language.HINDI: "सहायता",
        Language.ENGLISH: "Help",
    },
    # --- Persistent command-menu descriptions (setMyCommands, Task 19.6) ---
    "CMD_BROWSE": {
        Language.HINDI: "उत्पाद देखें",
        Language.ENGLISH: "Browse products",
    },
    "CMD_CART": {
        Language.HINDI: "टोकरी देखें",
        Language.ENGLISH: "View cart",
    },
    "CMD_ORDERS": {
        Language.HINDI: "मेरे ऑर्डर",
        Language.ENGLISH: "My orders",
    },
    "CMD_HELP": {
        Language.HINDI: "सहायता",
        Language.ENGLISH: "Help",
    },
    "CMD_LANGUAGE": {
        Language.HINDI: "भाषा बदलें",
        Language.ENGLISH: "Change language",
    },
    # Seller-only command descriptions (gated by Auth.require_admin).
    "CMD_CATALOG": {
        Language.HINDI: "कैटलॉग प्रबंधित करें",
        Language.ENGLISH: "Manage catalog",
    },
    "CMD_VERIFY": {
        Language.HINDI: "भुगतान सत्यापन",
        Language.ENGLISH: "Pending verifications",
    },
    "CMD_ACTIVE_ORDERS": {
        Language.HINDI: "सक्रिय ऑर्डर",
        Language.ENGLISH: "Active orders",
    },
    "CMD_SETTINGS": {
        Language.HINDI: "विक्रेता सेटिंग्स",
        Language.ENGLISH: "Seller settings",
    },
}


class _SafeFormatDict(dict):
    """A ``format_map`` backing dict that leaves unknown placeholders intact.

    This guarantees interpolation never raises ``KeyError`` for a missing
    placeholder — the literal ``{name}`` token is preserved instead, keeping
    with the "never block the flow" rule (Req 18.9).
    """

    def __missing__(self, key: str) -> str:  # noqa: D401 - dict hook
        return "{" + key + "}"


class MessageCatalog:
    """A versioned, language-aware store of user-facing strings.

    The catalog resolves a stable string ``key`` plus an active ``language`` to
    a localized, placeholder-interpolated string. Brand strings are
    language-independent. Lookups never raise.
    """

    def __init__(
        self,
        entries: Optional[Mapping[str, Mapping[Language, str]]] = None,
        brand_strings: Optional[Mapping[str, str]] = None,
        format_version: int = FORMAT_VERSION,
    ) -> None:
        # Defensive copies so a caller's mapping can't mutate catalog state.
        self._entries: Dict[str, Dict[Language, str]] = {
            key: dict(by_lang)
            for key, by_lang in (entries if entries is not None else _CATALOG).items()
        }
        self._brand: Dict[str, str] = dict(
            brand_strings if brand_strings is not None else _BRAND_STRINGS
        )
        self.format_version = format_version

    # -- Introspection ------------------------------------------------------
    def is_brand_key(self, key: str) -> bool:
        """Return whether ``key`` names a language-independent brand string."""
        return key in self._brand

    def has_key(self, key: str) -> bool:
        """Return whether ``key`` is a known brand or catalog key."""
        return key in self._brand or key in self._entries

    # -- Resolution ---------------------------------------------------------
    def resolve(
        self,
        key: str,
        language: Optional["Language | str"] = None,
        **placeholders: object,
    ) -> str:
        """Resolve ``key`` for the active ``language`` with interpolation.

        * ``language`` may be a ``Language``, a raw code/string, or ``None``.
          ``None`` (an unset preference) resolves to the Hindi default.
        * Brand keys render identically regardless of the active language.
        * If the key is missing/untranslated for the active language, the
          other supported language is used, preferring Hindi.
        * Unknown keys never raise; the key token itself is returned so the
          flow continues without interruption.
        """
        active = Language.from_preference(language)

        # Brand strings are language-independent (Req 18.7).
        if key in self._brand:
            return self._interpolate(self._brand[key], placeholders)

        entry = self._entries.get(key)
        if entry is None:
            # Unknown key: never raise/block — surface a safe, stable token.
            return self._interpolate(key, placeholders)

        template = self._select(entry, active)
        return self._interpolate(template, placeholders)

    # -- Internals ----------------------------------------------------------
    @staticmethod
    def _select(entry: Mapping[Language, str], active: Language) -> str:
        """Pick the active-language template, falling back preferring Hindi."""
        value = entry.get(active)
        if value:
            return value
        # Graceful fallback to the other supported language, Hindi first.
        for fallback in _FALLBACK_ORDER:
            value = entry.get(fallback)
            if value:
                return value
        # Both empty/missing: never raise — return an empty string.
        return ""

    @staticmethod
    def _interpolate(template: str, placeholders: Mapping[str, object]) -> str:
        """Interpolate ``placeholders`` into ``template`` without ever raising."""
        if not placeholders:
            return template
        try:
            return template.format_map(_SafeFormatDict(placeholders))
        except (ValueError, IndexError):
            # Malformed template braces: return the raw template rather than fail.
            return template


# A ready-to-use default catalog built from the seed keyset above.
DEFAULT_CATALOG = MessageCatalog()


def resolve(
    key: str,
    language: Optional["Language | str"] = None,
    **placeholders: object,
) -> str:
    """Module-level convenience wrapper around :data:`DEFAULT_CATALOG`."""
    return DEFAULT_CATALOG.resolve(key, language, **placeholders)
