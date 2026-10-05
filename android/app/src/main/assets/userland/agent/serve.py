from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import re
import time
from pathlib import Path
from typing import AsyncIterator

from .audit import AuditLog
from . import agents as agents_mod
from . import calls as calls_mod
from .home import engine_home
from . import mcp as mcp_mod
from . import pconfig as pconfig_mod
from . import threads as threads_mod
from . import toolargs as toolargs_mod
from .pconfig import (
    CONFIG_SECTIONS,
    PROVIDER_NAME_RE,
    WEB_SEARCH_MODES,
    config_section,
    configured_provider_names,
    env_pinned,
    list_models,
    open_provider,
    provider_config,
    public_config_section,
    public_section,
    read_providers_file,
    resolve_api_key,
    resolve_provider,
    resolve_tool_key,
    write_providers_file,
)
from .policy import (
    GRANT_SCOPES,
    SANDBOX_MODES,
    add_agent_usage,
    agent_ctx,
    agent_policy,
    agent_quota,
    agent_usage,
    clear_owner_consent,
    extract_owner_consent,
    no_provider_message,
    record_agent_grant,
    allows,
    load_policy,
    policy_path,
    record_grant,
    resolve_run_target,
    resolve_sandbox,
    revoke,
    save_policy,
    sandbox_ctx,
    set_owner_consent,
)
from .providers import BaseProvider, base_for
from .skills import (
    catalog_message as skills_catalog,
    get_skill as skills_get,
    list_skills as skills_list,
    refresh as skills_refresh,
    register_skill_tool,
    scan_skill_plugins,
    skill_messages,
    USER_DIR as SKILLS_USER_DIR,
)
from .threads import (
    DEFAULT_IDENTITY,
    archive_thread,
    build_messages,
    fold_output,
    fork_state,
    import_history,
    list_threads,
    load_state,
    materialize_state,
    memories_path,
    new_state,
    read_memories,
    read_thread,
    record_turn,
    save_state,
    state_path,
    transcript_text,
    tool_failed,
    unarchive_thread,
    write_memories,
)
from .tools import EXTRA_WRITE, TOOLS, approval_label, needs_approval
from .tools.plugins import scan_plugins
from .tools.track import current_turn as turn_ctx
from .tools.track import kill_turn
from .transport import Transport, on_connect, on_disconnect, send_to
from . import transport as transport_mod

# Thread owner cache for broadcast routing (thread_id -> agent_id).
# Populated lazily from state files; owners are stamped at creation and
# migration runs at boot before clients connect, so entries never go
# stale within a process lifetime.
_thread_agents: dict[str, str] = {}


async def broadcast(payload: dict) -> None:
    """Namespaced broadcast: thread events reach only the owner's agent.

    Payloads naming a thread_id get the owner's agent_id injected so
    transport routes them to that agent's sockets alone. Global events
    (no thread_id, or owner unknown/unresolvable) still go to all
    clients — the safe default, preserving today's behavior exactly.
    """
    tid = payload.get("thread_id")
    if tid and not payload.get("agent_id"):
        key = str(tid)
        agent = _thread_agents.get(key)
        if agent is None:
            try:
                state = load_state(key)
                agent = (state.get("agent", "") if state else "")
            except Exception:
                agent = ""
            # Cache only a REAL owner. handle_turn builds a new thread's
            # state in memory and save_state() only runs later, so the very
            # first broadcast for a new thread finds no state file and
            # resolves to "". Caching that here stuck for the whole process
            # lifetime: every later event for the thread fell through to the
            # GLOBAL path and leaked to every connected client, defeating
            # the isolation this routing exists to provide.
            if agent:
                _thread_agents[key] = agent
        if agent:
            payload = {**payload, "agent_id": agent}
    await transport_mod.broadcast(payload)

PLUGIN_DIR = Path(__file__).parent / "plugins"
scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)

# Epic D: skills load at boot; skill tool plugins use the same machinery.
register_skill_tool(TOOLS)
skills_refresh()
scan_skill_plugins(TOOLS, EXTRA_WRITE)


# Epic E: MCP proxy tools join the same registry (approval + audit via
# EXTRA_WRITE: every MCP call asks first, session-grantable like the rest).
def _mcp_register(add: list[str], remove: list[str]) -> list[str]:
    for key in remove:
        TOOLS.pop(key, None)
        EXTRA_WRITE.discard(key)
    added = []
    for key in add:
        if key in TOOLS:
            continue

        async def _proxy(_key: str = key, **kwargs):
            return await mcp_mod.call(_key, kwargs)

        TOOLS[key] = _proxy
        EXTRA_WRITE.add(key)
        added.append(key)
    if added:
        logger.info(f"mcp tools registered: {added}")
    if remove:
        logger.info(f"mcp tools removed: {remove}")
    return added


mcp_mod.set_registration_hook(_mcp_register)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("iagent")


audit = AuditLog()


_APPROVE_ALLOW_WORDS = {
    "approve", "accept", "allow", "yes", "always", "true",
    "acceptforsession",
}
_APPROVE_SESSION_WORDS = {"session", "always", "acceptforsession"}
_APPROVE_DENY_WORDS = {"deny", "decline", "refuse", "no", "false"}


def _parse_approval_decision(decision, scope: str = "turn") -> tuple[bool, str]:
    """(granted, grant_scope) from any client decision shape.

    Understands our plain words, our {decision, scope} RPC params, and
    compat-client decision literals: accept (once), acceptForSession (always),
    decline, plus amendment objects (one-shot allow — persistent policy
    amendments have no home here, so they degrade loudly to a single allow
    in the daemon log, never silently).
    """
    if isinstance(decision, dict):
        text = json.dumps(decision).lower()
        if "decline" in text or "deny" in text:
            return False, "turn"
        if "acceptforsession" in text:
            return True, "session"
        if "amendment" in text:
            logger.info("policy-amendment accept degraded to one-shot allow")
        return True, "turn"
    word = str(decision or "").strip().lower()
    if word in _APPROVE_DENY_WORDS or "decline" in word or "deny" in word:
        return False, "turn"
    if word in _APPROVE_ALLOW_WORDS or scope == "session":
        # "always"/acceptForSession mean session scope even when the
        # caller left scope at its default (matches the client's Always allow).
        if word in _APPROVE_SESSION_WORDS or scope == "session" \
                or "session" in word:
            return True, "session"
        return True, "turn"
    return False, "turn"


class ApprovalManager:
    def __init__(self):
        # fid -> {"future", "thread", "tool", "label", "params", "deadline"}
        self.pending: dict[str, dict] = {}

    def request_approval(
        self, thread_id: str, params: dict, tool: str = "", label: str = ""
    ) -> asyncio.Future:
        self.sweep_expired()
        fid = params["id"]
        entry: dict = {
            "future": asyncio.Future(),
            "thread": thread_id,
            "tool": tool,
            "label": label,
            "params": dict(params),
            "deadline": time.monotonic() + APPROVAL_TTL_S,
        }
        self.pending[fid] = entry
        return entry["future"]

    def approve(self, fid: str, decision, scope: str = "turn",
                agent_id: str = "") -> bool:
        entry = self.pending.pop(fid, None)
        if entry is None:
            return False
        granted, grant_scope = _parse_approval_decision(decision, scope)
        entry["future"].set_result(granted)
        if granted and grant_scope == "session" and entry.get("tool"):
            try:
                policy = load_policy()
                # Per-agent grants: the check path (run_tool_loop) reads
                # the AGENT's section, so recording globally would write
                # grants nothing ever honors. Setup phase (no agent)
                # keeps the legacy global path.
                if agent_id:
                    what = record_agent_grant(
                        policy, agent_id, entry["thread"],
                        entry["tool"], entry.get("label", ""),
                    )
                else:
                    what = record_grant(
                        policy, entry["thread"],
                        entry["tool"], entry.get("label", ""),
                    )
                save_policy(policy)
                audit.append({
                    "type": "grant",
                    "thread_id": entry["thread"],
                    "tool": entry["tool"],
                    "granted": what,
                    "scope": "session",
                    "agent_id": agent_id,
                })
                logger.info(
                    f"Session grant recorded: {entry['thread']} -> {what!r}"
                )
            except Exception:
                logger.exception("failed to record session grant")
        return True

    def drop_thread(self, thread_id: str) -> int:
        """Cancel pending approvals for a thread (turn cancelled)."""
        doomed = [
            fid for fid, entry in self.pending.items()
            if entry.get("thread") == thread_id
        ]
        for fid in doomed:
            try:
                self.pending[fid]["future"].cancel()
            except Exception:
                pass
            del self.pending[fid]
        return len(doomed)

    def drop_all(self) -> int:
        """Deny only EXPIRED approvals (client gone past TTL).

        Fresh pending approvals survive disconnects: the reconnect hook
        re-sends them so the user can still answer. Blanket-denying here
        is what produced phantom "denied by the client" verdicts on every
        socket flap.
        """
        return self.sweep_expired()

    def sweep_expired(self) -> int:
        """Resolve past-deadline approvals as denied. Returns count."""
        now = time.monotonic()
        n = 0
        for fid, entry in list(self.pending.items()):
            try:
                if float(entry.get("deadline", now + 1)) > now:
                    continue
            except Exception:
                pass
            try:
                fut = entry.get("future")
                if fut is not None and not fut.done():
                    fut.set_result(False)
                    n += 1
            except Exception:
                pass
            self.pending.pop(fid, None)
        if n:
            logger.info(f"expired {n} unanswered approval(s) as denied")
        return n


approvals = ApprovalManager()

RUNNING: dict[str, asyncio.Task] = {}

# Step 4 (quotas): live turn slots per agent (agent_id -> turn ids).
# Counted at turn start, released when the turn task ends (cancel, crash
# and normal paths all funnel through _run_turn's finally). Stale ids
# are pruned on read against RUNNING so a missed release degrades to a
# recount, never a permanent refusal.
_agent_turns: dict[str, set[str]] = {}


def _live_agent_turns(agent_id: str) -> set[str]:
    """Turn ids this agent currently has running (pruned)."""
    live = _agent_turns.get(agent_id)
    if not live:
        return set()
    keep = {t for t in live if t in RUNNING and not RUNNING[t].done()}
    if keep != live:
        if keep:
            _agent_turns[agent_id] = keep
        else:
            _agent_turns.pop(agent_id, None)
    return keep


def _release_turn_slot(agent_id: str, turn_id: str) -> None:
    """Forget one turn slot. Never raises, never blocks."""
    try:
        slots = _agent_turns.get(agent_id)
        if slots is not None:
            slots.discard(turn_id)
            if not slots:
                _agent_turns.pop(agent_id, None)
    except Exception:
        pass


def _may_signal(me: str | None, turn_id: str = "",
                thread_id: str = "") -> bool:
    """Whether a caller may cancel a turn. Setup phase allows all; the
    owner may always cancel (admin, parallels pairing management).
    Otherwise the turn must be the caller's own slot, or belong to a
    thread the caller owns. Denials look exactly like unknown/finished
    turns: no oracle for live turn ids."""
    if not me:
        return True
    try:
        if agents_mod.is_owner(me):
            return True
    except Exception:
        pass
    if turn_id and turn_id in _agent_turns.get(me, set()):
        return True
    tid = thread_id
    if not tid and turn_id and ":" in turn_id:
        # Turn ids are {thread}:{n}; threads here never contain colons.
        tid = turn_id.rsplit(":", 1)[0]
    if tid:
        try:
            state = load_state(tid)
        except Exception:
            state = None
        if state is not None and state.get("agent", "") == me:
            return True
    return False

# Step 1 (multi-agent platform): socket -> agent identity. Sockets are
# keyed by id() (same discipline as transport's send locks); the reverse
# map lets revoke drop an agent's live connections. Both are forgotten
# on disconnect (see _forget_socket, registered in main).
_sock_agents: dict[int, str] = {}
_agent_socks: dict[str, set] = {}


def _remember_socket(client_socket, agent_id: str) -> None:
    if client_socket is None:
        return
    key = id(client_socket)
    old = _sock_agents.get(key)
    if old is not None and old != agent_id:
        try:
            _agent_socks.get(old, set()).discard(client_socket)
            transport_mod.unregister_agent_socket(old, client_socket)
        except Exception:
            pass
    _sock_agents[key] = agent_id
    _agent_socks.setdefault(agent_id, set()).add(client_socket)
    transport_mod.register_agent_socket(agent_id, client_socket)


def _drop_socket(client_socket) -> None:
    try:
        agent_id = _sock_agents.pop(id(client_socket), None)
        if agent_id is not None:
            try:
                _agent_socks.get(agent_id, set()).discard(client_socket)
                transport_mod.unregister_agent_socket(agent_id, client_socket)
            except Exception:
                pass
    except Exception:
        pass


def _caller_agent(client_socket) -> str | None:
    try:
        return _sock_agents.get(id(client_socket))
    except Exception:
        return None


def _cancel_runs_on_disconnect(client_socket) -> None:
    """Stop this client's in-flight turns when it drops.

    A client that disconnected mid-turn used to leave the turn running
    server-side: `on_disconnect` dropped the socket and the pending waits
    but never the RUNNING task. An orphaned turn then kept executing
    tools (and its late frames resurfaced on the *next* connection), which
    is the cross-session leakage behind activity rows landing on the wrong
    turn.

    Must run BEFORE `_drop_socket`, which pops the socket->agent map that
    `_caller_agent` reads.
    """
    agent_id = _caller_agent(client_socket)
    if not agent_id:
        return
    for turn_id in list(_live_agent_turns(agent_id)):
        task = RUNNING.get(turn_id)
        try:
            kill_turn(turn_id)
        except Exception:
            logger.exception("disconnect: kill_turn failed for %s", turn_id)
        if task is not None:
            task.cancel()
    logger.info("disconnect: cancelled in-flight turns for agent %s", agent_id)


def _agents_configured() -> bool:
    """True once at least one agent is paired (gate is live)."""
    try:
        return bool(agents_mod.load_agents())
    except Exception:
        return False


# Methods reachable without a paired agent: self-description,
# handshake, and the pairing plane itself (owner-gated inside).
_PUBLIC_METHODS = ("capabilities", "initialize",
                   "pairing/list", "pairing/approve",
                   "pairing/deny", "pairing/revoke",
                   "pairing/wait")


def _not_paired(request_id) -> dict:
    return {"id": request_id, "error": {
        "code": -32001, "message": "not paired: complete pairing first"}}

# Messages the user sent at a turn that is already running, keyed by turn id.
#
# This is what makes `turn/steer` mean what its name says. The old
# implementation killed the running turn and started a new one from the thread's
# last *recorded* state, so "add this to what you're doing" destroyed the work
# in progress and restarted from a stale point -- and because it then awaited
# that new turn before replying, the send button stayed disabled for the whole
# of it. A message here is picked up at the next round boundary instead: the
# current round finishes, and the agent continues with the new instruction in
# hand. Nothing is lost and nothing has to be re-run.
_pending_steer: dict[str, list[str]] = {}

# How a mid-turn message is presented to the model. Explicit about being a new
# instruction from the user, and explicit that it does not cancel the work in
# flight -- otherwise a model reads "also check X" as "stop and do X instead"
# and abandons the task it was halfway through.
_steer_prompt = (
    "\n\n[user, sent while you were working] The user added this to your "
    "current task. Continue the work in progress and take this into account; "
    "do not discard what you have already done:\n"
)

# Epic P: subagents — background turns on child threads for parallel
# fan-out. Registry is in-memory (results persist in thread history);
# a daemon restart ends running subagents (documented, never silent:
# completed work is already in history + audit).
_subagents: dict[str, dict] = {}
_sub_counters: dict[str, int] = {}
MAX_SUBAGENTS = 8
SUBAGENT_DEFAULT_ROUNDS = 3

# Runaway backstop for the agentic loop, in rounds.
#
# NOT a task budget. The loop normally ends when the model stops asking for
# tools, which is the only thing that means the work is done. This number only
# bounds a model that never stops -- a loop that cannot be trusted to terminate
# still has to be stoppable, and a runaway turn bills real tokens on every
# pass.
#
# Deliberately enormous, and paired with a wall-clock limit (TURN_TIME_LIMIT_S)
# as the limit that actually matters. A round-count cap is the wrong unit for
# real work: the old cap of 8 cut a genuine task off mid-flight, and even 400
# is only ~45 minutes at a typical 7s round -- so a 30-minute build or training
# run could still be severed by a number that has nothing to do with how long
# the work actually takes. Time is the honest unit; rounds exist only to stop a
# pathological model that returns instantly and would otherwise never reach it.
#
# When either limit IS hit the turn is reported as truncated, never finished.
ROUND_BACKSTOP = 100_000

# Wall-clock ceiling on one turn, in seconds. ~6 hours: long enough that no real
# task hits it, short enough that a stuck loop is not billed indefinitely. The
# user can always stop a turn from the UI; this only catches what nobody is
# watching.
TURN_TIME_LIMIT_S = 6 * 60 * 60

# How long an approval card may wait for its human. Ten minutes covers a
# user who put the phone down mid-turn; past that the call is denied so a
# client that will never come back cannot hold a turn (and its billing)
# open forever. Reconnects re-send open cards (see _resend_approvals), so
# in practice this only fires for the truly gone.
APPROVAL_TTL_S = 10 * 60


