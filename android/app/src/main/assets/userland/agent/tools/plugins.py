"""Drop-in plugin loader.

Any `<plugindir>/*.py` file may define:
  TOOLS = {"name": async_callable, ...}
  WRITE_TOOLS = {"name", ...}   # subset needing approval (optional)

Only NEW files load per scan, so `tools_refresh` picks up drops without a
daemon restart. Files are never unloaded (restart to remove).
"""

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("tools.plugins")

_loaded: set[str] = set()


def scan_plugins(plugindir: Path, registry: dict, write: set) -> list[str]:
    """Import unseen plugin files into registry. Returns newly loaded names."""
    loaded: list[str] = []
    if not plugindir.is_dir():
        return loaded
    for f in sorted(plugindir.glob("*.py")):
        if f.stem in _loaded or f.stem.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"interlux_plugin_{f.stem}", f)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            tools = getattr(mod, "TOOLS", None)
            if not isinstance(tools, dict) or not tools:
                continue
            registry.update(tools)
            for t in getattr(mod, "WRITE_TOOLS", set()) or []:
                write.add(str(t))
            _loaded.add(f.stem)
            loaded.append(f.stem)
            logger.info(f"plugin loaded: {f.stem} tools={sorted(tools)}")
        except Exception as e:
            logger.error(f"plugin {f.stem} failed: {e}")
    return loaded
