"""Epic G: review recipe as an executable tool.

git_diff -> model critique -> structured findings, in one read-only call
(no approval: it only reads the repo and calls a provider). Returns
{status, repo, diff_stat, findings} where findings is the model's text
following the schema below (plus a best-effort parsed list).
"""

from __future__ import annotations

import json

from ..pconfig import configured_provider_names, load_provider, resolve_provider
from ..policy import agent_ctx, load_policy
from .git import git_diff

MAX_DIFF_CHARS = 20000

CRITIQUE_PROMPT = """You review an unstaged git diff. Be concrete and terse.
Reply with a short SUMMARY line, then one finding per line, EXACTLY in this
shape (severity is info|minor|major|critical):

SUMMARY: <one line verdict>
- [severity] path/to/file.py:LINE short issue -> concrete suggestion

Skip style nits and praise. If the diff is empty or trivially safe, reply
with just: SUMMARY: clean.
Skip anything you are unsure about. Do not invent files or lines.

DIFF:
"""


async def _git_stat(cwd: str) -> str:
    from .git import _git

    res = await _git(["diff", "--stat"], cwd)
    if res.get("status") == "success":
        return res.get("stdout", "")
    return ""


async def review(
    cwd: str = ".",
    provider: str = "",
    model: str = "",
    max_diff_chars: int = MAX_DIFF_CHARS,
) -> dict:
    """Critique the unstaged diff of a repo. Read-only.

    The provider is never named in code. It is the one the caller chose, or
    the configured default, or simply the only provider configured -- a vendor
    hardcoded here would be a vendor the user may hold no key for.
    """
    diff = await git_diff(cwd)
    if diff.get("status") != "success":
        return {
            "status": "error",
            "message": f"git diff failed: {diff.get('stderr', '')[:500]}",
        }
    text = diff.get("stdout", "") or ""
    if not text.strip():
        return {
            "status": "success",
            "repo": cwd,
            "diff_stat": "",
            "diff_chars": 0,
            "truncated": False,
            "summary": "clean",
            "findings": "SUMMARY: clean",
        }
    try:
        limit = max(1000, min(int(max_diff_chars or MAX_DIFF_CHARS), 100000))
    except (TypeError, ValueError):
        limit = MAX_DIFF_CHARS
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]

    if not provider:
        # Never name a vendor here -- see the docstring. This used to default
        # to "tokenharbor", so EVERY call failed with "provider not available"
        # on any install that held no Token Harbor key, even when another
        # provider was configured and working.
        provider = str(load_policy().get("default_provider") or "")
        if not provider:
            names = configured_provider_names()
            provider = names[0] if names else ""
        if not provider:
            return {"status": "error", "message": "no provider is configured"}

    # Per-agent credentials (step 3): the calling agent's key, same as
    # turns get. Setup phase (no agent) resolves the legacy globals.
    prov = load_provider(
        provider, resolve_provider(provider, agent_ctx.get("")))
    if prov is None:
        return {
            "status": "error",
            "message": f"provider not available: {provider}",
        }
    findings = ""
    try:
        async for delta in prov.stream_turn(
            [{"role": "user", "content": CRITIQUE_PROMPT + text}],
            model=model,
            images=None,
            api="chat",
            stream=False,
        ):
            if delta.get("type") == "text_delta":
                findings += delta.get("content", "")
            elif delta.get("type") == "error":
                return {
                    "status": "error",
                    "message": f"critique failed: {delta.get('message', '')[:500]}",
                }
    except Exception as e:
        return {"status": "error", "message": f"critique failed: {e}"}
    if not findings.strip():
        return {"status": "error", "message": "critique returned empty"}
    return {
        "status": "success",
        "repo": cwd,
        "diff_stat": await _git_stat(cwd),
        "diff_chars": len(text),
        "truncated": truncated,
        "summary": findings.strip().splitlines()[0][:300],
        "findings": findings.strip()[:20000],
    }