def _child_thread(parent: str) -> str:
    n = _sub_counters.get(parent, 0) + 1
    while state_path(f"{parent}-sub-{n}").exists():
        n += 1
    _sub_counters[parent] = n
    return f"{parent}-sub-{n}"


async def _run_subagent(entry: dict, payload: dict) -> None:
    """Background turn driver: records outcome, announces completion."""
    tid = entry["thread_id"]
    try:
        res = await handle_turn(payload, None)
        result = res.get("result", {}) if isinstance(res, dict) else {}
        # handle_turn only ACKs: it starts the turn as a background task and
        # answers {"turn_id", "started": True} immediately (Codex parity --
        # see the comment on its return). Treating that ack as completion
        # made every subagent report "done" instantly having done nothing,
        # made subagent/cancel unreachable (status was no longer "running"),
        # and orphaned the real turn until it ended on its own.
        # Follow the actual turn task; handle_turn itself is unchanged.
        turn_id = result.get("turn_id")
        real = RUNNING.get(turn_id) if turn_id else None
        if real is not None:
            try:
                await real
            except asyncio.CancelledError:
                # Cancelled through us: stop the turn too, or cancelling
                # this driver leaves the real turn running and broadcasting.
                if not real.done():
                    real.cancel()
                raise
        if result.get("cancelled") or (real is not None and real.cancelled()):
            entry["status"] = "cancelled"
        else:
            entry["status"] = "done"
        entry["result"] = result
    except asyncio.CancelledError:
        entry["status"] = "cancelled"
        raise
    except Exception as e:
        logger.exception(f"subagent {tid} failed")
        entry["status"] = "error"
        entry["result"] = {"error": str(e)[:500]}
    finally:
        entry["task"] = None
        try:
            audit.append({"type": "subagent", "action": "completed",
                          "thread_id": tid, "parent": entry.get("parent"),
                          "status": entry["status"]})
        except Exception:
            pass
        try:
            await broadcast({"type": "subagent/completed", "id": tid,
                             "thread_id": tid, "status": entry["status"]})
        except Exception:
            logger.exception(f"subagent {tid} completion broadcast failed")

# Epic I.3 (M5): client-registered tools. The model can call out to a tool
# the CLIENT implements (e.g. a provider probe): the daemon sends
# tool/call to the owning socket and awaits its answer. Ask-first approval
# (EXTRA_WRITE), audit like everything else. Registrations live until
# unregister/restart, or until the owner proves dead on first use.
_client_tools: dict[str, dict] = {}
_tool_call_pending: dict[int, asyncio.Future] = {}
_tool_call_seq = 0
CLIENT_TOOL_TIMEOUT = 30.0

# A single round may not run forever. The socket timeout in
# providers/streaming.py is an *idle* timeout -- every byte received resets it
# -- so a model that keeps producing tokens trips nothing, the round never
# returns, and the only remaining bound is the 6-hour turn clock. See the
# deadline in the round loop.
ROUND_TIME_LIMIT_S = 8 * 60
_CLIENT_TOOL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# Compat-client wire name so existing dispatchers fire unchanged.
CLIENT_TOOL_CALL_METHOD = "item/tool/call"


async def _resend_approvals(ws) -> None:
    """Reconnect hook: re-send every still-open approval card.

    A socket flap mid-turn used to orphan the card — the turn kept
    waiting, the fresh UI never heard about it, and the disconnect hook
    eventually denied it behind the user's back. Re-sending on connect
    closes that hole: the card reappears, answerable, on the live UI.
    """
    try:
        expired = approvals.sweep_expired()
        if expired:
            logger.info(f"swept {expired} expired on reconnect")
    except Exception:
        logger.exception("reconnect sweep failed")
    for fid, entry in list(approvals.pending.items()):
        try:
            params = entry.get("params") or {}
            if not params:
                continue
            await send_to(ws, {
                "type": "approval_request",
                "id": fid,
                "params": dict(params),
            })
            logger.info(f"re-sent approval {fid} to reconnected client")
        except Exception:
            logger.exception(f"resend of {fid} failed")


def drop_client_waits(_ws=None) -> None:
    """Disconnect hook: no orphaned wait may outlive its client.

    Approvals past their TTL are denied; fresh ones stay open for the
    reconnect resend below. Client-tool calls fail fast so the turn
    records the error instead of hanging on an answer that can never
    arrive.
    """
    try:
        approvals.drop_all()
    except Exception:
        logger.exception("drop_all approvals failed")
    for fid, fut in list(_tool_call_pending.items()):
        try:
            if fut is not None and not fut.done():
                fut.set_exception(RuntimeError("client disconnected"))
        except Exception:
            pass
        _tool_call_pending.pop(fid, None)


def _normalize_client_result(res, name: str) -> dict:
    """Client tool answer -> daemon tool result.

    Understands Codex DynamicToolCallResponse ({contentItems, success})
    and passes any other dict shape through untouched.
    """
    if isinstance(res, dict) and isinstance(res.get("contentItems"), list) \
            and isinstance(res.get("success"), bool):
        texts = []
        for item in res["contentItems"]:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        text = "\n".join(texts) or "(empty tool result)"
        if res["success"]:
            return {"status": "success", "tool": name,
                    "output": text[:20000]}
        return {"status": "error", "tool": name,
                "message": text[:4000]}
    if isinstance(res, dict):
        return res
    return {"status": "success", "output": res}


