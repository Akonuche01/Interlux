"""Standalone local-model serving for Interlux.

This package is deliberately SEPARATE from the agent daemon. It owns
on-device inference end to end:

  * it discovers every GGUF the user drops into the models directory,
  * it runs one ``llama-server`` per model on its own loopback port,
  * and it publishes a registry file that ANY agent -- Kara, or anything
    else the user chooses -- can read to find and use those endpoints.

It does **not** import the daemon, and the daemon does **not** import it.
Nothing here touches providers.json, the policy, or the daemon's turn
path. An agent that wants a local model reads ``local_models.json`` (or
calls the CLI) and speaks plain OpenAI to ``http://127.0.0.1:<port>/v1``.
"""

from .manager import (
    AUTOSTART_FILE,
    BIN,
    MODELS_DIR,
    REGISTRY_FILE,
    as_provider_block,
    autostart,
    base_url,
    discover,
    ensure,
    is_up,
    list_models,
    stop,
    write_registry,
)

__all__ = [
    "AUTOSTART_FILE",
    "BIN",
    "MODELS_DIR",
    "REGISTRY_FILE",
    "as_provider_block",
    "autostart",
    "base_url",
    "discover",
    "ensure",
    "is_up",
    "list_models",
    "stop",
    "write_registry",
]
