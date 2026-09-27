"""Shared daemon config readers (tools-safe: never imports serve).

provider_config(): turn-env override first, then wipe-proof home file,
then the bundled agent/providers.json. Sections (providers, web_search,
...) are read per call so config edits apply without a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .providers import PROVIDERS, BaseProvider

logger = logging.getLogger("pconfig")

PROVIDER_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Mirrors of the provider adapters' defaults (fallback when live
# discovery is impossible and no config override exists).
CURATED_MODELS = {
    "openai": ["gpt-4o"],
    "anthropic": ["claude-3-5-sonnet-20241022"],
    "inception": ["mercury-2.5"],
    "tokenharbor": ["deepseek-v4.1-flash:free"],
    "local": ["local"],
}
DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "inception": "https://api.inceptionlabs.ai/v1",
    "tokenharbor": "https://tokenharbor.ai/v1",
    "local": "http://127.0.0.1:4602/v1",
}
# Providers whose API shape exposes GET /models (OpenAI-compatible).
LIVE_MODELS_OK = {"openai", "tokenharbor", "local", "inception"}


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


# Non-provider sections of the provider config: tool configuration that has
# to survive a restart and stay readable by the tool that honours it.
# `web_search` is addressed by name over the wire (serve.py: `config/section`)
# because this daemon has no `config/read` -- and inventing a general one to
# carry a single section would be a second config system standing next to
# this file.
CONFIG_SECTIONS = ("web_search",)

# The modes the `web_search` section accepts. The client's WebSearchMode
# enum also carries `cached`; there is no cache here, so it is refused at
# the boundary rather than stored and ignored -- a setting that silently
# does nothing is how a feature dies.
WEB_SEARCH_MODES = ("disabled", "live")


def public_config_section(name: str, section: dict) -> dict:
    """Wire-safe view of a tool section (secrets never leave the daemon)."""
    key = str(section.get("api_key", "") or "")
    # Absent mode means live, so a fresh install searches before anyone opens
    # Capabilities and the read reports what the tool will actually do.
    return {
        "name": name,
        "mode": str(section.get("mode", "live") or "live"),
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


def _fetch_model_ids(base_url: str, api_key: str,
                     timeout: int = 15) -> list[str] | None:
    """Blocking GET {base}/models (runs in an executor). None when unusable."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        logger.info(f"live model discovery failed: {e}")
        return None
    items = data.get("data", []) if isinstance(data, dict) else []
    ids = sorted({str(x.get("id")) for x in items
                  if isinstance(x, dict) and x.get("id")})
    return ids or None


async def list_models(name: str) -> dict:
    """Models for a provider: config override > live /models > curated.

    Never includes secrets. Raises ValueError for unknown providers.
    """
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider: {name}")
    section = config_section(name)
    override = section.get("models")
    if isinstance(override, list) and override:
        ids = [str(m) for m in override if str(m).strip()]
        if ids:
            return {"provider": name, "default": ids[0],
                    "models": [{"id": m, "source": "config"} for m in ids]}
    if name in LIVE_MODELS_OK:
        base = str(section.get("base_url", "") or DEFAULT_BASES.get(name, ""))
        key = str(section.get("api_key", "") or "")
        if base and key:
            ids = await asyncio.to_thread(_fetch_model_ids, base, key)
            if ids:
                return {"provider": name, "default": ids[0],
                        "models": [{"id": m, "source": "live"} for m in ids]}
    curated = list(CURATED_MODELS.get(name, []))
    return {"provider": name, "default": (curated[0] if curated else ""),
            "models": [{"id": m, "source": "curated"} for m in curated]}


def load_provider(name: str, config: dict) -> BaseProvider | None:
    if name not in PROVIDERS:
        return None
    if not isinstance(config, dict):
        config = {}
    return PROVIDERS[name](
        config.get("api_key", ""),
        config.get("base_url"),
    )
