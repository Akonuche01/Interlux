"""Audit log — JSONL transcript of every turn + tool I/O."""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("INTERLUX_AGENT_CONFIG", "~/.interlux/agent")).expanduser()
AUDIT_FILE = CONFIG_DIR / "audit.jsonl"


class AuditLog:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def append(self, entry: dict) -> None:
        entry["ts"] = datetime.utcnow().isoformat() + "Z"
        entry["id"] = str(uuid.uuid4())
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_last(self, n: int = 10) -> list[dict]:
        if not AUDIT_FILE.exists():
            return []
        lines = AUDIT_FILE.read_text().splitlines()[-n:]
        return [json.loads(l) for l in lines if l.strip()]
