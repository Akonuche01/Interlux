"""Shared daemon config readers (tools-safe: never imports serve).

provider_config(): turn-env override first, then wipe-proof home file,
then the bundled agent/providers.json. Sections (providers, web_search,
...) are read per call so config edits apply without a restart.

Step 3 (per-agent credentials): secrets live under
`agents: {agent_id: {provider: {api_key}}}`. `api_key` resolves ONLY
from the caller's own section -- no global or cross-agent fallback,
ever. Non-secrets resolve agent section -> global -> builtins.
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

from .home import engine_config_dir
from .providers import BaseProvider, UnknownDialect, base_for, build_provider
from .providers.dialects import dialect_of
from .providers.base import BROWSER_UA

logger = logging.getLogger("pconfig")

PROVIDER_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def config_file_path() -> Path:
    return engine_config_dir() / "providers.json"


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


def agent_credential_sections(data: dict) -> dict:
    """The `agents` vault of a providers blob, {} when absent."""
    if not isinstance(data, dict):
        return {}
    agents = data.get("agents")
    return agents if isinstance(agents, dict) else {}


def agent_provider_block(data: dict, agent_id: str, name: str) -> dict:
    """The caller's own secret block for a provider, {} when none."""
    if not agent_id or not isinstance(data, dict):
        return {}
    section = agent_credential_sections(data).get(agent_id)
    if not isinstance(section, dict):
        return {}
    block = section.get(name)
    return block if isinstance(block, dict) else {}


def resolve_api_key(name: str, agent_id: str = "",
                    data: dict | None = None) -> str:
    """This caller's key for a provider, else "".

    No global fallback and no cross-agent fallback: a key the caller
    did not configure reads exactly as absent. The INTERLUX_PROVIDERS
    env override bypasses scoping by design (single-operator setups)
    and is the only exception.
    """
    if data is None:
        data = provider_config()
    if not isinstance(data, dict):
        return ""
    if env_pinned():
        top = data.get(name)
        if isinstance(top, dict):
            return str(top.get("api_key", "") or "")
        return ""
    return str(agent_provider_block(data, agent_id, name).get("api_key",
                                                              "") or "")


def resolve_provider(name: str, agent_id: str = "",
                     data: dict | None = None) -> dict:
    """Merged provider block shaped for load_provider.

    Non-secrets resolve agent section -> global block; `api_key` is the
    caller's own or "". Copying the global block first and then
    overwriting the key is what keeps a legacy top-level secret out of
    provider instantiation even before migration runs.
    """
    if data is None:
        data = provider_config()
    top = data.get(name) if isinstance(data, dict) else None
    top = top if isinstance(top, dict) else {}
    merged = dict(top)
    own = agent_provider_block(data if isinstance(data, dict) else {},
                               agent_id, name)
    for key, value in own.items():
        if key != "api_key":
            merged[key] = value
    merged["api_key"] = resolve_api_key(name, agent_id, data)
    return merged


def configured(section: dict) -> bool:
    """Whether a resolved provider section describes anything at all.

    A resolved section always carries an `api_key` key, possibly empty, so
    the key alone cannot answer this. A base URL, a model list, a real key,
    or an explicit dialect or options block means the user configured this
    provider; an empty section means the name was never configured, and a
    name that was never configured is not a provider.
    """
    if not isinstance(section, dict):
        return False
    return bool(
        str(section.get("api_key") or "").strip()
        or str(section.get("base_url") or "").strip()
        or str(section.get("dialect") or "").strip()
        or section.get("models")
        or section.get("options")
    )


def configured_provider_names(agent_id: str = "",
                              data: dict | None = None) -> list[str]:
    """Every provider this installation has actually configured.

    The engine keeps no list of its own, and that is the point. A provider
    exists because a config section exists for it -- at the top level or
    under an agent. So a vendor nobody holds a key for is never offered,
    and a vendor the code has never heard of works the moment it is
    configured, under whatever name the user chose.
    """
    if data is None:
        data = provider_config()
    if not isinstance(data, dict):
        return []
    found = {k for k, v in data.items()
             if k != "agents" and isinstance(v, dict)}
    agents = data.get("agents")
    if isinstance(agents, dict):
        blocks = ([agents.get(agent_id)] if agent_id
                  else list(agents.values()))
        for block in blocks:
            if isinstance(block, dict):
                found |= {k for k, v in block.items() if isinstance(v, dict)}
    return sorted(found)


def resolve_tool_key(section: str, agent_id: str = "",
                     data: dict | None = None) -> str:
    """This caller's key for a tool section (web_search), else ""."""
    return resolve_api_key(section, agent_id, data)


