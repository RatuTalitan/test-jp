"""Configuration layer.

Environment-based configuration loader (secrets read from env / host secret
manager only), the centralized branding/strings module ("Jan Purna", "जन पूर्णा",
"JP"), and other process-level settings. No secret values are committed or
logged.

The loader (Task 2.1) lives in :mod:`marketplace.config.loader`; its public API
is re-exported here for convenience.
"""

from marketplace.config.loader import (
    DEFAULT_UTR_PATTERN,
    Config,
    ConfigError,
    InvalidConfigError,
    MissingConfigError,
    Secret,
    get_config,
    load_config,
    reset_config_cache,
)

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
