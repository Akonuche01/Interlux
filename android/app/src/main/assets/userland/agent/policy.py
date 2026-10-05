"""Epic B: approval scopes + sandbox modes (policy.json).

policy.json lives in ~/.interlux/agent/ (wipe-proof: home survives userland
re-extracts — same rule as providers.json):

    {
      "sandbox": "full",
      "grants": {"<thread>": {"tools": [...], "commands": [...]}}
    }

Sandbox modes per turn:
  full       - today's behavior (default).
  workspace  - fs/image writes confined under one root (turn param
               sandbox_root, default $HOME).
  read-only  - no write-class tool runs at all: the loop blocks them
               BEFORE the approval prompt (asking to approve what is
               forbidden would be theater).

Grant semantics for approve(scope="session"):
command-class tools (shell/exec/pty_run/pkg/tabs) store the exact
approval label - "always allow THIS exact command for this thread";
everything else (fs_write/fs_edit/image_generate/plugins) stores the
tool name - "always allow THIS tool for this thread".

Approval mode: policy["approval_mode"] == "never" auto-grants every
tool call (the owning client's NeverAsk policy). Sandbox/read-only
and plan-mode blocks still apply first - they forbid, not ask.
Revocation: policy method with revoke=<thread>|"*".
"""

from __future__ import annotations

import contextvars
import json
import os
import re
from pathlib import Path

from .home import engine_config_dir, engine_home

SANDBOX_MODES = ("full", "workspace", "read-only")
GRANT_SCOPES = ("turn", "session")

# Tools whose standing grants are recorded per-exact-command, not per-tool.
COMMAND_TOOLS = {"shell", "exec", "pty_run", "pkg", "tabs"}

sandbox_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "interlux_sandbox", default=None
)

# Step 3/4: the agent on whose behalf the current turn runs. Tools that
# resolve per-agent config (provider keys, search keys) read this
# instead of taking an agent parameter the model could spoof: it is set
# by the daemon per turn from socket identity, never from wire params.
agent_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "interlux_agent", default=""
)


def policy_path() -> Path:
    return engine_config_dir() / "policy.json"


def load_policy() -> dict:
    try:
        path = policy_path()
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                if data.get("sandbox") not in SANDBOX_MODES:
                    data["sandbox"] = "full"
                if not isinstance(data.get("grants"), dict):
                    data["grants"] = {}
                if not isinstance(data.get("agents"), dict):
                    data["agents"] = {}
                return data
    except Exception:
        pass
    return {"sandbox": "full", "grants": {}, "agents": {}}


def save_policy(policy: dict) -> None:
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def agent_policy(policy: dict, agent_id: str) -> dict:
    """Merge global defaults with per-agent overrides.

    Agent override wins for: sandbox, default_provider, default_model,
    approval_mode. Grants are per-agent (stored under agents.<id>.grants).
    Returns a resolved policy dict for the given agent.
    """
    resolved = dict(policy)
    agents = policy.get("agents", {})
    if not isinstance(agents, dict):
        agents = {}
    override = agents.get(agent_id, {})
    if not isinstance(override, dict):
        override = {}
    for key in ("sandbox", "default_provider", "default_model",
                "approval_mode"):
        if key in override:
            resolved[key] = override[key]
    # Grants: per-agent grants live under agents.<id>.grants
    agent_grants = override.get("grants", {})
    if not isinstance(agent_grants, dict):
        agent_grants = {}
    resolved["grants"] = agent_grants
    # Observability (step 4): cumulative spend rides the resolved policy
    # so an admin screen can show per-agent usage with no new method.
    resolved["usage"] = {"tokens": agent_usage(policy, agent_id)}
    # Namespacing: the resolved view carries ONLY the caller's own
    # section, minus secret material. A shallow dict() copy would
    # otherwise hand every agent's quota, usage, grants -- and consent
    # hashes -- to whoever asks.
    if isinstance(agent_id, str) and agent_id:
        own = {k: v for k, v in override.items() if k != "consent"}
        resolved["agents"] = {agent_id: own}
    else:
        resolved["agents"] = {}
    return resolved


