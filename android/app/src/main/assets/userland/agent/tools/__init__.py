"""Tool registry. Approval is per tool + action (see needs_approval)."""

from pathlib import Path

from .shell import run_shell
from .exec import exec_tool
from .fs import fs_edit, fs_glob, fs_list, fs_read, fs_search, fs_write
from .git import git_status, git_log, git_diff
from .pkg import pkg, READ_ACTIONS as PKG_READ_ACTIONS
from .pty_run import pty_run
from .tabs import tabs, READ_ACTIONS as TABS_READ_ACTIONS
from .image import image_generate
from .plugins import scan_plugins

TOOLS = {
    "shell": run_shell,
    "exec": exec_tool,
    "pty_run": pty_run,
    "fs_list": fs_list,
    "fs_read": fs_read,
    "fs_write": fs_write,
    "fs_edit": fs_edit,
    "fs_search": fs_search,
    "fs_glob": fs_glob,
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    "pkg": pkg,
    "tabs": tabs,
    "image_generate": image_generate,
}

WRITE_TOOLS = {"shell", "exec", "pty_run", "fs_write", "fs_edit", "image_generate"}

EXTRA_WRITE: set[str] = set()

PLUGIN_DIR = Path(__file__).parent.parent / "plugins"
scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)


def needs_approval(tool_name: str, params: dict) -> bool:
    """True if executing this call must ask the client first."""
    if tool_name in WRITE_TOOLS or tool_name in EXTRA_WRITE:
        return True
    if tool_name == "pkg":
        return str(params.get("action", "list")).lower() not in PKG_READ_ACTIONS
    if tool_name == "tabs":
        return str(params.get("action", "list")).lower() not in TABS_READ_ACTIONS
    return False


def approval_label(tool_name: str, params: dict) -> str:
    """Short human label for the approval prompt.

    Tool-specific branches first: generic command/path fallbacks below
    would otherwise shadow them.
    """
    if tool_name == "exec":
        return " ".join([str(params.get("name", "exec"))] + [str(a) for a in params.get("args", [])])
    if tool_name == "pkg":
        pkgs = " ".join(params.get("packages") or [])
        return f"pkg {params.get('action', 'list')} {pkgs}".strip()
    if tool_name == "tabs":
        action = params.get("action", "list")
        if action == "send":
            return f"tabs send t{params.get('id')}: {str(params.get('data', ''))[:80]}"
        return f"tabs {action}"
    if tool_name == "image_generate":
        return f"image ({params.get('model', '?')}): {str(params.get('prompt', ''))[:100]}"
    if "command" in params:
        return str(params["command"])
    if "path" in params:
        return f"{tool_name} {params['path']}"
    return tool_name
