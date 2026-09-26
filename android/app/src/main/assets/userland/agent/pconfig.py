"""Shared daemon config readers (tools-safe: never imports serve).

provider_config(): turn-env override first, then wipe-proof home file,
then the bundled agent/providers.json. Sections (providers, web_search,
...) are read per call so config edits apply without a restart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .providers import PROVIDERS, BaseProvider


def provider_config() -> dict:
    """Full config mapping (providers + tool sections)."""
    try:
        config = json.loads(os.environ.get("INTERLUX_PROVIDERS", "{}"))
    except (json.JSONDecodeError, ValueError):
        config = {}
    if config:
        return config if isinstance(config, dict) else {}
    home = os.environ.get("HOME") or str(Path.home())
    candidates = [
        Path(home) / ".interlux/agent/providers.json",
        Path(__file__).parent / "providers.json",
    ]
    for config_file in candidates:
        try:
            if config_file.exists():
                data = json.loads(config_file.read_text())
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return {}


def config_section(name: str) -> dict:
    """One named section (e.g. a provider or web_search), {} when absent."""
    section = provider_config().get(name)
    return section if isinstance(section, dict) else {}


def make_provider(name: str) -> BaseProvider | None:
    """Instantiate a provider from its config section (None when unknown)."""
    return load_provider(name, config_section(name))


def load_provider(name: str, config: dict) -> BaseProvider | None:
    if name not in PROVIDERS:
        return None
    if not isinstance(config, dict):
        config = {}
    return PROVIDERS[name](
        config.get("api_key", ""),
        config.get("base_url"),
    )