async def _call_client_tool(name: str, arguments: dict, thread_id: str = "",
                         turn_id: str = "", call_id: str = "") -> dict:
    """Execute a client tool via its owner socket. Loud on every failure.

    The request frame carries the full compat-client DynamicToolCallParams
    shape (tool, arguments, callId, threadId, turnId) so client
    dispatchers fire unchanged.
    """
    global _tool_call_seq
    entry = _client_tools.get(name)
    if entry is None:
        TOOLS.pop(name, None)
        EXTRA_WRITE.discard(name)
        return {"status": "error",
                "message": f"client tool unregistered: {name}"}
    _tool_call_seq += 1
    fid = _tool_call_seq
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _tool_call_pending[fid] = fut
    # Look up the owner socket from the agent's live sockets.
    agent_id = entry.get("agent", "")
    owner_sock = None
    if agent_id:
        socks = _agent_socks.get(agent_id, set())
        if socks:
            owner_sock = next(iter(socks))
    if owner_sock is None:
        _client_tools.pop(name, None)
        TOOLS.pop(name, None)
        EXTRA_WRITE.discard(name)
        return {"status": "error",
                "message": f"client tool {name} unreachable: no live socket"}
    try:
        await send_to(owner_sock, {
            "id": fid, "method": CLIENT_TOOL_CALL_METHOD,
            "params": {"tool": name, "arguments": arguments or {},
                       "callId": call_id or str(fid),
                       "threadId": thread_id, "turnId": turn_id},
        })
    except Exception as e:
        _client_tools.pop(name, None)
        TOOLS.pop(name, None)
        EXTRA_WRITE.discard(name)
        _tool_call_pending.pop(fid, None)
        logger.error(f"client tool {name} unreachable ({e}); unregistered")
        return {"status": "error",
                "message": f"client tool {name} unreachable: {e}"[:500]}
    try:
        res = await asyncio.wait_for(fut, timeout=CLIENT_TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        _client_tools.pop(name, None)
        TOOLS.pop(name, None)
        EXTRA_WRITE.discard(name)
        logger.error(f"client tool {name} timed out; unregistered")
        return {"status": "error",
                "message": f"client tool {name} timed out (unregistered)"}
    except Exception as e:
        # The owner answered with an error: a live tool reporting failure.
        return {"status": "error", "message": str(e)[:500]}
    finally:
        _tool_call_pending.pop(fid, None)
    return _normalize_client_result(res, name)


def _scrub_fold_echo(text: str) -> str:
    """Drop a replayed fold block out of a round's model text.

    The loop folds each round's text plus its tool results into the next
    round's user-role message, wrapped in <assistant_work>. A model asked to
    answer from that context sometimes quotes the block back verbatim, and the
    quote reads as fresh output -- the bracket labels and the [tool] result
    lines are then broadcast to the client as the turn's answer.

    Matching is deliberately narrow: a whole fenced block, or a run of lines
    that are only fold labels and [tool]/[error] result lines. Prose that
    merely mentions a tool name, or a shell command the model is reasoning
    about in its own words, is left alone -- a real answer that happens to
    contain the word "shell" must survive.
    """
    if not text:
        return text
    import re

    # Whole block, if the model reproduced the fence.
    stripped = re.sub(
        r"<assistant_work\b.*?</assistant_work>", "", text,
        flags=re.DOTALL,
    ).strip()
    # Unclosed opener: drop it and everything after, same as the dangling
    # ```json case -- a half-quoted fold is not a usable answer.
    if stripped != text:
        stripped = re.sub(r"<assistant_work\b.*$", "", stripped,
                          flags=re.DOTALL).strip()
        return stripped
    if "<assistant_work" in text:
        return re.sub(r"<assistant_work\b.*$", "", text,
                      flags=re.DOTALL).strip()

    # Unfenced echo: the [assistant round N]: header plus the [tool] lines
    # fold_output emits. A line qualifies only if it is one of those labels
    # at the start of a line, so a sentence mentioning a tool is not eaten.
    # Closer lines ([/tool]) are fold syntax too: a model quoting a marked
    # block back would otherwise leave orphan closers in chat.
    label = re.compile(
        r"^\[assistant round \d+\]:", re.MULTILINE)
    result_line = re.compile(
        r"^\[(?:shell|fs_list|fs_read|fs_write|git|websearch|exec|"
        r"pty_run|pkg|image|plugins|tabs|track|review|mcp|error|[a-z_]+)"
        r"\][ \t]", re.MULTILINE)
    closer_line = re.compile(
        r"^\[/[a-z_][a-z0-9_.-]*\][ \t]?$", re.MULTILINE)
    # A marker that OPENS a line but has prose after it. The model glues the
    # fold's closer onto its own sentence — the live case was
    # `[/shell]The current user is **root**.` — and `closer_line` above only
    # matches a marker alone on its line, so it sailed straight through and the
    # marker was broadcast as part of the answer. Strip the marker, keep the
    # sentence: the prose after it is the model's own words.
    leading_marker = re.compile(
        r"^\[/?[a-z_][a-z0-9_.-]*[ \t]*\][ \t]*", re.MULTILINE)

    out: list[str] = []
    for line in text.splitlines():
        if label.match(line) or result_line.match(line):
            continue
        if closer_line.match(line):
            continue
        line = leading_marker.sub("", line)
        out.append(line)
    cleaned = "\n".join(out).strip()
    # Only accept the stripped form when it actually removed fold syntax,
    # so ordinary answers are never rewritten by this.
    return cleaned if len(cleaned) < len(text.strip()) else text


def _strip_asterisk_dividers(text: str) -> str:
    """Drop standalone asterisk-divider lines models emit (*****).

    Only outside fenced blocks: a divider inside a code sample is
    content. Inline asterisks (bold/italic) are never bare lines, so
    real markdown is untouched; dashes/underscores are left alone
    (--- doubles as front-matter and headings).
    """
    import re as _re
    fence = _re.compile(r"^\s*```")
    divider = _re.compile(r"^\*{3,}\s*$")
    out = []
    in_fence = False
    for line in text.split("\n"):
        if fence.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and divider.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def _extract_tool_calls(text: str) -> tuple[list, str, bool, bool]:
    """(calls, cleaned_text, found_syntax, broke_syntax) for one round.

    The parsing itself lives in `agent/calls/` -- one module per call
    language, discovered by asking, exactly as `agent/providers/dialects.py`
    finds wire formats. A model that writes a shape this engine has never met
    is therefore a missing module, not a broken turn, and adding one is
    dropping a file in beside the others.

    See `agent/calls/__init__.py` for the contract and the ordering rules.
    This stays a named function rather than an inline call because the four
    return values each carry a meaning the round loop depends on, and one
    name for them reads better than a bare four-tuple.
    """
    return calls_mod.extract(text)


def _tool_output_status(output: dict) -> str:
    """Outcome status of one recorded tool entry (for items arrays)."""
    if "result" in output and isinstance(output["result"], dict):
        return str(output["result"].get("status", "success"))
    return str(output.get("status", "error"))


def _turn_notice(turn: dict, tools_run: list) -> dict | None:
    """The turn's own verdict on why no tool ran, or None when one did.

    Authored here rather than inferred by the client, because this process is
    the only party that knows the difference between a model that asked for
    nothing and a model that asked for something we could not read: the parser
    knows (`_unreadable_calls`), and the turn looks identical from the other end
    of the socket either way. A client guessing from its own screen cannot tell
    those apart, so it reports a parse failure as a model that ignores the tool
    format -- two different faults with two different fixes.

    Stated on every turn where it is true, and never throttled here. A fact
    withheld is a fact the next client has to re-derive, which is how the
    guessing started; how often to show it is the client's business.
    """
    # An early stop outranks every other reason. The turn ended because a
    # guard fired, not because of anything about the tools: a turn that ran
    # two calls and then tripped the repeat breaker DID run tools, so the
    # `tools_run` test below would swallow the one fact worth reporting.
    stop = turn.get("_stop_notice")
    if stop:
        return stop
    if tools_run:
        return None
    if turn.get("_unreadable_calls"):
        return {
            "code": "unreadable-call-syntax",
            "text": (
                "No tool ran: the model asked for one in a format the engine "
                "could not read. Another model in Settings is the fix."
            ),
        }
    # A turn that ran no tool and asked for nothing is not a fault, and this
    # used to report one anyway: any turn that produced text without a tool
    # call was shown
    #
    #     "No tool was used in this turn. If you asked for something that needs
    #      one, this model may not follow the engine's tool format -- another
    #      model in Settings is the fix."
    #
    # The engine cannot support that sentence. It knows the parse produced no
    # call; it does not know whether the user asked for work. So a plain
    # "hello" -- which needs no tool and got a perfectly good answer -- was
    # warned about, once per thread, on a card that reads as the model being
    # broken. The fault that is real, a call the parser could not read, is the
    # branch above and keeps its own notice. A model that chose to answer is
    # not evidence of anything, and crying wolf on it is how the warning that
    # does matter stops being read.
    return None


def _note_tool_outcome(turn: dict, name: str, ok: bool) -> None:
    """Record one tool call's outcome on the turn (Finding 29 #4).

    `_failed_tools` is the turn's list of *unresolved* failures: a call that
    did not do what was asked is added, and a later call of the same tool that
    succeeds removes it.

    The removal is the point. Without it, a turn that hit a transient error and
    then recovered would still be told it was unfinished, and an alarm that
    fires on finished work is one the model learns to ignore -- which would
    leave the real case exactly as broken as before. Retrying the thing that
    broke and succeeding is the recovery this whole change is meant to produce,
    so it has to be able to clear.
    """
    if not isinstance(name, str) or not name:
        return
    failed = turn.setdefault("_failed_tools", [])
    if ok:
        if name in failed:
            failed.remove(name)
        return
    if name not in failed:
        failed.append(name)


def _failed_tool_names(turn: dict) -> list[str]:
    """The tools that still have not done what was asked this turn, in order.

    Reads the fact the round loop records (`_note_tool_outcome`) -- the same
    one `tool_failed` feeds from the fold -- so the engine's readers cannot
    disagree about which calls are outstanding.
    """
    return list(turn.get("_failed_tools") or [])


def _goal_state(turn: dict) -> str:
    """Whether a turn that hit a failure is allowed to close right now.

    One of:

      ""           -- nothing to do: no failure this turn, or already handled.
      "re-ask"     -- the model is trying to close over a failure. Give it
                      exactly one round to work out why and take a different
                      approach.
      "unfinished" -- it closed anyway. The engine states, in the answer
                      itself, that the task did not finish.

    Finding 29 #4. The loop used to hold no opinion here: a failure and a
    success both ended at the same place -- "the model stopped asking for
    tools" -- so a turn whose only command failed could close on a cheerful
    summary and be indistinguishable, on the wire, from a finished job. The
    engine is the only party that saw the result; it now says so, once to the
    model and then to the user.
    """
    if not turn.get("_failed_tools"):
        return ""
    if not turn.get("_goal_checked"):
        return "re-ask"
    if not turn.get("_unfinished_stated"):
        return "unfinished"
    return ""


def _unfinished_statement(turn: dict) -> str:
    """The engine's plain statement that a failed turn did not finish.

    Appended to the answer's own text, so it cannot be separated from the
    summary it qualifies and a client needs no new field to render it.
    """
    names = _failed_tool_names(turn)
    what = ", ".join(names) if names else "the last action"
    return (
        f"[engine] This turn did not finish: {what} failed and no successful "
        "alternative was run. Treat the work above as incomplete."
    )


# Finding 29 #1/#4: what a failure means to the model. The old wording said
# "say so plainly and stop" -- which told her to quit at the first refusal and
# left a summary as the only possible ending. A failed action is a fact to work
# around, not a reason to hand the job back.
_ROUND_DISCIPLINE = (
    "\n\n[system: end every round either by calling the tool you need, or by "
    "answering. Never end a round by announcing work you have not done -- "
    "saying you are about to scan, check or look is not an answer, and the "
    "turn ends the moment you stop asking for a tool. If a step is still "
    "needed, call it in this round. If the task is finished, say plainly what "
    "you found.]"
)


_ANSWER_OR_CONTINUE = (
    "\n\n[system: you have stopped asking for tools on a turn where you have "
    "already run some. Two things are possible and they are different. If the "
    "task is NOT finished, call the tool you need and continue -- do not merely "
    "describe it. If it IS finished, write your final answer to the user: state "
    "plainly what you found or what you did. A plan is not an answer, and work "
    "you have not done does not count.]"
)


_GOAL_CHECK_PROMPT = (
    "\n\n[system: this turn is NOT finished. At least one tool you called did "
    "not do what was asked -- its result is marked FAILED above. Do not "
    "summarise and do not report the task as done. Work out why it failed "
    "from the result you were given, then take a different approach: correct "
    "the arguments, use a different tool, or try another route. If nothing "
    "you can reach will work, say exactly what is blocking and what you "
    "already tried.]"
)


def _usage_total(usage) -> int:
    """Token count out of a usage block (total, else prompt+completion)."""
    if not isinstance(usage, dict):
        return 0
    try:
        total = int(usage.get("total_tokens", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    if total > 0:
        return total
    try:
        return int(usage.get("prompt_tokens", 0) or 0) + int(
            usage.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _accrue_turn_usage(agent_id: str, turn: dict) -> None:
    """Add a finished turn's tokens to the agent's cumulative spend.

    Best-effort and silent: accounting must never fail a turn, and a
    turn with no usage block accrues nothing.
    """
    if not agent_id:
        return
    spent = _usage_total(turn.get("usage") if isinstance(turn, dict) else None)
    if spent <= 0:
        return
    try:
        policy = load_policy()
        total = add_agent_usage(policy, agent_id, spent)
        save_policy(policy)
        logger.info(f"usage: {agent_id} +{spent} = {total}")
    except Exception:
        logger.exception("usage accrual failed")


def _agent_capabilities(agent_id: str) -> dict:
    """Which tools and skills this agent should be *told about*.

    The engine is shared. Its tool and skill catalogue is the union of
    everything any client on the device might need — shell, exec, pty,
    git, image generation, pentest, OSINT. Advertising all of it to every
    client is not neutral: it is prompt cost on every single message, and
    it is a standing invitation for a writing agent to reach for a shell.

    An agent opts in by declaring an allowlist under its own policy
    section:

        "agents": {
          "ag_…": {
            "capabilities": {
              "tools":  ["fs_read", "fs_write", "fs_edit", ...],
              "skills": ["review"]
            }
          }
        }

    **No section means no filtering** — every agent that has not asked to
    be narrowed (Kara, the owner, anything already on the device) keeps
    exactly the catalogue it has today. That is deliberate: this must not
    change behaviour for a client that did not opt in.

    `"all"` and `"*"` are accepted as explicit wide values, so a client
    can state "everything" rather than rely on absence.
    """
    empty = {"tools": None, "skills": None}
    if not agent_id:
        return empty
    try:
        policy = load_policy()
    except Exception:
        return empty
    agents = policy.get("agents")
    if not isinstance(agents, dict):
        return empty
    section = agents.get(agent_id)
    if not isinstance(section, dict):
        return empty
    caps = section.get("capabilities")
    if not isinstance(caps, dict):
        return empty

    def clean(key: str):
        """A declared list -> a set. Absent key -> None (= no opinion).

        These two must stay distinct, and conflating them is the bug this
        docstring exists to prevent: `"skills": []` means *this agent gets
        no skills*, while omitting `skills` entirely means *this agent did
        not opt in, do not filter*. Returning None for an empty list
        silently turned a strict "nothing" into "everything".
        """
        if key not in caps:
            return None
        raw = caps.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return None
        names = {str(x) for x in raw if str(x).strip()}
        if "all" in names or "*" in names:
            return None
        return names

    return {"tools": clean("tools"), "skills": clean("skills")}


def agency_messages(agent_id: str = "") -> list[dict]:
    """System prompt that makes the model agentic: tool catalog + calling
    convention. Without this the model never emits tool calls (it was never
    told the tools exist), which is exactly how the first client lost its
    agency on the iagent move — the old engine shipped this prompt
    engine-side, iagent did not.

    The prompt itself now rides as a `system` role and is carried to the
    wire by `providers/media.py:flatten_messages()`, which concatenates
    every role into the single user message these providers send. It is no
    longer duplicated inside the user content (see `_execute_turn`).
    """
    return [{"role": "system", "content": agency_text(agent_id)}]


def agency_text(agent_id: str = "") -> str:
    from .tools import WRITE_TOOLS
    caps = _agent_capabilities(agent_id)
    allow = caps["tools"]
    lines = [
        "You are an AGENT with tools. To act, emit a fenced json block:",
        '```json [{"tool": "<name>", "parameters": {...}}] ```',
        "Rules:",
        "- Call a tool ONLY when the request needs one. A greeting, thanks, a "
        "question about who you are, or ordinary conversation needs NO tool: "
        "answer directly, in words. Running something merely because you can "
        "is a mistake.",
        "- The other half of that rule, and the more dangerous one: if the "
        "user asks you to DO something — create, write, edit, run, check, "
        "read, delete — then you have NOT done it until a tool has, and you "
        "must not say it is done. Never report a file as created, a command as "
        "run, or a task as finished on the strength of what you intend to do. "
        "If you have not called the tool, call it; if it failed, say it "
        "failed.",
        "- A call that already told you nothing will not tell you more. If you "
        "find yourself about to make a call you have already made, stop and "
        "answer with what you have instead of calling it again.",
        "- You may call several tools in one block; they run in order.",
        "- Tool results return to you next round — use them, then answer.",
        "- NEVER claim a tool ran unless you received its tool_result.",
        "- If a tool_result is marked FAILED, or carries a non-zero exit, the "
        "action did NOT happen. Never describe a failed tool as done and "
        "never invent the output it would have made. Work out why from the "
        "result you were given, then take a different approach -- correct the "
        "arguments, use a different tool, or try another route. Only stop "
        "once you have succeeded or exhausted what you can reach; if you "
        "stop, say what failed and what you tried.",
        "- For live/external facts use web_search; your weights may be stale.",
        "- Read a file before editing it; prefer exact-match fs_edit.",
        "- Write tools (marked APPROVAL) pause for the user's approval — "
        "propose them, do not work around the pause.",
        "- Paths are on THIS phone: home is "
        f"{engine_home()}, "
        "shared storage is /storage/emulated/0 "
        "(Downloads: /storage/emulated/0/Download).",
        "- Never use /data/data/... paths (a different app layout -- they "
        "do not exist here); ~ means the home above.",
        "Your tools:",
    ]
    listed = 0
    for name in sorted(TOOLS):
        # Per-agent narrowing. `allow is None` means the agent did not opt
        # in, so it sees everything -- see _agent_capabilities().
        if allow is not None and name not in allow and name not in _client_tools:
            continue
        if name in _client_tools:
            spec = _client_tools[name]
            desc = str(spec.get("description", ""))
            schema = spec.get("inputSchema") or {}
            props = schema.get("properties") if isinstance(schema, dict) else None
            params = ", ".join(sorted(props)) if isinstance(props, dict) else "..."
            lines.append(f"- {name}({params}) [app tool, APPROVAL]: {desc[:160]}")
            listed += 1
            continue
        fn = TOOLS[name]
        try:
            sig = inspect.signature(fn)
            params = ", ".join(
                p for p in sig.parameters if p not in ("self", "cls")
            )
        except (TypeError, ValueError):
            params = "..."
        doc = (inspect.getdoc(fn) or "").splitlines()
        brief = doc[0] if doc else ""
        tag = " [APPROVAL]" if name in WRITE_TOOLS else ""
        lines.append(f"- {name}({params}){tag}: {brief[:160]}")
        listed += 1
    try:
        mcp_names = mcp_mod.describe().get("registered_tools", []) or []
    except Exception:
        mcp_names = []
    if mcp_names and allow is None:
        lines.append("MCP tools: " + ", ".join(sorted(str(t) for t in mcp_names)))
        listed += len(mcp_names)
    # An agent narrowed to no tools must not be told it has a toolset. The
    # rules above only make sense to a model that can call something, and
    # leaving them in invites it to hallucinate tool blocks.
    if listed == 0:
        return (
            "You have no tools on this device. Answer from what you know "
            "and from the conversation. If a task needs a tool you do not "
            "have, say so plainly instead of pretending."
        )
    # The skill catalogue is NOT appended here.
    #
    # It used to be, for the same single-message reason as the preamble, and it
    # was correct then. `flatten_messages()` now carries `system` roles, and
    # `skill_messages()` already emits the identical `catalog_message()` on
    # every turn — so appending it here sent the same ~980 characters twice,
    # from two different files. Removing this copy costs the model nothing: the
    # catalogue it now receives is byte-identical and still arrives every turn.
    lines.append(
        "Answer in the user's language, concisely. Plain text plus tool "
        "blocks only. Never echo a tool block back: after tools run, "
        "answer with the RESULT in your own words."
    )
    return "\n".join(lines)


def preamble_text(state: dict) -> str:
    """Identity + agency prompt as plain text, prepended to the user's
    message. Providers here are single-message (system roles never reach
    the wire), so without this the model would never see who it is or
    what tools it has.
    """
    developer = (state.get("developer") or "").strip()
    return f"{developer or DEFAULT_IDENTITY}\n\n{agency_text()}"


def _result_output_text(result) -> str:
    """A tool's result as the text a person would want to read.

    Tools return a dict with their own shape -- shell returns
    stdout/stderr/exit_code, others return a status and one payload field --
    so this pulls out whatever is worth showing rather than dumping JSON, which
    is what put `[fs_read]: {"ok": true}` into the transcript before.
    """
    if not isinstance(result, dict):
        return str(result) if result is not None else ""
    parts: list[str] = []
    for key in ("stdout", "output", "text", "content", "diff", "summary",
                "message", "data"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.rstrip())
    # stderr is where a failed command explains itself, so it belongs on
    # screen too -- and it was previously omitted from this list entirely,
    # which is why a failed call showed a stopped card with no text at all
    # and the reason never reached the person reading it. Labelled,
    # because an unlabelled mix with stdout is ambiguous.
    err = result.get("stderr")
    if isinstance(err, str) and err.strip():
        parts.append("[stderr] " + err.rstrip())
    if not parts:
        return ""
    out = "\n".join(parts)
    code = result.get("exit_code")
    if isinstance(code, int) and code != 0:
        out += f"\n[exit {code}]"
    return out


def _drain_steer(turn_id: str) -> str:
    """Take the messages queued for this turn and return them as one block.

    Returns "" when there were none. Called at the round boundary. Draining
    (rather than peeking) is deliberate: a message is consumed exactly once, so
    a fold can never apply the same instruction twice.
    """
    queued = _pending_steer.get(turn_id)
    if not queued:
        return ""
    _pending_steer.pop(turn_id, None)
    text = "\n".join(queued)
    audit.append({
        "type": "steer_applied",
        "turn_id": turn_id,
        "messages": [m[:200] for m in queued],
    })
    logger.info(f"steer applied to {turn_id}: {text[:200]}")
    return text


async def run_tool_loop(turn: dict, client_socket) -> None:
    """Execute tool calls with approval flow."""
    tool_calls = turn.get("tool_calls", [])
    logger.info(f"Processing {len(tool_calls)} tool call(s)")

    sandbox_mode = turn.get("sandbox", "full") or "full"
    turn_mode = turn.get("mode", "exec") or "exec"
    thread_id = turn.get("thread_id", "default")
    # Standing grants live on disk and survive turns; load once per loop.
    # Per-agent: resolve agent overrides over global defaults.
    _agent = _caller_agent(client_socket)
    policy = agent_policy(load_policy(), _agent or "")

    async def complete_item(item_id: str, tool_name: str, status: str) -> None:
        # Epic I.2: item-granular timeline event for every tool outcome.
        await broadcast({
            "type": "item/completed",
            "item_id": item_id,
            "turn_id": turn.get("id"),
            "thread_id": thread_id,
            "tool": tool_name,
            "status": status,
        })

    item_idx = turn.get("_item_next", 0)
    for call in tool_calls:
        tool_name = call.get("tool", "shell")
        params = call.get("parameters", {})
        # Params stay at debug: tool arguments can carry secrets
        # (keys pasted into commands), and info-level logging would
        # persist them into daemon.log on every call.
        logger.info(f"Tool: {tool_name}")
        logger.debug(f"Tool params: {params}")
        item_id = f"{turn.get('id')}:item:{item_idx}"
        item_idx += 1
        turn["_item_next"] = item_idx
        await broadcast({
            "type": "item/started",
            "item_id": item_id,
            "turn_id": turn.get("id"),
            "thread_id": thread_id,
            "tool": tool_name,
            "label": approval_label(tool_name, params),
        })

        if needs_approval(tool_name, params):
            # Epic G: plan mode blocks writes explicitly, like read-only
            # sandbox but as a declared intent ("look, don't touch").
            reason = ""
            if sandbox_mode == "read-only":
                reason = "sandbox is read-only"
            elif turn_mode == "plan":
                reason = "turn is plan mode (no writes)"
            if reason:
                logger.info(f"Blocked ({reason}): {tool_name}")
                blocked = {
                    "tool": tool_name,
                    "status": "error",
                    "message": f"write tool {tool_name!r} blocked: {reason}",
                }
                turn["output"].append(blocked)
                await broadcast({"type": "tool_result", **blocked})
                await complete_item(item_id, tool_name, "blocked")
                continue

            label = approval_label(tool_name, params)
            if allows(policy, thread_id, tool_name, label):
                logger.info(
                    f"Standing session grant covers {tool_name} ({label!r}) "
                    f"— skipping approval"
                )
            else:
                fid = f"{thread_id}:{len(approvals.pending)}"
                approval_params = {"command": label, "id": fid,
                                   "tool": tool_name,
                                   "thread_id": thread_id,
                                   "turn_id": str(turn.get("id", ""))}
                if isinstance(params, dict) and params.get("path"):
                    approval_params["path"] = str(params["path"])[:500]

                approval_future = approvals.request_approval(
                    thread_id, approval_params, tool=tool_name, label=label
                )
                logger.info(f"Requesting approval: {fid}")
                await broadcast({
                    "type": "approval_request",
                    "id": fid,
                    "params": approval_params,
                })

                logger.info("Waiting for client decision...")
                granted = await approval_future
                if not granted:
                    logger.info(f"Denied by client: {fid} — skipping")
                    denied = {"tool": tool_name, "status": "denied", "message": "denied by client"}
                    turn["output"].append(denied)
                    await broadcast({"type": "tool_result", **denied})
                    await complete_item(item_id, tool_name, "denied")
                    continue
                logger.info("Approval received!")

        logger.info(f"Executing tool: {tool_name}")
        if tool_name in TOOLS:
            try:
                if tool_name in _client_tools:
                    result = await _call_client_tool(
                        tool_name, params, thread_id=thread_id,
                        turn_id=str(turn.get("id", "")), call_id=item_id)
                else:
                    # Reconcile what the model *said* against what the tool
                    # *accepts* before the call, not after it fails. A
                    # mistyped or misnamed argument used to reach the tool
                    # and die somewhere inside it, where a broad except
                    # reduced it to a one-liner nobody could act on -- and
                    # the model, asked to summarise the call, filled the
                    # gap with invented output. Reconciling here means the
                    # tool either runs correctly or reports a message that
                    # names the parameters it actually wanted.
                    call_params, notes = toolargs_mod.reconcile(
                        tool_name, TOOLS[tool_name], params)
                    if notes:
                        logger.info(
                            f"{tool_name} args reconciled: {'; '.join(notes)}")
                    result = await TOOLS[tool_name](**call_params)
                # Results stay at debug: tool output can carry file
                # contents and pasted secrets; info would persist them.
                logger.debug(f"Tool result: {result}")
                tool_output = {
                    "tool": tool_name,
                    # The command, not just the tool. This entry is what the
                    # completion's `items` array is built from, and the client
                    # draws the row from it — so a name-only entry overwrote the
                    # labelled row `item/started` had already put on screen and
                    # "Running date" degraded to "Running shell".
                    "label": approval_label(tool_name, params),
                    "result": result,
                }
                turn["output"].append(tool_output)
                # Broadcast the output as a delta so the activity card can fill
                # in as the command runs.
                #
                # Without this the card knew only *that* a tool was called and
                # never *what it printed*: `item/started` carries the label and
                # `item/completed` carries only a status, so the row stayed
                # empty for the whole call and the command's output appeared
                # nowhere on screen. The tools capture stdout/stderr already
                # (see tools/shell.py); this is what puts it on the wire.
                #
                # Sent on completion rather than streamed live — these tools
                # return one dict when the process exits, so there is nothing
                # to stream. What this fixes is the empty card, and it arrives
                # the moment the command finishes instead of never.
                _out = _result_output_text(result)
                if _out:
                    await broadcast({
                        "type": "item/output",
                        "item_id": item_id,
                        "turn_id": turn.get("id"),
                        "thread_id": thread_id,
                        "output": _out,
                    })
                await broadcast({"type": "tool_result", **tool_output})
                status = result.get("status", "success") if isinstance(
                    result, dict) else "success"
                if isinstance(result, dict) and tool_failed(result):
                    # The one fact the loop must not lose. The model is told to
                    # take a different approach (see agency_text), and the turn
                    # must not be allowed to close as a finished job afterwards
                    # -- see the goal check in the round loop. Recorded here
                    # because this is the only place a result's outcome is
                    # known, and through `tool_failed` so it cannot disagree
                    # with the wording the fold hands the model.
                    _note_tool_outcome(turn, tool_name, ok=False)
                else:
                    # A later call of the same tool that works is the retry
                    # landing, and it clears the failure -- see
                    # _note_tool_outcome for why that half is not optional.
                    _note_tool_outcome(turn, tool_name, ok=True)
                await complete_item(item_id, tool_name, str(status))
                if (tool_name in ("fs_write", "fs_edit", "image_generate")
                        and isinstance(result, dict)
                        and result.get("status") == "success"
                        and result.get("path")):
                    await broadcast({
                        "type": "fs/changed",
                        "op": tool_name,
                        "path": str(result["path"]),
                        "turn_id": turn.get("id"),
                        "thread_id": thread_id,
                    })
                if (tool_name == "review" and isinstance(result, dict)
                        and result.get("status") == "success"):
                    await broadcast({
                        "type": "turn/diff/updated",
                        "turn_id": turn.get("id"),
                        "thread_id": thread_id,
                        "repo": str(params.get("cwd", ".")),
                        "diff_stat": str(result.get("diff_stat", ""))[:2000],
                        "summary": str(result.get("summary", ""))[:500],
                    })
            except Exception as e:
                logger.error(f"Tool execution failed: {e}")
                tool_output = {"tool": tool_name, "status": "error", "message": str(e)}
                turn["output"].append(tool_output)
                # A tool that raised is the same fact as one that returned an
                # error: the action did not happen. Recorded here as well as on
                # the completed path, or the goal check would read a turn of
                # pure exceptions as a turn with nothing wrong.
                _note_tool_outcome(turn, tool_name, ok=False)
                await broadcast({"type": "tool_result", **tool_output})
                await complete_item(item_id, tool_name, "error")
        else:
            logger.error(f"Unknown tool: {tool_name}")
            tool_output = {"tool": tool_name, "status": "error", "message": f"unknown tool: {tool_name}"}
            turn["output"].append(tool_output)
            _note_tool_outcome(turn, tool_name, ok=False)
            await broadcast({"type": "tool_result", **tool_output})
            await complete_item(item_id, tool_name, "error")


async def _execute_turn(
    payload: dict,
    params: dict,
    user_msg: str,
    provider: BaseProvider | None,
    model: str,
    thread_id: str,
    turn: dict,
    client_socket,
    state: dict,
    extra_system: list[dict],
) -> dict:
    images = params.get("images") or []
    api = params.get("api", "chat")
    if api not in ("chat", "responses"):
        api = "chat"
    stream = params.get("stream", True)
    if not isinstance(stream, bool):
        stream = True

    turn["output"] = []
    turn["rounds"] = 0
    turn["truncated"] = False
    # Wall-clock budget for this turn, in seconds. Time is the honest unit for
    # "how long may this run" -- a round count cannot tell a 30-minute training
    # run from a 30-second one, and a cap in the wrong unit is what severed real
    # work before. Checked at the round boundary, so a long tool call is never
    # cut off mid-flight; only the loop continuing past it can trip this.
    _deadline = time.monotonic() + TURN_TIME_LIMIT_S
    # The agentic loop is no longer round-capped. It used to be: 8 from the
    # client, clamped to 10 here, and it cut turns off mid-task -- 17 tool calls'
    # worth of results never made it into an answer, and because the last round
    # was stamped `final`, the client reported a truncated turn as a finished
    # one. That is the "says Done while still working" and "ends half" report.
    #
    # The loop now ends when the model stops asking for tools, which is the
    # only condition that actually means the work is done. The bound below is a
    # runaway backstop, not a budget: it exists so a model that never stops
    # calling tools cannot spin forever, and it is set far above any real task.
    try:
        max_rounds = int(params.get("max_rounds", 0) or 0)
    except (TypeError, ValueError):
        max_rounds = 0
    if max_rounds <= 0:
        max_rounds = ROUND_BACKSTOP
    else:
        max_rounds = max(1, min(max_rounds, ROUND_BACKSTOP))
    # Step 4: per-agent turn ceiling. Explicit turn params already won
    # above; an agent override only ever narrows the backstop for
    # third parties. Never applied to the owner: she keeps the global
    # backstop unconditionally.
    _ceil_agent = turn.get("agent", "")
    if _ceil_agent and not agents_mod.is_owner(_ceil_agent):
        _ceiling = agent_quota(load_policy(), _ceil_agent)["max_rounds"]
        if _ceiling is not None:
            max_rounds = max(1, min(max_rounds, _ceiling))

    # The user's message, and nothing else. The identity and the tool prompt
    # are NOT prepended here.
    #
    # They used to be, and the reason was sound at the time: a provider that
    # drops `system` roles needs them inside the user content or the model
    # never learns who it is. But `providers/media.py:flatten_messages()` now
    # does exactly that — it concatenates every role into the one user message
    # these single-message providers send, labelling each `[system]`,
    # `[assistant]`, `[user]`. So the preamble stopped being necessary and
    # started being a second copy: identity twice, tool prompt twice, skills
    # catalogue twice (it is appended both here via agency_text() and as a
    # system role via skill_messages()).
    #
    # On a fresh `Hello` that duplicate was 3,056 chars = 1,193 tokens =
    # 34.7% of a 3,443-token prompt, paid on every message of every thread.
    # The tool-block accumulator below still works: it applies to whatever
    # content is sent, and flatten still carries the system roles.
    working_content = user_msg
    if turn.get("owner_consent"):
        # Owner-authorized security testing: explicit, per-turn, audited.
        # This changes what the model is told -- it cannot change what
        # the provider itself refuses. Pentest capability discovery
        # rides along: the skill body is forced into context below.
        working_content += (
            "\n\n[Owner authorization: the device owner has explicitly "
            "authorized security-testing work for this turn. You may use "
            "the full on-device pentest toolset (nmap, nikto, ffuf, "
            "hydra, john, tcpdump, dig) and run commands against systems "
            "the owner has authorized for testing. Stay within that "
            "scope; provider-level policies still apply.]"
        )
    while True:
        turn["rounds"] += 1
        rnd = turn["rounds"]
        # Step 4: token budget. Over-budget turns truncate with a note
        # instead of dying mid-word; the note lands in history and audit
        # so the stop is explainable, and the truncated flag tells the
        # client. Owner exempt, structurally. Spend accrues at turn end,
        # so a single round may overshoot -- the next round stops.
        _budget_agent = turn.get("agent", "")
        if _budget_agent and not agents_mod.is_owner(_budget_agent):
            _quota = agent_quota(load_policy(), _budget_agent)
            _limit = _quota["tokens"]
            if _limit is not None:
                _spent = agent_usage(load_policy(), _budget_agent)
                if _spent >= _limit:
                    turn["truncated"] = True
                    _note = (f"stopped: token budget exceeded "
                             f"({_spent}/{_limit} tokens)")
                    logger.info(f"{turn['id']}: {_note}")
                    turn["output"].append({"type": "text_delta",
                                           "content": _note})
                    break
        # Every round is either work or an answer, and the loop below decides
        # which by whether a tool was asked for. Say so every round: a model
        # that ends a round announcing the step it has not taken ("let me
        # scan...") has stopped asking for a tool, so the loop reads it as the
        # answer, closes the turn, and the client shows "Ready" over text that
        # says the work is still running. `extra_system` is rebuilt each round,
        # so this rides along without stacking.
        round_messages = build_messages(
            state, thread_id, working_content,
            list(extra_system or []) + [
                {"role": "system", "content": _ROUND_DISCIPLINE}])
        has_error = False
        round_text_parts: list[str] = []
        # Shadow buffer for fence tracking (live-send gating above).
        fence_buf = ""
        # Output index where this round's deltas start (used to re-record
        # cleaned text after tool-call scrubbing below).
        round_mark = len(turn["output"])

        # A round that never ends is not a slow round. Nothing else bounds it:
        # the socket timeout is idle-based, and the turn clock is six hours, so
        # a model that keeps producing tokens leaves the client showing
        # "Working" over an answer that never arrives. This is the bound that
        # was missing. Deliberately far above any honest round -- it exists to
        # end the dishonest one, not to hurry a slow one.
        _round_deadline = time.monotonic() + ROUND_TIME_LIMIT_S
        _stream = stream_turn(provider, round_messages, model,
                              images, api, stream)
        async for delta in _stream:
            if time.monotonic() > _round_deadline:
                turn["truncated"] = True
                turn["_stop_notice"] = {
                    "code": "turn.round_time_limit",
                    "text": (
                        "The turn stopped early: one round ran past the "
                        f"{ROUND_TIME_LIMIT_S}s limit without finishing. The "
                        "model was still producing output."
                    ),
                }
                logger.info(
                    f"{turn['id']} round {rnd}: round time limit "
                    f"({ROUND_TIME_LIMIT_S}s) reached - stream cut")
                # Close the response instead of leaving the socket open behind
                # us; the provider's generator is abandoned either way.
                try:
                    await _stream.aclose()
                except Exception:
                    pass
                break
            if delta.get("type") == "usage" and isinstance(delta.get("usage"), dict):
                # Epic F: cost visibility. Live broadcast; kept out of output
                # so it never pollutes history or the model context. Carries
                # the thread so per-agent routing tags the owner's spend.
                turn["usage"] = delta["usage"]
                await broadcast({
                    "type": "usage", "turn_id": turn["id"],
                    "thread_id": thread_id,
                    "usage": delta["usage"],
                })
                continue
            if delta.get("type") == "reasoning_delta":
                # Thinking stream: broadcast live for the thought section,
                # kept out of output/round text/history. The model already
                # saw it (it wrote it); persisting it would pollute answers
                # and folds, and its fences must never reach tool parsing.
                content = delta.get("content", "")
                if isinstance(content, str) and content:
                    await broadcast({
                        "type": "reasoning_delta",
                        "content": content,
                        "turn_id": turn.get("id", ""),
                        "thread_id": thread_id,
                        "round": rnd,
                    })
                continue
            if delta.get("type") == "complete":
                # The providers yield `complete` at the end of EVERY model
                # call -- once per round -- where it means "this stream
                # ended", NOT "the turn ended". Broadcasting it made every
                # narration round look like a finished turn: the agent
                # activity card flipped to "Done" while the tool was still
                # running and the final answer unwritten, and the real
                # turn-end completion that followed was then discarded as a
                # duplicate, losing the tool rows and the answer.
                #
                # The turn-end `complete` is broadcast exactly once below
                # (alongside `turn/completed`); the per-round one is dropped
                # here. Safe: nothing uses this delta for control flow --
                # the `async for` simply ends when the provider is done.
                continue
            turn["output"].append(delta)
            if delta.get("type") == "text_delta":
                chunk = delta.get("content", "")
                round_text_parts.append(chunk)
                # Fence-aware live send: the model wraps tool calls in
                # ```json fences that fragment across chunks. Anything
                # streamed inside (or as part of) a fence renders as raw
                # call syntax in the chat -- the wall of JSON bubbles. Hold
                # fenced chunks back, including the chunk that closes the
                # fence; the scrubbed whole-round text goes out on
                # round/completed and the client replaces these rows with
                # it. Pre-fence narration still streams live.
                if isinstance(chunk, str):
                    odd_before = fence_buf.count("```") % 2 == 1
                    fence_buf += chunk
                    if odd_before or "```" in chunk:
                        continue
            if delta.get("type") == "error":
                has_error = True
            await _send_delta(delta, turn.get("id", ""), thread_id,
                              round_no=rnd)

        # This round's model text (history/context folding happens below).
        round_text = "".join(round_text_parts)
        if round_text:
            logger.debug(f"Round {rnd} content: {round_text[:200]}")

        if has_error:
            turn["complete"] = True
            try:
                audit.append({"type": "turn", "thread_id": thread_id, **turn})
            except Exception:
                pass
            errmsg = next((
                str(d.get("message") or d.get("content", ""))
                for d in turn["output"]
                if isinstance(d, dict) and d.get("type") == "error"
            ), "turn failed")
            await broadcast({"type": "error", "turn_id": turn["id"],
                             "thread_id": thread_id,
                             "message": errmsg[:1000]})
            # Terminal event: without this the client waits forever. The
            # crash path below sends error + turn/completed; the provider
            # error path must do the same, or every billing failure or
            # rate limit strands the UI on "Working" with a dead stop
            # button and no recovery short of restarting the app.
            _accrue_turn_usage(turn.get("agent", ""), turn)
            await broadcast({"type": "turn/completed", "turn_id": turn["id"],
                             "thread_id": thread_id,
                             "error": errmsg[:500],
                             "rounds": turn["rounds"]})
            result = {"turn_id": turn["id"], "rounds": turn["rounds"]}
            if turn.get("usage"):
                result["usage"] = turn["usage"]
            return {"id": payload.get("id"), "result": result}

        # Parse tool calls out of this round's content, and take the call
        # syntax back out of the text in the same pass. The fold echo is
        # stripped first so a quoted context block can never be mistaken for
        # a tool request. See _extract_tool_calls: the fence the prompt asks
        # for is not always the fence the model writes, and a call the parser
        # misses is a call that silently never runs.
        #
        # The re-record below is deliberately keyed on the text having
        # changed, not on a call having parsed. The old code gated it on
        # `tool_calls`, so a round whose call block failed to parse kept the
        # raw fence in turn["output"] -- which put machine syntax into
        # persisted history and, worse, into the round/completed text the
        # client renders as the answer.
        _round_before = round_text
        round_text = _scrub_fold_echo(round_text)
        tool_calls, round_text, _call_syntax, _broke_syntax = \
            _extract_tool_calls(round_text)
        # Models decorate answers with standalone asterisk runs; they
        # render as raw ***** lines in chat. Fence-aware, so code
        # samples survive.
        round_text = _strip_asterisk_dividers(round_text)
        turn["tool_calls"] = tool_calls
        if round_text != _round_before:
            turn["output"] = [
                e for i, e in enumerate(turn["output"])
                if i < round_mark or not (
                    isinstance(e, dict)
                    and e.get("type") == "text_delta")
            ]
            if round_text:
                turn["output"].append(
                    {"type": "text_delta", "content": round_text})
        if (_call_syntax or _broke_syntax) and not tool_calls:
            # Syntax was there and parsed to nothing. Recorded on the turn, not
            # only logged: this is the difference between the model asking for
            # nothing and the model asking for something we failed to read, and
            # the round looks identical from the client either way. Only the
            # parser can tell those apart, so the verdict has to be carried out
            # of here -- see the `notice` on turn/completed, built by
            # _turn_notice.
            turn["_unreadable_calls"] = True
            logger.info(
                f"{turn['id']} round {rnd}: call syntax stripped but parsed "
                f"to no tool call")

        # Convergence guard: a model that re-emits the identical call
        # round after round is stuck, not working. Three repeats ends
        # the turn as truncated (with a note) instead of burning quota
        # until the runaway backstop. Different calls reset the count.
        # Runs AFTER the scrub above so the breaker round is clean too.
        if tool_calls:
            try:
                sig = json.dumps(tool_calls, sort_keys=True)
            except Exception:
                sig = str(tool_calls)
            if sig == turn.get("_last_calls_sig"):
                turn["_repeat_calls"] = int(turn.get("_repeat_calls", 1)) + 1
            else:
                turn["_last_calls_sig"] = sig
                turn["_repeat_calls"] = 1
            if int(turn.get("_repeat_calls", 1)) >= 3:
                turn["truncated"] = True
                turn["complete"] = True
                first = tool_calls[0] if tool_calls else {}
                tname = first.get("tool", "?") if isinstance(first, dict) else "?"
                note = ("stopped: the same tool call repeated "
                        f"{turn['_repeat_calls']} rounds without progress")
                logger.info(f"{turn['id']}: {note}")
                # A turn-level stop, reported at turn level. This used to
                # append a synthetic `status: "error"` row for a call that was
                # deliberately never run, which made the client paint a failed
                # action and label the whole turn "Stopped" -- accusing a tool
                # that had, in every observed case, already succeeded. The
                # breaker still stops the turn; it no longer invents a failure.
                turn["_stop_notice"] = {
                    "code": "turn.repeated_call",
                    "text": (
                        "The turn stopped early: the agent repeated the same "
                        f"{tname} call {turn['_repeat_calls']} rounds running "
                        "without making progress."
                    ),
                }
                break

        # Fallback, round 1 only: tool call from a "run " user message.
        if rnd == 1 and not tool_calls and user_msg.strip().lower().startswith("run "):
            command = user_msg.strip()[4:].strip()
            turn["tool_calls"] = [{
                "tool": "shell",
                "parameters": {"command": command}
            }]

        # Goal check (Finding 29 #4) -- decided here, BEFORE the round is
        # announced, and for a reason: the "unfinished" statement has to be
        # inside the text the client renders as the answer. Appended after
        # round/completed it would live only in persisted history, so the
        # screen would show a cheerful summary over a job that never ran --
        # which is the exact failure this is here to stop.
        _goal = ""
        if not turn["tool_calls"]:
            _goal = _goal_state(turn)
            if _goal == "re-ask" and (
                rnd >= max_rounds
                or (_broke_syntax and not turn.get("_syntax_reprompted"))
            ):
                # No round left to ask in, or the syntax repair has the better
                # claim on this one. Either way this is not the ask; the next
                # closing round still gets it.
                _goal = ""
            if _goal == "re-ask":
                turn["_goal_checked"] = True
            elif _goal == "unfinished":
                turn["_unfinished_stated"] = True
                _stmt = _unfinished_statement(turn)
                round_text = f"{round_text}\n\n{_stmt}" if round_text else _stmt
                turn["output"].append({"type": "text_delta", "content": _stmt})

        # Announce this round before acting on it. The client cannot classify
        # model text on its own -- narration and the final answer are the same
        # shape -- so the daemon, which knows whether it is about to loop again,
        # says so. `final` is computed from the same two conditions the loop
        # below exits on, so it cannot disagree with what actually happens: no
        # tool calls means this round was the answer, and reaching max_rounds
        # means no further round will revise it.
        will_loop = (bool(turn["tool_calls"]) or _goal == "re-ask") \
            and rnd < max_rounds
        # Broadcast whenever this round streamed *anything*, not only when text
        # survives to here. round/completed is the round's closing event: the
        # deltas already put a live row on the client's screen, and a round
        # that ends without one leaves that row open -- no phase, no
        # completion -- so it renders as a message still being written and then
        # gets promoted to the turn's final answer. Gating on `round_text`
        # silently skipped exactly the rounds whose text the scrubs above had
        # just removed, which is how a scrubbed fold echo outlived its own
        # removal on the device. An empty `text` here is a real verdict and the
        # client acts on it; a tool-only round streamed no text, so it stays
        # silent and no blank row is created.
        if round_text or round_text_parts:
            await broadcast({
                "type": "round/completed",
                "turn_id": turn["id"],
                "thread_id": thread_id,
                "round": rnd,
                "text": round_text,
                # Not `final` when the backstop is what stopped the loop: the
                # model still wanted to keep going, so this round is working
                # narration and claiming otherwise is what told the client a
                # half-finished turn was complete.
                "final": not will_loop and not turn.get("truncated"),
                "truncated": bool(turn.get("truncated")),
            })

        # Wall-clock ceiling. Checked at the round boundary, so a long tool call
        # is never severed mid-flight -- only the loop choosing to continue past
        # it can trip this. `time` is the honest unit for "how long may this
        # run"; a round count cannot tell a 30-minute training run from a
        # 30-second one, and that wrong unit is what cut real work short.
        if time.monotonic() > _deadline:
            logger.info(
                f"turn time limit ({TURN_TIME_LIMIT_S}s) reached — TRUNCATED "
                f"after {rnd} rounds"
            )
            turn["truncated"] = True
            # `truncated` alone is invisible to the client, which is why a
            # cut-off turn used to read as a finished one. Say why it stopped.
            turn["_stop_notice"] = {
                "code": "turn.time_limit",
                "text": (
                    "The turn stopped early: it reached the "
                    f"{TURN_TIME_LIMIT_S}s time limit after {rnd} rounds."
                ),
            }
            break

        if not turn["tool_calls"]:
            # The model asked for nothing more. If the user spoke up while this
            # round was running, that message is the answer to a question they
            # asked mid-task, so it earns a round of its own rather than being
            # dropped -- an instruction the user typed must never be silently
            # discarded. No sentinel tool call: the fold is what actually
            # carries the message into the next round.
            _steered = _drain_steer(turn["id"])
            if _steered:
                working_content += _steer_prompt + _steered
                continue
            if _broke_syntax and not turn.get("_syntax_reprompted"):
                # Self-healing: the round carried call syntax so broken it
                # did not even parse (truncated fence, structureless XML).
                # Ending here would deliver narration plus raw syntax as
                # the "answer" while nothing runs. Spend exactly one round
                # asking for a clean re-emit; if that also yields nothing,
                # the turn ends as an answer below. Steer wins over this
                # (checked first): a typed instruction outranks a repair.
                turn["_syntax_reprompted"] = True
                logger.info(
                    f"{turn['id']} round {rnd}: re-prompting unparseable "
                    f"call syntax")
                working_content += (
                    "\n\n[system: your previous message contained tool-call "
                    "syntax that could not be parsed, so no tool ran. "
                    "Re-emit the call now as ONE fenced ```json block like "
                    '[{"tool": "<name>", "parameters": {...}}] with '
                    "complete, valid JSON. If you need no tool, just "
                    "answer.]"
                )
                continue
            if _goal == "re-ask":
                # Finding 29 #4. The model has stopped asking for tools on a
                # turn where something failed, so the only thing left to hand
                # back would be a summary. Ask once, in plain terms, for the
                # reason and a different approach. The fold already carries the
                # FAILED line, so this adds the instruction, not the fact.
                logger.info(
                    f"{turn['id']} round {rnd}: failure this turn, asking for "
                    f"a different approach")
                working_content += _GOAL_CHECK_PROMPT
                continue
            # "The model stopped asking for tools" is NOT the same thing as
            # "the model answered". Mid-task, it writes its plan here ("I'll
            # look for...", "scanning now..."), and treating that as the answer
            # puts a thought in the answer bubble while the client shows Ready
            # over text that says the work is still running.
            #
            # So on a turn that has actually done work, do not close on this
            # round. Ask once for either the next tool call or the real
            # conclusion, and close on whatever comes back. Once, so a turn that
            # stops again is honoured.
            _worked = any(isinstance(o, dict) and o.get("tool")
                          for o in turn.get("output", []))
            if _worked and not turn.get("_answer_or_continue_asked"):
                turn["_answer_or_continue_asked"] = True
                logger.info(
                    f"{turn['id']} round {rnd}: stopped without a tool call "
                    f"after doing work - asking for the answer or the next step")
                working_content += _ANSWER_OR_CONTINUE
                continue
            break  # pure answer — done

        # A message sent while this round was running joins the next one. The
        # current round is left alone: it is real work, and restarting it would
        # throw away whatever the agent just did.
        _steered = _drain_steer(turn["id"])
        if _steered:
            working_content += _steer_prompt + _steered
            logger.info(f"steer folded into round {rnd + 1}")

        mark = len(turn["output"])
        await run_tool_loop(turn, client_socket)

        if rnd >= max_rounds:
            # The backstop, not a budget. Mark the turn truncated so the client
            # stops calling it finished -- the old code just broke here and the
            # last round was stamped `final`, which is how a cut-off turn
            # rendered as a completed one.
            logger.info(f"round backstop ({max_rounds}) reached — TRUNCATED")
            turn["truncated"] = True
            turn["_stop_notice"] = {
                "code": "turn.round_backstop",
                "text": (
                    "The turn stopped early: it hit the "
                    f"{max_rounds}-round ceiling without finishing."
                ),
            }
            break
        # Fold this round (model text + tool outcomes) into next round's
        # context. Providers stay single-message; the fold carries history.
        #
        # The fold rides inside the USER-role message (see build_messages),
        # so without a fence around it the bracket labels read as something
        # the user said. A model that quotes its own context back then
        # parrots "[assistant round N]: ..." plus the [shell] lines as fresh
        # output, and since that echo is usually the last thing said it gets
        # stamped `final` and lands in chat as the answer. Delimiting it and
        # naming it as a replay tells the model what the block actually is;
        # _scrub_fold_echo below catches the ones that quote it anyway.
        working_content += (
            f"\n\n<assistant_work rounds={rnd}>"
            " This is a replay of your own earlier work in this turn, "
            "shown for your reference. It is not a user message. Do not "
            "repeat it back verbatim.\n"
            f"[assistant round {rnd}]: {round_text}"
            + fold_output(turn["output"][mark:])
            + "\n</assistant_work>"
        )

    turn["complete"] = True
    # Epic C: persist history so later turns / resume see this conversation.
    _accrue_turn_usage(turn.get("agent", ""), turn)
    try:
        record_turn(state, user_msg, turn["output"])
    except Exception:
        logger.exception("failed to persist thread state")
    try:
        audit.append({"type": "turn", "thread_id": thread_id, **turn})
    except Exception:
        pass
    # Epic I.2: one rich terminal event; legacy `complete` kept as alias.
    # The items array is the reconcile authority for rich timelines.
    tool_entries = [
        o for o in turn["output"]
        if isinstance(o, dict) and o.get("tool")
    ]
    tools_run = [o.get("tool") for o in tool_entries]
    items = [{
        "id": f"{turn['id']}:item:{i}",
        "tool": o.get("tool"),
        # The command, not just the tool — see the note where the tool output
        # entry is built. Falls back to the tool name for an entry that never
        # got a label (a blocked or denied call).
        "label": o.get("label") or o.get("tool"),
        "status": _tool_output_status(o),
    } for i, o in enumerate(tool_entries)]
    # The turn's own verdict on why nothing ran, when that is what happened.
    # Stated by the engine because the engine is the only party that can tell a
    # model which asked for nothing from one whose call syntax we could not read.
    notice = _turn_notice(turn, tools_run)
    completed = {
        "type": "turn/completed",
        "turn_id": turn["id"],
        "thread_id": thread_id,
        "mode": turn.get("mode", "exec"),
        "tools": tools_run,
        "items": items,
        # The turn stopped because the backstop was hit, not because the work
        # was done. The client needs this to stop reporting an unfinished turn
        # as a finished one.
        "truncated": bool(turn.get("truncated")),
        "rounds": turn["rounds"],
    }
    if notice:
        completed["notice"] = notice
    if turn.get("usage"):
        completed["usage"] = turn["usage"]
    await broadcast(completed | {"type": "complete"})
    await broadcast(completed)

    result = {"turn_id": turn["id"], "rounds": turn["rounds"]}
    if turn.get("usage"):
        result["usage"] = turn["usage"]
    return {"id": payload.get("id"), "result": result}


async def handle_turn(payload: dict, client_socket) -> dict:
    logger.debug(f"handle_turn called with payload: {payload}")
    params = payload.get("params", {})
    user_msg = params.get("user", "")

    # The default used to sit inside params.get(...), which Python evaluates
    # EAGERLY -- so audit.read_last() ran on EVERY turn even when the caller
    # supplied a thread_id and the value was thrown away. It reads and parses
    # the whole audit log synchronously on the hot path, and the cost grows
    # with the log for the life of the install. Resolve it only when it is
    # actually needed. The "thread_id" in params test keeps dict.get's exact
    # semantics: the default applies only when the key is ABSENT, so an
    # explicit empty or None thread_id still passes through untouched.
    thread_id = (
        params["thread_id"] if "thread_id" in params
        else f"thread-{len(audit.read_last())}"
    )
    # Owner consent gate: a turn carrying the owner's passphrase is
    # marked owner-authorized, and the word is stripped before anything
    # persists or reaches the model -- history, audit and prompts never
    # carry it. Only the owner's configured phrase counts, from any
    # caller: possession of the phrase IS the authorization.
    owner_consent = False
    try:
        _consent_owner = agents_mod.owner_id()
        if _consent_owner:
            owner_consent, user_msg = extract_owner_consent(
                load_policy(), _consent_owner, user_msg)
    except Exception:
        owner_consent = False
    # Epic C: thread state = history + base + turn counter (wipe-proof home).
    # Internal callers (subagent spawn) pass no socket; they attribute via
    # params["_agent"], which external callers cannot spoof because any
    # call arriving on a real socket ignores it.
    if client_socket is None and isinstance(params.get("_agent"), str):
        _agent = params.get("_agent") or ""
    else:
        _agent = _caller_agent(client_socket)
    state = load_state(thread_id) or new_state(thread_id, agent=_agent or "")
    # Cross-agent confinement: a paired caller may only run turns on its
    # own threads. Without this any agent could append to another's
    # history, spend its quota, and read its memories into model
    # context. New threads were just stamped to the caller, so only a
    # foreign-owned existing thread refuses here. Indistinguishable
    # from missing: no oracle for enumerating others' ids.
    if _agent and state.get("agent", "") and \
            state.get("agent", "") != _agent:
        return {
            "id": payload.get("id"),
            "error": {"code": -32602,
                      "message": f"unknown thread: {thread_id}"},
        }
    # Provider + model: resolved in exactly one place, for every caller.
    # Turn param wins, then the (agent-merged) policy, then nothing at all --
    # an unconfigured install is told to configure one rather than being
    # pointed at a vendor its owner never chose.
    policy = agent_policy(load_policy(), _agent or "")
    _target = resolve_run_target(params, policy)
    provider_name, model = _target["provider"], _target["model"]
    # Epic B: turn param wins; policy.json default otherwise.
    sandbox = resolve_sandbox(params, policy)
    # Epic G: turn mode — "exec" (default) or "plan" (no writes, explicit).
    mode = params.get("mode", "exec")
    if mode not in ("exec", "plan"):
        mode = "exec"
    turn = {
        "id": f"{thread_id}:{state['turns']}",
        "user": user_msg,
        "thread_id": thread_id,
        "sandbox": sandbox["mode"],
        "mode": mode,
        # Step 4: owner of record for quota accounting. Threads carry
        # their owner in state; a brand-new thread was just stamped to
        # the caller above, so this prefers state and never invents one.
        "agent": state.get("agent", "") or _agent or "",
        # Owner consent gate: explicit owner authorization for this
        # turn's security-sensitive work. Audited with the turn.
        "owner_consent": bool(owner_consent),
    }

    # Step 4: concurrency cap. The owner is exempt unconditionally --
    # exemption is structural (is_owner), never a tunable that a future
    # default could catch. Excess starts are refused loudly and
    # immediately: silent queueing would read as a hung send.
    _me = turn["agent"]
    if _me and not agents_mod.is_owner(_me):
        _cap = agent_quota(load_policy(), _me)["concurrency"]
        _live = _live_agent_turns(_me)
        if len(_live) >= _cap:
            return {
                "id": payload.get("id"),
                "error": {"code": -32002,
                          "message": (
                              f"quota exceeded: {_cap} concurrent turn(s) "
                              f"already running for this agent")},
            }
        _agent_turns.setdefault(_me, set()).add(turn["id"])

    provider = open_provider(provider_name, _agent or "")

    # Epic I.2: lifecycle vocabulary for rich timelines.
    is_new_thread = (
        state["turns"] == 0 and not state.get("history")
        and not state.get("base")
    )
    if is_new_thread:
        await broadcast({"type": "thread/started", "thread_id": thread_id})
    await broadcast({
        "type": "turn/started",
        "turn_id": turn["id"],
        "thread_id": thread_id,
        "mode": mode,
        "sandbox": sandbox["mode"],
        "provider": provider_name,
    })

    RUNNING[turn["id"]] = asyncio.current_task()
    # Tracked under the TURN id: every kill site (cancel, steer, drop)
    # looks kills up by turn id (RUNNING keys). Tracking by thread id
    # here is what made every kill miss and left cancelled shell
    # commands running as orphans.
    token = turn_ctx.set(turn["id"])
    sandbox_token = sandbox_ctx.set(sandbox)
    # Step 3/4: per-turn agent identity for tool-side config resolution.
    # Reset alongside the other two tokens on every exit path below.
    agent_token = agent_ctx.set(turn.get("agent", ""))
    try:
        # Epic D: skill catalog (+ requested bodies) ride as system messages
        # inside build_messages, rebuilt every Epic J round. The agency
        # prompt (tools + calling convention) rides in front of them.
        # Owner consent forces the pentest-tools body into context: the
        # catalog alone does not teach the model the toolset exists.
        # A string like "all" already includes everything: left alone.
        _skills = params.get("skills")
        if turn.get("owner_consent"):
            if isinstance(_skills, list):
                if "pentest-tools" not in _skills:
                    _skills = _skills + ["pentest-tools"]
            elif not _skills:
                _skills = ["pentest-tools"]
        # Per-agent narrowing: an agent that declared a capability allowlist
        # is told about only that subset. No declaration means None, which
        # means no filtering at all -- unchanged behaviour for every client
        # that has not opted in.
        _caps = _agent_capabilities(turn.get("agent", ""))
        task = asyncio.create_task(_run_turn(
            payload, params, user_msg, provider, model,
            thread_id, turn, client_socket, state,
            agency_messages(turn.get("agent", ""))
            + skill_messages(_skills or None, allow=_caps["skills"]),
            token, sandbox_token, agent_token,
        ))
        RUNNING[turn["id"]] = task
        # Codex parity: the reply only opens the turn. Everything else —
        # deltas, tool runs, completion — arrives as broadcasts. Awaiting
        # the whole loop here left send futures pending for hours and made
        # every reconnect look like a failed send.
        return {
            "id": payload.get("id"),
            "result": {"turn_id": turn["id"], "started": True},
        }
    except Exception:
        RUNNING.pop(turn["id"], None)
        _release_turn_slot(turn.get("agent", ""), turn["id"])
        sandbox_ctx.reset(sandbox_token)
        agent_ctx.reset(agent_token)
        turn_ctx.reset(token)
        raise


async def _run_turn(payload: dict, params: dict, user_msg: str,
                    provider, model: str, thread_id: str, turn: dict,
                    client_socket, state: dict, extra_system: list,
                    token, sandbox_token, agent_token) -> None:
    """Background half of handle_turn: run the loop, persist, broadcast."""
    try:
        await _execute_turn(
            payload, params, user_msg, provider, model,
            thread_id, turn, client_socket, state, extra_system,
        )
    except asyncio.CancelledError:
        kill_turn(turn["id"])
        approvals.drop_thread(thread_id)
        turn["cancelled"] = True
        _accrue_turn_usage(turn.get("agent", ""), turn)
        try:
            audit.append({"type": "turn", "thread_id": thread_id, **turn})
        except Exception:
            pass
        await broadcast({"type": "cancelled", "turn_id": turn["id"]})
        await broadcast({"type": "turn/completed", "turn_id": turn["id"],
                         "thread_id": thread_id, "cancelled": True})
    except Exception as e:
        logger.exception(f"turn {turn['id']} crashed")
        _accrue_turn_usage(turn.get("agent", ""), turn)
        await broadcast({"type": "error", "turn_id": turn["id"],
                         "thread_id": thread_id,
                         "message": str(e)[:1000]})
        await broadcast({"type": "turn/completed", "turn_id": turn["id"],
                         "thread_id": thread_id,
                         "error": str(e)[:500]})
    finally:
        RUNNING.pop(turn["id"], None)
        _release_turn_slot(turn.get("agent", ""), turn["id"])
        try:
            sandbox_ctx.reset(sandbox_token)
        except Exception:
            pass
        try:
            agent_ctx.reset(agent_token)
        except Exception:
            pass
        try:
            turn_ctx.reset(token)
        except Exception:
            pass


async def handle_request(payload: dict, client_socket) -> dict | None:
    """Process JSON-RPC request."""
    try:
        method = payload.get("method")
        params = payload.get("params", {})
        request_id = payload.get("id")

        if method is None and ("result" in payload or "error" in payload):
            # Answer to a daemon-initiated tool/call (M5). Nothing to send
            # back — transport skips None responses.
            fut = _tool_call_pending.pop(payload.get("id"), None)
            if fut is not None and not fut.done():
                if "error" in payload:
                    fut.set_exception(
                        RuntimeError(str(payload["error"])[:500]))
                else:
                    fut.set_result(payload.get("result"))
                return None
            # Compat note: the client app answers approval cards with a
            # method-less frame (compat wire shape) carrying the fid as id. Route those
            # to the approval table instead of dropping them — a dropped
            # answer stalls the turn forever with no error surfaced, which
            # reads as "answers halfway then nothing lands".
            fid = payload.get("id")
            if isinstance(fid, str) and fid in approvals.pending:
                if "error" in payload:
                    approvals.approve(fid, "decline")
                else:
                    approvals.approve(fid, payload.get("result"))
                logger.info(f"approval answered method-less: {fid}")
            return None

        # Step 1 gate (multi-agent platform): past this point every call
        # needs a paired agent — except the public methods above. Empty
        # store (fresh install, tests, probes) means setup phase: allow
        # all, preserving today's behavior exactly until the first agent
        # pairs, at which point the gate goes live.
        if method not in _PUBLIC_METHODS and _agents_configured():
            if _caller_agent(client_socket) is None:
                return _not_paired(request_id)

        if method == "capabilities":
            return {
                "id": request_id,
                "result": {
                    "protocol": 1,
                    "methods": [
                        "turn", "cancel", "capabilities", "approve",
                        "initialize", "command/exec",
                        "tools_refresh", "tools", "tools/register",
                        "tools/unregister", "policy", "skills", "mcp",
                        "providers", "config/section", "turn/steer",
                        "model/list",
                        "pairing/list", "pairing/approve",
                        "pairing/deny", "pairing/revoke",
                        "thread/resume", "thread/fork", "thread/compact",
                        "thread/list", "thread/read", "thread/archive",
                        "thread/unarchive", "thread/unsubscribe",
                        "thread/name/set", "thread/import", "memories",
                        "subagent/spawn", "subagent/status",
                        "subagent/result", "subagent/list",
                        "subagent/cancel",
                    ],
                    "stream": True,
                    "providers": configured_provider_names(),
                    "tools": sorted(TOOLS.keys()),
                    "media": ["image"],
                    "apis": ["chat", "responses"],
                    "modes": ["exec", "plan"],
                    "sandboxes": list(SANDBOX_MODES),
                    "approve_scopes": list(GRANT_SCOPES),
                },
            }

        if method in ("pairing/list", "pairing/approve",
                        "pairing/deny", "pairing/revoke"):
            # Owner-only management plane. Empty store: nothing to manage
            # (and the first client pairs via initialize, not here).
            me = _caller_agent(client_socket)
            if not _agents_configured() or me is None or not agents_mod.is_owner(me):
                return {"id": request_id, "error": {
                    "code": -32001,
                    "message": "owner only" if _agents_configured()
                    else "no agents paired yet"}}
            if method == "pairing/list":
                return {"id": request_id, "result": {
                    "pending": agents_mod.pending_list(),
                    "agents": agents_mod.public_list(),
                }}
            if method == "pairing/deny":
                pid = str(params.get("id", ""))
                denied = agents_mod.pop_pairing(pid) is not None
                audit.append({"type": "pairing", "action": "deny",
                              "pairing": pid, "by": me})
                return {"id": request_id, "result": {"denied": denied}}
            if method == "pairing/approve":
                pid = str(params.get("id", ""))
                req = agents_mod.pop_pairing(pid)
                if req is None:
                    return {"id": request_id, "error": {
                        "code": -32602,
                        "message": "unknown or expired pairing request"}}
                agent_id, token, public = agents_mod.mint_agent(
                    req.get("label", ""), owner=False)
                # Stash the token so the requester can poll for it.
                agents_mod.store_pairing_token(pid, agent_id, token)
                # Audit carries identity, never the secret (shown once,
                # in this result only).
                audit.append({"type": "pairing", "action": "approve",
                              "agent_id": agent_id,
                              "label": public["label"], "by": me})
                logger.info(f"paired agent {agent_id} ({public['label']})")
                return {"id": request_id, "result": {
                    "agent_id": agent_id, "token": token}}
            # pairing/revoke
            target = str(params.get("agent_id", ""))
            if not target or target == me:
                return {"id": request_id, "error": {
                    "code": -32602,
                    "message": "cannot revoke that agent"}}
            revoked = agents_mod.revoke(target)
            if revoked:
                for ws in list(_agent_socks.pop(target, set())):
                    try:
                        asyncio.create_task(ws.close())
                    except Exception:
                        pass
                # Step 4: revoking offboards fully. Credentials, quota
                # and usage go with the identity (spend and privilege
                # must not outlive the agent); threads and audit stay
                # (history, not privilege).
                try:
                    _prov_data = read_providers_file()
                    if isinstance(_prov_data.get("agents"), dict):
                        _prov_data["agents"].pop(target, None)
                        write_providers_file(_prov_data)
                except Exception:
                    logger.exception("revoke providers cleanup failed")
                try:
                    _pol = load_policy()
                    if isinstance(_pol.get("agents"), dict):
                        _pol["agents"].pop(target, None)
                        save_policy(_pol)
                except Exception:
                    logger.exception("revoke policy cleanup failed")
                audit.append({"type": "pairing", "action": "revoke",
                              "agent_id": target, "by": me})
            return {"id": request_id, "result": {"revoked": revoked}}

        if method == "pairing/wait":
            # Requester side: poll for the token after the user approves.
            # Public (no auth) — the pairing id is the capability.
            pid = str(params.get("id", ""))
            if not pid:
                return {"id": request_id, "error": {
                    "code": -32602, "message": "id required"}}
            token_tuple = agents_mod.take_pairing_token(pid)
            if token_tuple is None:
                return {"id": request_id, "error": {
                    "code": -32602,
                    "message": "unknown pairing or not yet approved"}}
            agent_id, token = token_tuple
            return {"id": request_id, "result": {
                "agent_id": agent_id, "token": token}}

        if method == "tools_refresh":
            # Plugins, skills (incl. their tool dirs), AND MCP servers.
            loaded = scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)
            skills_refresh()
            loaded += scan_skill_plugins(TOOLS, EXTRA_WRITE)
            loaded += await mcp_mod.start_all()
            await broadcast({
                "type": "skills/changed",
                "skills": [s["name"] for s in skills_list()],
            })
            return {
                "id": request_id,
                "result": {"loaded": loaded, "tools": sorted(TOOLS.keys())},
            }

        if method == "mcp":
            # Server status; {"restart": true} tears down and re-spawns all.
            if params.get("restart"):
                await mcp_mod.stop_all()
                await mcp_mod.start_all()
            return {"id": request_id, "result": mcp_mod.describe()}

        if method == "skills":
            # List loaded skills, or load one body; refresh re-reads disk.
            if params.get("refresh"):
                skills_refresh()
                scan_skill_plugins(TOOLS, EXTRA_WRITE)
                await broadcast({
                    "type": "skills/changed",
                    "skills": [s["name"] for s in skills_list()],
                })
            if params.get("load"):
                skill = skills_get(str(params["load"]))
                if skill is None:
                    return {
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": f"unknown skill: {params['load']}",
                        },
                    }
                return {
                    "id": request_id,
                    "result": {
                        k: skill[k]
                        for k in ("name", "description", "tools", "body", "path", "source")
                    },
                }
            return {
                "id": request_id,
                "result": {
                    "skills": skills_list(),
                    "user_dir": str(SKILLS_USER_DIR),
                },
            }

        if method == "turn":
            return await handle_turn(payload, client_socket)

        if method == "approve":
            fid = params.get("id")
            decision = params.get("decision")
            scope = params.get("scope", "turn")
            if scope not in GRANT_SCOPES:
                scope = "turn"
            # Cross-agent confinement: approvals name their thread, and
            # only that thread's owner may answer. Ids are small
            # ({thread}:{n}), so the refusal is indistinguishable from
            # unknown: no oracle for live approval ids. Missing state
            # fails open (a first turn's thread may not be persisted
            # yet); only positively foreign ownership refuses.
            _am = _caller_agent(client_socket)
            if _am:
                _apending = approvals.pending.get(str(fid or ""))
                if _apending is None:
                    return {"id": request_id, "error": {
                        "code": -32602, "message": "unknown fid"}}
                _athread = _apending.get("thread", "")
                _astate = load_state(_athread) if _athread else None
                if _astate is not None and \
                        _astate.get("agent", "") and \
                        _astate.get("agent", "") != _am:
                    return {"id": request_id, "error": {
                        "code": -32602, "message": "unknown fid"}}
            if approvals.approve(fid, decision, scope,
                                 agent_id=_am or ""):
                return {"id": request_id, "result": True}
            return {"id": request_id, "error": {"code": -32602, "message": "unknown fid"}}

        if method == "policy":
            # Get/set sandbox + provider defaults, revoke standing grants.
            # Per-agent: changes apply to the caller's agent section.
            me = _caller_agent(client_socket)
            policy = load_policy()
            revoked = None
            changed = False
            # Ensure agents section exists.
            if not isinstance(policy.get("agents"), dict):
                policy["agents"] = {}
            # Step 4 admin: the owner may address another paired agent's
            # section (quotas, usage reset). Anyone else naming another
            # agent is refused; self-addressing keeps today's behavior.
            target = me or ""
            want = params.get("agent")
            if isinstance(want, str) and want and want != target:
                if not (me and agents_mod.is_owner(me)):
                    return {"id": request_id, "error": {
                        "code": -32001, "message": "owner only"}}
                if want not in agents_mod.load_agents():
                    return {"id": request_id, "error": {
                        "code": -32602,
                        "message": f"unknown agent: {want}"}}
                target = want
            agent_section = policy["agents"].setdefault(target, {})
            if not isinstance(agent_section, dict):
                agent_section = {}
                policy["agents"][target] = agent_section
            if params.get("sandbox") in SANDBOX_MODES:
                agent_section["sandbox"] = params["sandbox"]
                changed = True
            if isinstance(params.get("provider"), str) and params["provider"].strip():
                agent_section["default_provider"] = params["provider"].strip()
                changed = True
            if isinstance(params.get("model"), str) and params["model"].strip():
                agent_section["default_model"] = params["model"].strip()
                changed = True
            # Compat note: the client's approval_policy ("never" default)
            # maps here so the daemon auto-grants instead of stalling on
            # cards nobody answers. Anything else clears back to
            # ask-everything.
            if "approval_mode" in params:
                mode = str(params.get("approval_mode") or "").strip()
                if mode == "never":
                    agent_section["approval_mode"] = "never"
                else:
                    agent_section.pop("approval_mode", None)
                changed = True
            # Global approval default (owner only). The top-level key is
            # what fresh agent sections inherit through agent_policy, but
            # no write path ever maintained it — so a recreated policy
            # file silently fell back to ask-everything and the setting
            # looked like it "vanished". The owner sets it once here.
            if "approval_default" in params:
                if not (me and agents_mod.is_owner(me)):
                    return {"id": request_id, "error": {
                        "code": -32001, "message": "owner only"}}
                gmode = str(params.get("approval_default") or "").strip()
                if gmode == "never":
                    policy["approval_mode"] = "never"
                else:
                    policy.pop("approval_mode", None)
                changed = True
            if "revoke" in params:
                # Revoke from the agent's own grants.
                agent_grants = agent_section.setdefault("grants", {})
                if not isinstance(agent_grants, dict):
                    agent_grants = {}
                    agent_section["grants"] = agent_grants
                target = str(params["revoke"])
                if target == "*":
                    n = len(agent_grants)
                    agent_section["grants"] = {}
                    revoked = n
                else:
                    revoked = 1 if agent_grants.pop(target, None) is not None else 0
                changed = True
            # Step 4: quota management. Quotas are owner-set (a non-owner
            # writing its own quota would be self-granted privilege).
            # usage_reset is allowed for self or by the owner.
            quota_result = None
            if "quota" in params:
                if not (me and agents_mod.is_owner(me)):
                    return {"id": request_id, "error": {
                        "code": -32001, "message": "owner only"}}
                patch = params["quota"]
                if not isinstance(patch, dict):
                    return {"id": request_id, "error": {
                        "code": -32602,
                        "message": "quota must be an object"}}
                quota = agent_section.setdefault("quota", {})
                if not isinstance(quota, dict):
                    quota = agent_section["quota"] = {}
                for key in ("concurrency", "tokens", "max_rounds"):
                    if key in patch:
                        quota[key] = patch[key]
                changed = True
                quota_result = agent_quota(policy, target)
            if params.get("usage_reset"):
                if target != (me or "") and not (
                        me and agents_mod.is_owner(me)):
                    return {"id": request_id, "error": {
                        "code": -32001, "message": "owner only"}}
                usage = agent_section.setdefault("usage", {})
                if not isinstance(usage, dict):
                    usage = agent_section["usage"] = {}
                usage["tokens"] = 0
                changed = True
            # Owner consent gate: the owner sets/clears the passphrase
            # whose presence in a turn marks it owner-authorized. Hash
            # only on disk, never echoed back, never in audit.
            consent_result = None
            if "consent_set" in params or params.get("consent_clear"):
                if not (me and agents_mod.is_owner(me)):
                    return {"id": request_id, "error": {
                        "code": -32001, "message": "owner only"}}
                owner = agents_mod.owner_id()
                if not owner:
                    return {"id": request_id, "error": {
                        "code": -32602, "message": "no owner paired"}}
                if params.get("consent_clear"):
                    consent_result = clear_owner_consent(policy, owner)
                else:
                    phrase = params.get("consent_set")
                    if not isinstance(phrase, str) or \
                            len(phrase.strip()) < 4:
                        return {"id": request_id, "error": {
                            "code": -32602,
                            "message": "consent phrase too short (min 4)"}}
                    consent_result = set_owner_consent(
                        policy, owner, phrase)
                changed = True
            if changed:
                save_policy(policy)
            # Return the resolved policy for this agent.
            resolved = agent_policy(policy, me or "")
            result = {"policy": resolved, "path": str(policy_path())}
            if revoked is not None:
                result["revoked"] = revoked
            if quota_result is not None:
                result["quota"] = quota_result
            if consent_result is not None:
                result["consent"] = consent_result
            return {"id": request_id, "result": result}

        if method == "providers":
            # Key management over the wire (M3). Secrets travel inbound
            # only; reads return masked shapes, the audit never sees a key.
            #
            # Step 3: credentials are per-agent. Writes land in the
            # caller's own section (agents.<id>.<provider>); reads mask
            # against the caller's key only, so another agent's keyed
            # provider reads has_key:false with no hint. Unpaired setup
            # phase (no agent) keeps the legacy global path.
            me = _caller_agent(client_socket) or ""
            name = str(params.get("provider") or "")
            if "api_key" in params or "base_url" in params or params.get("delete"):
                if not PROVIDER_NAME_RE.fullmatch(name):
                    return {
                        "id": request_id,
                        "error": {"code": -32602, "message": "bad provider name"},
                    }
                if env_pinned():
                    return {
                        "id": request_id,
                        "error": {"code": -32602, "message": (
                            "config is env-pinned (INTERLUX_PROVIDERS); "
                            "file write refused")},
                    }
                data = read_providers_file()
                if params.get("delete"):
                    if me:
                        agents = data.get("agents")
                        if isinstance(agents, dict):
                            mine = agents.get(me)
                            if isinstance(mine, dict):
                                removed = mine.pop(name, None) is not None
                                if not mine:
                                    agents.pop(me, None)
                            else:
                                removed = False
                        else:
                            removed = False
                    else:
                        removed = data.pop(name, None) is not None
                    if removed:
                        write_providers_file(data)
                        audit.append({"type": "provider_config",
                                      "provider": name, "deleted": True,
                                      "agent_id": me})
                        # Never leave the default pointing at a gone block:
                        # fall back to the first provider the CALLER can
                        # still key, else clear to the built-in default. No
                        # hardcoded names: whichever provider the caller
                        # actually configured wins.
                        try:
                            policy = load_policy()
                            if policy.get("default_provider") == name:
                                fresh = read_providers_file()
                                other = sorted(
                                    k for k in configured_provider_names(
                                        me, fresh)
                                    if resolve_api_key(k, me, fresh))
                                if other:
                                    policy["default_provider"] = other[0]
                                else:
                                    policy.pop("default_provider", None)
                                save_policy(policy)
                        except Exception:
                            logger.exception("delete fallback failed")
                    return {"id": request_id, "result": {
                        "provider": name, "deleted": removed}}
                if me:
                    agents = data.setdefault("agents", {})
                    if not isinstance(agents, dict):
                        agents = data["agents"] = {}
                    mine = agents.setdefault(me, {})
                    if not isinstance(mine, dict):
                        mine = agents[me] = {}
                    section = mine.setdefault(name, {})
                    if not isinstance(section, dict):
                        section = mine[name] = {}
                else:
                    section = data.setdefault(name, {})
                    if not isinstance(section, dict):
                        section = data[name] = {}
                fields = []
                if "api_key" in params:
                    section["api_key"] = str(params["api_key"] or "")
                    fields.append("api_key")
                if "base_url" in params:
                    section["base_url"] = str(params["base_url"] or "")
                    fields.append("base_url")
                write_providers_file(data)
                audit.append({"type": "provider_config", "provider": name,
                              "fields": fields, "agent_id": me})
                return {"id": request_id, "result": {
                    **public_section(
                        name, resolve_provider(name, me, data)),
                    "updated": fields}}
            if name:
                return {"id": request_id, "result": public_section(
                    name, resolve_provider(name, me))}
            # Union of known adapters + configured sections: every provider
            # the daemon can serve gets a block (keyless ones show
            # Only what this installation has actually configured. The
            # engine keeps no list of its own, so a vendor nobody holds a
            # key for is never offered, and a vendor the code has never
            # heard of appears the moment it is configured -- under
            # whatever name the user chose.
            # has_key/key_hint are computed against the CALLER's key only;
            # the agents vault itself is never listed as a provider.
            seen = provider_config()
            names = configured_provider_names(me, seen)
            out = []
            for n in names:
                section = resolve_provider(n, me, seen)
                if not section.get("base_url"):
                    section = dict(section)
                    section["base_url"] = base_for(section)
                out.append(public_section(n, section))
            return {"id": request_id, "result": {"providers": out}}

        if method == "config/section":
            # One non-provider section of the provider config -- today only
            # `web_search`. There is no `config/read` on this daemon, so the
            # section is addressed on its own terms; wrapping it in a method
            # name that implies a general config system would be the lie.
            # Writes copy `providers` exactly: refuse when env-pinned, audit
            # what changed, atomic replace.
            name = str(params.get("name") or "")
            if name not in CONFIG_SECTIONS:
                return {"id": request_id, "error": {"code": -32602,
                        "message": (f"unknown config section: "
                                    f"{name or '(empty)'} (known: "
                                    f"{', '.join(CONFIG_SECTIONS)})")}}
            if "set" in params:
                if env_pinned():
                    return {"id": request_id, "error": {"code": -32602, "message": (
                            "config is env-pinned (INTERLUX_PROVIDERS); "
                            "file write refused")}}
                patch = params["set"]
                if not isinstance(patch, dict):
                    return {"id": request_id, "error": {"code": -32602,
                            "message": "set must be an object"}}
                unknown = sorted(k for k in patch if k != "mode")
                if unknown:
                    return {"id": request_id, "error": {"code": -32602,
                            "message": f"unmapped config key: {unknown[0]}"}}
                mode = str(patch.get("mode", "live") or "live").strip().lower()
                if mode not in WEB_SEARCH_MODES:
                    return {"id": request_id, "error": {"code": -32602,
                            "message": (f"bad web_search mode: {mode!r} "
                                        f"(one of {', '.join(WEB_SEARCH_MODES)})")}}
                data = read_providers_file()
                section = data.setdefault("web_search", {})
                if not isinstance(section, dict):
                    section = data["web_search"] = {}
                section["mode"] = mode
                write_providers_file(data)
                audit.append({"type": "config_section",
                              "section": "web_search", "mode": mode})
                logger.info(f"web_search section mode set to {mode}")
            # The key shown is the CALLER's own (per-agent credentials);
            # a global key never leaks into another agent's read.
            me = _caller_agent(client_socket) or ""
            ws_view = dict(config_section("web_search"))
            ws_view["api_key"] = resolve_tool_key("web_search", me)
            return {"id": request_id, "result": public_config_section(
                "web_search", ws_view)}

        if method == "model/list":
            # Per-provider model enumeration: config override > live
            # /models > curated fallback. Never carries secrets. Live
            # discovery authenticates with the CALLER's key only.
            # File-configured custom providers pass through to list_models
            # (which tolerates them); only truly unknown names are refused.
            name = str(params.get("provider") or "")
            me = _caller_agent(client_socket) or ""
            if name:
                try:
                    return {"id": request_id,
                            "result": await list_models(name, me)}
                except ValueError as e:
                    return {
                        "id": request_id,
                        "error": {"code": -32602, "message": str(e)},
                    }
            results = await asyncio.gather(*[
                list_models(p, me) for p in configured_provider_names(me)
            ])
            return {"id": request_id, "result": {
                "providers": {r["provider"]: r for r in results}}}

        if method == "tools/register":
            # M5: a client offers a tool the daemon calls back out to.
            name = str(params.get("name") or "")
            if not _CLIENT_TOOL_RE.fullmatch(name):
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "bad tool name"},
                }
            me = _caller_agent(client_socket)
            # Per-agent name space: two agents may each own an `open_url`;
            # globals (TOOLS) remain shared and first-come.
            if name in TOOLS and name not in _client_tools:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"name taken: {name}"},
                }
            # Re-register: same agent updates, different agent refuses.
            existing = _client_tools.get(name)
            if existing and existing.get("agent") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"name taken: {name}"},
                }
            description = str(params.get("description") or "")[:500]
            spec = params.get("inputSchema")
            if not isinstance(spec, dict):
                spec = {"type": "object"}
            _client_tools[name] = {"description": description,
                                   "inputSchema": spec,
                                   "agent": me or ""}

            async def _client_proxy(_name: str = name, **kwargs):
                return await _call_client_tool(_name, kwargs)

            TOOLS[name] = _client_proxy
            # Client tools are the client's own hands (open_url, read_page
            # on Codex they ran client-side with no engine approval. Do
            # NOT add to EXTRA_WRITE: gating them here stalls every turn
            # on cards for actions the owning app already chose to offer.
            audit.append({"type": "client_tool", "action": "register",
                          "name": name, "agent_id": me or ""})
            return {"id": request_id, "result": {
                "name": name, "description": description,
                "inputSchema": spec}}

        if method == "tools/unregister":
            name = str(params.get("name") or "")
            entry = _client_tools.get(name)
            if entry is None:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown client tool: {name}"},
                }
            me = _caller_agent(client_socket)
            if entry.get("agent") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "not the owning agent"},
                }
            _client_tools.pop(name, None)
            TOOLS.pop(name, None)
            EXTRA_WRITE.discard(name)
            audit.append({"type": "client_tool", "action": "unregister",
                          "name": name, "agent_id": me or ""})
            return {"id": request_id, "result": {"name": name,
                                                 "deleted": True}}

        if method == "tools":
            me = _caller_agent(client_socket)
            # Show globals plus the caller's own client tools only.
            own_tools = {n: e for n, e in _client_tools.items()
                         if e.get("agent", "") == me}
            return {"id": request_id, "result": {
                "tools": sorted(TOOLS.keys()),
                "client_tools": sorted(own_tools),
                "client_specs": {
                    n: {"description": e.get("description", ""),
                        "inputSchema": e.get("inputSchema", {})}
                    for n, e in sorted(own_tools.items())
                },
                "mcp_tools": sorted(
                    mcp_mod.describe().get("registered_tools", [])),
            }}

        if method == "thread/list":
            # Newest-first summaries for history drawers. Pure read.
            try:
                limit = int(params.get("limit", 50))
            except (TypeError, ValueError):
                limit = 50
            me = _caller_agent(client_socket)
            return {
                "id": request_id,
                "result": {"threads": list_threads(limit, agent=me or "")},
            }

        if method == "thread/read":
            # Pure read of one thread (never creates state, unlike resume).
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            try:
                limit = int(params.get("limit", 100))
            except (TypeError, ValueError):
                limit = 100
            me = _caller_agent(client_socket)
            data = read_thread(tid, limit, agent=me or "")
            if data is None:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": f"unknown thread: {tid}"},
                }
            return {"id": request_id, "result": data}

        if method == "initialize":
            # Step 1: pairing-aware handshake. `params.auth` carries
            # {agent_id, token} once paired. Empty store (fresh install,
            # tests): the first client auto-becomes the OWNER and gets
            # its token right here. Otherwise unknown credentials get a
            # pairing code and nothing else works until approved.
            auth = params.get("auth") if isinstance(params, dict) else None
            if not _agents_configured():
                label = ""
                if isinstance(auth, dict):
                    label = str(auth.get("label", ""))
                if not label and isinstance(params, dict):
                    info = params.get("clientInfo")
                    if isinstance(info, dict):
                        label = str(info.get("name", ""))
                agent_id, token, public = agents_mod.mint_agent(
                    label or "first app", owner=True)
                _remember_socket(client_socket, agent_id)
                logger.info(f"paired owner {agent_id} ({public['label']})")
                return {"id": request_id, "result": {
                    "server": "iagent", "protocol": 1,
                    "client": params.get("clientInfo", {}) if isinstance(
                        params, dict) else {},
                    "agent_id": agent_id,
                    "paired": True,
                    "owner": True,
                    "token": token,
                }}
            agent_id, token = "", ""
            if isinstance(auth, dict):
                agent_id = str(auth.get("agent_id", ""))
                token = str(auth.get("token", ""))
            entry = agents_mod.verify(agent_id, token) if agent_id else None
            if entry is None:
                label = ""
                if isinstance(params, dict):
                    info = params.get("clientInfo")
                    if isinstance(info, dict):
                        label = str(info.get("name", ""))
                pending = agents_mod.request_pairing(label or "unknown app")
                return {"id": request_id, "result": {
                    "server": "iagent", "protocol": 1,
                    "client": params.get("clientInfo", {}) if isinstance(
                        params, dict) else {},
                    "paired": False,
                    "pairing_required": True,
                    "pairing": pending,
                }}
            _remember_socket(client_socket, agent_id)
            return {"id": request_id, "result": {
                "server": "iagent", "protocol": 1,
                "client": params.get("clientInfo", {}) if isinstance(
                    params, dict) else {},
                "agent_id": agent_id,
                "paired": True,
                "owner": bool(entry.get("owner")),
            }}

        if method == "thread/archive":
            # Hide from thread/list (restorable). Audit history untouched.
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            me = _caller_agent(client_socket)
            if not archive_thread(tid, agent=me or ""):
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            audit.append({"type": "thread", "action": "archive",
                          "thread_id": tid, "agent_id": me or ""})
            return {"id": request_id, "result": {"thread_id": tid,
                                                 "archived": True}}

        if method == "thread/unarchive":
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            me = _caller_agent(client_socket)
            if not unarchive_thread(tid, agent=me or ""):
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": (
                        f"cannot unarchive: {tid}")},
                }
            audit.append({"type": "thread", "action": "unarchive",
                          "thread_id": tid, "agent_id": me or ""})
            return {"id": request_id, "result": {"thread_id": tid,
                                                 "archived": False}}

        if method == "thread/unsubscribe":
            # Hygiene no-op (client-compat): nothing server-pushed to stop.
            # Object, never a bare bool: strict decoders throw on non-maps.
            return {"id": request_id, "result": {}}

        if method == "thread/import":
            # Migration path (client-compat): bulk-load history from another
            # system, e.g. exported thread/read output. Validated + audited.
            tid = str(params.get("thread_id") or "").strip()
            mode = str(params.get("mode", "fail") or "fail").lower()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            if mode not in ("fail", "overwrite", "append"):
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "mode must be fail|overwrite|append"},
                }
            me = _caller_agent(client_socket)
            try:
                state = import_history(
                    tid, params.get("messages"),
                    base=params.get("base", ""),
                    name=params.get("name", ""), mode=mode,
                    agent=me or "")
            except FileExistsError as e:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            except ValueError as e:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            audit.append({"type": "thread", "action": "import",
                          "thread_id": tid, "turns": state["turns"],
                          "mode": mode, "agent_id": me or "",
                          "source": str(params.get("source", "migration"))[:100]})
            return {"id": request_id, "result": {
                "thread_id": tid, "turns": state["turns"],
                "messages": len(state["history"]), "mode": mode}}

        if method == "thread/name/set":
            tid = str(params.get("thread_id") or "").strip()
            name = str(params.get("name") or "").strip()[:200]
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            me = _caller_agent(client_socket)
            state = load_state(tid)
            if state is None:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            # Cross-agent check: refuse to rename another agent's thread.
            if me and state.get("agent", "") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            state["name"] = name
            save_state(state)
            audit.append({"type": "thread", "action": "name",
                          "thread_id": tid, "name": name, "agent_id": me or ""})
            await broadcast({"type": "thread/name/updated",
                             "thread_id": tid, "name": name})
            return {"id": request_id, "result": {"thread_id": tid,
                                                 "name": name}}

        if method == "thread/resume":
            # Open a thread: state file first, audit replay fallback,
            # brand-new empty thread otherwise (idempotent).
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            me = _caller_agent(client_socket)
            state, source = materialize_state(tid, agent=me or "")
            # Cross-agent check: resume must not open another agent's
            # thread (it returns full history — same sensitivity as
            # read). New threads are stamped to the caller above, so
            # only foreign-owned existing state refuses here.
            if me and source == "state" and state.get("agent", "") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            # Codex parity: opening a thread materializes it. A persisted
            # (even empty) thread means resume/read/open work uniformly,
            # and a resumed id is never silently "new" again later.
            # steer/fork/compact call materialize_state directly, so their
            # unknown-thread errors are unaffected.
            if source == "new":
                try:
                    save_state(state)
                except Exception:
                    logger.exception("failed to persist new thread")
            # Compat note: thread/start carries developerInstructions
            # (persona) and adapters forward them here; persist so every
            # turn (and resume/fork) injects them as system messages.
            dev = params.get("developer_instructions")
            if isinstance(dev, str) and dev.strip():
                dev = dev.strip()[:4000]
                if state.get("developer") != dev:
                    state["developer"] = dev
                    try:
                        save_state(state)
                    except Exception:
                        logger.exception("failed to persist developer prompt")
            return {
                "id": request_id,
                "result": {
                    "thread_id": tid,
                    "source": source,
                    "turns": state["turns"],
                    "base": state.get("base", ""),
                    "history": state["history"],
                    "memories": read_memories(tid),
                    "path": str(state_path(tid)),
                },
            }

        if method == "thread/fork":
            src = str(params.get("thread_id") or "").strip()
            if not src:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            dst = str(params.get("to") or params.get("new_thread_id") or "").strip()
            if not dst:
                dst, n = f"{src}-fork", 2
                while state_path(dst).exists():
                    dst, n = f"{src}-fork-{n}", n + 1
            elif state_path(dst).exists():
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": f"target thread exists: {dst}"},
                }
            me = _caller_agent(client_socket)
            # Cross-agent confinement on the SOURCE: without this any
            # agent can copy another's full history into its own thread
            # and read it. Same indistinguishable refusal.
            _src_state = load_state(src)
            if me and _src_state is not None and \
                    _src_state.get("agent", "") and \
                    _src_state.get("agent", "") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {src}"},
                }
            try:
                forked = fork_state(src, dst, agent=me or "")
            except FileNotFoundError as e:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            audit.append({"type": "fork", "from": src, "to": dst, "thread_id": dst,
                          "agent_id": me or ""})
            return {
                "id": request_id,
                "result": {
                    "thread_id": dst,
                    "from": src,
                    "turns": forked["turns"],
                    "history_len": len(forked["history"]),
                    "base": forked.get("base", ""),
                    "path": str(state_path(dst)),
                },
            }

        if method == "thread/compact":
            # Model summarizes the transcript into a persistent base message;
            # history resets; audit keeps a compact marker.
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            me = _caller_agent(client_socket)
            state, _source = materialize_state(tid, agent=me or "")
            # Cross-agent check: refuse to compact another agent's thread.
            if me and state.get("agent", "") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            if not state["history"]:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "nothing to compact"},
                }
            # Same resolver as a turn: param, then the agent-merged policy,
            # then nothing. This used to read a literal "openai" while the
            # turn path read the policy, so compaction demanded a vendor the
            # user had never configured and failed on a phone whose provider
            # was apinex. One resolver, so the two cannot disagree again.
            _target = resolve_run_target(params,
                                         agent_policy(load_policy(), me or ""))
            provider = open_provider(_target["provider"], me or "")
            if provider is None:
                return {
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": no_provider_message(_target["provider"]),
                    },
                }
            prompt = (
                "Summarize this terminal-agent conversation for continuity. "
                "Keep: decisions made, file paths, commands run, results that "
                "matter, and open tasks. Drop chatter. Be dense and factual."
                "\n\n" + transcript_text(state)
            )
            summary, err = "", None
            async for delta in provider.stream_turn(
                [{"role": "user", "content": prompt}],
                model=_target["model"],
                images=None,
                api="chat",
                stream=False,
            ):
                if delta.get("type") == "text_delta":
                    summary += delta.get("content", "")
                elif delta.get("type") == "error":
                    err = delta.get("message", "provider error")
            if err or not summary.strip():
                return {
                    "id": request_id,
                    "error": {"code": -32700, "message": f"compact failed: {err or 'empty summary'}"},
                }
            prior_turns = state["turns"]
            state["base"] = summary.strip()
            state["history"] = []
            save_state(state)
            audit.append({
                "type": "compact",
                "thread_id": tid,
                "summary": state["base"],
                "turns_before": prior_turns,
                "agent_id": me or "",
            })
            await broadcast({"type": "thread/compacted", "thread_id": tid,
                             "turns_before": prior_turns})
            return {
                "id": request_id,
                "result": {
                    "thread_id": tid,
                    "summary": state["base"],
                    "turns_before": prior_turns,
                    "path": str(state_path(tid)),
                },
            }

        if method == "memories":
            # Client-writable per-thread notes the daemon injects as system msg.
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            me = _caller_agent(client_socket)
            state = load_state(tid)
            if state is None:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": f"unknown thread: {tid}"},
                }
            # Cross-agent check: refuse to access another agent's memories.
            if me and state.get("agent", "") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": f"unknown thread: {tid}"},
                }
            if "content" in params:
                write_memories(tid, str(params.get("content") or ""))
            return {
                "id": request_id,
                "result": {
                    "thread_id": tid,
                    "memories": read_memories(tid),
                    "path": str(memories_path(tid)),
                },
            }

        if method == "turn/steer":
            # Redirect a thread mid-flight, WITHOUT destroying the work in
            # progress.
            #
            # This used to kill the running turn and start a new one from the
            # thread's last *recorded* state, then await that new turn before
            # replying. Three consequences, all of them the bug: the work
            # already done was thrown away, the agent restarted from a stale
            # point, and -- because the reply did not arrive until the new turn
            # finished -- the client's send button stayed disabled for the whole
            # of it, so steering was unreachable in practice.
            #
            # Now: a running turn picks the message up at its next round
            # boundary and carries on. If nothing is running, this is an ordinary
            # first message and starts a turn as before. Either way the reply is
            # immediate.
            tid = str(params.get("thread_id") or "").strip()
            message = str(params.get("message") or "").strip()
            if not tid or not message:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "thread_id and message required"},
                }
            # Cross-agent confinement, before anything else: without it a
            # caller could queue text into (or kill) another agent's live
            # turn, append to its persisted history, and start turns on it.
            # Same indistinguishable-from-missing refusal as everywhere.
            _steer_me = _caller_agent(client_socket)
            _steer_state = load_state(tid)
            if _steer_me and _steer_state is not None and \
                    _steer_state.get("agent", "") and \
                    _steer_state.get("agent", "") != _steer_me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            start = params.get("start", True)
            if isinstance(start, str):
                start = start.lower() not in ("false", "0", "no")
            else:
                start = bool(start)
            prefix = f"{tid}:"
            hit = next(
                (key for key in RUNNING if key.startswith(prefix)), None,
            )
            running = hit is not None and not RUNNING[hit].done()
            if running and not start:
                # The common case: the agent is working and the user has typed
                # something. Hand it over and return. Nothing is cancelled.
                _pending_steer.setdefault(hit, []).append(message)
                audit.append({"type": "steer", "thread_id": tid,
                              "turn_id": hit, "cancelled": False,
                              "applied": "queued", "message": message[:500]})
                logger.info(f"steer queued for running turn {hit}")
                return {"id": request_id, "result": {
                    "thread_id": tid,
                    "turn_id": hit,
                    "turnId": hit,
                    "cancelled": False,
                    "applied": "queued",
                    "steered": True,
                }}
            cancelled = False
            if running:
                # Explicit replacement was asked for: stop the old turn first.
                task = RUNNING[hit]
                kill_turn(hit)
                approvals.drop_thread(tid)
                task.cancel()
                cancelled = True
                try:
                    await asyncio.wait_for(task, timeout=10)
                except (asyncio.CancelledError, asyncio.TimeoutError,
                        Exception):
                    pass
                _pending_steer.pop(hit, None)
            state, source = materialize_state(tid, agent=_steer_me or "")
            if source == "new" and not cancelled:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {tid}"},
                }
            audit.append({"type": "steer", "thread_id": tid,
                          "cancelled": cancelled, "started": start,
                          "message": message[:500],
                          "agent_id": _steer_me or ""})
            if not start:
                state["history"].append({"role": "user", "content": message})
                save_state(state)
                return {"id": request_id, "result": {
                    "thread_id": tid, "cancelled": cancelled,
                    "started": False,
                    "history": state["history"][-2:],
                }}
            passthrough = {
                k: params[k] for k in (
                    "provider", "model", "sandbox", "sandbox_root", "mode",
                    "api", "stream", "images", "skills",
                ) if k in params
            }
            return await handle_turn(
                {"id": request_id, "method": "turn",
                 "params": {"user": message, "thread_id": tid, **passthrough}},
                client_socket,
            )

        if method == "subagent/spawn":
            # Epic P: fan out a background turn on a child thread. Returns
            # immediately; poll subagent/status|result or watch broadcasts.
            parent = str(params.get("thread_id") or "").strip()
            message = str(params.get("message") or "").strip()
            if not parent or not message:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "thread_id and message required"},
                }
            running = [e for e in _subagents.values()
                       if e["status"] == "running"]
            if len(running) >= MAX_SUBAGENTS:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": (
                        f"too many running subagents ({MAX_SUBAGENTS})")},
                }
            child = _child_thread(parent)
            me = _caller_agent(client_socket)
            _spawn_parent = load_state(parent)
            # Cross-agent confinement: spawning on another agent's
            # thread would run background work under their quota and
            # stream phantom subagents into their chat. Absent parent
            # state stays spawnable (fresh child, owned below).
            if me and _spawn_parent is not None and \
                    _spawn_parent.get("agent", "") and \
                    _spawn_parent.get("agent", "") != me:
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown thread: {parent}"},
                }
            if params.get("context") == "fork":
                try:
                    fork_state(parent, child, agent=me or "")
                except FileNotFoundError as e:
                    return {
                        "id": request_id,
                        "error": {"code": -32602, "message": str(e)},
                    }
            turn_params = {"user": message, "thread_id": child}
            # Attribute the child turn for quotas (and stamp its thread):
            # the parent thread's owner first, the caller as fallback.
            # handle_turn only honors _agent on socket-less internal
            # calls, so external callers cannot spoof this.
            _parent_state = load_state(parent)
            turn_params["_agent"] = (
                (_parent_state.get("agent", "") if _parent_state else "")
                or me or "")
            for key in ("provider", "model", "sandbox", "sandbox_root",
                        "mode", "api", "stream", "images", "skills",
                        "max_rounds"):
                if key in params:
                    turn_params[key] = params[key]
            turn_params.setdefault("max_rounds", SUBAGENT_DEFAULT_ROUNDS)
            entry = {"id": child, "parent": parent, "thread_id": child,
                     "status": "running", "result": None, "task": None,
                     "agent": turn_params.get("_agent", "") or me or ""}
            _subagents[child] = entry
            entry["task"] = asyncio.create_task(_run_subagent(
                entry, {"id": request_id, "method": "turn",
                        "params": turn_params}))
            audit.append({"type": "subagent", "action": "spawn",
                          "thread_id": child, "parent": parent})
            return {"id": request_id, "result": {
                "id": child, "thread_id": child, "parent": parent,
                "status": "running"}}

        def _subagent_entry(sid: str):
            entry = _subagents.get(str(sid or ""))
            if entry is None:
                return None, {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown subagent: {sid}"},
                }
            # Cross-agent confinement: entries record their owner's agent
            # at spawn; legacy entries without one stay visible.
            _em = _caller_agent(client_socket)
            if _em and entry.get("agent") and entry.get("agent") != _em:
                return None, {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": f"unknown subagent: {sid}"},
                }
            return entry, None

        if method == "subagent/status":
            entry, err = _subagent_entry(params.get("id"))
            if err:
                return err
            return {"id": request_id, "result": {
                "id": entry["id"], "status": entry["status"],
                "thread_id": entry["thread_id"], "parent": entry["parent"]}}

        if method == "subagent/result":
            entry, err = _subagent_entry(params.get("id"))
            if err:
                return err
            return {"id": request_id, "result": {
                "id": entry["id"], "status": entry["status"],
                "thread_id": entry["thread_id"], "parent": entry["parent"],
                "result": entry["result"]}}

        if method == "subagent/list":
            parent = str(params.get("thread_id") or "")
            _lm = _caller_agent(client_socket)
            return {"id": request_id, "result": {"subagents": [
                {"id": e["id"], "status": e["status"],
                 "thread_id": e["thread_id"], "parent": e["parent"]}
                for e in _subagents.values()
                if (not parent or e["parent"] == parent)
                and not (_lm and e.get("agent") and e["agent"] != _lm)
            ]}}

        if method == "subagent/cancel":
            entry, err = _subagent_entry(params.get("id"))
            if err:
                return err
            if entry["status"] != "running":
                return {"id": request_id, "result": {
                    "id": entry["id"], "cancelled": False,
                    "status": entry["status"]}}
            child = entry["thread_id"]
            cancelled = False
            hit = next((k for k in RUNNING if k.startswith(f"{child}:")),
                       None)
            if hit is not None:
                task = RUNNING[hit]
                if not task.done():
                    kill_turn(hit)
                    approvals.drop_thread(child)
                    task.cancel()
                    cancelled = True
            if not cancelled:
                task = entry.get("task")
                if task is not None and not task.done():
                    task.cancel()
                    cancelled = True
            return {"id": request_id, "result": {
                "id": entry["id"], "cancelled": cancelled,
                "status": entry["status"]}}

        if method == "command/exec":
            # Out-of-band shell for the owning client (client-compat):
            # quota-free, model-free device commands (attachment pulls,
            # provider probes, wake-lock tolerance). No turn, no approval,
            # no quota — the caller is trusted UI acting on user taps, same
            # loopback trust as the rest of this daemon. Every call IS
            # audited, with argv redacted past argv[0] (callers pass keys
            # in argv).
            cmd = params.get("command", [])
            if (not isinstance(cmd, list) or not cmd
                    or not all(isinstance(x, str) for x in cmd)):
                return {
                    "id": request_id,
                    "error": {"code": -32602,
                             "message": "command must be a string array"},
                }
            home = str(engine_home())
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=home,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=60)
                    rc = proc.returncode
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    stdout, stderr, rc = b"", b"timeout after 60s", 124
            except FileNotFoundError:
                stdout, stderr, rc = (
                    b"", f"not on PATH: {cmd[0]}".encode(), 127)
            except Exception as e:
                stdout, stderr, rc = b"", str(e).encode(), 126
            out = stdout.decode(errors="replace")[:65536]
            err = stderr.decode(errors="replace")[:65536]
            try:
                audit.append({"type": "exec", "argv0": cmd[0],
                              "argc": len(cmd), "exit_code": rc,
                              "agent_id": _caller_agent(client_socket) or ""})
            except Exception:
                pass
            return {"id": request_id, "result": {
                "exitCode": rc, "stdout": out, "stderr": err}}

        if method == "cancel":
            tid = params.get("turn_id") or params.get("id") or ""
            task = RUNNING.get(tid)
            if task is None and params.get("thread_id"):
                prefix = f"{params['thread_id']}:"
                hit = next(
                    (key for key in RUNNING if key.startswith(prefix)),
                    None,
                )
                if hit is not None:
                    tid, task = hit, RUNNING[hit]
            if task is None:
                return {"id": request_id, "result": False}
            if not _may_signal(_caller_agent(client_socket), str(tid or ""),
                               str(params.get("thread_id") or "")):
                return {"id": request_id, "result": False}
            # Kill child processes BEFORE cancelling: the turn's finally
            # blocks untrack first, which would hide them from kill_turn.
            kill_turn(tid)
            task.cancel()
            return {"id": request_id, "result": True}

        return {
            "id": request_id,
            "error": {"code": -32601, "message": f"unsupported method: {method}"},
        }

    except Exception as e:
        logger.exception("Request handling failed")
        return {
            "id": payload.get("id"),
            "error": {"code": -32700, "message": str(e)},
        }


