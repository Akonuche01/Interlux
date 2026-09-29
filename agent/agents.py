"""Agent pairing store (step 1 of the multi-agent platform).

Interlux is a neutral engine: ANY agent app may connect, but nothing
works until the user approves it. This module owns the identities:

- `agents.json` (wipe-proof home, same rule as providers.json) maps
  agent_id -> {label, package, key_hash, created, owner}.
- Only sha256 hashes touch disk. The raw token is shown once, at
  mint time, over the pairing connection. Logs and audit must never
  carry it (callers: construct audit entries without the token).
- Pending pairings (short code -> label/time) live in memory only;
  a restart drops unapproved requests, which is the safe default.

The FIRST client ever seen auto-becomes the owner (physical device
possession is the consent). Everything after that goes through the
approve tap. An empty store therefore means "setup phase": callers
treat it as allow-all so existing tests, probes, and first boot keep
working with zero migration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("INTERLUX_AGENT_CONFIG", "~/.interlux/agent")
).expanduser()

AGENTS_FILE = CONFIG_DIR / "agents.json"

# Pairing codes live this long; approve/deny/revoke RPCs and the sweep
# in request_pairing() enforce it. Short enough that a code seen
# over someone's shoulder dies fast.
PAIRING_TTL_S = 10 * 60

_pending: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def agents_path() -> Path:
    return AGENTS_FILE


def load_agents() -> dict:
    """All paired agents {agent_id: entry}. Empty dict when none."""
    try:
        if AGENTS_FILE.exists():
            data = json.loads(AGENTS_FILE.read_text())
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def save_agents(data: dict) -> Path:
    """Atomic tmp+replace (same discipline as policy/threads)."""
    AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AGENTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(AGENTS_FILE)
    return AGENTS_FILE


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_agent(label: str, owner: bool = False) -> tuple[str, str, dict]:
    """Create an agent, persist it, return (agent_id, token, public entry).

    The token is returned ONCE. Persisted entry holds only the hash.
    """
    agents = load_agents()
    agent_id = "ag_" + secrets.token_hex(8)
    while agent_id in agents:
        agent_id = "ag_" + secrets.token_hex(8)
    token = secrets.token_hex(32)
    entry = {
        "label": str(label or "unknown app")[:120],
        "key_hash": _hash(token),
        "created": _now(),
        "owner": bool(owner),
    }
    agents[agent_id] = entry
    save_agents(agents)
    public = {
        "agent_id": agent_id,
        "label": entry["label"],
        "created": entry["created"],
        "owner": entry["owner"],
    }
    return agent_id, token, public


def verify(agent_id: str, token: str) -> dict | None:
    """Entry when the credential matches, else None (constant-time)."""
    if not agent_id or not token:
        return None
    entry = load_agents().get(str(agent_id))
    if not isinstance(entry, dict):
        return None
    want = str(entry.get("key_hash", ""))
    if not want:
        return None
    if hmac.compare_digest(want, _hash(str(token))):
        return entry
    return None


def is_owner(agent_id: str) -> bool:
    entry = load_agents().get(str(agent_id))
    return bool(isinstance(entry, dict) and entry.get("owner"))


def revoke(agent_id: str) -> bool:
    """Delete an agent. The owner itself can never be revoked (lockout)."""
    agents = load_agents()
    entry = agents.get(str(agent_id))
    if not isinstance(entry, dict) or entry.get("owner"):
        return False
    del agents[str(agent_id)]
    save_agents(agents)
    return True


def public_list() -> list[dict]:
    """Pairing UI view: metadata only, never hashes or tokens."""
    out = []
    for agent_id, entry in sorted(load_agents().items()):
        if not isinstance(entry, dict):
            continue
        out.append({
            "agent_id": agent_id,
            "label": str(entry.get("label", "")),
            "created": str(entry.get("created", "")),
            "owner": bool(entry.get("owner")),
        })
    return out


def request_pairing(label: str) -> dict:
    """Open a pairing request, return {id, code} for the UI + client."""
    now = time.monotonic()
    expired = [k for k, v in _pending.items()
               if now - float(v.get("at", 0)) > PAIRING_TTL_S]
    for k in expired:
        _pending.pop(k, None)
    code = f"{secrets.randbelow(900000) + 100000:06d}"
    pid = "pair_" + secrets.token_hex(4)
    _pending[pid] = {"code": code, "label": str(label or "unknown app")[:120],
                     "at": now}
    return {"id": pid, "code": code}


def pending_list() -> list[dict]:
    """Open pairing requests for the UI (codes included — the user reads
    the code off the approve screen, that IS the confirmation)."""
    now = time.monotonic()
    out = []
    for pid, v in list(_pending.items()):
        if now - float(v.get("at", 0)) > PAIRING_TTL_S:
            _pending.pop(pid, None)
            continue
        out.append({"id": pid, "code": v["code"], "label": v["label"]})
    return out


def pop_pairing(pid: str) -> dict | None:
    """Take (and close) a pairing request. None when unknown/expired."""
    v = _pending.pop(str(pid), None)
    if not isinstance(v, dict):
        return None
    if time.monotonic() - float(v.get("at", 0)) > PAIRING_TTL_S:
        return None
    return v
