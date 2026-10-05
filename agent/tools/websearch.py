"""Epic F: web_search tool (read-only, no approval by default).

Config (never hardcoded): a "web_search" section in the provider config —
INTERLUX_PROVIDERS env JSON first, then ~/.interlux/agent/providers.json:
  {"web_search": {"mode": "live",        # "live"|"disabled"; absent == live
                  "base_url": "https://api.tavily.com",
                  "api_key": "...", "path": "/search",
                  "extra": {"search_depth": "basic"}}}

`mode` is the client's Capabilities toggle landing in the same file the
tool already reads: `disabled` turns the search away with the reason, and
anything else (including an absent mode) searches.

Request shape is Tavily-compatible: {"api_key","query","max_results",...extra}.
Any endpoint answering {"answer", "results":[{title,url,content}]} works.

With no web_search section configured, a keyless DuckDuckGo fallback
answers instead (instant-answer JSON, else lite HTML) so the agent is
never without the web; a configured backend always wins.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import urllib.parse
import urllib.request

from ..pconfig import config_section, resolve_tool_key
from ..policy import agent_ctx

TIMEOUT = 20
RESULT_CAP = 10
SNIPPET_CAP = 500

_DDG_IA = "https://api.duckduckgo.com/"
_DDG_LITE = "https://lite.duckduckgo.com/lite/"


def _section() -> dict:
    # Per-agent credentials (step 3): mode/base_url stay global admin
    # config; the key is the calling agent's own. agent_ctx is set per
    # turn from socket identity, never from wire params.
    section = dict(config_section("web_search"))
    section["api_key"] = resolve_tool_key("web_search", agent_ctx.get(""))
    return section


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _ddg_search(query: str, count: int) -> dict:
    """Keyless DuckDuckGo fallback: instant-answer JSON, else lite HTML."""
    try:
        count = max(1, min(int(count or 5), RESULT_CAP))
    except (TypeError, ValueError):
        count = 5
    try:
        raw = _get(
            _DDG_IA + "?" + urllib.parse.urlencode(
                {"q": query, "format": "json", "no_html": 1}),
            TIMEOUT,
        )
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return {"status": "error",
                "message": f"web_search request failed: {e}"}
    answer = str(data.get("AbstractText", "") or "")
    results = []
    for t in (data.get("RelatedTopics") or []):
        if not isinstance(t, dict):
            continue
        if "Topics" in t and isinstance(t["Topics"], list):
            subs = t["Topics"]
        else:
            subs = [t]
        for s in subs:
            if not isinstance(s, dict):
                continue
            url = str(s.get("FirstURL", "") or "")
            text = _html.unescape(
                re.sub(r"<[^>]+>", "", str(s.get("Result", "") or "")))
            if url and text:
                results.append({"title": text[:120], "url": url,
                                "snippet": text[:SNIPPET_CAP]})
            if len(results) >= count:
                break
        if len(results) >= count:
            break
    if not results:
        # Instant answer empty: scrape lite HTML (same shape, no key).
        # Links look like:
        #   <a ... href="//duckduckgo.com/l/?uddg=<urlenc>&amp;rut=..."
        #    class='result-link'>Title</a> ... <td class='result-snippet'>…</td>
        try:
            html = _get(
                _DDG_LITE + "?" + urllib.parse.urlencode({"q": query}),
                TIMEOUT,
            ).decode("utf-8", "replace")
        except Exception as e:
            return {"status": "error",
                    "message": f"web_search request failed: {e}"}
        links: list[tuple[str, str]] = []
        for m in re.finditer(
                r"<a[^>]+href=\"([^\"]+)\"[^>]*class=['\"]result-link['\"]"
                r"[^>]*>(.*?)</a>",
                html, re.DOTALL):
            raw_link = _html.unescape(m.group(1))
            link = ""
            if "uddg=" in raw_link:
                link = urllib.parse.unquote(
                    raw_link.split("uddg=", 1)[1].split("&")[0])
            elif raw_link.startswith("http"):
                link = raw_link
            if not link.startswith("http"):
                continue
            title = _html.unescape(
                re.sub(r"<[^>]+>", "", m.group(2) or "")).strip()
            links.append((title, link))
        # Snippet cells follow their link rows in order — zip positionally.
        snips = [
            _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            for s in re.findall(
                r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
                html, re.DOTALL)
        ]
        for i, (title, link) in enumerate(links[:count]):
            snip = snips[i] if i < len(snips) else ""
            if title or snip:
                results.append({"title": title[:120], "url": link,
                                "snippet": snip[:SNIPPET_CAP]})
    if not results and not answer:
        return {"status": "error",
                "message": "web_search: no results (network restricted?)"}
    return {"status": "success", "query": query, "answer": answer,
            "results": results}


async def web_search(query: str = "", count: int = 5) -> dict:
    """Web search. Returns answer + [{title, url, snippet}]."""
    query = str(query or "").strip()
    if not query:
        return {"status": "error", "message": "query is required"}
    cfg = _section()
    # Capabilities -> Web search. Absent means live, so the tool works before
    # that screen is ever opened; only an explicit "disabled" turns a query
    # away, and it says why rather than failing as if the network broke.
    if str(cfg.get("mode", "live") or "live").strip().lower() == "disabled":
        return {"status": "error",
                "message": "web_search is off (Capabilities -> Web search)"}
    base = str(cfg.get("base_url", "")).rstrip("/")
    key = str(cfg.get("api_key", ""))
    if not base or not key:
        # No configured backend (no secrets on device): keyless
        # DuckDuckGo fallback so the agent can still use the web.
        return await asyncio.to_thread(_ddg_search, query, count)
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
