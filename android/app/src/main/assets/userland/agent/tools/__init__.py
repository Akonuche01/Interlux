"""Tool registry. Approval is per tool + action (see needs_approval)."""

from .shell import run_shell
from .fs import fs_list, fs_read, fs_write
from .git import git_status, git_log, git_diff
from .pkg import pkg, READ_ACTIONS as PKG_READ_ACTIONS
from .pty_run import pty_run

TOOLS = {
    "shell": run_shell,
    "pty_run": pty_run,
    "fs_list": fs_list,
    "fs_read": fs_read,
    "fs_write": fs_write,
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    "pkg": pkg,
}

WRITE_TOOLS = {"shell", "pty_run", "fs_write"}


def needs_approval(tool_name: str, params: dict) -> bool:
    """True if executing this call must ask the client first."""
    if tool_name in WRITE_TOOLS:
        return True
    if tool_name == "pkg":
        return str(params.get("action", "list")).lower() not in PKG_READ_ACTIONS
    return False


def approval_label(tool_name: str, params: dict) -> str:
    """Short human label for the approval prompt."""
    if "command" in params:
        return str(params["command"])
    if "path" in params:
        return f"{tool_name} {params['path']}"
    if tool_name == "pkg":
        pkgs = " ".join(params.get("packages") or [])
        return f"pkg {params.get('action', 'list')} {pkgs}".strip()
    return tool_name
