"""Record exact coordinator/role messages without interpreting their correctness."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def main():
    role, recipient, kind, raw = sys.argv[1:]
    item = {"timestamp": datetime.now(timezone.utc).isoformat(), "role": role,
            "recipient": recipient, "kind": kind, "text": raw}
    try:
        item["parsed"] = json.loads(raw)
    except ValueError:
        pass
    with (HERE / "role-messages.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