def migrate_provider_keys(agent_id: str) -> list[str]:
    """Move top-level api_keys into one agent's section.

    Returns the moved block names (never values) for the log. Blocks
    already keyed under the agent keep their key; the top-level secret
    is deleted either way. After migration no `api_key` remains at top
    level. Env-pinned setups are left alone (nothing on disk to move).
    """
    if not agent_id or env_pinned():
        return []
    data = read_providers_file()
    if not isinstance(data, dict):
        return []
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    section = agents.setdefault(agent_id, {})
    if not isinstance(section, dict):
        section = {}
        agents[agent_id] = section
    moved: list[str] = []
    for name, block in list(data.items()):
        if name == "agents" or not isinstance(block, dict):
            continue
        key = block.get("api_key")
        if not (isinstance(key, str) and key):
            continue
        dest = section.setdefault(name, {})
        if not isinstance(dest, dict):
            dest = {}
            section[name] = dest
        if not dest.get("api_key"):
            dest["api_key"] = key
        del block["api_key"]
        moved.append(name)
    if moved:
        write_providers_file(data)
        logger.info(f"migrated provider keys to agent {agent_id}: {moved}")
    return moved


def top_level_key_names() -> list[str]:
    """Provider/tool blocks still carrying a top-level api_key.

    Post-migration drift check: a name here means a secret bypasses
    per-agent scoping and the boot log should say so loudly.
    """
    if env_pinned():
        return []
    data = read_providers_file()
    if not isinstance(data, dict):
        return []
    out = []
    for name, block in data.items():
        if name == "agents" or not isinstance(block, dict):
            continue
        key = block.get("api_key")
        if isinstance(key, str) and key:
            out.append(name)
    return out


def public_section(name: str, section: dict) -> dict:
    """Wire-safe view of a config section (secrets never leave the daemon)."""
    key = str(section.get("api_key", "") or "")
    return {
        "provider": name,
        "base_url": str(section.get("base_url", "") or ""),
        "has_key": bool(key),
        "key_hint": mask_key(key),
        # A provider speaking the on-device dialect is a local model served
        # from this device -- Kara gives those their own card, and any agent
        # can treat the endpoint as a plain OpenAI server.
        "local": dialect_of(section) == "on-device",
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
                     timeout: int = 15) -> tuple[list[str] | None, str | None]:
    """Blocking GET {base}/models (runs in an executor).

    Returns (ids, error): ids None when unusable, with a short reason
    (HTTP status or transport failure) instead of silence — callers
    surface it so "empty catalogue" and "could not ask" stay distinct.
    """
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": BROWSER_UA,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        logger.info(f"live model discovery failed: {e}")
        return None, "unreachable"
    items = data.get("data", []) if isinstance(data, dict) else []
    ids = sorted({str(x.get("id")) for x in items
                  if isinstance(x, dict) and x.get("id")})
    if not ids:
        return None, "empty catalogue"
    return ids, None


async def list_models(name: str, agent_id: str = "") -> dict:
    """Models for a provider: config override > live catalogue.

    Never includes secrets. There is no list of known providers to check
    against -- a provider exists because it is configured, and a name that
    is configured for nothing raises. A provider's own `models` list wins;
    otherwise the vendor's catalogue is fetched with the CALLER's key only,
    so another agent's key never leaves its section.
    """
    section = resolve_provider(name, agent_id)
    if not configured(section):
        raise ValueError(f"unknown provider: {name}")
    override = section.get("models")
    if isinstance(override, list) and override:
        ids = [str(m) for m in override if str(m).strip()]
        if ids:
            return {"provider": name, "default": ids[0],
                    "models": [{"id": m, "source": "config"} for m in ids]}
    base = base_for(section)
    key = str(section.get("api_key", "") or "")
    discovery: dict = {"attempted": False, "error": None}
    if base and key:
        discovery["attempted"] = True
        ids, err = await asyncio.to_thread(_fetch_model_ids, base, key)
        if ids:
            return {"provider": name, "default": ids[0],
                    "models": [{"id": m, "source": "live"} for m in ids],
                    "discovery": discovery}
        discovery["error"] = err or "unknown"
    return {"provider": name, "default": "",
            "models": [], "discovery": discovery}



def load_provider(name: str, config: dict) -> BaseProvider | None:
    """The adapter for one configured provider, or None.

    There is nothing to look up and nothing to fall back to. A provider is
    whatever the user configured, and the wire format it speaks defaults to
    the OpenAI chat-completions format -- what nearly every gateway, relay,
    local runner and vendor clone speaks. A section that asks for a format
    the engine cannot speak is refused out loud rather than served by
    something that only looks like it works.

    None means "this name is not configured", and every caller already
    reports that as a failure rather than proceeding. That is deliberate:
    the previous behaviour here -- quietly serving a generic adapter for any
    unrecognised name -- is how a one-word typo cost the thoughts half of
    the client's activity card for weeks without a single line in the log.
    """
    if not isinstance(config, dict) or not configured(config):
        return None
    try:
        return build_provider(name, config)
    except UnknownDialect as e:
        logger.error(str(e))
        return None


def open_provider(name: str, agent_id: str = "") -> BaseProvider | None:
    """The adapter for a resolved provider name, or None when it is not configured.

    The two-step it replaces -- ``load_provider(n, resolve_provider(n, agent))``
    -- was written out at every call site, and the sites drifted: one of them
    passed a hardcoded name instead of the resolved one, which is how a
    compaction came to ask for a provider the user had never configured. One
    call, one behaviour, no site left to get it subtly wrong.

    An empty ``name`` is not an error here; it means nothing is configured.
    The caller reports that with :func:`policy.no_provider_message`.
    """
    if not name:
        return None
    return load_provider(name, resolve_provider(name, agent_id))
