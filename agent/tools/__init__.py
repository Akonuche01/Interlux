"""Tool registry. Read-only tools run free; WRITE_TOOLS need approval."""

from .shell import run_shell
from .fs import fs_list, fs_read, fs_write
from .git import git_status, git_log, git_diff

TOOLS = {
    "shell": run_shell,
    "fs_list": fs_list,
    "fs_read": fs_read,
    "fs_write": fs_write,
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
}

WRITE_TOOLS = {"shell", "fs_write"}
