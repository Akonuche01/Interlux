"""Epic D: Codex-shaped markdown skills.

Layout (both scanned, user skill wins on name collision):
  agent/skills/*.md          bundled recipes (shipped in the APK)
  ~/.interlux/agent/skills/  client/user-writable skills (wipe-proof)

Front-matter (simple `key: value`, no yaml dependency):
  ---
  name: alpine-guest
  description: when to use this skill (this is what the model reads)
  tools: shell, fs_read              # optional, advisory
  tools_dir: tools                   # optional: dir of *.py tool plugins
  ---
  body (instructions)

Injection per turn: a compact catalog system message (all skills) plus full
bodies when the turn passes `params.skills` (list of names, or "all"). The
model can also pull a body itself via the read-only `skill_load` tool; the
result folds into thread history like any tool output.

Skill tool plugins: a skill may declare `tools_dir` — its *.py files are
loaded through the same scan_plugins machinery as drop-in plugins (same
approval/audit/EXTRA_WRITE path), on boot and on every `tools_refresh`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .tools.plugins import scan_plugins

logger = logging.getLogger("skills")

BUNDLED_DIR = Path(__file__).parent / "skills"
USER_DIR = Path(
    __import__("os").environ.get("INTERLUX_AGENT_CONFIG", "~/.interlux/agent")
).expanduser() / "skills"

_KEYS = ("name", "description", "tools", "tools_dir", "plugin")


def parse_skill(text: str, path: Path, source: str) -> dict | None:
    """Front-matter + body -> skill dict (None if unusable)."""
    if not text.startswith("---"):
        return None
    try:
        _, fm, body = text.split("---", 2)
    except ValueError:
        return None
    meta: dict = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if key in _KEYS and val:
            meta[key] = val
    if not meta.get("name"):
        return None
    tools_raw = meta.get("tools", "")
    tools_raw = tools_raw.strip("[]")
    tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    return {
        "name": meta["name"],
        "description": meta.get("description", ""),
        "tools": tools,
        "tools_dir": meta.get("tools_dir") or meta.get("plugin") or "",
        "body": body.strip(),
        "path": str(path),
        "source": source,
    }


def _scan_dir(directory: Path, source: str, into: dict) -> None:
    if not directory.is_dir():
        return
    for f in sorted(directory.glob("*.md")):
        try:
            skill = parse_skill(f.read_text(), f, source)
        except Exception as e:
            logger.error(f"skill {f.name} failed to read: {e}")
            continue
        if skill:
            into[skill["name"]] = skill


_cache: dict[str, dict] = {}


def refresh(bundled_dir: Path | None = None, user_dir: Path | None = None) -> dict[str, dict]:
    """(Re)load skills; user dir scanned second so it wins collisions."""
    found: dict[str, dict] = {}
    _scan_dir(bundled_dir or BUNDLED_DIR, "bundled", found)
    _scan_dir(user_dir or USER_DIR, "user", found)
    _cache.clear()
    _cache.update(found)
    logger.info(f"skills loaded: {sorted(found)}")
    return dict(_cache)


def _ensure() -> dict[str, dict]:
    if not _cache:
        refresh()
    return _cache


def list_skills() -> list[dict]:
    return [
        {k: s[k] for k in ("name", "description", "tools", "source", "path")}
        for s in sorted(_ensure().values(), key=lambda s: s["name"])
    ]


def get_skill(name: str) -> dict | None:
    return _ensure().get(str(name or ""))


def catalog_message() -> str:
    """Compact per-turn system message listing every skill."""
    skills = list_skills()
    if not skills:
        return ""
    lines = [
        "Skills available (read-only `skill_load` tool fetches the full instructions):"
    ]
    for s in skills:
        desc = s["description"] or "(no description)"
        extra = f" [tools: {', '.join(s['tools'])}]" if s["tools"] else ""
        lines.append(f"- {s['name']}: {desc}{extra}")
    return "\n".join(lines)


def messages_for(names) -> list[dict]:
    """Full-body system messages for a turn's `skills` param.

    names: list[str] | "all" | None. Unknown names are ignored (noted once).
    """
    if not names:
        return []
    all_skills = _ensure()
    if isinstance(names, str):
        names = [names]
    wanted: list[str] = []
    unknown: list[str] = []
    for n in names:
        n = str(n)
        if n == "all":
            wanted = sorted(all_skills)
            unknown = []
            break
        (wanted if n in all_skills else unknown).append(n)
    msgs: list[dict] = []
    for n in wanted:
        s = all_skills[n]
        msgs.append({
            "role": "system",
            "content": f"Skill `{s['name']}`:\n{s['body']}",
        })
    if unknown:
        msgs.append({
            "role": "system",
            "content": "Unknown skill(s) requested: " + ", ".join(unknown),
        })
    return msgs


def skill_messages(param) -> list[dict]:
    """Per-turn skill injection: catalog first, then requested bodies."""
    msgs: list[dict] = []
    catalog = catalog_message()
    if catalog:
        msgs.append({"role": "system", "content": catalog})
    msgs.extend(messages_for(param))
    return msgs


async def skill_load(name: str = "") -> dict:
    """Read-only tool: fetch a skill body (no approval, safe in read-only)."""
    skill = get_skill(name)
    if skill is None:
        return {"status": "error", "message": f"unknown skill: {name!r}"}
    return {
        "status": "success",
        "name": skill["name"],
        "description": skill["description"],
        "tools": skill["tools"],
        "body": skill["body"],
        "path": skill["path"],
        "source": skill["source"],
    }


def register_skill_tool(registry: dict) -> list[str]:
    """Register the skill_load tool (called once at boot)."""
    before = set(registry)
    registry["skill_load"] = skill_load
    return sorted(set(registry) - before)


def scan_skill_plugins(registry: dict, write: set) -> list[str]:
    """Load each skill's tools_dir through the drop-in plugin machinery."""
    loaded: list[str] = []
    for skill in _ensure().values():
        if not skill.get("tools_dir"):
            continue
        plugindir = Path(skill["path"]).parent / skill["tools_dir"]
        try:
            loaded.extend(scan_plugins(plugindir, registry, write))
        except Exception as e:
            logger.error(f"skill {skill['name']} plugin scan failed: {e}")
    return loaded
