"""Bounded teacher access to the actual Session; no learner answers/oracles."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
from coursekit.classroom import Session


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--after", type=int, default=0)
    advice = sub.add_parser("advise")
    advice.add_argument("--message", required=True)
    control = sub.add_parser("control")
    control.add_argument("verb", choices=["pause", "resume", "close", "stop"])
    args = parser.parse_args()
    context = json.loads((HERE / "context.json").read_text())
    session = Session.open(context["session_id"], root=Path(context["temporary_root"]))
    if args.action == "observe":
        result = {"state": session.snapshot(), "events": session.events(after=args.after, limit=100)}
    elif args.action == "advise":
        advice_count = 0
        cursor = 0
        while batch := session.events(after=cursor, limit=100):
            advice_count += sum(event["event"] == "teacher_advice" for event in batch)
            cursor = batch[-1]["sequence"]
        if advice_count >= context["maximum_teacher_advice"]:
            raise RuntimeError("The bounded playtest allows at most three teacher messages")
        result = session.advise(args.message)
    else:
        result = session.control(args.verb)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "request": vars(args), "result": result}
    with (HERE / "teacher-actions.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
