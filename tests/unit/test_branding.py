"""Unit tests for the centralized branding/strings module (Task 2.3).

These assert the brand constants are present and *exact*, since downstream copy
(the /start welcome, onboarding, future channels) depends on them being the
single source of truth. Brand strings are language-independent, so there is no
localization to exercise here.

Requirements: 1.1, 1.5 (brand carried by the first interaction / welcome).
"""

import pytest

from marketplace.config import branding
from marketplace.config.branding import BRANDING, Branding


@pytest.mark.smoke
def test_product_name_is_exact():
    assert branding.PRODUCT_NAME == "Jan Purna"


@pytest.mark.smoke
def test_devanagari_form_is_exact():
    assert branding.PRODUCT_NAME_DEVANAGARI == "जन पूर्णा"


@pytest.mark.smoke
def test_short_name_and_monogram_are_exact():
    assert branding.SHORT_NAME == "JP"
    assert branding.MONOGRAM == "JP"


@pytest.mark.smoke
def test_display_name_combines_both_scripts():
    assert branding.DISPLAY_NAME == "Jan Purna (जन पूर्णा)"


@pytest.mark.smoke
def test_taglines_present_and_non_empty():
    assert branding.TAGLINES, "at least one tagline must be defined"
    for key, value in branding.TAGLINES.items():
        assert isinstance(key, str) and key
        assert isinstance(value, str) and value.strip()


@pytest.mark.smoke
def test_default_branding_instance_matches_constants():
    assert BRANDING.product_name == branding.PRODUCT_NAME
    assert BRANDING.product_name_devanagari == branding.PRODUCT_NAME_DEVANAGARI
    assert BRANDING.short_name == branding.SHORT_NAME
    assert BRANDING.monogram == "JP"
    assert BRANDING.display_name == "Jan Purna (जन पूर्णा)"


@pytest.mark.smoke
def test_welcome_intro_carries_the_brand():
    # The /start welcome (Req 1.1/1.5) introduces the service brand-first.
    intro = BRANDING.welcome_intro()
    assert "Jan Purna" in intro
    assert "जन पूर्णा" in intro


@pytest.mark.smoke
def test_branding_instance_is_immutable():
    # Frozen dataclass: brand strings cannot be mutated at runtime.
    with pytest.raises(Exception):
        BRANDING.product_name = "Something Else"  # type: ignore[misc]


@pytest.mark.smoke
def test_branding_is_overridable_without_touching_consumers():
    # A white-label deployment can supply its own brand via this one module.
    custom = Branding(
        product_name="Other Brand",
        product_name_devanagari="अन्य",
        short_name="OB",
    )
    assert custom.display_name == "Other Brand (अन्य)"
    assert custom.monogram == "OB"
    # The canonical default is unaffected.
    assert BRANDING.product_name == "Jan Purna"
