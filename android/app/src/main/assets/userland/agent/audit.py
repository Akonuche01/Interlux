"""Audit log — JSONL transcript of every turn + tool I/O."""

import json
import uuid
from datetime import datetime, timezone

from .home import engine_config_dir

CONFIG_DIR = engine_config_dir()
AUDIT_FILE = CONFIG_DIR / "audit.jsonl"


class AuditLog:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def append(self, entry: dict) -> None:
        entry["ts"] = datetime.now(timezone.utc).isoformat() + "Z"
        entry["id"] = str(uuid.uuid4())
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_last(self, n: int = 10) -> list[dict]:
        if not AUDIT_FILE.exists():
            return []
        lines = AUDIT_FILE.read_text().splitlines()[-n:]
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                # A torn write (crash mid-append, disk full) must not
                # take down every reader; skip the fragment.
                continue
        return out
