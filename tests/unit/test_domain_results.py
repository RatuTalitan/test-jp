"""Unit tests for the typed domain result objects (task 4.1).

Verifies the shared failure vocabulary (Rejected / NotFound / NotAuthorized /
Unauthenticated / Conflict) is immutable, carries stable status codes, and is
recognized by the ``is_failure`` helper, while domain entities are not.
"""

import dataclasses

import pytest

from marketplace.domain.results import (
    Conflict,
    NotAuthorized,
    NotFound,
    Rejected,
    Unauthenticated,
    is_failure,
)


@pytest.mark.smoke
def test_failures_are_frozen():
    rej = Rejected(code="QTY_BELOW_MOQ", reason="below minimum")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rej.code = "OTHER"  # type: ignore[misc]


@pytest.mark.smoke
def test_default_codes_are_stable():
    assert NotAuthorized().code == "NOT_AUTHORIZED"
    assert Unauthenticated().code == "UNAUTHENTICATED"
    assert NotFound(entity="product").code == "NOT_FOUND"


@pytest.mark.smoke
def test_rejected_and_conflict_carry_details():
    rej = Rejected(code="STOCK_EXCEEDED", details={"stock": 5})
    conflict = Conflict(code="DUPLICATE_UTR", details={"utr": "ABC123XYZ789"})
    assert rej.details["stock"] == 5
    assert conflict.details["utr"] == "ABC123XYZ789"


@pytest.mark.smoke
def test_is_failure_recognizes_every_failure_type():
    for failure in (
        Rejected(code="X"),
        NotFound(entity="order"),
        NotAuthorized(),
        Unauthenticated(),
        Conflict(code="Y"),
    ):
        assert is_failure(failure) is True


@pytest.mark.smoke
def test_is_failure_false_for_non_failures():
    assert is_failure("ok") is False
    assert is_failure(None) is False
    assert is_failure(42) is False