def thread_grants(policy: dict, thread_id: str) -> dict:
    grants = policy.setdefault("grants", {})
    entry = grants.get(thread_id)
    if not isinstance(entry, dict):
        entry = {"tools": [], "commands": []}
        grants[thread_id] = entry
    for key in ("tools", "commands"):
        if not isinstance(entry.get(key), list):
            entry[key] = []
    return entry


def record_grant(policy: dict, thread_id: str, tool: str, label: str) -> str:
    """Add a standing grant for this thread; returns what was recorded."""
    entry = thread_grants(policy, thread_id)
    if tool in COMMAND_TOOLS:
        what = label
        bucket = entry["commands"]
    else:
        what = tool
        bucket = entry["tools"]
    if what not in bucket:
        bucket.append(what)
    return what


def record_agent_grant(policy: dict, agent_id: str, thread_id: str,
                       tool: str, label: str) -> str:
    """Standing grant inside one agent's section (the resolved policy
    reads agent grants, so global recording would never be honored)."""
    if not isinstance(policy, dict):
        policy = {}
    agents = policy.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = policy["agents"] = {}
    section = agents.setdefault(agent_id, {})
    if not isinstance(section, dict):
        section = agents[agent_id] = {}
    grants = section.setdefault("grants", {})
    if not isinstance(grants, dict):
        grants = section["grants"] = {}
    entry = grants.get(thread_id)
    if not isinstance(entry, dict):
        entry = {"tools": [], "commands": []}
        grants[thread_id] = entry
    for key in ("tools", "commands"):
        if not isinstance(entry.get(key), list):
            entry[key] = []
    if tool in COMMAND_TOOLS:
        what = label
        bucket = entry["commands"]
    else:
        what = tool
        bucket = entry["tools"]
    if what not in bucket:
        bucket.append(what)
    return what


def allows(policy: dict, thread_id: str, tool: str, label: str) -> bool:
    """True when a standing grant covers this exact call."""
    if not isinstance(policy, dict):
        return False
    # Compat note: a client's NeverAsk policy auto-answers every approval
    # client-side; on iagent the daemon honors it directly.
    if policy.get("approval_mode") == "never":
        return True
    entry = policy.get("grants", {}).get(thread_id)
    if not isinstance(entry, dict):
        return False
    if tool not in COMMAND_TOOLS and tool in (entry.get("tools") or []):
        return True
    if label in (entry.get("commands") or []):
        return True
    return False


def revoke(policy: dict, thread_id: str) -> int:
    """Drop grants for one thread (or '*' for all). Returns entries removed."""
    grants = policy.setdefault("grants", {})
    if thread_id == "*":
        n = len(grants)
        policy["grants"] = {}
        return n
    return 1 if grants.pop(thread_id, None) is not None else 0


def resolve_sandbox(params: dict, policy: dict) -> dict:
    """Turn param wins; policy default otherwise. Returns {mode, root}."""
    mode = params.get("sandbox")
    if mode not in SANDBOX_MODES:
        mode = policy.get("sandbox", "full")
    root = params.get("sandbox_root") or str(engine_home())
    return {"mode": mode, "root": str(Path(root).expanduser())}


def resolve_run_target(params: dict, policy: dict) -> dict:
    """The provider and model a call runs on. Returns {provider, model}.

    The **only** place either is decided. A turn, a compaction, a fork, a
    subagent -- every caller resolves through here, so no two of them can
    disagree about what this installation runs on.

    Resolution order, and nothing else:

        the call's own params  ->  the policy  ->  ``""``

    The empty string is the point of the function, not an oversight. This used
    to fall back to a named vendor and a named model, and that one line is
    exactly how ``thread/compact`` came to demand ``openai`` on a phone whose
    provider was ``apinex``: the turn path read the policy, the compact path
    read a literal, and the two agreed only on a machine where that vendor
    happened to be configured. Neither name belongs in the engine.

    A default provider is also a **data-egress decision**. A fresh install with
    no policy would have quietly sent the user's work to whichever vendor the
    source named, without anyone choosing it. That is not the engine's call to
    make. When nothing is configured the honest answer is "nothing is
    configured", said out loud -- see :func:`no_provider_message` -- so the fix
    is the one the user can actually perform.

    ``policy`` is expected to be already agent-merged (see :func:`agent_policy`).
    """
    # Strip each candidate *before* the `or` chain. Written the other way
    # round -- `params.get("provider") or policy.get(...)` then `.strip()`
    # -- a whitespace-only param is truthy, wins the chain, and is then
    # stripped to nothing, so a caller sending `"provider": "  "` blanks a
    # provider the policy had already named. The strip has to happen while
    # the candidate is still a candidate.
    provider = (str(params.get("provider") or "").strip()
                or str(policy.get("default_provider") or "").strip())
    model = (str(params.get("model") or "").strip()
             or str(policy.get("default_model") or "").strip())
    return {"provider": provider, "model": model}


