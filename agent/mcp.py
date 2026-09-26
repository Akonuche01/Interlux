"""Epic E: MCP client — spawn stdio MCP servers, proxy their tools.

Config (wipe-proof): ~/.interlux/agent/mcp.json
  {"servers": {"name": {"command": ["python3", "-u", "server.py"],
                         "env": {...}, "cwd": "..."}}}

Transport: newline-delimited JSON-RPC 2.0 over stdio (MCP stdio transport).
Handshake: initialize -> notifications/initialized -> tools/list.
Registered MCP tools become `mcp_<server>__<tool>` in the normal registry:
same approval path (EXTRA_WRITE: always asks, session-grantable via Epic B),
same audit trail, same sandbox accounting. Failures are logged and surfaced
through the `mcp` RPC — never silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .transport import broadcast

logger = logging.getLogger("mcp")

CONFIG_DIR = Path(
    os.environ.get("INTERLUX_AGENT_CONFIG", "~/.interlux/agent")
).expanduser()
CONFIG_FILE = CONFIG_DIR / "mcp.json"

PROTOCOL_VERSION = "2025-06-18"
HANDSHAKE_TIMEOUT = 15.0
CALL_TIMEOUT = 30.0


def config_path() -> Path:
    return CONFIG_FILE


def load_config() -> dict:
    try:
        if not CONFIG_FILE.exists():
            return {}
        cfg = json.loads(CONFIG_FILE.read_text())
        if not isinstance(cfg, dict):
            return {}
        servers = cfg.get("servers")
        return {"servers": servers} if isinstance(servers, dict) else {}
    except Exception as e:
        logger.error(f"mcp.json unreadable: {e}")
        return {}


class McpServer:
    """One stdio MCP server: handshake, request routing, tool proxying."""

    def __init__(self, name: str, command: list, env: dict | None = None,
                 cwd: str | None = None):
        self.name = name
        self.command = [str(c) for c in command]
        self.env = dict(env or {})
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 1
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.stderr_tail: list[str] = []
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self.error: str = ""
        self._closing = False

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if self.proc is None or self.proc.returncode is not None:
            return "dead"
        return "running"

    async def start(self) -> None:
        """Spawn + handshake + tools/list. Raises on failure (logged by caller)."""
        env = {**os.environ, **self.env}
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.cwd,
        )
        self._closing = False
        self.reader_task = asyncio.create_task(self._read_loop())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        init = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "iagent", "version": "1.0"},
            },
            timeout=HANDSHAKE_TIMEOUT,
        )
        self.server_info = init.get("serverInfo", {}) if isinstance(init, dict) else {}
        await self.notify("notifications/initialized")
        listed = await self.request("tools/list", {}, timeout=HANDSHAKE_TIMEOUT)
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        logger.info(
            f"mcp[{self.name}] up: {self.server_info.get('name', '?')} "
            f"tools={len(self.tools)}"
        )

    async def request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT):
        if self.proc is None or self.proc.returncode is not None:
            raise RuntimeError(f"mcp[{self.name}] not running")
        rid = self.next_id
        self.next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
        except Exception as e:
            logger.error(f"mcp[{self.name}] notify failed: {e}")

    async def _reply(self, rid, result=None, error=None) -> None:
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        try:
            if self.proc and self.proc.stdin:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode())
                await self.proc.stdin.drain()
        except Exception:
            pass

    async def _drain_stderr(self) -> None:
        """Keep stderr flowing (PIPE would block a chatty server) + keep a tail."""
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                self.stderr_tail.append(line.decode(errors="replace")[:300])
                self.stderr_tail = self.stderr_tail[-50:]
        except Exception:
            pass

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Non-JSON chatter on stdout: log, never crash the loop.
                    logger.debug(f"mcp[{self.name}] stdout: {line[:200]!r}")
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self.pending.get(msg.get("id"))
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(
                                    f"mcp[{self.name}] error: {msg['error']}"
                                )
                            )
                        else:
                            fut.set_result(msg.get("result"))
                elif "id" in msg and "method" in msg:
                    # Server-initiated request: answer ping, refuse the rest.
                    if msg["method"] == "ping":
                        await self._reply(msg["id"], {})
                    else:
                        await self._reply(msg["id"], error={
                            "code": -32601,
                            "message": f"method not supported: {msg['method']}",
                        })
                # notifications (and anything else) are ignored
        except Exception as e:
            logger.error(f"mcp[{self.name}] read loop: {e}")
        finally:
            # Fail every in-flight request loudly.
            for fut in list(self.pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(f"mcp[{self.name}] disconnected"))
            self.pending.clear()
            if not self._closing:
                self.error = "server exited"
                logger.error(f"mcp[{self.name}] server exited")

    async def list_tools(self) -> list[dict]:
        if self.status != "running":
            return []
        listed = await self.request("tools/list", {})
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return self.tools

    async def call_tool(self, tool: str, arguments: dict) -> dict:
        """MCP tools/call -> normalized daemon tool result."""
        res = await self.request(
            "tools/call", {"name": tool, "arguments": arguments or {}}
        )
        res = res if isinstance(res, dict) else {}
        parts = []
        for item in res.get("content", []) or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        text = "\n".join(parts) if parts else json.dumps(res, ensure_ascii=False)
        if res.get("isError"):
            return {
                "status": "error",
                "server": self.name,
                "tool": tool,
                "message": text[:4000],
            }
        return {
            "status": "success",
            "server": self.name,
            "tool": tool,
            "output": text[:20000],
        }

    async def stop(self) -> None:
        self._closing = True
        if self.proc is None:
            return
        try:
            if self.proc.returncode is None:
                self.proc.stdin.close()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await self.proc.wait()
        except Exception as e:
            logger.error(f"mcp[{self.name}] stop: {e}")
        finally:
            self.proc = None
            for task in (self.reader_task, self.stderr_task):
                if task:
                    task.cancel()
            self.reader_task = None
            self.stderr_task = None


# name -> server; tool key -> (server_name, mcp_tool_name)
_servers: dict[str, McpServer] = {}
_tool_index: dict[str, tuple[str, str]] = {}
_registration_hook = None


def set_registration_hook(hook) -> None:
    """serve.py injects fn(add_keys, remove_keys) -> registered tool names."""
    global _registration_hook
    _registration_hook = hook


def _tool_key(server: str, tool: str) -> str:
    safe_s = "".join(c if c.isalnum() or c in "-_" else "_" for c in server)
    safe_t = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool)
    return f"mcp_{safe_s}__{safe_t}"


async def start_all() -> list[str]:
    """Start/refresh servers from config. Returns newly registered tools."""
    cfg = load_config()
    wanted = cfg.get("servers", {})
    registered: list[str] = []

    # Stop servers removed from config (and unregister their proxy tools).
    removed_keys: list[str] = []
    for name in list(_servers):
        if name not in wanted:
            await _servers[name].stop()
            for key, (sn, _t) in list(_tool_index.items()):
                if sn == name:
                    _tool_index.pop(key, None)
                    removed_keys.append(key)
            del _servers[name]
            try:
                await broadcast({"type": "mcpServer/stopped", "server": name})
            except Exception:
                logger.exception(f"mcp[{name}] stopped broadcast failed")

    for name, spec in wanted.items():
        if not isinstance(spec, dict) or not spec.get("command"):
            logger.error(f"mcp[{name}] invalid config (need command[])")
            continue
        srv = _servers.get(name)
        if srv is not None and srv.status == "running":
            try:
                await srv.list_tools()  # hot-refresh tool list
            except Exception as e:
                logger.error(f"mcp[{name}] tools/list refresh failed: {e}")
                continue
        else:
            srv = McpServer(
                name,
                spec["command"],
                env=spec.get("env"),
                cwd=spec.get("cwd"),
            )
            _servers[name] = srv
            try:
                await srv.start()
            except Exception as e:
                tail = "; ".join(srv.stderr_tail[-3:])
                srv.error = (str(e)[:400] + (f" | stderr: {tail}" if tail else ""))[:500]
                logger.error(f"mcp[{name}] failed to start: {srv.error}")
                continue
            try:
                await broadcast({
                    "type": "mcpServer/started",
                    "server": name,
                    "tools": [t.get("name", "") for t in srv.tools],
                })
            except Exception:
                logger.exception(f"mcp[{name}] started broadcast failed")
        for t in srv.tools:
            key = _tool_key(name, t["name"])
            if key in _tool_index:
                continue
            _tool_index[key] = (name, t["name"])
            registered.append(key)

    if (registered or removed_keys) and _registration_hook is not None:
        try:
            _registration_hook(registered, removed_keys)
        except Exception:
            logger.exception("mcp tool registration hook failed")
    return registered


async def stop_all() -> None:
    for name, srv in list(_servers.items()):
        await srv.stop()
        try:
            await broadcast({"type": "mcpServer/stopped", "server": name})
        except Exception:
            logger.exception(f"mcp[{name}] stopped broadcast failed")
    _servers.clear()
    _tool_index.clear()


def is_mcp_tool(tool_name: str) -> bool:
    return tool_name in _tool_index


async def call(tool_key: str, arguments: dict) -> dict:
    """Registry proxy: run an MCP tool by its registered key."""
    entry = _tool_index.get(tool_key)
    if entry is None:
        return {"status": "error", "message": f"unknown mcp tool: {tool_key}"}
    server_name, mcp_tool = entry
    srv = _servers.get(server_name)
    if srv is None or srv.status != "running":
        return {
            "status": "error",
            "server": server_name,
            "tool": mcp_tool,
            "message": f"mcp server not running: {server_name}",
        }
    try:
        return await srv.call_tool(mcp_tool, arguments)
    except Exception as e:
        return {
            "status": "error",
            "server": server_name,
            "tool": mcp_tool,
            "message": str(e)[:2000],
        }


def describe() -> dict:
    """`mcp` RPC payload: status of every configured server."""
    cfg = load_config()
    configured = set(cfg.get("servers", {}))
    out = []
    for name in sorted(configured | set(_servers)):
        srv = _servers.get(name)
        if srv is None:
            out.append({"name": name, "status": "not started", "tools": [],
                        "error": ""})
        else:
            out.append({
                "name": name,
                "status": srv.status,
                "tools": [t.get("name", "") for t in srv.tools],
                "server_info": srv.server_info,
                "error": srv.error,
            })
    return {"servers": out, "config": str(CONFIG_FILE),
            "registered_tools": sorted(_tool_index)}
