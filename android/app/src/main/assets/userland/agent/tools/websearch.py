"""Epic F: web_search tool (read-only, no approval by default).

Config (never hardcoded): a "web_search" section in the provider config —
INTERLUX_PROVIDERS env JSON first, then ~/.interlux/agent/providers.json:
  {"web_search": {"base_url": "https://api.tavily.com",
                  "api_key": "...", "path": "/search",
                  "extra": {"search_depth": "basic"}}}

Request shape is Tavily-compatible: {"api_key","query","max_results",...extra}.
Any endpoint answering {"answer", "results":[{title,url,content}]} works.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path

TIMEOUT = 20
RESULT_CAP = 10
SNIPPET_CAP = 500


def _section() -> dict:
    try:
        env = json.loads(os.environ.get("INTERLUX_PROVIDERS", "{}"))
        if isinstance(env, dict) and isinstance(env.get("web_search"), dict):
            return env["web_search"]
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        home = os.environ.get("HOME") or str(Path.home())
        path = Path(home) / ".interlux/agent/providers.json"
        if path.exists():
            cfg = json.loads(path.read_text())
            if isinstance(cfg, dict) and isinstance(cfg.get("web_search"), dict):
                return cfg["web_search"]
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


async def web_search(query: str = "", count: int = 5) -> dict:
    """Web search. Returns answer + [{title, url, snippet}]."""
    query = str(query or "").strip()
    if not query:
        return {"status": "error", "message": "query is required"}
    cfg = _section()
    base = str(cfg.get("base_url", "")).rstrip("/")
    key = str(cfg.get("api_key", ""))
    if not base or not key:
        return {
            "status": "error",
            "message": (
                "web_search is not configured: add a web_search section "
                "{base_url, api_key} to the provider config"
            ),
        }
    try:
        count = max(1, min(int(count or 5), RESULT_CAP))
    except (TypeError, ValueError):
        count = 5
    body = {"api_key": key, "query": query, "max_results": count}
    extra = cfg.get("extra")
    if isinstance(extra, dict):
        body.update(extra)
    url = base + str(cfg.get("path", "/search"))
    try:
        data = await asyncio.to_thread(_post, url, body, TIMEOUT)
    except Exception as e:
        return {"status": "error", "message": f"web_search request failed: {e}"}
    if not isinstance(data, dict):
        return {"status": "error", "message": "web_search: bad response shape"}
    results = []
    for item in (data.get("results") or [])[:RESULT_CAP]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "") or "")
        results.append({
            "title": str(item.get("title", "") or ""),
            "url": str(item.get("url", "") or ""),
            "snippet": content[:SNIPPET_CAP],
        })
    return {
        "status": "success",
        "query": query,
        "answer": str(data.get("answer", "") or ""),
        "results": results,
    }