def no_provider_message(provider: str) -> str:
    """Why a call could not run, phrased for the person who can fix it.

    One sentence, from one place, so a missing provider reads the same whether
    it stopped a turn or a compaction -- the two used to say different things
    about the same fault, and one of them blamed a vendor the user had never
    configured.
    """
    if not provider:
        return ("no provider configured: add one in Settings and choose it as "
                "the default")
    return (f"provider not available: {provider} is not configured -- check "
            f"its API key in Settings")


def sandbox_adjust(path: Path) -> Path:
    """Workspace mode: re-root relative paths under the sandbox root so the
    confinement check and the actual write agree (relative writes land in
    the workspace, not wherever the daemon's cwd happens to be)."""
    ctx = sandbox_ctx.get()
    if ctx and ctx.get("mode") == "workspace" and not path.is_absolute():
        return Path(ctx.get("root") or ".") / path
    return path


def check_write(path: Path | str) -> str | None:
    """Sandbox gate for file-writing tools. None = allowed."""
    ctx = sandbox_ctx.get()
    if not ctx:
        return None
    mode = ctx.get("mode", "full")
    if mode == "read-only":
        return "sandbox is read-only"
    if mode == "workspace":
        try:
            root = Path(ctx.get("root") or ".").resolve()
            target = Path(path).resolve()
        except Exception:
            return "sandbox: cannot resolve path"
        if not target.is_relative_to(root):
            return f"outside workspace root {root}"
    return None


def agent_usage(policy: dict, agent_id: str) -> int:
    """Cumulative tokens spent by an agent (0 when untracked)."""
    if isinstance(policy, dict):
        agents = policy.get("agents")
        if isinstance(agents, dict):
            entry = agents.get(agent_id)
            if isinstance(entry, dict):
                usage = entry.get("usage")
                if isinstance(usage, dict):
                    try:
                        return max(0, int(usage.get("tokens", 0) or 0))
                    except (TypeError, ValueError):
                        pass
    return 0


