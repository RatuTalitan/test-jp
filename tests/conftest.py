"""Shared test configuration for the Cotton Seed Oil Cake Marketplace harness.

Registers and loads a Hypothesis profile that enforces the design's
property-based-testing floor of ``max_examples >= 100`` (design.md -> Testing
Strategy: "each property test runs a minimum of 100 iterations").

The active profile can be overridden via the ``HYPOTHESIS_PROFILE`` environment
variable (e.g. ``ci`` runs more examples), but the default profile always
satisfies the 100-example floor.
"""

import os

from hypothesis import HealthCheck, Verbosity, settings

# Minimum number of examples every property test must explore (design floor).
MAX_EXAMPLES_FLOOR = 100

# "default" profile: the floor used for local runs and as the baseline.
settings.register_profile(
    "default",
    settings(
        max_examples=MAX_EXAMPLES_FLOOR,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    ),
)

# "dev" profile: faster feedback loop, but still meets the 100-example floor.
settings.register_profile(
    "dev",
    settings(
        max_examples=MAX_EXAMPLES_FLOOR,
        deadline=None,
        verbosity=Verbosity.normal,
    ),
)

# "ci" profile: explores more examples for stronger coverage in CI.
settings.register_profile(
    "ci",
    settings(
        max_examples=500,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    ),
)

# Load the profile named by HYPOTHESIS_PROFILE, defaulting to "default".
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
