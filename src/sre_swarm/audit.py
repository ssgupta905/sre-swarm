"""JSONL audit-log writer per spec B.11."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, default=str) + "\n")
