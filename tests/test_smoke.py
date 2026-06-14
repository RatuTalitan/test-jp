"""Smoke tests proving the project skeleton and test harness are runnable.

These are intentionally minimal (Task 1.1 is scaffolding only - no business
logic). They verify that:
  * the top-level package and every subsystem package import cleanly;
  * the Hypothesis "default" profile is loaded and meets the >= 100 floor;
  * the custom pytest markers (property / integration / smoke) are registered;
  * a trivial Hypothesis property actually executes (the PBT plumbing works).
"""

import importlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import marketplace
from tests.conftest import MAX_EXAMPLES_FLOOR

# Every subsystem package declared in the design's modular-monolith layout.
SUBSYSTEM_PACKAGES = [
    "marketplace.bot_interface",
    "marketplace.auth",
    "marketplace.catalog",
    "marketplace.cart",
    "marketplace.order",
    "marketplace.payment",
    "marketplace.fulfillability",
    "marketplace.notification",
    "marketplace.admin",
    "marketplace.domain",
    "marketplace.db",
    "marketplace.config",
]


@pytest.mark.smoke
def test_top_level_package_imports():
    """The top-level marketplace package imports and exposes a version."""
    assert marketplace.__version__ == "0.1.0"


@pytest.mark.smoke
@pytest.mark.parametrize("module_name", SUBSYSTEM_PACKAGES)
def test_subsystem_packages_import(module_name):
    """Each modular-monolith subsystem package is importable."""
    module = importlib.import_module(module_name)
    assert module is not None


@pytest.mark.smoke
def test_hypothesis_profile_meets_floor():
    """The loaded Hypothesis profile enforces the >= 100 example floor."""
    assert settings().max_examples >= MAX_EXAMPLES_FLOOR


@pytest.mark.smoke
def test_pytest_markers_registered(pytestconfig):
    """The custom markers are registered (so --strict-markers passes)."""
    registered = "\n".join(pytestconfig.getini("markers"))
    for marker in ("property", "integration", "smoke"):
        assert f"{marker}:" in registered


@pytest.mark.smoke
@settings(max_examples=MAX_EXAMPLES_FLOOR)
@given(st.integers())
def test_hypothesis_runs_a_property(value):
    """A trivial property executes, proving the Hypothesis plumbing works."""
    assert value + 0 == value