async def stream_turn(
    provider: BaseProvider | None,
    messages: list[dict],
    model: str,
    images: list[str] | None = None,
    api: str = "chat",
    stream: bool = True,
) -> AsyncIterator[dict]:
    logger.info(f"stream_turn called with provider={provider}, model={model}")
    if not provider:
        # Not a text_delta: a bare sentence here is indistinguishable from the
        # model speaking, and the old one named the *model* -- "No provider
        # found: gpt-4o" -- which reads as a vendor problem rather than "you
        # have not configured one". An error frame ends the turn properly and
        # carries the sentence the user can act on.
        logger.info("stream_turn: no provider adapter resolved")
        yield {"type": "error", "message": no_provider_message("")}
        yield {"type": "complete"}
        return

    try:
        async for delta in provider.stream_turn(
            messages, model=model, images=images, api=api, stream=stream
        ):
            yield delta
    except Exception as e:
        yield {"type": "error", "message": str(e)}
        yield {"type": "complete"}


async def _send_delta(delta: dict, turn_id: str = "",
                      thread_id: str = "", round_no: int = 0) -> None:
    logger.debug(f"_send_delta called with delta={delta}")
    content = delta.get("content", delta.get("message", ""))
    logger.debug(f"Broadcasting: type={delta.get('type')}, content={content}")
    payload = {"type": delta.get("type"), "content": content}
    # Error frames keep their `message` key too: her adapter reads
    # frame['message'] and falls back to a generic "turn failed" that
    # buries the real provider error.
    if isinstance(delta.get("message"), str) and delta["message"]:
        payload["message"] = delta["message"]
    if turn_id:
        payload["turn_id"] = turn_id
    if thread_id:
        payload["thread_id"] = thread_id
    # Which pass of the agentic loop produced this text. Round 1 narrates
    # ("let me look at X"), the last round answers. A client cannot tell those
    # apart from the text alone, and guessing wrong either buries the answer
    # in a log or floods the chat with narration -- so the daemon, which knows
    # whether it will loop again, says so. See `_round_item_key` in the
    # compat client's adapter, which keys one message item per round.
    payload["round"] = round_no
    await broadcast(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Interlux agent daemon")
    parser.add_argument("-p", "--port", type=int, default=4600)
    args = parser.parse_args()

    logger.info(f"Starting iagent on port {args.port}")

    async def _run() -> None:
        try:
            # Epic E: spawn configured MCP servers before accepting requests.
            started = await mcp_mod.start_all()
            if started:
                logger.info(f"MCP tools ready: {started}")
            # Step 2 migration: stamp existing threads to the agent that
            # actually worked them. Pre-pairing traffic came from AI
            # clients, never from the owner app (it only manages
            # pairing), so the first NON-owner agent wins; owner-only
            # stores fall back to the first agent. One-time;
            # already-stamped threads are left alone.
            agents = agents_mod.load_agents()
            if agents:
                worker = next(
                    (aid for aid, e in agents.items()
                     if not (isinstance(e, dict) and e.get("owner"))),
                    next(iter(agents)),
                )
                count = threads_mod.migrate_threads_to_agent(worker)
                if count:
                    logger.info(f"migrated {count} threads to agent {worker}")
                # Step 3 migration: pre-pairing provider keys belonged to the
                # first client too. Same worker rule; names only in the log,
                # values. A loud warning if top-level secrets reappear
                # later (post-migration drift back into the global file).
                moved = pconfig_mod.migrate_provider_keys(worker)
                if moved:
                    logger.info(f"migrated provider keys to agent {worker}: "
                                f"{moved}")
                drift = pconfig_mod.top_level_key_names()
                if drift:
                    logger.warning(
                        "top-level provider secrets bypass per-agent "
                        f"scoping: {drift}")
            on_disconnect.append(drop_client_waits)
            # Must precede _drop_socket: that pops the socket->agent map that
            # _cancel_runs_on_disconnect reads to find the client's turns.
            on_disconnect.append(_cancel_runs_on_disconnect)
            on_disconnect.append(_drop_socket)
            on_connect.append(_resend_approvals)
            transport = Transport(handle_request, args.port)
            await transport.start()
        finally:
            try:
                await mcp_mod.stop_all()
            except Exception:
                logger.exception("mcp shutdown failed")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
