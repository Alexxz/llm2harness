"""Журнал наблюдаемых событий, а не запись внутренних вычислений модели."""

import json
from datetime import datetime, timezone
from pathlib import Path


class Trace:
    def __init__(self):
        self.events = []

    def emit(self, event, **data):
        item = {
            "sequence": len(self.events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": event,
            **data,
        }
        self.events.append(item)
        return item

    def save(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