def add_agent_usage(policy: dict, agent_id: str, tokens: int) -> int:
    """Add spend to an agent's cumulative total. Returns the new total."""
    try:
        amount = int(tokens)
    except (TypeError, ValueError):
        return agent_usage(policy, agent_id)
    if amount <= 0:
        return agent_usage(policy, agent_id)
    agents = policy.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = policy["agents"] = {}
    entry = agents.setdefault(agent_id, {})
    if not isinstance(entry, dict):
        entry = agents[agent_id] = {}
    usage = entry.setdefault("usage", {})
    if not isinstance(usage, dict):
        usage = entry["usage"] = {}
    try:
        total = int(usage.get("tokens", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    total = max(0, total) + amount
    usage["tokens"] = total
    return total


# Owner consent gate (pentest authorization): the owner sets a
# passphrase; speaking it in a turn marks that turn owner-authorized.
# Stored as a salted hash only, never the phrase -- the resolved policy
# never carries it (see agent_policy sanitizing), so it cannot leak
# over RPC. Minimum length keeps accidental triggers out.
CONSENT_MIN_LENGTH = 4
_CONSENT_SALT = "interlux-owner-consent-v1:"


def _consent_hash(phrase: str) -> str:
    import hashlib
    return hashlib.sha256(
        (_CONSENT_SALT + phrase).encode("utf-8")).hexdigest()


def set_owner_consent(policy: dict, owner_id: str, phrase: str) -> bool:
    """Store a consent passphrase hash for the owner. False when refused.

    Refused (nothing written) when the phrase is shorter than
    CONSENT_MIN_LENGTH: short phrases trigger on ordinary chat.
    Matching is per-word and case-insensitive (see below), so the
    stored form is normalized the same way.
    """
    if not isinstance(phrase, str):
        return False
    norm = phrase.strip().lower()
    if len(norm) < CONSENT_MIN_LENGTH:
        return False
    if not isinstance(policy, dict):
        return False
    agents = policy.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = policy["agents"] = {}
    entry = agents.setdefault(owner_id, {})
    if not isinstance(entry, dict):
        entry = agents[owner_id] = {}
    entry["consent"] = {"phrase_hash": _consent_hash(norm)}
    return True


def clear_owner_consent(policy: dict, owner_id: str) -> bool:
    """Remove the owner's consent passphrase. True when one existed."""
    try:
        entry = policy.get("agents", {}).get(owner_id)
        if isinstance(entry, dict) and entry.pop("consent", None) is not None:
            return True
    except Exception:
        pass
    return False


def extract_owner_consent(policy: dict, owner_id: str,
                          text: str) -> tuple[bool, str]:
    """Detect the owner's consent passphrase as a word inside text.

    Returns (matched, cleaned) where cleaned has the first matching
    word removed. Matching is per-word, case-insensitive, punctuation
    stripped at the edges: the passphrase rides along inside a normal
    instruction ("run nmap on my lab, father") instead of requiring a
    bare magic message (which would leave turns with no content).
    Comparison is hash-to-hash per token; the phrase itself is never
    stored or logged.
    """
    try:
        entry = policy.get("agents", {}).get(owner_id)
        if not isinstance(entry, dict):
            return False, text
        want = entry.get("consent", {}).get("phrase_hash", "")
        if not want or not isinstance(text, str):
            return False, text
        import hmac
        import string as _string
        for raw in text.split():
            token = raw.strip(_string.punctuation).lower()
            if len(token) < CONSENT_MIN_LENGTH:
                continue
            if hmac.compare_digest(str(want), _consent_hash(token)):
                start = text.find(raw)
                cleaned = (text[:start] + text[start + len(raw):]).strip()
                return True, re.sub(r"[ \\t]{2,}", " ", cleaned)
        return False, text
    except Exception:
        return False, text


def check_owner_consent(policy: dict, owner_id: str, text: str) -> bool:
    """Whether text carries the owner's consent passphrase."""
    matched, _ = extract_owner_consent(policy, owner_id, text)
    return matched


# Step 4 quotas live in the per-agent policy section:
# agents.<agent_id>.quota = {"concurrency": N, "tokens": M,
# "max_rounds": R}. Defaults: 4 concurrent turns, unlimited tokens,
# global turn ceiling. The owner is exempt from every limit (checked
# by callers via agents.is_owner, never here -- exemption must be
# impossible to misconfigure into place).
DEFAULT_CONCURRENCY = 4


def agent_quota(policy: dict, agent_id: str) -> dict:
    """Concurrency cap + token budget + turn ceiling for an agent.

    Returns {"concurrency": int >= 1, "tokens": int | None,
    "max_rounds": int | None}.
    Malformed values fall back to defaults rather than failing open
    (unlimited) or closed (zero): quotas must never crash a turn.
    """
    quota: dict = {}
    if isinstance(policy, dict):
        agents = policy.get("agents")
        if isinstance(agents, dict):
            entry = agents.get(agent_id)
            if isinstance(entry, dict):
                raw = entry.get("quota")
                if isinstance(raw, dict):
                    quota = raw
    try:
        concurrency = int(quota.get("concurrency", DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        concurrency = DEFAULT_CONCURRENCY
    concurrency = max(1, concurrency)
    tokens = quota.get("tokens", None)
    try:
        tokens = int(tokens) if tokens is not None else None
    except (TypeError, ValueError):
        tokens = None
    if tokens is not None and tokens < 0:
        tokens = None
    ceiling = quota.get("max_rounds", None)
    try:
        ceiling = int(ceiling) if ceiling is not None else None
    except (TypeError, ValueError):
        ceiling = None
    if ceiling is not None and ceiling < 1:
        ceiling = None
    return {"concurrency": concurrency, "tokens": tokens,
            "max_rounds": ceiling}
