"""Centralized branding / brand-strings module for "Jan Purna" (जन पूर्णा).

This module is the **single source of truth** for the product's brand strings so
that no handler, service, or notification ever hard-codes brand copy. All bot
copy (e.g. the ``/start`` welcome and onboarding in Task 19.6) references the
constants here, and a future WhatsApp / web / Mini App channel (Phase 2) reuses
the very same values — updating the brand never touches domain services or
stored data.

Design alignment (design.md -> "Branding"):
  * Product name:        "Jan Purna"
  * Devanagari form:     "जन पूर्णा"
  * Short name/monogram: "JP"
  * Taglines live here too.

Brand strings are **language-independent** (design.md -> "Language and
Localization" / Req 18.7): they render identically in Hindi and English and are
therefore kept *separate* from the translatable ``Message_Catalog`` (Task 2.4),
which holds the surrounding localized copy and may embed these brand strings by
reference.

Logo/asset handling is intentionally *not* in code (the application performs no
image generation or manipulation); only the textual brand identity lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# ---------------------------------------------------------------------------
# Brand constants (the canonical, exact values — do not localize these).
# ---------------------------------------------------------------------------

#: The product name in Latin script, rendered identically in every language.
PRODUCT_NAME: str = "Jan Purna"

#: The product name in Devanagari script, rendered identically in every language.
PRODUCT_NAME_DEVANAGARI: str = "जन पूर्णा"

#: The short name / monogram used for the bot avatar and compact contexts.
SHORT_NAME: str = "JP"

#: Alias kept for callers that think in terms of the visual monogram.
MONOGRAM: str = SHORT_NAME

#: Combined display form carrying both scripts, used where space allows
#: (e.g. the first line of the /start welcome): ``Jan Purna (जन पूर्णा)``.
DISPLAY_NAME: str = f"{PRODUCT_NAME} ({PRODUCT_NAME_DEVANAGARI})"

#: Language-independent taglines. Keyed so callers reference a stable name
#: rather than a positional index; values are emitted unchanged in any language.
TAGLINES: Mapping[str, str] = {
    "primary": "Jan Purna (जन पूर्णा)",
    "short": "JP — Jan Purna",
}

# ---------------------------------------------------------------------------
# Compatibility aliases for the Message_Catalog (i18n) brand import.
#
# The Message_Catalog (``marketplace.bot_interface.i18n``) pulls the canonical
# brand strings from this module under the ``BRAND_*`` names so the catalog and
# the branding module are a single, connected source of truth (Req 18.7). The
# constants above are the authoritative values; these aliases simply expose
# them under the names the catalog imports, so updating a brand string here is
# reflected everywhere with no duplication.
# ---------------------------------------------------------------------------

#: Alias of :data:`PRODUCT_NAME` used by the Message_Catalog brand import.
BRAND_NAME: str = PRODUCT_NAME

#: Alias of :data:`PRODUCT_NAME_DEVANAGARI` used by the Message_Catalog.
BRAND_NAME_DEVANAGARI: str = PRODUCT_NAME_DEVANAGARI

#: Alias of :data:`SHORT_NAME` used by the Message_Catalog brand import.
BRAND_SHORT_NAME: str = SHORT_NAME


@dataclass(frozen=True)
class Branding:
    """Immutable bundle of the brand strings.

    A frozen dataclass keeps the brand configurable from this one module while
    preventing accidental mutation at runtime. Callers may construct an
    alternative instance (e.g. for a white-label deployment) without changing
    any consuming code, but the default :data:`BRANDING` instance below mirrors
    the canonical constants.
    """

    product_name: str = PRODUCT_NAME
    product_name_devanagari: str = PRODUCT_NAME_DEVANAGARI
    short_name: str = SHORT_NAME
    taglines: Mapping[str, str] = field(default_factory=lambda: dict(TAGLINES))

    @property
    def monogram(self) -> str:
        """The monogram is the short name (the ``JP`` mark)."""
        return self.short_name

    @property
    def display_name(self) -> str:
        """Combined two-script display form: ``Jan Purna (जन पूर्णा)``."""
        return f"{self.product_name} ({self.product_name_devanagari})"

    def welcome_intro(self) -> str:
        """The brand introduction line used by the ``/start`` welcome (Task 19.6).

        This returns only the language-independent brand fragment; the
        surrounding localized welcome copy is composed by the Bot_Interface from
        the ``Message_Catalog`` and embeds this fragment by reference.
        """
        return self.display_name


#: The default, canonical branding instance used across the application.
BRANDING: Branding = Branding()


__all__ = [
    "PRODUCT_NAME",
    "PRODUCT_NAME_DEVANAGARI",
    "SHORT_NAME",
    "MONOGRAM",
    "DISPLAY_NAME",
    "TAGLINES",
    "BRAND_NAME",
    "BRAND_NAME_DEVANAGARI",
    "BRAND_SHORT_NAME",
    "Branding",
    "BRANDING",
]
