from __future__ import annotations

import argparse
import asyncio
import json
import os
import logging
from pathlib import Path
from typing import AsyncIterator

from .audit import AuditLog
from . import mcp as mcp_mod
from .policy import (
    GRANT_SCOPES,
    SANDBOX_MODES,
    allows,
    load_policy,
    policy_path,
    record_grant,
    resolve_sandbox,
    revoke,
    save_policy,
    sandbox_ctx,
)
from .providers import PROVIDERS, BaseProvider
from .skills import (
    get_skill as skills_get,
    list_skills as skills_list,
    refresh as skills_refresh,
    register_skill_tool,
    scan_skill_plugins,
    skill_messages,
    USER_DIR as SKILLS_USER_DIR,
)
from .threads import (
    build_messages,
    fork_state,
    load_state,
    materialize_state,
    memories_path,
    new_state,
    read_memories,
    record_turn,
    save_state,
    state_path,
    transcript_text,
    write_memories,
)
from .tools import EXTRA_WRITE, TOOLS, approval_label, needs_approval
from .tools.plugins import scan_plugins
from .tools.track import current_turn as turn_ctx
from .tools.track import kill_turn
from .transport import Transport, broadcast

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


class ApprovalManager:
    def __init__(self):
        # fid -> {"future", "thread", "tool", "label"}
        self.pending: dict[str, dict] = {}

    def request_approval(
        self, thread_id: str, params: dict, tool: str = "", label: str = ""
    ) -> asyncio.Future:
        fid = params["id"]
        entry: dict = {
            "future": asyncio.Future(),
            "thread": thread_id,
            "tool": tool,
            "label": label,
        }
        self.pending[fid] = entry
        return entry["future"]

    def approve(self, fid: str, decision: str, scope: str = "turn") -> bool:
        entry = self.pending.pop(fid, None)
        if entry is None:
            return False
        granted = str(decision or "").strip().lower() in (
            "approve", "accept", "allow", "yes", "always", "true",
        )
        entry["future"].set_result(granted)
        if granted and scope == "session" and entry.get("tool"):
            try:
                policy = load_policy()
                what = record_grant(
                    policy, entry["thread"], entry["tool"], entry.get("label", "")
                )
                save_policy(policy)
                audit.append({
                    "type": "grant",
                    "thread_id": entry["thread"],
                    "tool": entry["tool"],
                    "granted": what,
                    "scope": "session",
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


approvals = ApprovalManager()

RUNNING: dict[str, asyncio.Task] = {}


def load_provider(name: str, config: dict) -> BaseProvider | None:
    if name not in PROVIDERS:
        return None
    if not isinstance(config, dict):
        config = {}
    return PROVIDERS[name](
        config.get("api_key", ""),
        config.get("base_url"),
    )


def provider_config() -> dict:
    """Turn-env override first, then wipe-proof home, then bundled file."""
    config = json.loads(os.environ.get("INTERLUX_PROVIDERS", "{}"))
    if config:
        return config
    home = os.environ.get("HOME") or str(Path.home())
    candidates = [
        Path(home) / ".interlux/agent/providers.json",
        Path(__file__).parent / "providers.json",
    ]
    for config_file in candidates:
        try:
            if config_file.exists():
                return json.loads(config_file.read_text())
        except Exception:
            pass
    return {}


def make_provider(name: str) -> BaseProvider | None:
    return load_provider(name, provider_config().get(name, {}))


async def run_tool_loop(turn: dict, client_socket) -> None:
    """Execute tool calls with approval flow."""
    tool_calls = turn.get("tool_calls", [])
    logger.info(f"Processing {len(tool_calls)} tool call(s)")

    sandbox_mode = turn.get("sandbox", "full") or "full"
    thread_id = turn.get("thread_id", "default")
    # Standing grants live on disk and survive turns; load once per loop.
    policy = load_policy()

    for call in tool_calls:
        tool_name = call.get("tool", "shell")
        params = call.get("parameters", {})
        logger.info(f"Tool: {tool_name}, params: {params}")

        if needs_approval(tool_name, params):
            if sandbox_mode == "read-only":
                logger.info(f"Blocked by read-only sandbox: {tool_name}")
                blocked = {
                    "tool": tool_name,
                    "status": "error",
                    "message": f"write tool {tool_name!r} blocked: sandbox is read-only",
                }
                turn["output"].append(blocked)
                await broadcast({"type": "tool_result", **blocked})
                continue

            label = approval_label(tool_name, params)
            if allows(policy, thread_id, tool_name, label):
                logger.info(
                    f"Standing session grant covers {tool_name} ({label!r}) "
                    f"— skipping approval"
                )
            else:
                fid = f"{thread_id}:{len(approvals.pending)}"
                approval_params = {"command": label, "id": fid}

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
                    continue
                logger.info("Approval received!")

        logger.info(f"Executing tool: {tool_name}")
        if tool_name in TOOLS:
            try:
                result = await TOOLS[tool_name](**params)
                logger.info(f"Tool result: {result}")
                tool_output = {"tool": tool_name, "result": result}
                turn["output"].append(tool_output)
                await broadcast({"type": "tool_result", **tool_output})
            except Exception as e:
                logger.error(f"Tool execution failed: {e}")
                tool_output = {"tool": tool_name, "status": "error", "message": str(e)}
                turn["output"].append(tool_output)
                await broadcast({"type": "tool_result", **tool_output})
        else:
            logger.error(f"Unknown tool: {tool_name}")
            tool_output = {"tool": tool_name, "status": "error", "message": f"unknown tool: {tool_name}"}
            turn["output"].append(tool_output)
            await broadcast({"type": "tool_result", **tool_output})


async def _execute_turn(
    payload: dict,
    params: dict,
    user_msg: str,
    provider: BaseProvider | None,
    model: str,
    thread_id: str,
    turn: dict,
    client_socket,
    messages: list[dict],
    state: dict,
) -> dict:
    images = params.get("images") or []
    api = params.get("api", "chat")
    if api not in ("chat", "responses"):
        api = "chat"
    stream = params.get("stream", True)
    if not isinstance(stream, bool):
        stream = True

    turn["output"] = []
    has_error = False

    async for delta in stream_turn(provider, messages, model, images, api, stream):
        if delta.get("type") == "usage" and isinstance(delta.get("usage"), dict):
            # Epic F: cost visibility. Live broadcast; kept out of output
            # so it never pollutes history or the model context.
            turn["usage"] = delta["usage"]
            await broadcast({
                "type": "usage", "turn_id": turn["id"], "usage": delta["usage"],
            })
            continue
        turn["output"].append(delta)
        if delta.get("type") == "error":
            has_error = True
        await _send_delta(delta)

    # Parse tool calls from provider response
    content = ""
    for item in turn["output"]:
        if item.get("type") == "text_delta":
            content += item.get("content", "")

    if content:
        logger.info(f"Content from provider: {content[:200]}")

    if has_error:
        turn["complete"] = True
        try:
            audit.append({"type": "turn", "thread_id": thread_id, **turn})
        except Exception:
            pass
        result = {"turn_id": turn["id"]}
        if turn.get("usage"):
            result["usage"] = turn["usage"]
        return {"id": payload.get("id"), "result": result}

    # Parse tool calls from content
    if content:
        import re
        # Look for JSON blocks like ```json {...} ```
        json_blocks = re.findall(r'```json\s*(.*?)\s*```', content, re.DOTALL)
        if json_blocks:
            try:
                tool_calls = json.loads(json_blocks[0])
                if isinstance(tool_calls, list):
                    turn["tool_calls"] = tool_calls
                elif "tool_calls" in tool_calls:
                    turn["tool_calls"] = tool_calls["tool_calls"]
            except json.JSONDecodeError:
                pass

    # Fallback: Generate tool call from user message if it starts with "run "
    if not turn.get("tool_calls") and user_msg.strip().lower().startswith("run "):
        command = user_msg.strip()[4:].strip()
        turn["tool_calls"] = [{
            "tool": "shell",
            "parameters": {"command": command}
        }]

    await run_tool_loop(turn, client_socket)

    turn["complete"] = True
    # Epic C: persist history so later turns / resume see this conversation.
    try:
        record_turn(state, user_msg, turn["output"])
    except Exception:
        logger.exception("failed to persist thread state")
    try:
        audit.append({"type": "turn", "thread_id": thread_id, **turn})
    except Exception:
        pass
    await broadcast({"type": "complete", "turn_id": turn["id"]})

    result = {"turn_id": turn["id"]}
    if turn.get("usage"):
        result["usage"] = turn["usage"]
    return {"id": payload.get("id"), "result": result}


async def handle_turn(payload: dict, client_socket) -> dict:
    logger.info(f"handle_turn called with payload: {payload}")
    params = payload.get("params", {})
    user_msg = params.get("user", "")
    provider_name = params.get("provider", "openai")
    model = params.get("model", "gpt-4o")

    thread_id = params.get("thread_id", f"thread-{len(audit.read_last())}")
    # Epic C: thread state = history + base + turn counter (wipe-proof home).
    state = load_state(thread_id) or new_state(thread_id)
    # Epic B: turn param wins; policy.json default otherwise.
    sandbox = resolve_sandbox(params, load_policy())
    turn = {
        "id": f"{thread_id}:{state['turns']}",
        "user": user_msg,
        "thread_id": thread_id,
        "sandbox": sandbox["mode"],
    }

    provider = make_provider(provider_name)

    RUNNING[turn["id"]] = asyncio.current_task()
    token = turn_ctx.set(thread_id)
    sandbox_token = sandbox_ctx.set(sandbox)
    try:
        # Epic D: skill catalog (+ requested bodies) ride as system messages.
        return await _execute_turn(
            payload, params, user_msg, provider, model,
            thread_id, turn, client_socket,
            build_messages(
                state, thread_id, user_msg, skill_messages(params.get("skills"))
            ),
            state,
        )
    except asyncio.CancelledError:
        kill_turn(turn["id"])
        approvals.drop_thread(thread_id)
        turn["cancelled"] = True
        try:
            audit.append({"type": "turn", "thread_id": thread_id, **turn})
        except Exception:
            pass
        await broadcast({"type": "cancelled", "turn_id": turn["id"]})
        return {
            "id": payload.get("id"),
            "result": {"turn_id": turn["id"], "cancelled": True},
        }
    finally:
        RUNNING.pop(turn["id"], None)
        sandbox_ctx.reset(sandbox_token)
        turn_ctx.reset(token)


async def handle_request(payload: dict, client_socket) -> dict:
    """Process JSON-RPC request."""
    try:
        method = payload.get("method")
        params = payload.get("params", {})
        request_id = payload.get("id")

        if method == "capabilities":
            return {
                "id": request_id,
                "result": {
                    "protocol": 1,
                    "methods": [
                        "turn", "cancel", "capabilities", "approve",
                        "tools_refresh", "policy", "skills", "mcp",
                        "thread/resume", "thread/fork", "thread/compact",
                        "memories",
                    ],
                    "stream": True,
                    "providers": list(PROVIDERS.keys()),
                    "tools": sorted(TOOLS.keys()),
                    "media": ["image"],
                    "apis": ["chat", "responses"],
                    "sandboxes": list(SANDBOX_MODES),
                    "approve_scopes": list(GRANT_SCOPES),
                },
            }

        if method == "tools_refresh":
            # Plugins, skills (incl. their tool dirs), AND MCP servers.
            loaded = scan_plugins(PLUGIN_DIR, TOOLS, EXTRA_WRITE)
            skills_refresh()
            loaded += scan_skill_plugins(TOOLS, EXTRA_WRITE)
            loaded += await mcp_mod.start_all()
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
            if approvals.approve(fid, decision, scope):
                return {"id": request_id, "result": True}
            return {"id": request_id, "error": {"code": -32602, "message": "unknown fid"}}

        if method == "policy":
            # Get/set sandbox default + revoke standing grants.
            policy = load_policy()
            revoked = None
            changed = False
            if params.get("sandbox") in SANDBOX_MODES:
                policy["sandbox"] = params["sandbox"]
                changed = True
            if "revoke" in params:
                revoked = revoke(policy, str(params["revoke"]))
                changed = True
            if changed:
                save_policy(policy)
            result = {"policy": policy, "path": str(policy_path())}
            if revoked is not None:
                result["revoked"] = revoked
            return {"id": request_id, "result": result}

        if method == "thread/resume":
            # Open a thread: state file first, audit replay fallback,
            # brand-new empty thread otherwise (idempotent).
            tid = str(params.get("thread_id") or "").strip()
            if not tid:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "thread_id required"},
                }
            state, source = materialize_state(tid)
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
            try:
                forked = fork_state(src, dst)
            except FileNotFoundError as e:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            audit.append({"type": "fork", "from": src, "to": dst, "thread_id": dst})
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
            state, _source = materialize_state(tid)
            if not state["history"]:
                return {
                    "id": request_id,
                    "error": {"code": -32602, "message": "nothing to compact"},
                }
            provider = make_provider(str(params.get("provider", "openai")))
            if provider is None:
                return {
                    "id": request_id,
                    "error": {
                        "code": -32602,
                        "message": f"provider not available: {params.get('provider', 'openai')}",
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
                model=str(params.get("model", "gpt-4o")),
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
            })
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
        logger.info("No provider found, yielding error message")
        yield {"type": "text_delta", "content": f"No provider found: {model}"}
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


async def _send_delta(delta: dict) -> None:
    logger.info(f"_send_delta called with delta={delta}")
    content = delta.get("content", delta.get("message", ""))
    logger.info(f"Broadcasting: type={delta.get('type')}, content={content}")
    await broadcast({"type": delta.get("type"), "content": content})


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
