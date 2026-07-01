"""
src/utils/config_loader.py
───────────────────────────
Configuration loader with environment variable substitution.
Supports ${VAR_NAME} syntax in YAML values.
"""

from __future__ import annotations

import os
import re
import yaml
from typing import Any, Dict


def load_config(path: str) -> Dict[str, Any]:
    """
    Load YAML config file and substitute ${ENV_VAR} placeholders
    with actual environment variable values.
    """
    with open(path, "r") as f:
        content = f.read()

    # Substitute ${VAR_NAME} with environment variable values
    def replace_env_var(match: re.Match) -> str:
        var_name = match.group(1)
        value    = os.environ.get(var_name, "")
        if not value:
            import logging
            logging.getLogger("migration.config").warning(
                f"Environment variable '{var_name}' is not set — using empty string"
            )
        return value

    content = re.sub(r"\$\{([^}]+)\}", replace_env_var, content)
    config  = yaml.safe_load(content)
    return config or {}
