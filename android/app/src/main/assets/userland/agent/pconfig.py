"""Shared daemon config readers (tools-safe: never imports serve).

provider_config(): turn-env override first, then wipe-proof home file,
then the bundled agent/providers.json. Sections (providers, web_search,
...) are read per call so config edits apply without a restart.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .providers import PROVIDERS, BaseProvider

PROVIDER_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def config_file_path() -> Path:
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".interlux/agent/providers.json"


def provider_config() -> dict:
    """Full config mapping (providers + tool sections)."""
    try:
        config = json.loads(os.environ.get("INTERLUX_PROVIDERS", "{}"))
    except (json.JSONDecodeError, ValueError):
        config = {}
    if config:
        return config if isinstance(config, dict) else {}
    for config_file in (config_file_path(), Path(__file__).parent / "providers.json"):
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


def env_pinned() -> bool:
    """True when INTERLUX_PROVIDERS overrides the file (file writes inert)."""
    try:
        return bool(json.loads(os.environ.get("INTERLUX_PROVIDERS", "{}")))
    except (json.JSONDecodeError, ValueError):
        return False


def mask_key(key: str) -> str:
    """first4...last4 hint (never the secret)."""
    s = str(key or "")
    if len(s) <= 8:
        return "****" if s else ""
    return f"{s[:4]}...{s[-4:]}"


def public_section(name: str, section: dict) -> dict:
    """Wire-safe view of a config section (secrets never leave the daemon)."""
    key = str(section.get("api_key", "") or "")
    return {
        "provider": name,
        "base_url": str(section.get("base_url", "") or ""),
        "has_key": bool(key),
        "key_hint": mask_key(key),
    }


def read_providers_file() -> dict:
    try:
        if config_file_path().exists():
            data = json.loads(config_file_path().read_text())
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def write_providers_file(data: dict) -> Path:
    """Atomic tmp+replace (same discipline as policy/threads)."""
    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return path


def load_provider(name: str, config: dict) -> BaseProvider | None:
    if name not in PROVIDERS:
        return None
    if not isinstance(config, dict):
        config = {}
    return PROVIDERS[name](
        config.get("api_key", ""),
        config.get("base_url"),
    )
